"""Regressions for the resource ledger and its online reconciliation (GLINK-RES-01).

The obligation this file pins is the one that replaced "any unreleased resource
blocks every tenant": a hold **consumes slots** (its owner tenant's and the
global one) in multi-tenant mode, keeps consuming them until the execution face
itself confirms the resource is gone, and is confirmed online by addressing only
the identity persisted with the run. No test here touches a real Docker daemon
or the real E2B SDK: the probes and the sandbox factory are injected fakes.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from pi_glink.config import HostConfig, build_config
from pi_glink.events import EventBus
from pi_glink.kernel import docker_env
from pi_glink.kernel.execution import (
    ArtifactRecovery,
    CleanupReport,
    ExecutionOutcome,
    SessionInfo,
)
from pi_glink.kernel.runner import ResourcesUnreleased, RunManager, RunQuotaExceeded
from pi_glink.ownership import DataRootOwnership
from pi_glink.store import FAILED, FINISHED, RUNNING, RunStore
from pi_glink.tenancy import TenantContext

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_config(tmp_path: Path, **overrides: str) -> HostConfig:
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent-web-search.json"),
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


def make_multitenant_config(tmp_path: Path, **overrides: str) -> HostConfig:
    """Multi-tenant Docker config; the identity entry does not affect the ledger."""
    values = {
        "LOCAL_WEB_EXECUTOR": "docker",
        "PI_GLINK_TENANCY": "multitenant",
        "PI_GLINK_AUTH_MODE": "accounts",
        "PI_GLINK_MODEL_CREDENTIALS": "shared",
    }
    values.update(overrides)
    return make_config(tmp_path, **values)


def make_e2b_config(tmp_path: Path, **overrides: str) -> HostConfig:
    values = {
        "LOCAL_WEB_EXECUTOR": "e2b",
        "E2B_API_KEY": "e2b-secret-key",
        "LOCAL_WEB_E2B_TEMPLATE": "pi-glink-worker",
    }
    values.update(overrides)
    return make_config(tmp_path, **values)


def context(tenant_id: str, user_id: str = "user") -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        user_id=user_id,
        role="admin",
        audience="test",
        expires_at=time.time() + 300,
    )


def record_hold(
    store: RunStore,
    run_id: str,
    *,
    tenant_id: str | None = None,
    container: str | None = None,
    sandbox: str | None = None,
    released: bool = False,
) -> None:
    """A terminal record whose cleanup did not confirm the release."""
    store.create("泄漏的 run", run_id=run_id, tenant_id=tenant_id)
    store.update(
        run_id,
        status=FINISHED,
        finished_at=time.time(),
        container_name=container,
        sandbox_id=sandbox,
        cleanup={"released": released, "failures": ["synthetic"], "external": {}},
    )


def write_corrupt_record(store: RunStore, run_id: str) -> Path:
    directory = store.run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    meta = directory / "meta.json"
    meta.write_text("{ this is not JSON", encoding="utf-8")
    return meta


# -- the ledger counts instead of blocking ------------------------------------


def test_a_leak_consumes_its_tenant_and_the_global_slots(tmp_path: Path) -> None:
    config = make_multitenant_config(
        tmp_path, PI_GLINK_MAX_TENANT_RUNS="2", PI_GLINK_MAX_GLOBAL_RUNS="4"
    )
    store = RunStore(config.data_root / "runs")
    record_hold(store, "leak-a1", tenant_id="tenant-a", container="pi-glink-leak-a1")
    record_hold(store, "leak-a2", tenant_id="tenant-a", container="pi-glink-leak-a2")
    record_hold(store, "leak-legacy", container="pi-glink-leak-legacy")
    manager = RunManager(config, EventBus(), store)

    with pytest.raises(RunQuotaExceeded) as tenant_hit:
        manager._check_admission(context("tenant-a"))  # noqa: SLF001
    assert (tenant_hit.value.scope, tenant_hit.value.limit) == ("tenant", 2)

    # Another tenant is not blocked by tenant A's leftovers: global 3/4 is free.
    manager._check_admission(context("tenant-b"))  # noqa: SLF001

    record_hold(store, "leak-b1", tenant_id="tenant-b", container="pi-glink-leak-b1")
    with pytest.raises(RunQuotaExceeded) as global_hit:
        manager._check_admission(context("tenant-b"))  # noqa: SLF001
    assert (global_hit.value.scope, global_hit.value.limit) == ("global", 4)
    # A refusal names the ceiling, never another tenant's run.
    assert "leak" not in str(global_hit.value)


def test_local_mode_still_refuses_on_any_hold(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    record_hold(store, "leak")
    manager = RunManager(config, EventBus(), store)

    with pytest.raises(ResourcesUnreleased) as refusal:
        manager._check_admission(None)  # noqa: SLF001
    assert refusal.value.run_ids == ["leak"]


def test_the_ledger_survives_a_restart(tmp_path: Path) -> None:
    config = make_multitenant_config(tmp_path, PI_GLINK_MAX_GLOBAL_RUNS="1")
    store = RunStore(config.data_root / "runs")
    record_hold(store, "leak", tenant_id="tenant-a", container="pi-glink-leak")

    restarted = RunManager(config, EventBus(), RunStore(config.data_root / "runs"))
    with pytest.raises(RunQuotaExceeded):
        restarted._check_admission(context("tenant-b"))  # noqa: SLF001
    assert [entry["runId"] for entry in restarted.unreleased_resources()] == ["leak"]


# -- corrupt records are a global hold ----------------------------------------


def test_a_corrupt_record_is_hidden_from_lists_but_holds_globally(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    store.create("正常", run_id="good", tenant_id="tenant-a")
    store.update(
        "good",
        status=FINISHED,
        finished_at=time.time(),
        cleanup={"released": True, "failures": [], "external": {}},
    )
    meta = write_corrupt_record(store, "broken")
    before = meta.read_bytes()

    assert [record.run_id for record in store.list(limit=None)] == ["good"]
    assert store.get("broken") is None, "a corrupt record must not be served"
    assert store.unreadable_holds() == ["broken"]
    assert store.scan_all() == store.list(limit=None)
    assert store.scan_unreadable() == ["broken"]

    manager = RunManager(config, EventBus(), store)
    assert manager.unreleased_resources() == [
        {
            "runId": "broken",
            "status": None,
            "stopReason": None,
            "container": None,
            "sandbox": None,
            "cleanup": None,
            "corrupt": True,
        }
    ]
    with pytest.raises(ResourcesUnreleased) as refusal:
        manager._check_admission(None)  # noqa: SLF001
    assert refusal.value.run_ids == ["broken"]

    ownership = DataRootOwnership.acquire(config.data_root)
    try:
        adjudicating = RunManager(config, EventBus(), store, ownership=ownership)
        assert adjudicating.adjudicate_release("broken", "已在控制台确认资源不存在") is None
    finally:
        ownership.release()
    assert meta.read_bytes() == before, "the unreadable metadata is never rewritten"
    sidecar = json.loads(
        (store.run_dir("broken") / "resource-adjudication.json").read_text(encoding="utf-8")
    )
    assert sidecar["decision"] == "released" and sidecar["source"] == "operator"
    assert "控制台" in sidecar["note"]
    assert store.unreadable_holds() == []
    assert manager.unreleased_resources() == []
    manager._check_admission(None)  # noqa: SLF001 - released, admission proceeds


def test_a_corrupt_record_consumes_the_global_ceiling(tmp_path: Path) -> None:
    config = make_multitenant_config(tmp_path, PI_GLINK_MAX_GLOBAL_RUNS="1")
    store = RunStore(config.data_root / "runs")
    write_corrupt_record(store, "broken")
    manager = RunManager(config, EventBus(), store)

    with pytest.raises(RunQuotaExceeded) as refusal:
        manager._check_admission(context("tenant-b"))  # noqa: SLF001
    assert (refusal.value.scope, refusal.value.limit) == ("global", 1)

    ownership = DataRootOwnership.acquire(config.data_root)
    try:
        adjudicating = RunManager(config, EventBus(), store, ownership=ownership)
        adjudicating.adjudicate_release("broken", "已核对")
    finally:
        ownership.release()
    manager._check_admission(context("tenant-b"))  # noqa: SLF001


def test_adjudicating_a_readable_record_still_uses_the_record_path(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    record_hold(store, "leak")
    ownership = DataRootOwnership.acquire(config.data_root)
    try:
        manager = RunManager(config, EventBus(), store, ownership=ownership)
        with pytest.raises(KeyError):
            manager.adjudicate_release("unknown", "已核对")
        updated = manager.adjudicate_release("leak", "已核对")
    finally:
        ownership.release()
    assert updated is not None
    assert updated.resource_adjudication["source"] == "operator"
    assert store.holding_records() == []
    assert not (store.run_dir("leak") / "resource-adjudication.json").exists()


def test_the_operator_command_lists_and_releases_a_corrupt_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from pi_glink.app import _resource_command  # noqa: PLC0415

    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    write_corrupt_record(store, "broken")

    assert _resource_command(config, None, None) == 0
    out = capsys.readouterr().out
    assert '"runId": "broken"' in out
    assert '"corrupt": true' in out

    assert _resource_command(config, "broken", "已在控制台确认没有遗留资源") == 0
    assert RunStore(config.data_root / "runs").unreadable_holds() == []
    assert _resource_command(config, "broken", "重复裁决") == 1


# -- online reconciliation ----------------------------------------------------


def test_a_confirmed_probe_releases_capacity(tmp_path: Path) -> None:
    config = make_config(tmp_path, LOCAL_WEB_EXECUTOR="docker")
    store = RunStore(config.data_root / "runs")
    record_hold(store, "leak", container="pi-glink-leak")
    calls: list[str] = []

    def probe(name: str) -> tuple[bool, str]:
        calls.append(name)
        return True, "fake docker: 容器不存在"

    manager = RunManager(config, EventBus(), store, docker_probe=probe)
    assert manager.reconcile_resources() == ["leak"]
    assert calls == ["pi-glink-leak"], "探测只使用 meta 中持久化的身份"
    record = store.get("leak")
    assert record.resource_adjudication["source"] == "auto-verified"
    assert record.resource_adjudication["method"] == "docker-inspect"
    assert "容器不存在" in record.resource_adjudication["note"]
    assert record.cleanup["released"] is False, "原清理事实不得被改写"
    assert record.status == FINISHED
    assert store.holding_records() == []
    assert manager.unreleased_resources() == []
    assert manager.reconcile_resources() == [], "已释放的持有不再重复探测"
    assert calls == ["pi-glink-leak"]


@pytest.mark.parametrize("mode", ["uncertain", "error", "timeout"])
def test_a_probe_that_cannot_confirm_keeps_the_hold(tmp_path: Path, mode: str) -> None:
    config = make_config(tmp_path, LOCAL_WEB_EXECUTOR="docker")
    store = RunStore(config.data_root / "runs")
    record_hold(store, "leak", container="pi-glink-leak")

    def probe(_name: str) -> tuple[bool, str]:
        if mode == "uncertain":
            return False, "fake docker: 无法判定"
        if mode == "error":
            raise OSError("docker CLI 消失")
        raise subprocess.TimeoutExpired("docker", 20)

    manager = RunManager(config, EventBus(), store, docker_probe=probe)
    assert manager.reconcile_resources() == []
    record = store.get("leak")
    assert record.resource_adjudication is None
    assert record.holds_resources is True
    assert [entry["runId"] for entry in manager.unreleased_resources()] == ["leak"]


def test_a_probe_never_guesses_a_missing_identity(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = make_config(tmp_path, LOCAL_WEB_EXECUTOR="e2b")
    store = RunStore(config.data_root / "runs")
    record_hold(store, "no-identity")
    calls: list[str] = []
    manager = RunManager(
        config,
        EventBus(),
        store,
        sandbox_probe=lambda sandbox_id: (calls.append(sandbox_id) or True, "fake"),
    )
    with caplog.at_level(logging.INFO, logger="pi_glink.kernel.runner"):
        assert manager.reconcile_resources() == []
    assert calls == [], "没有持久化身份时不得猜测或批量操作"
    assert "缺少持久化的 sandbox_id" in caplog.text
    assert store.get("no-identity").holds_resources is True


def test_the_probe_only_touches_the_persisted_container_name(tmp_path: Path) -> None:
    """The Docker probe itself: inspect by name, remove by name, confirm again."""
    calls: list[list[str]] = []
    present = {"value": True}

    def runner(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess:
        calls.append(argv)
        if argv[0:2] == ["docker", "inspect"]:
            if present["value"]:
                return subprocess.CompletedProcess(argv, 0, "abc123\n", "")
            return subprocess.CompletedProcess(argv, 1, "", "Error: No such object: x")
        present["value"] = False  # docker rm -f
        return subprocess.CompletedProcess(argv, 0, "x\n", "")

    confirmed, detail = docker_env.confirm_absent("pi-glink-run1", runner=runner)
    assert (confirmed, "不存在" in detail) == (True, True)
    assert calls[0] == [
        "docker", "inspect", "--type", "container", "--format", "{{.Id}}", "pi-glink-run1",
    ]
    assert calls[1] == ["docker", "rm", "-f", "pi-glink-run1"]
    assert all("pi-glink-run1" in argv for argv in calls)

    # A daemon that cannot answer keeps the hold instead of assuming release.
    def broken(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess:
        raise OSError("docker CLI 不可用")

    assert docker_env.confirm_absent("pi-glink-run1", runner=broken)[0] is False
    assert docker_env.confirm_absent("", runner=broken)[0] is False


def test_the_sweep_never_probes_a_run_that_is_still_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_multitenant_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    record_hold(store, "finished-leak", tenant_id="tenant-a", container="pi-glink-finished-leak")
    calls: list[str] = []
    manager = RunManager(
        config,
        EventBus(),
        store,
        docker_probe=lambda name: (calls.append(name) or True, "fake docker: 不存在"),
    )
    adapter = _BlockingAdapter()
    monkeypatch.setattr("pi_glink.kernel.runner.build_adapter", lambda spec, **kwargs: adapter)
    monkeypatch.setattr("pi_glink.kernel.runner.docker_available", lambda: True)

    run_id = manager.start("进行中", context("tenant-a")).run_id
    assert store.get(run_id).holds_resources is True, "运行中的 run 也是持有"
    assert manager.reconcile_resources() == ["finished-leak"]
    assert calls == ["pi-glink-finished-leak"], "在途 run 由自己的执行面负责"
    manager.stop(run_id, context("tenant-a"))
    assert manager.wait(run_id, timeout=30).status in {"cancelled", "failed"}


def test_the_watch_thread_sweeps_once_after_startup_and_stops(tmp_path: Path) -> None:
    config = make_config(
        tmp_path, LOCAL_WEB_EXECUTOR="docker", PI_GLINK_RESOURCE_RECONCILE_SECONDS="1"
    )
    store = RunStore(config.data_root / "runs")
    record_hold(store, "leak", container="pi-glink-leak")
    probed = threading.Event()
    calls: list[str] = []

    def probe(name: str) -> tuple[bool, str]:
        calls.append(name)
        probed.set()
        return True, "fake docker: 容器不存在"

    manager = RunManager(config, EventBus(), store, docker_probe=probe)
    thread = manager.start_resource_watch()
    assert thread is not None
    assert manager.start_resource_watch() is thread, "重复启动返回同一个线程"
    assert probed.wait(10), "启动后必须立即对账一次"
    manager.stop_resource_watch()
    assert not thread.is_alive()
    assert store.holding_records() == []
    assert calls == ["pi-glink-leak"]

    # inproc has no external resource, and 0 disables the sweep entirely.
    inproc = RunManager(make_config(tmp_path / "other"), EventBus(), RunStore(tmp_path / "other" / "runs"))
    assert inproc.start_resource_watch() is None
    disabled = RunManager(
        make_config(tmp_path / "off", LOCAL_WEB_EXECUTOR="docker", PI_GLINK_RESOURCE_RECONCILE_SECONDS="0"),
        EventBus(),
        RunStore(tmp_path / "off" / "runs"),
    )
    assert disabled.start_resource_watch() is None


# -- identity is persisted as soon as the resource exists ---------------------


class _PartialSandbox:
    """A sandbox whose creation succeeds and whose next two steps fail."""

    def __init__(self) -> None:
        self.sandbox_id = "fake-sandbox-partial"
        self.killed = False

    def make_dir(self, _path: str) -> None:
        return None

    def write(self, _path: str, _content: Any) -> None:
        raise OSError("synthetic upload failure")

    def kill(self) -> None:
        raise OSError("synthetic kill failure")


def test_e2b_partial_creation_keeps_the_persisted_sandbox_identity(tmp_path: Path) -> None:
    config = make_e2b_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    sandbox = _PartialSandbox()
    manager = RunManager(
        config, EventBus(), store, sandbox_factory=lambda **_: sandbox
    )
    record = manager.start("上传失败")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == FAILED
    assert final.sandbox_id == sandbox.sandbox_id, "拿到 id 后必须立即持久化身份"
    assert final.cleanup["released"] is False
    assert final.cleanup["external"] == {"sandbox": sandbox.sandbox_id}

    entry = next(
        item
        for item in manager.unreleased_resources()
        if item["runId"] == record.run_id
    )
    assert entry["sandbox"] == sandbox.sandbox_id

    probed: list[str] = []
    manager._sandbox_probe = lambda sandbox_id: (  # noqa: SLF001 - the injected face
        probed.append(sandbox_id) or True,
        "fake e2b: 沙箱已终止",
    )
    assert manager.reconcile_resources() == [record.run_id]
    assert probed == [sandbox.sandbox_id]
    assert store.holding_records() == []


def test_the_docker_face_persists_the_container_name_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path, LOCAL_WEB_EXECUTOR="docker")
    store = RunStore(config.data_root / "runs")
    monkeypatch.setattr("pi_glink.kernel.runner.docker_available", lambda: True)
    monkeypatch.setattr(
        "pi_glink.kernel.execution.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 1, "", "Error: No such container"
        ),
    )

    def failing_client(argv: list[str], **_kwargs: Any) -> Any:
        raise OSError("synthetic docker run failure")

    manager = RunManager(config, EventBus(), store, client_factory=failing_client)
    record = manager.start("容器起不来")

    final = manager.wait(record.run_id, timeout=30)
    assert final.status == FAILED
    assert final.container_name == docker_env.container_name_for(record.run_id)
    assert final.cleanup["external"] == {"container": final.container_name}


class _BlockingAdapter:
    """A run that stays in flight until it is cancelled."""

    kind = "docker"

    def __init__(self) -> None:
        self._cancelled = threading.Event()

    def start(self) -> None:
        return None

    def session_info(self) -> SessionInfo | None:
        return None

    def deliver_input(self) -> None:
        return None

    def wait(self, *, cancel, deadline: float) -> ExecutionOutcome:
        while not self._cancelled.is_set():
            if cancel():
                break
            time.sleep(0.01)
        return ExecutionOutcome(settled=True, cancelled=True)

    def cancel(self) -> None:
        self._cancelled.set()

    def recover_artifacts(self, destination: Path) -> ArtifactRecovery:
        return ArtifactRecovery()

    def close(self) -> CleanupReport:
        return CleanupReport(released=True)

    def identity(self) -> dict[str, str]:
        return {}


def test_the_run_state_helper_keeps_its_meaning() -> None:
    """Guard the assumption this file builds on: a running record holds."""
    from pi_glink.store import RunRecord

    record = RunRecord(
        run_id="x", prompt="p", status=RUNNING, created_at=time.time(), workspace="/tmp/x"
    )
    assert record.holds_resources is True
