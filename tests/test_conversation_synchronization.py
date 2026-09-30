"""Real Manager/ACP/fake-agent concurrency, not hand-written final run states."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from pi_glink.contracts import FollowupUnavailable
from pi_glink.events import EventBus
from pi_glink.runs.runner import RunManager
from pi_glink.store import RunStore
from test_kernel_acp_hot_path import (
    ParkableSandbox, fake_agent_command, fake_kill_by_id, make_config,
)


@pytest.mark.parametrize("operation", ["sweep", "delete"])
def test_cleanup_and_followup_admission_share_conversation_ownership(tmp_path, monkeypatch, operation):
    """An old holding snapshot must never authorize killing a running follow-up."""
    import pi_glink.runs.resource_ledger as ledger_module

    ParkableSandbox.REGISTRY.clear()
    fake_kill_by_id(monkeypatch)
    config = make_config(tmp_path, TEST_ACP_COMMAND=fake_agent_command(tmp_path, {}))
    store = RunStore(config.data_root / "runs")
    sandboxes = []
    roots = {}
    connected = threading.Event()

    def factory(**kw):
        sandbox = ParkableSandbox(tmp_path, sandbox_id=f"s{len(sandboxes)}",
                                  conversation_dir=roots.get("conversation"))
        sandboxes.append(sandbox)
        return sandbox

    def resume(**kw):
        sandbox = ParkableSandbox.connect(**kw)
        connected.set()
        return sandbox

    manager = RunManager(config, EventBus(), store=store, sandbox_factory=factory,
                         sandbox_resume_factory=resume)
    first = manager.start("REMEMBER:SAFE-991")
    assert manager.wait(first.run_id, timeout=20).status == "finished"
    roots["conversation"] = store.conversations_root() / first.run_id
    snapshot_taken = threading.Event()
    continue_cleanup = threading.Event()
    killed_running = []
    failures = []
    followups = []
    original_holding = store.holding_conversation_roots

    def holding():
        snapshot = original_holding()
        if threading.current_thread().name == "cleanup-race":
            snapshot_taken.set()
            if not continue_cleanup.wait(5):
                raise RuntimeError("test cleanup barrier timed out")
        return snapshot

    def probe(sandbox_id):
        sandbox = ParkableSandbox.REGISTRY.get(sandbox_id)
        if sandbox:
            killed_running.append(sandbox.state == "running")
            sandbox.kill()
        return True, "fake kill"

    def cleanup():
        try:
            if operation == "sweep":
                manager.reconcile_leases()
            else:
                manager.delete_conversation(first.run_id)
        except Exception as exc:
            failures.append(exc)

    def followup():
        try:
            followups.append(manager.start("RECALL NOSETTLE", parent_run_id=first.run_id))
        except Exception as exc:
            failures.append(exc)

    monkeypatch.setattr(store, "holding_conversation_roots", holding)
    monkeypatch.setattr(manager._ledger, "_sandbox_probe", probe)
    # Advance only the sweep's external clock, not the runtime/admission clock.
    monkeypatch.setattr(ledger_module, "time", SimpleNamespace(time=lambda: 10**12))
    cleaner = threading.Thread(target=cleanup, name="cleanup-race")
    starter = threading.Thread(target=followup, name="followup-race")
    try:
        cleaner.start()
        assert snapshot_taken.wait(5)
        starter.start()
        # Old code admits/connects during the stale-snapshot window; corrected
        # code holds admission behind this conversation's cleanup transaction.
        connected.wait(1)
        continue_cleanup.set()
        cleaner.join(5)
        starter.join(5)
        assert not cleaner.is_alive() and not starter.is_alive()
        assert not any(killed_running), "cleanup killed a sandbox already resumed by a run"
        if followups:
            # A sweep-first ordering safely resumes via cold state; a delete-
            # first ordering refuses admission before creating a child run.
            manager.stop(followups[0].run_id)
            assert manager.wait(followups[0].run_id, timeout=20) is not None
        if operation == "sweep":
            assert not failures and len(followups) == 1
        else:
            assert not followups and len(failures) == 1
            assert isinstance(failures[0], FollowupUnavailable)
            assert failures[0].code == "session_unavailable"
            assert len(store.list(limit=None)) == 1  # no child was inserted
    finally:
        continue_cleanup.set()
        cleaner.join(5)
        if starter.ident:
            starter.join(5)
        monkeypatch.setattr(store, "holding_conversation_roots", original_holding)
        manager.shutdown()
        for sandbox in sandboxes:
            if not sandbox.killed:
                sandbox.kill()
        ParkableSandbox.REGISTRY.clear()


def test_slow_conversation_cleanup_does_not_lock_unrelated_admission(tmp_path, monkeypatch):
    """A waiting follow-up must not acquire the global lock before its guard."""
    import pi_glink.runs.resource_ledger as ledger_module

    ParkableSandbox.REGISTRY.clear()
    fake_kill_by_id(monkeypatch)
    config = make_config(tmp_path, TEST_ACP_COMMAND=fake_agent_command(tmp_path, {}))
    store = RunStore(config.data_root / "runs")
    sandboxes = []
    root = {}

    def factory(**kw):
        sandbox = ParkableSandbox(tmp_path, sandbox_id=f"independent-{len(sandboxes)}",
                                  conversation_dir=root.get("conversation"))
        sandboxes.append(sandbox)
        return sandbox

    manager = RunManager(config, EventBus(), store, sandbox_factory=factory)
    probe_entered, release_probe = threading.Event(), threading.Event()
    guard_attempted, unrelated_admitted = threading.Event(), threading.Event()
    results, failures = {}, []

    def probe(sid):
        probe_entered.set()
        assert release_probe.wait(5)
        ParkableSandbox.REGISTRY[sid].kill()
        return True, "fake kill"

    def start(name, parent=None):
        try:
            results[name] = manager.start(name, parent_run_id=parent)
            if name == "unrelated":
                unrelated_admitted.set()
        except Exception as exc:
            failures.append(exc)

    cleaner = threading.Thread(target=manager.reconcile_leases)
    waiting = threading.Thread()
    other = threading.Thread()
    try:
        first = manager.start("REMEMBER:INDEPENDENT")
        assert manager.wait(first.run_id, timeout=20).status == "finished"
        root["conversation"] = store.conversations_root() / first.run_id
        family = manager._admission.family_for("acp")
        original_guard = family.continuation_guard

        def guard(parent):
            guard_attempted.set()
            return original_guard(parent)

        monkeypatch.setattr(family, "continuation_guard", guard)
        monkeypatch.setattr(manager._ledger, "_sandbox_probe", probe)
        monkeypatch.setattr(ledger_module, "time", SimpleNamespace(time=lambda: 10**12))
        cleaner.start()
        assert probe_entered.wait(5)
        waiting = threading.Thread(target=start, args=("followup", first.run_id))
        waiting.start()
        assert guard_attempted.wait(5)
        other = threading.Thread(target=start, args=("unrelated",))
        other.start()
        assert unrelated_admitted.wait(2), "same-conversation wait blocked the global admission lock"
        assert manager.wait(results["unrelated"].run_id, timeout=20).status == "finished"
        release_probe.set()
        cleaner.join(5)
        waiting.join(5)
        other.join(5)
        assert not failures
        assert manager.wait(results["followup"].run_id, timeout=20).status == "finished"
    finally:
        release_probe.set()
        for thread in (cleaner, waiting, other):
            if thread.ident:
                thread.join(5)
        manager.shutdown()
        for sandbox in sandboxes:
            if not sandbox.killed:
                sandbox.kill()
        ParkableSandbox.REGISTRY.clear()
