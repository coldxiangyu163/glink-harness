"""阶段 1 收尾（ADR 0019）：上限语义的三分支、自适应 resume 超时与观测。

上限从「会话寿命配额」改为「异常检测」后，继承/回收/回灌各有三档：

* **正常**：低于警告线（:data:`~glink_harness.kernel.acp_env.INHERIT_WARN_*`），
  无声通过——正常研究会话（dsh 实测 ~420KB/轮，50 轮 ≈ 21MB、数百文件）
  永远住在这里；
* **警告**：越过警告线但低于 fail-closed 线——拷贝/发布照常完成，事实以
  ``kernel_status``（`acp_state_threshold_warning`）进事件流，前端可呈现，
  追问照常受理；
* **fail-closed**：越过 :data:`~glink_harness.kernel.acp_env.INHERIT_MAX_*`
  （对齐归档通道上限）——拒绝，不呈现残缺状态。

resume RPC 的超时按状态根体量自适应（保底 60s、4MiB/s 线性、硬上限
600s），取代写死的 60s。观测义务：继承（`stateInherit`）、回灌
（`stateBackfill`）、回收（`stateRecovery`）与 resume 解析耗时
（`resumeDurationMs`）进 `session_ready`。S1a 的通道/回灌回归见
``tests/test_archive_channel.py`` 与 ``tests/test_kernel_acp_archive.py``。
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Any, Mapping

import pytest

from glink_harness.config import HostConfig, build_config

from acp_pack import apply_acp_pack
from glink_harness.events import EventBus
from glink_harness.kernel.acp_e2b_env import (
    ARCHIVE_COMMAND_TIMEOUT_SECONDS,
    AcpStateRecovery,
)
from glink_harness.kernel.acp_env import (
    INHERIT_MAX_FILE_BYTES,
    INHERIT_MAX_FILES,
    INHERIT_MAX_TOTAL_BYTES,
    INHERIT_WARN_FILE_BYTES,
    INHERIT_WARN_FILES,
    INHERIT_WARN_TOTAL_BYTES,
    AcpRuntime,
    AcpStateInheritError,
    inherit_acp_state,
    prepare_acp_runtime,
    state_threshold_warning,
)
from glink_harness.kernel.acp_execution import (
    RESUME_TIMEOUT_BASE_SECONDS,
    RESUME_TIMEOUT_BYTES_PER_SECOND,
    RESUME_TIMEOUT_MAX_SECONDS,
    AcpExecutionAdapter,
    _tree_bytes,
    resume_timeout_seconds,
)
from glink_harness.kernel.acp_projection import AcpProjector
from glink_harness.kernel.acp_rpc import REQUEST_TIMEOUT_SECONDS
from glink_harness.kernel.execution import ExecutionSpec
from glink_harness.kernel.sandbox import Launch, open_sandbox
from glink_harness.redaction import SecretRedactor
from glink_harness.runs.runner import RunManager
from glink_harness.store import FINISHED, RunStore
from store_helpers import events_text

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_ACP = Path(__file__).resolve().parent / "fixtures" / "fake_acp.py"
SESSION_ID = "fake-acp-session"


# -- helpers --------------------------------------------------------------------


def fake_agent_command(tmp_path: Path, settings: Mapping[str, Any]) -> str:
    path = tmp_path / "agent-settings.json"
    path.write_text(json.dumps(dict(settings), ensure_ascii=False), encoding="utf-8")
    return (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_ACP))}"
        f" --config {shlex.quote(str(path))}"
    )


def make_config(tmp_path: Path, **overrides: str) -> HostConfig:
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "GLINK_HARNESS": "acp",
        "TEST_ACP_COMMAND": fake_agent_command(
            tmp_path, {"FAKE_ACP_LOG": str(tmp_path / "agent.jsonl")}
        ),
    }
    values.update(overrides)
    apply_acp_pack(values, tmp_path)
    return build_config(values, REPO_ROOT)


def make_runtime(tmp_path: Path, *, inherit_facts: Mapping[str, Any] | None = None) -> AcpRuntime:
    home = tmp_path / "acp-agent" / "home"
    home.mkdir(parents=True, exist_ok=True)
    return AcpRuntime(
        directory=tmp_path / "acp-agent",
        home=home,
        command="agent --acp",
        inherit_facts=dict(inherit_facts) if inherit_facts else None,
    )


# -- the three tiers ------------------------------------------------------------


def test_warning_bounds_sit_between_a_real_session_and_the_fail_closed_line() -> None:
    """数值纪律：警告线在正常会话之上、fail-closed 线之下；线与通道对齐。

    依据（ADR 0019）：dsh 实测 ~420KB/轮线性增长，50 轮 ≈ 21MB、数百文件；
    警告线取其数倍（64MB/32MB/2048），fail-closed 线对齐归档通道传输上限
    （256MB）与继承上限（128MB 单文件/16384 文件）。任何一个不等式失效都
    意味着两档语义塌缩成同一档，必须重新给出依据。
    """
    assert INHERIT_WARN_FILES < INHERIT_MAX_FILES
    assert INHERIT_WARN_FILE_BYTES < INHERIT_MAX_FILE_BYTES
    assert INHERIT_WARN_TOTAL_BYTES < INHERIT_MAX_TOTAL_BYTES
    # A healthy 50-turn session (≈21MB, a few hundred files) stays silent.
    assert 21 * 1024 * 1024 < INHERIT_WARN_TOTAL_BYTES
    assert 500 < INHERIT_WARN_FILES


def test_state_threshold_warning_reports_each_dimension() -> None:
    assert state_threshold_warning(files=10, total_bytes=1024, largest_file=512) is None
    warning = state_threshold_warning(
        files=INHERIT_WARN_FILES + 1, total_bytes=1, largest_file=1
    )
    assert warning is not None and "文件数" in warning
    warning = state_threshold_warning(
        files=1, total_bytes=1, largest_file=INHERIT_WARN_FILE_BYTES + 1
    )
    assert warning is not None and "单文件" in warning
    warning = state_threshold_warning(
        files=1, total_bytes=INHERIT_WARN_TOTAL_BYTES + 1, largest_file=1
    )
    assert warning is not None and "总量" in warning
    # The per-file dimension is optional (the recovery verdict has no largest file).
    assert (
        state_threshold_warning(files=1, total_bytes=INHERIT_WARN_TOTAL_BYTES + 1)
        is not None
    )
    assert state_threshold_warning(files=1, total_bytes=1, largest_file=None) is None


def test_inheritance_healthy_session_is_silent(tmp_path: Path) -> None:
    parent = tmp_path / "parent-home"
    (parent / ".dsh").mkdir(parents=True)
    (parent / ".dsh" / "state.zst").write_bytes(b"frame" * 100)
    child = tmp_path / "child-home"
    child.mkdir()
    outcome = inherit_acp_state(parent, child)
    assert outcome.warning is None
    assert outcome.files == 1
    assert outcome.bytes == 500
    assert outcome.largest_file == 500
    assert (child / ".dsh" / "state.zst").read_bytes() == b"frame" * 100


def test_inheritance_over_the_warning_line_copies_everything_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """警告档：超过警告线但低于 fail-closed 线——全部拷贝 + warning 事实。

    缩小警告线（总量 16 字节）而保留 fail-closed 线在真值：状态越线但远
    不到拒绝量级，拷贝必须完整完成（不截断、不拒收），warning 描述进
    outcome。这就是「追问仍受理」的分支。
    """
    monkeypatch.setattr("glink_harness.kernel.acp_env.INHERIT_WARN_TOTAL_BYTES", 16)
    parent = tmp_path / "parent-home"
    (parent / ".dsh").mkdir(parents=True)
    (parent / ".dsh" / "state.zst").write_bytes(b"x" * 4096)
    child = tmp_path / "child-home"
    child.mkdir()
    outcome = inherit_acp_state(parent, child)
    assert outcome.warning is not None and "总量" in outcome.warning
    assert outcome.files == 1 and outcome.bytes == 4096
    assert (child / ".dsh" / "state.zst").read_bytes() == b"x" * 4096


def test_inheritance_over_the_fail_closed_line_still_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fail-closed 档：越过 INHERIT_MAX_* 仍然整次拒绝（纪律不放松）。"""
    monkeypatch.setattr("glink_harness.kernel.acp_env.INHERIT_WARN_TOTAL_BYTES", 8)
    monkeypatch.setattr("glink_harness.kernel.acp_env.INHERIT_MAX_TOTAL_BYTES", 16)
    parent = tmp_path / "parent-home"
    parent.mkdir()
    (parent / "state.bin").write_bytes(b"x" * 4096)
    child = tmp_path / "child-home"
    child.mkdir()
    with pytest.raises(AcpStateInheritError):
        inherit_acp_state(parent, child)


def test_prepare_runtime_carries_inherit_facts_for_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """prepare_acp_runtime 把继承事实（体量/耗时/警告）挂到 runtime 上。"""
    config = make_config(tmp_path)
    monkeypatch.setattr("glink_harness.kernel.acp_env.INHERIT_WARN_TOTAL_BYTES", 16)
    parent = tmp_path / "parent-home"
    parent.mkdir()
    (parent / "state.bin").write_bytes(b"y" * 300)
    first = prepare_acp_runtime(config, runtime_dir=tmp_path / "r1")
    assert first.inherit_facts is None, "首轮没有继承，也就没有事实"
    second = prepare_acp_runtime(
        config, runtime_dir=tmp_path / "r2", inherit_from=parent
    )
    assert second.inherit_facts is not None
    assert second.inherit_facts["files"] == 1
    assert second.inherit_facts["bytes"] == 300
    assert second.inherit_facts["durationMs"] >= 0.0
    assert second.inherit_facts["warning"] is not None
    # A healthy parent stays silent in the same facts.
    healthy = tmp_path / "healthy-home"
    healthy.mkdir()
    (healthy / "s.bin").write_bytes(b"z")
    third = prepare_acp_runtime(
        config, runtime_dir=tmp_path / "r3", inherit_from=healthy
    )
    assert third.inherit_facts is not None and third.inherit_facts["warning"] is None


# -- the adaptive resume deadline ------------------------------------------------


def test_resume_timeout_constants_and_shape() -> None:
    """保底=协议预算，硬上限对齐归档命令预算，线性项按 4MiB/s。"""
    assert RESUME_TIMEOUT_BASE_SECONDS == REQUEST_TIMEOUT_SECONDS == 60.0
    assert RESUME_TIMEOUT_MAX_SECONDS == ARCHIVE_COMMAND_TIMEOUT_SECONDS
    assert RESUME_TIMEOUT_BYTES_PER_SECOND == 4 * 1024 * 1024
    assert resume_timeout_seconds(0) == 60.0
    assert resume_timeout_seconds(-5) == 60.0
    # A 30-turn session (≈13MB) gets ~3s of headroom, not a refusal-shaped wait.
    assert resume_timeout_seconds(13 * 1024 * 1024) == pytest.approx(63.25)
    # The hard ceiling holds far beyond the largest legal root (2GiB+ here). A
    # root at the fail-closed bound (256MB) only ever asks for ~124s.
    assert resume_timeout_seconds(3 * 1024**3) == RESUME_TIMEOUT_MAX_SECONDS
    assert resume_timeout_seconds(256 * 1024 * 1024) == pytest.approx(124.0)


def test_tree_bytes_counts_regular_files_without_following_links(
    tmp_path: Path,
) -> None:
    root = tmp_path / "home"
    (root / "a").mkdir(parents=True)
    (root / "a" / "one.bin").write_bytes(b"x" * 100)
    (root / "two.bin").write_bytes(b"y" * 28)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"z" * 1000)
    (root / "link.bin").symlink_to(outside)
    assert _tree_bytes(root) == 128
    # The bound stops the walk instead of hanging on a malformed tree: one
    # file's size comes back, never the whole tree (the walk order is
    # filesystem-defined, so either single file is the honest answer).
    assert _tree_bytes(root, max_files=1) in (28, 100)


class TimeoutRecordingClient:
    """A scripted client that records the deadline each request carried."""

    def __init__(self, argv, **kwargs: Any) -> None:
        self.argv = list(argv)
        self.timeouts: dict[str, float] = {}
        self.on_notification = kwargs.get("on_notification")
        self.on_request = kwargs.get("on_request")

    def start(self) -> None:
        pass

    def request(self, method: str, params=None, timeout=None) -> dict:
        self.timeouts[method] = float(timeout) if timeout is not None else -1.0
        if method == "initialize":
            return {
                "protocolVersion": 1,
                "agentInfo": {"name": "scripted", "version": "0"},
                "agentCapabilities": {"sessionCapabilities": {"resume": {}}},
            }
        if method == "session/new":
            return {"sessionId": "s-new"}
        return {"sessionId": "s-resumed"}

    def notify(self, method: str, params=None) -> None:
        pass

    def close(self, *, timeout: float = 5.0) -> int | None:
        return 0

    def stderr_tail(self, limit: int = 20) -> list[str]:
        return []


def run_resume_adapter(
    tmp_path: Path, *, state_bytes: int
) -> tuple[TimeoutRecordingClient, Any]:
    config = make_config(tmp_path)
    runtime = make_runtime(tmp_path)
    if state_bytes:
        # One synthetic file of the target size: the deadline must grow with it
        # the same way it grows with a real dsh session store.
        (runtime.home / "sessions.zst").write_bytes(b"\0" * state_bytes)
    spec = ExecutionSpec(
        config=config,
        runtime=runtime,
        run_id="threshold-run",
        prompt="追问",
        system_prompt="",
        workspace=tmp_path / "ws",
        timeout_seconds=5,
        resume_session=SESSION_ID,
        expected_session_id=SESSION_ID,
    )
    created: list[TimeoutRecordingClient] = []

    def factory(argv, **kwargs):
        client = TimeoutRecordingClient(argv, **kwargs)
        created.append(client)
        return client

    adapter = AcpExecutionAdapter(
        spec,
        sandbox=open_sandbox(
            "inproc", config=config, run_id=spec.run_id, workspace=spec.workspace
        ),
        launch=Launch(argv=("agent",)),
        client_factory=factory,
        on_frame=lambda _frame: None,
    )
    adapter.start()
    info = adapter.session_info()
    assert info is not None
    # The observation duty: the reparse's own wall clock rides session_ready.
    assert isinstance(info.payload.get("resumeDurationMs"), float)
    adapter.close()
    assert len(created) == 1
    return created[0], info


def test_resume_deadline_grows_with_the_state_root(tmp_path: Path) -> None:
    client, _info = run_resume_adapter(tmp_path, state_bytes=0)
    assert client.timeouts["session/resume"] == pytest.approx(60.0)
    (tmp_path / "home-copy").mkdir()
    big_client, _big_info = run_resume_adapter(tmp_path, state_bytes=9 * 1024 * 1024)
    grown = big_client.timeouts["session/resume"]
    assert grown == pytest.approx(60.0 + 9 * 1024 * 1024 / RESUME_TIMEOUT_BYTES_PER_SECOND)
    assert grown > 60.5, "大状态根的 resume 预算必须明显高于保底"


# -- the warning frames and their projection -------------------------------------


def test_projection_translates_the_threshold_warning_frame() -> None:
    projector = AcpProjector(SecretRedactor())
    produced = projector.project(
        {
            "type": "acp_state_threshold_warning",
            "source": "inherit",
            "detail": "会话状态超过异常检测警告线（总量 70000000 > 67108864 字节）",
        }
    )
    assert [(kind, payload) for kind, payload in produced][0][0] == "kernel_status"
    payload = produced[0][1]
    assert payload["level"] == "warning"
    assert payload["message"] == "acp_state_threshold_warning"
    assert payload["detail"].startswith("[inherit] 会话状态超过异常检测警告线")


def test_adapter_emits_the_inherit_threshold_warning_on_start(
    tmp_path: Path,
) -> None:
    """宿主侧继承越线：adapter 在 agent 启动时发警告帧，run 不被拒绝。"""
    config = make_config(tmp_path)
    runtime = make_runtime(
        tmp_path,
        inherit_facts={
            "files": 3000,
            "bytes": 80 * 1024 * 1024,
            "durationMs": 12.5,
            "warning": "会话状态超过异常检测警告线（文件数 3000 > 2048）",
        },
    )
    spec = ExecutionSpec(
        config=config,
        runtime=runtime,
        run_id="warn-run",
        prompt="追问",
        system_prompt="",
        workspace=tmp_path / "ws",
        timeout_seconds=5,
    )
    frames: list[Mapping[str, Any]] = []
    created: list[TimeoutRecordingClient] = []

    def factory(argv, **kwargs):
        client = TimeoutRecordingClient(argv, **kwargs)
        created.append(client)
        return client

    adapter = AcpExecutionAdapter(
        spec,
        sandbox=open_sandbox(
            "inproc", config=config, run_id=spec.run_id, workspace=spec.workspace
        ),
        launch=Launch(argv=("agent",)),
        client_factory=factory,
        on_frame=frames.append,
    )
    adapter.start()
    warnings = [
        frame
        for frame in frames
        if frame.get("type") == "acp_state_threshold_warning"
    ]
    assert len(warnings) == 1
    assert warnings[0]["source"] == "inherit"
    info = adapter.session_info()
    assert info is not None
    assert info.payload["stateInherit"]["files"] == 3000
    adapter.close()


def test_recovery_over_the_warning_line_emits_a_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回收越线（但仍成功发布）：recover_artifacts 发 recovery 来源警告。"""
    from test_kernel_acp_execution import (
        ScriptedRemoteSandbox,
        make_config as make_execution_config,
        make_spec,
    )

    config = make_execution_config(tmp_path)
    spec = make_spec(config, tmp_path)
    frames: list[Mapping[str, Any]] = []

    class BigRecoveryTransfer:
        def __init__(self) -> None:
            self.state = AcpStateRecovery(
                ok=True,
                files=tuple(f"f{i}" for i in range(10)),
                bytes=INHERIT_WARN_TOTAL_BYTES + 1,
                mode="archive-incremental",
                duration_ms=4.0,
            )

        def upload_home(self) -> None:
            pass

        def upload_continuation(self) -> None:
            pass

        def recover_state(self) -> AcpStateRecovery:
            return self.state

    transfer = BigRecoveryTransfer()
    monkeypatch.setattr(
        "glink_harness.kernel.acp_execution.AcpSandboxTransfer",
        lambda *_args, **_kwargs: transfer,
    )
    sandbox = ScriptedRemoteSandbox(
        [], config=config, run_id=spec.run_id, workspace=spec.workspace
    )
    adapter = AcpExecutionAdapter(
        spec,
        sandbox=sandbox,
        launch=Launch(argv=("agent",)),
        client_factory=TimeoutRecordingClient,
        on_frame=frames.append,
    )
    # start() opens the sandbox, builds the (patched) transfer and starts the
    # agent — recover_artifacts only runs with both in place.
    adapter.start()
    adapter.recover_artifacts(spec.workspace)
    adapter.close()
    warnings = [
        frame for frame in frames if frame.get("type") == "acp_state_threshold_warning"
    ]
    assert len(warnings) == 1
    assert warnings[0]["source"] == "recovery"
    assert "总量" in warnings[0]["detail"]


# -- the observation contract end to end ------------------------------------------


def test_followup_session_ready_carries_state_observability(tmp_path: Path) -> None:
    """inproc 追问的 session_ready 携带 stateInherit 与 resumeDurationMs。

    走真实 runner 生命周期（fake agent 子进程），观测字段就是阶段 1 的
    事件流义务：继承体量/耗时与 resume 解析耗时可从事件流读出。
    """
    config = make_config(
        tmp_path,
        TEST_ACP_COMMAND=fake_agent_command(
            tmp_path, {"FAKE_ACP_LOG": str(tmp_path / "agent.jsonl")}
        ),
    )
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    parent = manager.start("第一轮：请记住代号 REMEMBER:OBS-4471")
    final = manager.wait(parent.run_id, timeout=30)
    assert final is not None and final.status == FINISHED
    first_ready = [
        json.loads(line)
        for line in events_text(store, parent.run_id).splitlines()
        if line.strip() and json.loads(line)["type"] == "session_ready"
    ][0]
    assert "stateInherit" not in first_ready["payload"]
    assert "resumeDurationMs" not in first_ready["payload"]

    child = manager.start("RECALL 复述代号", parent_run_id=parent.run_id)
    child_final = manager.wait(child.run_id, timeout=30)
    assert child_final is not None and child_final.status == FINISHED
    ready = [
        json.loads(line)
        for line in events_text(store, child.run_id).splitlines()
        if line.strip() and json.loads(line)["type"] == "session_ready"
    ][0]
    payload = ready["payload"]
    inherit = payload["stateInherit"]
    assert inherit["files"] >= 1
    assert inherit["bytes"] > 0
    assert inherit["durationMs"] >= 0.0
    assert inherit["warning"] is None, "fake agent 的状态很小，必须落在正常档"
    assert isinstance(payload["resumeDurationMs"], float)
    # No warning was invented along the way.
    warnings = [
        json.loads(line)
        for line in events_text(store, child.run_id).splitlines()
        if line.strip()
        and json.loads(line)["type"] == "kernel_status"
        and json.loads(line)["payload"].get("message") == "acp_state_threshold_warning"
    ]
    assert warnings == []
