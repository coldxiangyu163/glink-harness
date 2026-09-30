"""对话租约的生命周期对账与删除链路（阶段 2，docs/adr/0020）。

覆盖三块，全部合成（fake kill 探测，不打真实 E2B）：

* **到期对账**（``ResourceLedger.reconcile_leases``）：过期租约按沙箱 id
  销毁、确认后删记录；销毁未确认保留到下轮；未过期只重试 retired；无
  E2B key 时不动任何租约；
* **删除链路**（``RunManager.delete_conversation``）：销毁租约沙箱 + 删除
  宿主对话状态目录（state/lease）；任一沙箱未确认销毁则目录与租约原样
  保留（fail-closed）；多租户 owner 门禁（未知/异租户不销毁不删除）；运行
  记录与事件日志不属于这个最小闭环；
* **租约不占 run 槽位**：泊车的沙箱不构成 resource hold，本对话/其他对话
  的新 run 照常受理。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


from pi_glink.config import build_config
from pi_glink.events import EventBus
from pi_glink.kernel.acp_sandbox_park import park_sandbox
from pi_glink.kernel.conversation_lease import (
    ConversationLease,
    read_lease,
    write_lease,
)
from pi_glink.runs.runner import RunManager
from pi_glink.store import FINISHED, RunSpec, RunStore
from pi_glink.tenancy import TenantContext

from store_helpers import finish

REPO_ROOT = Path(__file__).resolve().parents[1]


def context(tenant_id: str) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        user_id="user",
        role="admin",
        audience="test",
        expires_at=time.time() + 300,
    )


def make_config(tmp_path: Path, **overrides: str):
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_EXECUTOR": "e2b",
        "E2B_API_KEY": "e2b-secret-key",
        "PI_GLINK_EGRESS_PUBLIC_URL": "https://egress.example.test",
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


def a_lease(*, run_id: str = "conv-1", sandbox_id: str = "sbx-1", generation: int = 1,
            paused_ago: float = 0.0, ttl: float = 3600.0,
            retired: tuple[str, ...] = ()) -> ConversationLease:
    """A valid lease: ``paused_ago + ttl`` 过去即已到期（且到期晚于暂停）。"""
    now = time.time()
    return ConversationLease(
        conversation_run="conv-1",
        sandbox_id=sandbox_id,
        generation=generation,
        template="glink-acp-worker",
        pack="dsh",
        run_id=run_id,
        paused_at=now - paused_ago,
        expires_at=now - paused_ago + ttl,
        retired_sandbox_ids=retired,
    )


def seeded_store(config) -> tuple[RunStore, Path]:
    """A one-turn conversation: its root run id names the conversation dir."""
    store = RunStore(config.data_root / "runs")
    conversation = store.conversation_dir("conv-1")
    store.create(
        "泊车的 run",
        run_id="conv-1",
        spec=RunSpec(family="acp", pack="dsh", executor="e2b"),
    )
    finish(store, "conv-1", FINISHED, finished_at=time.time(),
           cleanup={"released": True, "failures": [], "external": {"sandbox": "sbx-1"}})
    return store, conversation


def a_manager(config, store, *, confirm=None):
    def default_confirm(sandbox_id: str, **_kwargs: Any):
        # The kill-based contract: not-found counts as confirmed gone.
        return (True, "fake confirm")

    return RunManager(
        config, EventBus(), store=store,
        sandbox_probe=confirm or default_confirm,
    )


# -- expiry sweep -----------------------------------------------------------------


def test_expired_lease_is_destroyed_and_removed(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store, conversation = seeded_store(config)
    write_lease(conversation, a_lease(paused_ago=7200, ttl=3600))
    destroyed: list[str] = []
    manager = a_manager(
        config, store,
        confirm=lambda sandbox_id, **_k: (destroyed.append(sandbox_id) or (True, "gone")),
    )
    assert manager.reconcile_leases() == ["conv-1"]
    assert destroyed == ["sbx-1"]
    assert read_lease(conversation) is None


def test_unexpired_lease_is_left_alone(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store, conversation = seeded_store(config)
    write_lease(conversation, a_lease(paused_ago=10, ttl=3600))
    manager = a_manager(config, store, confirm=lambda _id, **_k: (True, "should not run"))
    assert manager.reconcile_leases() == []
    assert read_lease(conversation) is not None


def test_unconfirmed_destroy_keeps_the_lease(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store, conversation = seeded_store(config)
    write_lease(conversation, a_lease(paused_ago=7200, ttl=3600))
    manager = a_manager(config, store, confirm=lambda _id, **_k: (False, "gateway wedged"))
    assert manager.reconcile_leases() == []
    # fail-closed：记录保留，下一轮重试。
    assert read_lease(conversation) is not None


def test_destroy_probe_error_keeps_the_lease(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store, conversation = seeded_store(config)
    write_lease(conversation, a_lease(paused_ago=7200, ttl=3600))

    def explode(_sandbox_id: str, **_k: Any):
        raise TimeoutError("probe hung")

    manager = a_manager(config, store, confirm=explode)
    assert manager.reconcile_leases() == []
    assert read_lease(conversation) is not None


def test_retired_sandboxes_are_retried_on_a_live_lease(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store, conversation = seeded_store(config)
    write_lease(
        conversation,
        a_lease(paused_ago=10, ttl=3600, retired=("sbx-stuck-1", "sbx-stuck-2")),
    )
    confirmed: list[str] = []
    manager = a_manager(
        config, store,
        confirm=lambda sandbox_id, **_k: (
            confirmed.append(sandbox_id) or (
                (False, "still stuck") if sandbox_id == "sbx-stuck-1" else (True, "gone")
            )
        ),
    )
    assert manager.reconcile_leases() == []
    lease = read_lease(conversation)
    assert lease is not None
    # 只有确认销毁的离开挂账；未确认的留到下轮。
    assert lease.retired_sandbox_ids == ("sbx-stuck-1",)
    assert confirmed == ["sbx-stuck-1", "sbx-stuck-2"]


def test_no_e2b_key_leaves_every_lease_in_place(tmp_path: Path) -> None:
    config = make_config(tmp_path, E2B_API_KEY="")
    store, conversation = seeded_store(config)
    write_lease(conversation, a_lease(paused_ago=7200, ttl=3600))
    manager = a_manager(config, store, confirm=lambda _id, **_k: (True, "must not run"))
    assert manager.reconcile_leases() == []
    assert read_lease(conversation) is not None


# -- the deletion link --------------------------------------------------------------


def test_delete_conversation_destroys_and_removes(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store, conversation = seeded_store(config)
    write_lease(conversation, a_lease(retired=("sbx-retired",)))
    (conversation / "state").mkdir()
    (conversation / "state" / "session.zst").write_bytes(b"state")
    destroyed: list[str] = []
    manager = a_manager(
        config, store,
        confirm=lambda sandbox_id, **_k: (destroyed.append(sandbox_id) or (True, "gone")),
    )
    result = manager.delete_conversation("conv-1")
    assert result["deleted"] is True and result["failures"] == []
    assert destroyed == ["sbx-1", "sbx-retired"]
    assert not conversation.exists()
    # The run record is a separate product surface: untouched.
    assert store.get("conv-1") is not None


def test_delete_conversation_keeps_state_when_a_destroy_is_unconfirmed(tmp_path: Path):
    config = make_config(tmp_path)
    store, conversation = seeded_store(config)
    write_lease(conversation, a_lease())
    manager = a_manager(config, store, confirm=lambda _id, **_k: (False, "still alive"))
    result = manager.delete_conversation("conv-1")
    assert result["deleted"] is False
    assert result["failures"] and "sbx-1" in result["failures"][0]
    assert conversation.is_dir()
    assert read_lease(conversation) is not None


def test_delete_conversation_without_a_lease_just_removes_the_directory(tmp_path: Path):
    config = make_config(tmp_path)
    store, conversation = seeded_store(config)
    (conversation / "state").mkdir()
    manager = a_manager(config, store, confirm=lambda _id, **_k: (True, "gone"))
    assert manager.delete_conversation("conv-1")["deleted"] is True
    assert not conversation.exists()


def test_delete_conversation_gates_on_tenancy(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        PI_GLINK_TENANCY="multitenant",
        PI_GLINK_AUTH_MODE="accounts",
        PI_GLINK_MODEL_CREDENTIALS="shared",
        OPENAI_API_KEY="sk-test",
    )
    store, conversation = seeded_store(config)
    write_lease(conversation, a_lease())
    manager = a_manager(config, store, confirm=lambda _id, **_k: (True, "gone"))
    # Unknown conversation and foreign owner answer alike and destroy nothing.
    assert manager.delete_conversation("no-such")["reason"] == "unknown"
    assert manager.delete_conversation("conv-1", context("other-tenant"))["reason"] == "unknown"
    assert conversation.is_dir()
    store.update("conv-1", tenant_id="tenant-a")
    assert manager.delete_conversation("conv-1", context("tenant-a"))["deleted"] is True
    assert not conversation.exists()


def test_delete_conversation_unknown_in_local_mode(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store, _ = seeded_store(config)
    manager = a_manager(config, store)
    assert manager.delete_conversation("no-such") == {"deleted": False, "reason": "unknown"}
    # 台账层直接删一个不存在的对话目录：无租约无沙箱，直接成功。
    assert manager._ledger.delete_conversation("never-created") == (True, [])  # noqa: SLF001


# -- leases do not occupy run slots -------------------------------------------------


def test_a_parked_run_holds_no_slot(tmp_path: Path) -> None:
    """泊车的沙箱不是 resource hold：下一个 run 照常受理（local 模式最严）。"""
    config = make_config(tmp_path)
    store, conversation = seeded_store(config)
    write_lease(conversation, a_lease())
    manager = a_manager(config, store)
    assert manager._ledger.resource_holds() == []
    # The local-mode admission gate refuses on any hold; with none, the check
    # passes (the manager is not asked to actually run anything here).
    manager._check_admission(None, manager.store.holding_records())  # noqa: SLF001


def test_conversation_ids_list_only_real_directories(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store, _ = seeded_store(config)
    assert store.conversation_ids() == ["conv-1"]
    # A stray file (not a directory) is never a conversation.
    (store.conversations_root() / "noise.txt").write_text("x", encoding="utf-8")
    assert store.conversation_ids() == ["conv-1"]


# -- 活跃对话保护（cc 终审 S1/S3） --------------------------------------------------


def test_sweep_skips_a_conversation_with_a_holding_run(tmp_path: Path) -> None:
    """过期租约若对话内仍有 run 持有资源：跳过本轮，不销毁热路径正用的沙箱。"""
    config = make_config(tmp_path)
    store, conversation = seeded_store(config)
    # 一个仍持有资源的子 run（追问进行中）：parent 链归根 conv-1。
    store.create(
        "追问中",
        run_id="conv-2",
        spec=RunSpec(family="acp", pack="dsh", executor="e2b"),
        parent_run_id="conv-1",
    )
    assert store.holding_conversation_roots() == {"conv-1"}
    write_lease(conversation, a_lease(paused_ago=7200.0, ttl=3600.0))
    manager = a_manager(config, store)
    assert manager.reconcile_leases() == [], "对话内有活跃 run：本轮跳过销毁"
    # 终态释放后，下一轮恢复销毁。
    finish(store, "conv-2", FINISHED, finished_at=time.time(),
           cleanup={"released": True, "failures": [], "external": {}})
    assert store.holding_conversation_roots() == set()
    assert manager.reconcile_leases() == ["conv-1"]


def test_delete_conversation_refuses_while_a_run_holds_resources(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store, conversation = seeded_store(config)
    write_lease(conversation, a_lease(retired=("sbx-retired",)))
    store.create(
        "追问中",
        run_id="conv-2",
        spec=RunSpec(family="acp", pack="dsh", executor="e2b"),
        parent_run_id="conv-1",
    )
    manager = a_manager(config, store)
    ok, failures = manager._ledger.delete_conversation("conv-1")  # noqa: SLF001
    assert ok is False and any("运行中的 run" in f for f in failures)
    assert conversation.exists(), "对话目录与租约原样保留"


def test_holding_roots_survive_broken_records(tmp_path: Path) -> None:
    """损坏 record / 断父链：holding 归属按已知事实回退，不丢不崩（S1/S3 的判定地基）。"""
    import sqlite3

    config = make_config(tmp_path)
    store, _conversation = seeded_store(config)
    store.create(
        "追问中",
        run_id="conv-2",
        spec=RunSpec(family="acp", pack="dsh", executor="e2b"),
        parent_run_id="conv-1",
    )
    assert store.holding_conversation_roots() == {"conv-1"}
    # 父 record 损坏：链在损坏行断开，holding 归到该行自身的 run id——
    # 恰是对话根名，sweep/delete 的判定仍然正确。
    with sqlite3.connect(store.run_dir("conv-1").parent / "runs.db") as conn:
        conn.execute(
            "UPDATE runs SET record = '{bad json' WHERE run_id = 'conv-1'"
        )
        conn.commit()
    assert store.holding_conversation_roots() == {"conv-1"}
    # 自身 record 损坏：归自身为根，绝不静默消失。
    with sqlite3.connect(store.run_dir("conv-2").parent / "runs.db") as conn:
        conn.execute(
            "UPDATE runs SET record = '{bad json' WHERE run_id = 'conv-2'"
        )
        conn.commit()
    assert store.holding_conversation_roots() == {"conv-2"}


def _parkable_handle(calls: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        files=SimpleNamespace(write=lambda *_args: None),
        commands=SimpleNamespace(run=lambda *_args, **_kwargs: None),
        pause=lambda **_kwargs: calls.append("pause"),
        kill=lambda: calls.append("kill"),
    )


def test_parked_sandboxes_are_capped_host_wide(tmp_path: Path) -> None:
    """原义务：TTL 只限制每个暂停沙箱保留多久，不限制累计数量——大量短对话可以
    在云端堆积任意多个暂停沙箱。
    新义务：仍有效的租约数达到 ``PI_GLINK_MAX_PARKED_SANDBOXES`` 时不再泊车，
    本沙箱按冷路径销毁（宿主权威状态不受影响，下一轮走回灌）。"""
    config = make_config(tmp_path, PI_GLINK_MAX_PARKED_SANDBOXES="1")
    store = RunStore(config.data_root / "runs")
    write_lease(store.conversation_dir("other"), a_lease(sandbox_id="sbx-other"))
    calls: list[str] = []
    outcome = park_sandbox(
        _parkable_handle(calls), config=config, conversation_dir=store.conversation_dir("conv-1"),
        conversation_run="conv-1", sandbox_id="sbx-new", template="glink-acp-worker",
        pack="dsh", run_id="conv-1", credential_dests=(),
    )
    assert not outcome.ok and outcome.destroyed
    assert "上限" in (outcome.reason or "")
    assert calls == ["kill"]
    assert read_lease(store.conversation_dir("conv-1")) is None


def test_the_park_cap_ignores_expired_leases_and_this_conversation(tmp_path: Path) -> None:
    config = make_config(tmp_path, PI_GLINK_MAX_PARKED_SANDBOXES="1")
    store = RunStore(config.data_root / "runs")
    # An expired lease is already the sweep's to destroy; it holds no hot slot.
    write_lease(store.conversation_dir("stale"), a_lease(sandbox_id="sbx-stale", paused_ago=7200, ttl=60))
    # Re-parking the same conversation replaces its own lease: not an extra slot.
    write_lease(store.conversation_dir("conv-1"), a_lease(sandbox_id="sbx-prev"))
    calls: list[str] = []
    outcome = park_sandbox(
        _parkable_handle(calls), config=config, conversation_dir=store.conversation_dir("conv-1"),
        conversation_run="conv-1", sandbox_id="sbx-new", template="glink-acp-worker",
        pack="dsh", run_id="conv-2", credential_dests=(),
        kill_by_id=lambda _sandbox_id: (True, "gone"),
    )
    assert outcome.ok, outcome.reason
    assert calls == ["pause"]


def test_the_park_cap_must_be_positive(tmp_path: Path) -> None:
    from pi_glink.config import ConfigError

    with pytest.raises(ConfigError, match="PI_GLINK_MAX_PARKED_SANDBOXES"):
        make_config(tmp_path, PI_GLINK_MAX_PARKED_SANDBOXES="0")


def test_concurrent_parks_cannot_both_take_the_last_slot(tmp_path: Path, monkeypatch) -> None:
    """原义务：各对话在自己的锁里"读占用数→写租约"，两个并发泊车都读到 0，上限 1
    下留下 2 份有效租约。新义务：计数与写租约是全宿主的一个预留临界段（纯本地，
    不含云调用），上限在并发下仍成立。"""
    import threading

    from pi_glink.kernel import acp_sandbox_park

    config = make_config(tmp_path, PI_GLINK_MAX_PARKED_SANDBOXES="1")
    store = RunStore(config.data_root / "runs")
    original = acp_sandbox_park._live_parked_elsewhere
    entered = threading.Event()

    def slow_count(directory):
        count = original(directory)
        entered.set()
        time.sleep(0.3)  # widen the old read→write window
        return count

    monkeypatch.setattr(acp_sandbox_park, "_live_parked_elsewhere", slow_count)
    outcomes: list = []

    def park(name: str) -> None:
        outcomes.append(park_sandbox(
            _parkable_handle([]), config=config, conversation_dir=store.conversation_dir(name),
            conversation_run=name, sandbox_id=f"sbx-{name}", template="glink-acp-worker",
            pack="dsh", run_id=name, credential_dests=(),
        ))

    threads = [threading.Thread(target=park, args=(name,)) for name in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    live = [read_lease(store.conversation_dir(name)) for name in ("A", "B")]
    assert sum(outcome.ok for outcome in outcomes) == 1
    assert sum(lease is not None and not lease.is_expired() for lease in live) == 1


def test_a_refused_park_does_not_leave_a_phantom_slot(tmp_path: Path) -> None:
    """原义务：超限拒绝时只销毁本沙箱，本对话的旧租约仍未到期，继续被计入上限
    （它指向的沙箱已被本轮替换销毁）。新义务：拒绝后本对话的记录已结清——全部确认
    销毁即删除；有未确认身份则以**已过期**记录保留给对账，不再占名额。"""
    config = make_config(tmp_path, PI_GLINK_MAX_PARKED_SANDBOXES="1")
    store = RunStore(config.data_root / "runs")
    write_lease(store.conversation_dir("other"), a_lease(sandbox_id="sbx-other"))
    write_lease(store.conversation_dir("conv-1"), a_lease(sandbox_id="sbx-prev"))
    killed: list[str] = []
    outcome = park_sandbox(
        _parkable_handle([]), config=config, conversation_dir=store.conversation_dir("conv-1"),
        conversation_run="conv-1", sandbox_id="sbx-new", template="glink-acp-worker",
        pack="dsh", run_id="conv-2", credential_dests=(),
        kill_by_id=lambda sandbox_id: (killed.append(sandbox_id) or (True, "gone")),
    )
    assert not outcome.ok and outcome.destroyed
    assert killed == ["sbx-prev"]
    assert read_lease(store.conversation_dir("conv-1")) is None


def test_a_refused_park_keeps_unconfirmed_identities_expired(tmp_path: Path) -> None:
    config = make_config(tmp_path, PI_GLINK_MAX_PARKED_SANDBOXES="1")
    store = RunStore(config.data_root / "runs")
    write_lease(store.conversation_dir("other"), a_lease(sandbox_id="sbx-other"))
    write_lease(store.conversation_dir("conv-1"), a_lease(sandbox_id="sbx-prev"))

    def fail(**_kwargs: Any) -> None:
        raise OSError("control plane unavailable")

    handle = SimpleNamespace(
        files=SimpleNamespace(write=lambda *_args: None),
        commands=SimpleNamespace(run=lambda *_args, **_kwargs: None),
        pause=lambda **_kwargs: None, kill=fail,
    )
    outcome = park_sandbox(
        handle, config=config, conversation_dir=store.conversation_dir("conv-1"),
        conversation_run="conv-1", sandbox_id="sbx-new", template="glink-acp-worker",
        pack="dsh", run_id="conv-2", credential_dests=(),
        kill_by_id=lambda _sandbox_id: (False, "stuck"),
    )
    assert not outcome.ok and not outcome.destroyed
    lease = read_lease(store.conversation_dir("conv-1"))
    assert lease is not None and lease.is_expired()
    assert {lease.sandbox_id, *lease.retired_sandbox_ids} == {"sbx-new", "sbx-prev"}
