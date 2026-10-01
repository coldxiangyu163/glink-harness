"""Regressions for the resource ledger and its online reconciliation (GLINK-RES-01).

The obligation this file pins is the one that replaced "any unreleased resource
blocks every tenant": a hold **consumes slots** (its owner tenant's and the
global one) in multi-tenant mode, keeps consuming them until the execution face
itself confirms the resource is gone, and is confirmed online by addressing only
the identity persisted with the run. No test here touches a real Docker daemon
or the real E2B SDK: the probes and the sandbox factory are injected fakes.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any

import pytest

from glink_harness import event_schema
from glink_harness.config import HostConfig, build_config
from glink_harness.events import EventBus
from glink_harness.kernel import docker_env, e2b_env
from glink_harness.kernel.execution import (
    ArtifactRecovery,
    CleanupReport,
    ExecutionAdapter,
    ExecutionOutcome,
    SessionInfo,
)
from glink_harness.runs.runner import ResourcesUnreleased, RunManager, RunQuotaExceeded
from glink_harness.ownership import DataRootOwnership
from glink_harness.store import FAILED, FINISHED, RUNNING, RunSpec, RunStore, StoreError
from glink_harness.tenancy import TenantContext
from store_helpers import finish

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_config(tmp_path: Path, **overrides: str) -> HostConfig:
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


def make_multitenant_config(tmp_path: Path, **overrides: str) -> HostConfig:
    """Multi-tenant Docker config; the identity entry does not affect the ledger."""
    values = {
        "LOCAL_WEB_EXECUTOR": "docker",
        "GLINK_TENANCY": "multitenant",
        "GLINK_AUTH_MODE": "accounts",
        "GLINK_MODEL_CREDENTIALS": "shared",
    }
    values.update(overrides)
    return make_config(tmp_path, **values)


def make_e2b_config(tmp_path: Path, **overrides: str) -> HostConfig:
    values = {
        "LOCAL_WEB_EXECUTOR": "e2b",
        "E2B_API_KEY": "e2b-secret-key",
        "LOCAL_WEB_E2B_TEMPLATE": "glink-harness-worker",
        "GLINK_EGRESS_PUBLIC_URL": "https://egress.example.test",
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
    store.create("泄漏的 run", run_id=run_id, tenant_id=tenant_id, spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    store.update(run_id, container_name=container, sandbox_id=sandbox)
    finish(store, run_id, FINISHED, finished_at=time.time(), cleanup={"released": released, "failures": ["synthetic"], "external": {}})


# -- the ledger counts instead of blocking ------------------------------------


def test_a_leak_consumes_its_tenant_and_the_global_slots(tmp_path: Path) -> None:
    config = make_multitenant_config(
        tmp_path, GLINK_MAX_TENANT_RUNS="2", GLINK_MAX_GLOBAL_RUNS="4"
    )
    store = RunStore(config.data_root / "runs")
    record_hold(store, "leak-a1", tenant_id="tenant-a", container="glink-harness-leak-a1")
    record_hold(store, "leak-a2", tenant_id="tenant-a", container="glink-harness-leak-a2")
    record_hold(store, "leak-legacy", container="glink-harness-leak-legacy")
    manager = RunManager(config, EventBus(), store)

    with pytest.raises(RunQuotaExceeded) as tenant_hit:
        manager._check_admission(context("tenant-a"), manager.store.holding_records())  # noqa: SLF001
    assert (tenant_hit.value.scope, tenant_hit.value.limit) == ("tenant", 2)

    # Another tenant is not blocked by tenant A's leftovers: global 3/4 is free.
    manager._check_admission(context("tenant-b"), manager.store.holding_records())  # noqa: SLF001

    record_hold(store, "leak-b1", tenant_id="tenant-b", container="glink-harness-leak-b1")
    with pytest.raises(RunQuotaExceeded) as global_hit:
        manager._check_admission(context("tenant-b"), manager.store.holding_records())  # noqa: SLF001
    assert (global_hit.value.scope, global_hit.value.limit) == ("global", 4)
    # A refusal names the ceiling, never another tenant's run.
    assert "leak" not in str(global_hit.value)


def test_local_mode_still_refuses_on_any_hold(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    record_hold(store, "leak")
    manager = RunManager(config, EventBus(), store)

    with pytest.raises(ResourcesUnreleased) as refusal:
        manager._check_admission(None, manager.store.holding_records())  # noqa: SLF001
    assert refusal.value.run_ids == ["leak"]


def test_the_ledger_survives_a_restart(tmp_path: Path) -> None:
    config = make_multitenant_config(tmp_path, GLINK_MAX_GLOBAL_RUNS="1")
    store = RunStore(config.data_root / "runs")
    record_hold(store, "leak", tenant_id="tenant-a", container="glink-harness-leak")

    restarted = RunManager(config, EventBus(), RunStore(config.data_root / "runs"))
    with pytest.raises(RunQuotaExceeded):
        restarted._check_admission(context("tenant-b"), restarted.store.holding_records())  # noqa: SLF001
    assert [entry["runId"] for entry in restarted.unreleased_resources()] == ["leak"]


# -- corrupt records are a global hold ----------------------------------------


def test_an_unparseable_row_fails_admission_closed(tmp_path: Path) -> None:
    """A row the store cannot parse never frees capacity: admission refuses.

    Red→green mechanism: counting holds by skipping unparseable rows would
    admit the run; the store raises ``StoreError`` inside the admission
    transaction instead, which rolls the insert back.
    """
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    record_hold(store, "broken")
    with store._lock:  # noqa: SLF001 - synthetic corruption of one row
        store._connection.execute(  # noqa: SLF001
            "UPDATE runs SET record = '{ not JSON' WHERE run_id = 'broken'"
        )
    manager = RunManager(config, EventBus(), store)

    with pytest.raises(StoreError):
        manager.start("不可解析的行不能让准入放行")
    assert [row[0] for row in store._connection.execute("SELECT run_id FROM runs")] == ["broken"]  # noqa: SLF001


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


# -- online reconciliation ----------------------------------------------------


def test_a_confirmed_probe_releases_capacity(tmp_path: Path) -> None:
    config = make_config(tmp_path, LOCAL_WEB_EXECUTOR="docker")
    store = RunStore(config.data_root / "runs")
    record_hold(store, "leak", container="glink-harness-leak")
    calls: list[str] = []

    def probe(name: str) -> tuple[bool, str]:
        calls.append(name)
        return True, "fake docker: 容器不存在"

    manager = RunManager(config, EventBus(), store, docker_probe=probe)
    assert manager.reconcile_resources() == ["leak"]
    assert calls == ["glink-harness-leak"], "探测只使用 meta 中持久化的身份"
    record = store.get("leak")
    assert record.resource_adjudication["source"] == "auto-verified"
    assert record.resource_adjudication["method"] == "docker-inspect"
    assert "容器不存在" in record.resource_adjudication["note"]
    assert record.cleanup["released"] is False, "原清理事实不得被改写"
    assert record.status == FINISHED
    assert store.holding_records() == []
    assert manager.unreleased_resources() == []
    assert manager.reconcile_resources() == [], "已释放的持有不再重复探测"
    assert calls == ["glink-harness-leak"]


@pytest.mark.parametrize("mode", ["uncertain", "error", "timeout"])
def test_a_probe_that_cannot_confirm_keeps_the_hold(tmp_path: Path, mode: str) -> None:
    config = make_config(tmp_path, LOCAL_WEB_EXECUTOR="docker")
    store = RunStore(config.data_root / "runs")
    record_hold(store, "leak", container="glink-harness-leak")

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
    config = make_config(
        tmp_path,
        LOCAL_WEB_EXECUTOR="e2b",
        GLINK_EGRESS_PUBLIC_URL="https://egress.example.test",
    )
    store = RunStore(config.data_root / "runs")
    record_hold(store, "no-identity")
    calls: list[str] = []
    manager = RunManager(
        config,
        EventBus(),
        store,
        sandbox_probe=lambda sandbox_id: (calls.append(sandbox_id) or True, "fake"),
    )
    with caplog.at_level(logging.INFO, logger="glink_harness.runs.resource_ledger"):
        assert manager.reconcile_resources() == []
    assert calls == [], "没有持久化身份时不得猜测或批量操作"
    assert "缺少持久化的执行身份" in caplog.text
    assert store.get("no-identity").holds_resources is True


def test_the_probe_face_follows_the_persisted_identity(tmp_path: Path) -> None:
    """An operator may have switched faces since the run; the record decides."""
    for executor in ("docker", "e2b"):
        config = make_config(
            tmp_path / executor,
            LOCAL_WEB_EXECUTOR=executor,
            GLINK_EGRESS_PUBLIC_URL="https://egress.example.test",
        )
        store = RunStore(config.data_root / "runs")
        record_hold(store, "container-hold", container="glink-harness-container-hold")
        record_hold(store, "sandbox-hold", sandbox="sbx-hold")
        record_hold(store, "both-hold", container="glink-harness-both", sandbox="sbx-both")
        record_hold(store, "no-identity")
        docker_calls: list[str] = []
        sandbox_calls: list[str] = []
        # 默认参数绑定循环体内的列表：闭包不再经过循环变量间接访问（B023）。
        manager = RunManager(
            config,
            EventBus(),
            store,
            docker_probe=lambda name, _calls=docker_calls: (_calls.append(name) or True, "确认不存在"),
            sandbox_probe=lambda sid, _calls=sandbox_calls: (_calls.append(sid) or True, "确认不存在"),
        )
        assert sorted(manager.reconcile_resources()) == [
            "both-hold",
            "container-hold",
            "sandbox-hold",
        ]
        assert sorted(docker_calls) == ["glink-harness-both", "glink-harness-container-hold"]
        assert sorted(sandbox_calls) == ["sbx-both", "sbx-hold"]
        assert [record.run_id for record in store.holding_records()] == ["no-identity"]


def test_a_record_with_two_identities_needs_all_of_them_confirmed(tmp_path: Path) -> None:
    config = make_config(tmp_path, LOCAL_WEB_EXECUTOR="docker")
    store = RunStore(config.data_root / "runs")
    record_hold(store, "both", container="glink-harness-both", sandbox="sbx-both")
    manager = RunManager(
        config,
        EventBus(),
        store,
        docker_probe=lambda _name: (True, "容器不存在"),
        sandbox_probe=lambda _sid: (False, "sandbox 仍存活"),
    )
    assert manager.reconcile_resources() == []
    assert store.get("both").holds_resources is True
    assert store.get("both").resource_adjudication is None


def _install_fake_e2b(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: bool = True,
    errors: list[BaseException] | None = None,
    calls: list[tuple[str, dict[str, Any]]] | None = None,
) -> tuple[type[BaseException], list[BaseException]]:
    """Install a fake ``e2b`` module whose kill follows the SDK's contract."""
    pending = errors if errors is not None else []

    class FakeNotFound(Exception):
        pass

    class FakeSandbox:
        @staticmethod
        def kill(sandbox_id: str, **opts: Any) -> bool:
            if calls is not None:
                calls.append((sandbox_id, opts))
            if pending:
                raise pending.pop(0)
            return result

    module = types.ModuleType("e2b")
    module.NotFoundException = FakeNotFound  # type: ignore[attr-defined]
    module.SandboxNotFoundException = FakeNotFound  # type: ignore[attr-defined]
    module.Sandbox = FakeSandbox  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "e2b", module)
    return FakeNotFound, pending


def test_the_default_e2b_probe_authenticates_with_the_frozen_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe takes its credential from the frozen config, not the environment."""
    config = make_e2b_config(tmp_path)
    calls: list[tuple[str, dict[str, Any]]] = []
    _install_fake_e2b(monkeypatch, calls=calls)

    store = RunStore(config.data_root / "runs")
    record_hold(store, "leak", sandbox="sbx-1")
    manager = RunManager(config, EventBus(), store)
    assert manager.reconcile_resources() == ["leak"]
    assert calls == [("sbx-1", {"api_key": "e2b-secret-key"})]
    assert store.get("leak").resource_adjudication["source"] == "auto-verified"


def test_release_is_confirmed_only_by_the_sdk_not_found_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A message that mentions 404/not found is not evidence of release."""

    class Unauthorized(Exception):
        pass

    not_found, errors = _install_fake_e2b(monkeypatch)
    assert e2b_env.confirm_sandbox_absent("sbx", api_key="k")[0] is True, "kill 返回 True"
    errors.append(not_found("sandbox not found"))
    confirmed, detail = e2b_env.confirm_sandbox_absent("sbx", api_key="k")
    assert confirmed is True and "FakeNotFound" in detail
    errors.append(Unauthorized("401 404 not found: invalid api key"))
    confirmed, detail = e2b_env.confirm_sandbox_absent("sbx", api_key="k")
    assert confirmed is False, "消息里的 404/not found 不得当作已释放"
    assert "Unauthorized" in detail

    absent, _pending = _install_fake_e2b(monkeypatch, result=False)
    assert absent is not None
    assert e2b_env.confirm_sandbox_absent("sbx", api_key="k")[0] is True, "kill 返回 False"


@pytest.mark.parametrize(
    "message",
    ["401 not found: invalid api key", "404 sandbox not found"],
    ids=["auth", "gateway"],
)
def test_a_non_not_found_error_keeps_the_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    class Unauthorized(Exception):
        pass

    config = make_e2b_config(tmp_path)
    _install_fake_e2b(monkeypatch, errors=[Unauthorized(message)])
    store = RunStore(config.data_root / "runs")
    record_hold(store, "leak", sandbox="sbx-1")

    manager = RunManager(config, EventBus(), store)
    assert manager.reconcile_resources() == []
    assert store.get("leak").holds_resources is True
    assert store.get("leak").resource_adjudication is None


def test_a_real_sdk_not_found_type_confirms_the_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    e2b = pytest.importorskip("e2b")
    _install_fake_e2b(
        monkeypatch, errors=[e2b.SandboxNotFoundException("sandbox not found")]
    )
    monkeypatch.setattr(
        "glink_harness.kernel.e2b_env._not_found_types",
        lambda: (e2b.SandboxNotFoundException, e2b.NotFoundException),
    )
    config = make_e2b_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    record_hold(store, "leak", sandbox="sbx-1")

    manager = RunManager(config, EventBus(), store)
    assert manager.reconcile_resources() == ["leak"]
    assert store.holding_records() == []


def test_the_probe_only_touches_the_persisted_container_name(tmp_path: Path) -> None:
    """The Docker probe itself: inspect by name, remove by name, confirm again."""
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

    confirmed, detail = docker_env.confirm_absent("glink-harness-run1", runner=runner)
    assert (confirmed, "不存在" in detail) == (True, True)
    assert calls[0] == [
        "docker", "inspect", "--type", "container", "--format", "{{.Id}}", "glink-harness-run1",
    ]
    assert calls[1] == ["docker", "rm", "-f", "glink-harness-run1"]
    assert all("glink-harness-run1" in argv for argv in calls)

    # A daemon that cannot answer keeps the hold instead of assuming release.
    def broken(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess:
        raise OSError("docker CLI 不可用")

    assert docker_env.confirm_absent("glink-harness-run1", runner=broken)[0] is False
    assert docker_env.confirm_absent("", runner=broken)[0] is False


def test_the_sweep_never_probes_a_run_that_is_still_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_multitenant_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    record_hold(store, "finished-leak", tenant_id="tenant-a", container="glink-harness-finished-leak")
    calls: list[str] = []
    manager = RunManager(
        config,
        EventBus(),
        store,
        docker_probe=lambda name: (calls.append(name) or True, "fake docker: 不存在"),
    )
    adapter = _BlockingAdapter()
    monkeypatch.setattr("glink_harness.kernel.harness_pi.PiExecutionAdapter", lambda spec, **kwargs: adapter)
    monkeypatch.setattr("glink_harness.kernel.sandbox.docker_available", lambda: True)

    run_id = manager.start("进行中", context("tenant-a")).run_id
    assert store.get(run_id).holds_resources is True, "运行中的 run 也是持有"
    assert manager.reconcile_resources() == ["finished-leak"]
    assert calls == ["glink-harness-finished-leak"], "在途 run 由自己的执行面负责"
    manager.stop(run_id, context("tenant-a"))
    assert manager.wait(run_id, timeout=30).status in {"cancelled", "failed"}


def test_the_watch_thread_sweeps_once_after_startup_and_stops(tmp_path: Path) -> None:
    config = make_config(
        tmp_path, LOCAL_WEB_EXECUTOR="docker", GLINK_RESOURCE_RECONCILE_SECONDS="1"
    )
    store = RunStore(config.data_root / "runs")
    record_hold(store, "leak", container="glink-harness-leak")
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
    assert calls == ["glink-harness-leak"]

    # inproc has no external resource, and 0 disables the sweep entirely.
    inproc = RunManager(make_config(tmp_path / "other"), EventBus(), RunStore(tmp_path / "other" / "runs"))
    assert inproc.start_resource_watch() is None
    disabled = RunManager(
        make_config(tmp_path / "off", LOCAL_WEB_EXECUTOR="docker", GLINK_RESOURCE_RECONCILE_SECONDS="0"),
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
    monkeypatch.setattr("glink_harness.kernel.sandbox.docker_available", lambda: True)
    monkeypatch.setattr(
        "glink_harness.kernel.sandbox.subprocess.run",
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


class _BlockingAdapter(ExecutionAdapter):
    """A run that stays in flight until it is cancelled."""

    kind = "docker"

    def __init__(self) -> None:
        super().__init__(None, on_frame=lambda frame: None)  # type: ignore[arg-type]
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
    from glink_harness.store import RunRecord

    record = RunRecord(
        run_id="x", prompt="p", status=RUNNING, created_at=time.time(), workspace="/tmp/x",
        spec=RunSpec(family="pi", pack="pi", executor="inproc"),
    )
    assert record.holds_resources is True


def test_reconcile_closes_leftover_children_and_summarizes_them(tmp_path: Path) -> None:
    """The interrupted close-out sweeps child documents too, and its terminal
    frame carries the same ``children`` summary as a normal verdict."""
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    store.create("遗留 run", run_id="leftover-kids", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    store.write_child(
        "leftover-kids",
        {"n": 0, "toolCallId": "c1", "agent": "researcher", "task": "调研甲",
         "status": "running", "resultPreview": "", "enrichment": "none"},
    )
    store.write_child(
        "leftover-kids",
        {"n": 1, "toolCallId": "c2", "agent": "researcher", "task": "调研乙",
         "status": "finished", "resultPreview": "摘要", "enrichment": "none"},
    )
    ownership = DataRootOwnership.acquire(config.data_root)
    try:
        manager = RunManager(config, EventBus(), store, ownership=ownership)
        assert manager.reconcile() == ["leftover-kids"]
    finally:
        ownership.release()

    assert store.read_child("leftover-kids", 0)["status"] == "interrupted"
    assert store.read_child("leftover-kids", 1)["status"] == "finished", "已收口的不改写"
    terminal = store.committed_terminal("leftover-kids")
    assert terminal["type"] == "run_failed"
    # The reconciled terminal bypasses the bus but is a versioned, schema-valid
    # envelope like any bus frame.
    assert event_schema.envelope_problems(terminal) == []
    assert terminal["payload"]["children"] == [
        {"n": 0, "agent": "researcher", "task": "调研甲", "status": "interrupted"},
        {"n": 1, "agent": "researcher", "task": "调研乙", "status": "finished"},
    ]
