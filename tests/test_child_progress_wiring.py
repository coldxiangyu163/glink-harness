"""R3a 接线测试：适配器 child_progress_source seam 与 runner 观察线程。

契约见 docs/architecture.md §3.5「运行中进度（R3a）」：观察者是瞬时事实的
供给方——懒启动、每 run 至多一个轮询线程、每 child 节流、映射不上不伪造、
finished 后不再发、finalize 时退出、连续失败按有界退避后**继续重试**（临时故障
不造成 run 生命周期内永久失明）；它永远不改变 verdict、不写 child.json、不在
准入锁内执行。这里全部用合成源与无内核的 execution（与 test_runner 的 typed-event
seam 同一风格），不触真实内核。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from pi_glink import events
from pi_glink.config import HostConfig, build_config

from acp_pack import apply_acp_pack
from pi_glink.events import EventBus
from pi_glink.kernel.acp_child_progress import AcpChildProgressSource
from pi_glink.kernel.acp_child_progress import ProgressFact as AcpProgressFact
from pi_glink.kernel.acp_execution import AcpExecutionAdapter
from pi_glink.kernel.child_progress import PiChildProgressSource
from pi_glink.kernel.child_progress import ProgressFact as PiProgressFact
from pi_glink.kernel.execution import (
    ArtifactRecovery,
    CleanupReport,
    ExecutionAdapter,
    ExecutionOutcome,
    ExecutionSpec,
    PiExecutionAdapter,
)
from pi_glink.kernel.sandbox import Launch, Sandbox, open_sandbox
from pi_glink.kernel.child_progress import E2bChildProgressSource
from pi_glink.runs import child_progress_observer as observer_module
from pi_glink.runs.child_progress_observer import ChildProgressObserver
from pi_glink.runs.runner import RunManager
from pi_glink.runs.state import RunExecution as _Execution
from pi_glink.store import RunStore

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
    apply_acp_pack(values, tmp_path)
    return build_config(values, REPO_ROOT)


@pytest.fixture(autouse=True)
def fast_progress_clock(monkeypatch: pytest.MonkeyPatch):
    """Keep the observer's cadence test-sized; the bounds themselves stay real."""
    monkeypatch.setattr(observer_module, "_PROGRESS_POLL_SECONDS", 0.02)
    monkeypatch.setattr(observer_module, "_PROGRESS_THROTTLE_SECONDS", 0.12)
    monkeypatch.setattr(observer_module, "_PROGRESS_MAX_BACKOFF_SECONDS", 0.05)


def build_manager(tmp_path: Path, **overrides: str):
    config = make_config(tmp_path, **overrides)
    bus = EventBus()
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, bus, store, client_factory=lambda *a, **k: None)
    return manager, store


def read_events(store: RunStore, run_id: str) -> list[Mapping[str, Any]]:
    path = store.events_path(run_id)
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def wait_for(predicate, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def progress_events(store: RunStore, run_id: str) -> list[Mapping[str, Any]]:
    return [
        event["payload"]
        for event in read_events(store, run_id)
        if event["type"] == events.SUBAGENT_UPDATED
    ]


class FakeSource:
    """A pollable progress source the test drives by mutating ``facts``."""

    def __init__(self, facts: list[Any] | None = None, error: Exception | None = None):
        self.facts = list(facts or [])
        self.error = error
        self.polls = 0
        self.closed = False

    def poll(self) -> list[Any]:
        self.polls += 1
        if self.error is not None:
            raise self.error
        return list(self.facts)

    def close(self) -> None:
        self.closed = True


class FakeAdapter(ExecutionAdapter):
    """An ABC adapter carrying one progress source (no kernel behind it).

    ``start``/``deliver_input``/``wait`` are never called on this path (the
    tests drive the typed-event seam directly); they exist only to satisfy
    the ABC.
    """

    def __init__(self, source: FakeSource | None):
        super().__init__(None, on_frame=lambda frame: None)  # type: ignore[arg-type]
        self._source = source

    def child_progress_source(self) -> FakeSource | None:
        return self._source

    def start(self) -> None:
        raise AssertionError("原路径本不会调到")

    def deliver_input(self) -> None:
        raise AssertionError("原路径本不会调到")

    def wait(self, *, cancel, deadline):
        raise AssertionError("原路径本不会调到")

    def recover_artifacts(self, destination: Path) -> ArtifactRecovery:
        return ArtifactRecovery()

    def close(self) -> CleanupReport:
        return CleanupReport(released=True)

    def identity(self) -> Mapping[str, str]:
        return {}

    def cancel(self) -> None:
        pass


def open_execution(
    manager: RunManager,
    store: RunStore,
    workspace: Path,
    run_id: str,
    *,
    adapter: Any = None,
) -> _Execution:
    """An open execution without a kernel: the typed-event commit seam."""
    # The run is admitted with the manager's own RunSpec, so an ACP-configured
    # host records an ACP run and the family-specific progress mapping applies.
    spec = manager._admission.spec(None)  # noqa: SLF001
    record = store.create("子代理进度", run_id=run_id, workspace=workspace, spec=spec)
    execution = _Execution(record=record, workspace=workspace)
    execution.adapter = adapter
    manager.bus.open_run(record.run_id)
    return execution


def emit(manager: RunManager, execution: _Execution, event_type: str, payload: Mapping[str, Any]) -> None:
    with execution.lock:
        assert manager._pipeline.channel.emit_frame(execution, event_type, payload) is True  # noqa: SLF001


def started(manager: RunManager, execution: _Execution, n: int, call_id: str, agent: str = "researcher") -> None:
    emit(
        manager,
        execution,
        events.SUBAGENT_STARTED,
        {"n": n, "toolCallId": call_id, "agent": agent, "task": f"任务{n}"},
    )


def progress_of(manager: RunManager, execution: _Execution) -> Any:
    """The run's progress observer, owned by its ``ChildProgressPlugin``."""
    from pi_glink.runs.plugins import ChildProgressPlugin  # noqa: PLC0415

    plugin = next(
        p for p in manager._pipeline.plugins(execution) if isinstance(p, ChildProgressPlugin)  # noqa: SLF001
    )
    assert plugin.observer is not None
    return plugin.observer


def finalize_ok(manager: RunManager, execution: _Execution) -> None:
    """Drive the real finalizer with a settled outcome (a clean finish)."""
    execution.execution_facts.outcome = ExecutionOutcome(settled=True)
    manager._pipeline.close_out(execution)  # noqa: SLF001


# -- adapter seam: child_progress_source() --------------------------------------


def _spec(tmp_path: Path, runtime: Any) -> ExecutionSpec:
    return ExecutionSpec(
        config=make_config(tmp_path),
        runtime=runtime,
        run_id="seam000",
        prompt="p",
        system_prompt="",
        workspace=tmp_path / "ws",
        timeout_seconds=60,
    )


def _acp_spec(tmp_path: Path, runtime: Any, **overrides: str) -> ExecutionSpec:
    """An ACP spec whose pack declares the dsh session progress source."""
    values = {"TEST_ACP_COMMAND": "fake-acp", "TEST_ACP_CHILD_PROGRESS": "dsh-session-zstd"}
    values.update(overrides)
    return ExecutionSpec(
        config=make_config(tmp_path, **values),
        runtime=runtime,
        run_id="seam000",
        prompt="p",
        system_prompt="",
        workspace=tmp_path / "ws",
        timeout_seconds=60,
    )


def _sandbox(spec: ExecutionSpec, executor: str) -> Sandbox:
    return open_sandbox(
        executor, config=spec.config, run_id=spec.run_id, workspace=spec.workspace
    )


def _pi_adapter(spec: ExecutionSpec, executor: str) -> PiExecutionAdapter:
    return PiExecutionAdapter(
        spec,
        sandbox=_sandbox(spec, executor),
        launch=Launch(argv=()),
        client_factory=lambda *a, **k: None,
        on_frame=lambda frame: None,
    )


def _acp_adapter(spec: ExecutionSpec, executor: str) -> AcpExecutionAdapter:
    return AcpExecutionAdapter(
        spec,
        sandbox=_sandbox(spec, executor),
        launch=Launch(argv=()),
        on_frame=lambda frame: None,
    )


def test_rpc_adapter_sources_the_host_session_dir(tmp_path: Path) -> None:
    """pi inproc/docker: the transcripts live in session_dir/subagent-artifacts."""
    session_dir = tmp_path / "sessions" / "seam000"
    spec = _spec(tmp_path, SimpleNamespace(session_dir=session_dir))
    for executor in ("inproc", "docker"):
        source = _pi_adapter(spec, executor).child_progress_source()

        assert isinstance(source, PiChildProgressSource)
        assert source._directory == session_dir / "subagent-artifacts"  # noqa: SLF001
        source.close()


def test_e2b_adapter_sources_only_while_the_sandbox_lives(tmp_path: Path) -> None:
    """pi e2b: no client (or a closed sandbox) honestly means no source."""
    adapter = _pi_adapter(_spec(tmp_path, SimpleNamespace(session_dir=tmp_path / "s")), "e2b")
    assert adapter.child_progress_source() is None, "start 前没有沙箱可观察"

    handle = SimpleNamespace(files=SimpleNamespace())
    adapter.sandbox._handle = handle  # noqa: SLF001 - 沙箱已创建
    source = adapter.child_progress_source()
    assert isinstance(source, E2bChildProgressSource)
    assert source._root.endswith("/subagent-artifacts")  # noqa: SLF001

    adapter.sandbox._handle = None  # noqa: SLF001 - sandbox 已销毁
    assert adapter.child_progress_source() is None


def test_acp_adapter_sources_the_state_root_sessions(tmp_path: Path) -> None:
    """ACP inproc/docker: dsh sessions below the run's state root (zstd 可用时)。"""
    home = tmp_path / "acp-home"
    spec = _acp_spec(tmp_path, SimpleNamespace(home=home))
    assert spec.config.pack.capabilities.child_progress == "dsh-session-zstd"
    for executor in ("inproc", "docker"):
        source = _acp_adapter(spec, executor).child_progress_source()

        assert isinstance(source, AcpChildProgressSource)
        assert source._root == home / ".dsh" / "sessions"  # noqa: SLF001
        source.close()


def test_acp_adapter_has_no_source_when_the_pack_declares_none(tmp_path: Path) -> None:
    """未声明 capabilities.child_progress 的 ACP 包如实没有进度源。

    族代码只读能力声明：即使状态根目录下恰好有 ``.dsh/sessions``，也不按
    目录或包名猜它是 dsh。
    """
    home = tmp_path / "acp-home"
    (home / ".dsh" / "sessions").mkdir(parents=True)
    spec = _acp_spec(tmp_path, SimpleNamespace(home=home), TEST_ACP_CHILD_PROGRESS="")
    assert spec.config.pack.capabilities.child_progress is None
    for executor in ("inproc", "docker"):
        assert _acp_adapter(spec, executor).child_progress_source() is None


def test_acp_adapter_has_no_source_without_zstandard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """zstandard 缺失时该面如实无进度，不报错（可选依赖降级路径）。"""
    monkeypatch.setattr(
        "pi_glink.kernel.acp_execution.acp_progress_available", lambda: False
    )
    adapter = _acp_adapter(_acp_spec(tmp_path, SimpleNamespace(home=tmp_path / "acp-home")), "inproc")

    assert adapter.child_progress_source() is None


def test_acp_sandbox_face_stays_coarse(tmp_path: Path) -> None:
    """ACP e2b 面不实现进度源（能力如实停留 coarse），沙箱活着也一样。"""
    adapter = _acp_adapter(_acp_spec(tmp_path, SimpleNamespace(home=tmp_path / "acp-home")), "e2b")
    assert adapter.child_progress_source() is None
    adapter.sandbox._handle = SimpleNamespace(files=SimpleNamespace())  # noqa: SLF001
    assert adapter.child_progress_source() is None


# -- runner wiring: lazy start, mapping, throttle, lifecycle --------------------


def test_no_subagent_means_no_observer(tmp_path: Path) -> None:
    """非 subagent run 零开销：从不提交 started，就从不创建线程或源。"""
    manager, store = build_manager(tmp_path)
    source = FakeSource()
    execution = open_execution(manager, store, tmp_path / "ws", "prog00", adapter=FakeAdapter(source))
    emit(manager, execution, events.KERNEL_STATUS, {"level": "info", "message": "ordinary"})

    finalize_ok(manager, execution)

    assert progress_of(manager, execution).started is False
    assert progress_of(manager, execution).thread is None
    assert source.polls == 0 and source.closed is False


def test_an_adapter_inheriting_the_default_seam_gets_no_thread(tmp_path: Path) -> None:
    """ABC 默认空实现：不覆写该方法即该面无进度，不报错。"""

    class NoSeamAdapter(ExecutionAdapter):
        """Everything an adapter needs, with the ABC's default (no) seam."""

        def __init__(self) -> None:
            super().__init__(None, on_frame=lambda frame: None)  # type: ignore[arg-type]

        def start(self) -> None:
            raise AssertionError("原路径本不会调到")

        def deliver_input(self) -> None:
            raise AssertionError("原路径本不会调到")

        def wait(self, *, cancel, deadline):
            raise AssertionError("原路径本不会调到")

        def recover_artifacts(self, destination: Path) -> ArtifactRecovery:
            return ArtifactRecovery()

        def close(self) -> CleanupReport:
            return CleanupReport(released=True)

        def identity(self) -> Mapping[str, str]:
            return {}

        def cancel(self) -> None:
            pass

    manager, store = build_manager(tmp_path)
    execution = open_execution(manager, store, tmp_path / "ws", "prog01", adapter=NoSeamAdapter())

    started(manager, execution, 0, "call_1")

    assert progress_of(manager, execution).started is True
    assert progress_of(manager, execution).thread is None
    assert progress_events(store, "prog01") == []
    assert not [
        e for e in read_events(store, "prog01") if e["type"] == events.KERNEL_STATUS
    ], "没有观察源不是失败，不发警告"
    finalize_ok(manager, execution)


def test_pi_facts_map_by_in_flight_call_position_and_agent(tmp_path: Path) -> None:
    """pi 族：index == 在飞 call 内 task 位置且 agent 同名才配对；对不上丢弃。"""
    manager, store = build_manager(tmp_path)
    source = FakeSource(
        [
            PiProgressFact("researcher", 0, 1, "read", "a.md", 10),
            PiProgressFact("researcher", 1, 9, "read", "x.md", 99),  # agent 错配
            PiProgressFact("writer", 7, 9, "read", "y.md", 99),  # index 越界
        ]
    )
    execution = open_execution(manager, store, tmp_path / "ws", "prog02", adapter=FakeAdapter(source))
    started(manager, execution, 0, "call_1", agent="researcher")
    started(manager, execution, 1, "call_1", agent="writer")

    assert wait_for(lambda: progress_events(store, "prog02"))
    time.sleep(0.2)  # 多轮 poll 之后映射不上的 fact 仍不得出现

    payloads = progress_events(store, "prog02")
    assert len(payloads) == 1
    assert payloads[0] == {
        "n": 0,
        "steps": 1,
        "lastToolName": "read",
        "lastToolTarget": "a.md",
        "bytes": 10,
    }
    finalize_ok(manager, execution)


def test_unchanged_facts_emit_once_and_throttle_bounds_the_rate(tmp_path: Path) -> None:
    """facts 无实际变化不重复发；有变化也受每 child ≤1 条/窗口节流。"""
    manager, store = build_manager(tmp_path)
    source = FakeSource([PiProgressFact("researcher", 0, 1, "read", "a.md", 10)])
    execution = open_execution(manager, store, tmp_path / "ws", "prog03", adapter=FakeAdapter(source))
    started(manager, execution, 0, "call_1")
    assert wait_for(lambda: len(progress_events(store, "prog03")) == 1)

    first_polls = source.polls
    time.sleep(0.2)  # 多轮 poll，facts 未变
    assert source.polls > first_polls
    assert len(progress_events(store, "prog03")) == 1, "无变化不得重复发"

    with execution.lock:
        # 重置该 child 的发射时刻为「现在」：窗口由此确定，与此前耗时无关。
        # facts 尚未变化，此刻不可能有新发射，顺序无竞态。
        progress_of(manager, execution).emit_ts[0] = time.monotonic()
    source.facts = [PiProgressFact("researcher", 0, 2, "read", "a.md", 20)]
    time.sleep(0.06)  # 节流窗口内：变化可见但暂不发
    assert len(progress_events(store, "prog03")) == 1

    assert wait_for(lambda: len(progress_events(store, "prog03")) == 2)
    assert progress_events(store, "prog03")[-1]["steps"] == 2
    finalize_ok(manager, execution)


def test_bytes_snapshot_never_moves_backwards(tmp_path: Path) -> None:
    """bytes 是单调快照：transcript 被替换重新解析后不回退已报值。"""
    manager, store = build_manager(tmp_path)
    source = FakeSource([PiProgressFact("researcher", 0, 1, "read", "a.md", 100)])
    execution = open_execution(manager, store, tmp_path / "ws", "prog04", adapter=FakeAdapter(source))
    started(manager, execution, 0, "call_1")
    assert wait_for(lambda: len(progress_events(store, "prog04")) == 1)

    source.facts = [PiProgressFact("researcher", 0, 2, "write", "b.md", 40)]

    assert wait_for(lambda: len(progress_events(store, "prog04")) == 2)
    assert progress_events(store, "prog04")[-1]["bytes"] == 100
    finalize_ok(manager, execution)


def test_a_finished_child_gets_no_more_progress(tmp_path: Path) -> None:
    """subagent_finished 提交后该 call 离场：后续 facts 一律不再投影。"""
    manager, store = build_manager(tmp_path)
    source = FakeSource([PiProgressFact("researcher", 0, 1, "read", "a.md", 10)])
    execution = open_execution(manager, store, tmp_path / "ws", "prog05", adapter=FakeAdapter(source))
    started(manager, execution, 0, "call_1")
    assert wait_for(lambda: len(progress_events(store, "prog05")) == 1)

    emit(
        manager,
        execution,
        events.SUBAGENT_FINISHED,
        {
            "toolCallId": "call_1",
            "ns": [0],
            "status": "finished",
            "isError": False,
            "resultPreview": "摘要",
        },
    )
    source.facts = [PiProgressFact("researcher", 0, 5, "write", "b.md", 50)]
    time.sleep(0.3)

    assert len(progress_events(store, "prog05")) == 1, "finished 后不再发"
    finalize_ok(manager, execution)


def test_finalize_stops_the_thread_and_closes_the_source(tmp_path: Path) -> None:
    """finalize 开始即停：线程有界退出、源被 close，不泄漏到下一个 run。"""
    manager, store = build_manager(tmp_path)
    source = FakeSource([PiProgressFact("researcher", 0, 1, "read", "a.md", 10)])
    execution = open_execution(manager, store, tmp_path / "ws", "prog06", adapter=FakeAdapter(source))
    started(manager, execution, 0, "call_1")
    assert wait_for(lambda: len(progress_events(store, "prog06")) == 1)
    thread = progress_of(manager, execution).thread
    assert thread is not None and thread.is_alive()

    finalize_ok(manager, execution)

    assert wait_for(lambda: not thread.is_alive(), timeout=5.0)
    assert source.closed is True
    before = len(progress_events(store, "prog06"))
    time.sleep(0.2)
    assert len(progress_events(store, "prog06")) == before, "终态后不再有任何帧"
    terminal = store.committed_terminal("prog06")
    assert terminal is not None
    assert terminal["type"] == events.RUN_FINISHED, "观察者不参与终态判定"


def test_repeated_poll_failures_back_off_then_recover(tmp_path: Path) -> None:
    """临时连续失败不导致永久失明：越过原停摆预算后仍存活，故障恢复后继续发进度。

    GLINK-OBS-01 第 4 项：真实 pi r2 里 5 次 E2B 请求超时后观察者永久停止，
    run 剩下 20 分钟无进度。原义务是「连续失败 5 次即停」，新义务是只降频重试。
    """
    manager, store = build_manager(tmp_path)
    source = FakeSource(error=OSError("simulated unreadable directory"))
    execution = open_execution(manager, store, tmp_path / "ws", "prog07", adapter=FakeAdapter(source))
    started(manager, execution, 0, "call_1")
    thread = progress_of(manager, execution).thread
    assert thread is not None

    # 越过当初触发永久停止的阈值（5 次）后线程必须仍在轮询。
    assert wait_for(lambda: source.polls >= 6, timeout=5.0)
    assert thread.is_alive(), "临时失败不得让观察者永久停摆"

    warnings = [
        event
        for event in read_events(store, "prog07")
        if event["type"] == events.KERNEL_STATUS
        and event["payload"].get("message") == "child_progress_failed"
    ]
    assert len(warnings) == 1, "观察者失败只发一次警告"

    # 故障恢复后（成功计数把失败清零）进度照常发出。
    source.error = None
    source.facts = [PiProgressFact("researcher", 0, 1, "read", "a.md", 10)]
    assert wait_for(lambda: len(progress_events(store, "prog07")) == 1), "故障恢复后必须继续发进度"

    finalize_ok(manager, execution)
    assert wait_for(lambda: not thread.is_alive(), timeout=5.0)
    terminal = store.committed_terminal("prog07")
    assert terminal["type"] == events.RUN_FINISHED, "观察者失败不改变 verdict"


def test_a_failing_source_factory_is_one_warning_not_a_crash(tmp_path: Path) -> None:
    """child_progress_source() 自身抛错：一次警告，无线程，run 照常收口。"""

    class ExplodingAdapter(FakeAdapter):
        def child_progress_source(self) -> FakeSource:
            raise RuntimeError("simulated factory failure")

    manager, store = build_manager(tmp_path)
    execution = open_execution(
        manager, store, tmp_path / "ws", "prog08", adapter=ExplodingAdapter(None)
    )
    started(manager, execution, 0, "call_1")

    assert progress_of(manager, execution).thread is None
    warnings = [
        event
        for event in read_events(store, "prog08")
        if event["type"] == events.KERNEL_STATUS
        and event["payload"].get("message") == "child_progress_failed"
    ]
    assert len(warnings) == 1
    finalize_ok(manager, execution)
    assert store.committed_terminal("prog08")["type"] == events.RUN_FINISHED


def test_acp_facts_map_by_call_id(tmp_path: Path) -> None:
    """ACP 族：fact 的 call_id 精确命中 started 事件的 toolCallId 才投影。"""
    manager, store = build_manager(
        tmp_path, PI_GLINK_HARNESS="acp", TEST_ACP_COMMAND="dsh"
    )
    child = "e3282967-2222-4222-8222-222222222222"
    source = FakeSource(
        [
            AcpProgressFact("call_1", child, 3, "read", "a.md", 64),
            AcpProgressFact("call_unknown", child, 9, "read", "z.md", 99),  # 对不上
        ]
    )
    execution = open_execution(manager, store, tmp_path / "ws", "prog09", adapter=FakeAdapter(source))
    started(manager, execution, 0, "call_1")

    assert wait_for(lambda: progress_events(store, "prog09"))
    time.sleep(0.2)

    payloads = progress_events(store, "prog09")
    assert len(payloads) == 1
    assert payloads[0]["n"] == 0 and payloads[0]["steps"] == 3
    finalize_ok(manager, execution)


def test_a_started_after_the_observer_stop_starts_no_thread(tmp_path: Path) -> None:
    """评审阻塞项回归：finalize 停止观察者后（closed 标志已置），停止与 verdict
    之间迟到的首个 subagent_started 不得再启动线程——它会无人停止地泄漏。"""
    manager, store = build_manager(tmp_path)
    source = FakeSource([PiProgressFact("researcher", 0, 1, "read", "a.md", 10)])
    execution = open_execution(manager, store, tmp_path / "ws", "prog07", adapter=FakeAdapter(source))

    manager._pipeline.observe.finish(execution)  # noqa: SLF001 - Observe 阶段的停止点
    started(manager, execution, 0, "call_1")
    time.sleep(0.3)

    assert progress_of(manager, execution).thread is None, "closed 之后不得再起线程"
    assert progress_events(store, "prog07") == []
    finalize_ok(manager, execution)


def test_steps_snapshot_never_moves_backwards(tmp_path: Path) -> None:
    """steps 与 bytes 同为单调快照：transcript 替换重解析后事件流不回退。"""
    manager, store = build_manager(tmp_path)
    source = FakeSource([PiProgressFact("researcher", 0, 5, "read", "a.md", 100)])
    execution = open_execution(manager, store, tmp_path / "ws", "prog08", adapter=FakeAdapter(source))
    started(manager, execution, 0, "call_1")
    assert wait_for(lambda: len(progress_events(store, "prog08")) == 1)

    source.facts = [PiProgressFact("researcher", 0, 2, "write", "b.md", 150)]

    assert wait_for(lambda: len(progress_events(store, "prog08")) == 2)
    assert progress_events(store, "prog08")[-1]["steps"] == 5
    finalize_ok(manager, execution)


# -- observer unit tests: closed flag, throttle and monotonic clamp ------------


def test_observer_closed_by_stop_never_starts_a_thread() -> None:
    """对象级回归：stop() 置 closed 后，迟到的 started 帧不再创建源或线程。

    在飞表仍按原语义记录（``_track_child_progress`` 原先也是先记表、后
    检查 closed）；关键义务是不再起无人停止的线程。
    """
    lock = threading.RLock()
    sources: list[FakeSource] = []

    def source_factory() -> FakeSource:
        source = FakeSource()
        sources.append(source)
        return source

    observer = ChildProgressObserver(
        run_id="unit01",
        lock=lock,
        is_open=lambda: True,
        match_n=lambda fact, calls: 0,
        source_factory=source_factory,
        emit_frame=lambda event_type, payload: True,
        emit_warning=lambda detail: None,
    )

    observer.stop()
    with lock:
        observer.track(
            events.SUBAGENT_STARTED,
            {"n": 0, "toolCallId": "call_1", "agent": "researcher"},
        )

    assert observer.closed is True
    assert observer.started is False, "closed 之后连观察源都不创建"
    assert observer.thread is None
    assert sources == []


def test_observer_throttles_changes_and_clamps_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """节流窗口与单调钳制：无变化不发、窗口内变化不发、回退按已报值钳制。"""
    lock = threading.RLock()
    frames: list[Mapping[str, Any]] = []

    def record(event_type: str, payload: Mapping[str, Any]) -> bool:
        frames.append(payload)
        return True

    observer = ChildProgressObserver(
        run_id="unit02",
        lock=lock,
        is_open=lambda: True,
        match_n=lambda fact, calls: 0,
        source_factory=lambda: None,  # 无轮询线程：直接驱动一次发布
        emit_frame=record,
        emit_warning=lambda detail: None,
    )
    with lock:
        observer.track(
            events.SUBAGENT_STARTED,
            {"n": 0, "toolCallId": "call_1", "agent": "researcher"},
        )

    monkeypatch.setattr(observer_module, "_PROGRESS_THROTTLE_SECONDS", 0.0)
    first = PiProgressFact("researcher", 0, 5, "read", "a.md", 100)
    observer._publish([first])  # noqa: SLF001 - 发布体是本次被测单元
    assert frames == [
        {
            "n": 0,
            "steps": 5,
            "lastToolName": "read",
            "lastToolTarget": "a.md",
            "bytes": 100,
        }
    ]

    observer._publish([first])  # noqa: SLF001
    assert len(frames) == 1, "无变化不发，窗口为 0 也不发"

    regressed = PiProgressFact("researcher", 0, 2, "write", "b.md", 40)
    observer._publish([regressed])  # noqa: SLF001
    assert len(frames) == 2
    assert frames[-1]["steps"] == 5 and frames[-1]["bytes"] == 100, "快照不回退"
    assert frames[-1]["lastToolName"] == "write", "变化的工具事实照常更新"

    monkeypatch.setattr(observer_module, "_PROGRESS_THROTTLE_SECONDS", 60.0)
    advanced = PiProgressFact("researcher", 0, 6, "read", "c.md", 200)
    observer._publish([advanced])  # noqa: SLF001
    assert len(frames) == 2, "节流窗口内即使有变化也暂不发"

    monkeypatch.setattr(observer_module, "_PROGRESS_THROTTLE_SECONDS", 0.0)
    observer._publish([advanced])  # noqa: SLF001
    assert len(frames) == 3
    assert frames[-1]["steps"] == 6 and frames[-1]["bytes"] == 200
