"""R2（multi-harness-plan §6）：执行核心可以脱离 HTTP 面组装、执行与重放。

``build_execution_service`` 是执行核心的生产组装路径：数据根所有权 → 身份
材料 → 存储 → 启动对账，不绑定端口、不启动资源扫描、不导入任何 web 模块
（导入方向由 ``tests/test_repository_boundaries.py`` 的 service 规则卡住）。
这里的四条回归对应补行为证明：无 HTTP 直驱执行与重放、组合关系与唯一拆除
路径、同数据根互斥、build 内对账收口遗留 running 行。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from glink_harness.app import create_server
from glink_harness.config import build_config
from glink_harness.kernel.execution import (
    ArtifactRecovery,
    CleanupReport,
    ExecutionAdapter,
    ExecutionOutcome,
    SessionInfo,
)
from glink_harness.ownership import DataRootBusy, DataRootOwnership
from glink_harness.service import ExecutionService, build_execution_service
from glink_harness.store import CANCELLED, FAILED, FINISHED, RunSpec, RunStore, StoreError

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC = RunSpec(family="pi", pack="pi", executor="inproc")


def make_config(tmp_path: Path, **overrides: str):
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "GLINK_HARNESS": "pi",
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_PORT": "0",
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


class _SettledAdapter(ExecutionAdapter):
    """A run that settles at once (no pi process, no HTTP face needed)."""

    kind = "inproc"

    def __init__(self) -> None:
        super().__init__(None, on_frame=lambda frame: None)  # type: ignore[arg-type]

    def start(self) -> None:
        return None

    def session_info(self) -> SessionInfo | None:
        return None

    def deliver_input(self) -> None:
        return None

    def wait(self, *, cancel, deadline: float) -> ExecutionOutcome:
        return ExecutionOutcome(settled=True, cancelled=False)

    def cancel(self) -> None:
        return None

    def recover_artifacts(self, destination: Path) -> ArtifactRecovery:
        return ArtifactRecovery()

    def close(self) -> CleanupReport:
        return CleanupReport(released=True)

    def identity(self) -> dict[str, str]:
        return {}


class _SlowAdapter(_SettledAdapter):
    """A run that takes a moment — close must wait for its real verdict.

    ``started`` fires when the kernel is already working (``wait`` entered),
    so the test can close *mid-flight* instead of racing the pre-start
    cancellation path (``stages.py``: a cancel seen before the kernel starts
    skips the work entirely). The kernel then ignores the stop request and
    settles normally — the verdict is decided by the run's own thread.
    """

    def __init__(self, delay: float) -> None:
        super().__init__()
        self._delay = delay
        self.started = threading.Event()

    def wait(self, *, cancel, deadline: float) -> ExecutionOutcome:
        self.started.set()
        time.sleep(self._delay)
        return ExecutionOutcome(settled=True, cancelled=False)


def wait_terminal(store: RunStore, run_id: str, *, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = store.get(run_id)
        if record is not None and record.is_terminal:
            return record
        time.sleep(0.05)
    return store.get(run_id)


def test_the_core_executes_and_replays_without_any_http_face(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """执行核心直驱：manager.start → 终态 → 持久事件重放，全程无 HTTP。"""
    adapter = _SettledAdapter()
    monkeypatch.setattr(
        "glink_harness.kernel.harness_pi.PiExecutionAdapter", lambda spec, **kwargs: adapter
    )
    service = build_execution_service(make_config(tmp_path))
    try:
        record = service.manager.start("无 HTTP 面的一次执行")
        settled = wait_terminal(service.store, record.run_id)
        assert settled is not None and settled.status == FINISHED
        # 重放读的是持久事件日志，不依赖总线内存窗口或任何 HTTP 读面。
        events = list(service.store.iter_events(record.run_id))
        assert events[0]["type"] == "run_started"
        assert events[-1]["type"] == "run_finished"
        assert service.store.committed_terminal(record.run_id)["payload"]["status"] == FINISHED
    finally:
        service.close()


def test_create_server_composes_the_face_over_the_service_and_close_releases_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP 面只是加在执行核心上的一层：扁平属性再暴露同一批对象，关闭走同一条拆除路径。"""
    monkeypatch.setattr(
        "glink_harness.kernel.harness_pi.PiExecutionAdapter", lambda spec, **kwargs: _SettledAdapter()
    )
    server = create_server(make_config(tmp_path))
    try:
        assert isinstance(server.service, ExecutionService)
        assert server.config is server.service.config
        assert server.bus is server.service.bus
        assert server.store is server.service.store
        assert server.manager is server.service.manager
        assert server.ownership is server.service.ownership
        assert server.server_address[1] != 0, "端口已绑定（绑端口属于 HTTP 面，最后发生）"
    finally:
        server.server_close()
    # server_close 只有一条拆除路径：执行核心的 data-root 锁随之释放。
    second = DataRootOwnership.acquire(server.service.config.data_root)
    second.release()


def test_a_second_builder_on_the_same_data_root_is_refused(tmp_path: Path) -> None:
    """互斥属于执行核心，不属于 HTTP 面：第二个核心组装在打开任何记录前被拒。"""
    config = make_config(tmp_path)
    service = build_execution_service(config)
    try:
        with pytest.raises(DataRootBusy):
            build_execution_service(config)
    finally:
        service.close()
    # 关闭即释放：同数据根可以再次组装（也是唯一拆除路径的另一面）。
    rebuilt = build_execution_service(config)
    rebuilt.close()


def test_build_reconciles_leftover_running_rows_before_returning(tmp_path: Path) -> None:
    """上一进程遗留的 running 行在 build 返回前按 interrupted 收口（服务就绪义务）。"""
    config = make_config(tmp_path)
    leftover = RunStore(config.data_root / "runs")
    leftover.create("宿主退出前的遗留 run", run_id="leftover", spec=SPEC)
    leftover.close()

    service = build_execution_service(config)
    try:
        record = service.store.get("leftover")
        assert record is not None and record.status == FAILED
        assert record.stop_reason == "interrupted"
        assert record.holds_resources, "资源状态未知（released: null）的持有不因收口消失"
        assert [event["type"] for event in service.store.iter_events("leftover")] == ["run_failed"]
    finally:
        service.close()


def test_close_waits_for_inflight_runs_before_releasing_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """在途 run 时关闭：终态提交之前不释放所有权，真实裁决不被下一个宿主的对账改写。

    close 的第一步是取消并等待在途 run 到终态提交，最后才释放数据根锁；
    若锁先走，第二个组装的启动对账会把仍在执行的 run 改写为
    interrupted，原 run 线程的真实裁决随即撞终态提交冲突。这里在途 run
    的裁决是 CANCELLED/abort——shutdown 的取消请求被接受（stages 的
    「被接受的取消赢」），由本进程的 run 线程提交，而不是被任何对账改写。
    """
    adapter = _SlowAdapter(delay=1.5)
    monkeypatch.setattr(
        "glink_harness.kernel.harness_pi.PiExecutionAdapter", lambda spec, **kwargs: adapter
    )
    config = make_config(tmp_path)
    service = build_execution_service(config)
    record = service.manager.start("close 期间仍在执行的一次 run")
    assert adapter.started.wait(timeout=5.0), "run 线程没有进入执行段"

    started = time.monotonic()
    service.close()
    assert time.monotonic() - started >= 1.0, "close 在在途 run 终态提交前就返回了"

    # close 已关掉自己的存储连接：用独立 reader 读回真实裁决。
    reader = RunStore(config.data_root / "runs")
    try:
        settled = reader.get(record.run_id)
    finally:
        reader.close()
    assert settled is not None and settled.status == CANCELLED
    assert settled.stop_reason == "abort"

    # 所有权已随 close 释放：同数据根可立即重建，且其启动对账不再改写该 run。
    second = build_execution_service(config)
    try:
        again = second.store.get(record.run_id)
        assert again is not None and again.status == CANCELLED
        assert again.stop_reason == "abort"
    finally:
        second.close()


def test_close_shuts_the_run_store_connection_and_recovers_the_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """close 按「组装的逆序」完整拆除：runs.db 连接关闭、WAL/SHM 侧车回收。"""
    monkeypatch.setattr(
        "glink_harness.kernel.harness_pi.PiExecutionAdapter", lambda spec, **kwargs: _SettledAdapter()
    )
    config = make_config(tmp_path)
    service = build_execution_service(config)
    record = service.manager.start("close 后存储连接必须关闭")
    assert wait_terminal(service.store, record.run_id) is not None
    service.close()

    # RunStore 把 sqlite 错误统一包成 StoreError：底层 "closed database"
    # 文案证明连接真的关了，而不是空表或读路径短路。
    with pytest.raises(StoreError, match="closed database"):
        service.store.get(record.run_id)
    runs_dir = config.data_root / "runs"
    assert not (runs_dir / "runs.db-wal").exists()
    assert not (runs_dir / "runs.db-shm").exists()


def test_a_watch_thread_that_fails_to_start_propagates_and_releases_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """thread.start 失败：原始异常透传（不被未启动线程的 join 次生异常掩盖）、锁释放可重建。

    watch 线程在 start() 前就已登记；若失败不回滚，create_server 的失败回滚
    走 close → stop_resource_watch 会对未启动线程 join 抛 RuntimeError，
    顶替「线程起不来」的原始错误并跳过所有权释放。
    """
    config = make_config(tmp_path, E2B_API_KEY="e2b-test-key")

    def _broken_start(self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    with monkeypatch.context() as patcher:
        patcher.setattr(threading.Thread, "start", _broken_start)
        with pytest.raises(RuntimeError, match="can't start new thread"):
            create_server(config)

    rebuilt = create_server(config)
    try:
        watch = rebuilt.manager._ledger._resource_watch
        assert watch is not None and watch.ident is not None, "重建后 watch 正常启动"
    finally:
        rebuilt.server_close()
