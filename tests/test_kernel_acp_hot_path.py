"""阶段 2（docs/adr/0020）热路径回归：追问重连泊车沙箱，失败降级冷路径。

覆盖四层，全部合成（fake 沙箱 + fake ACP agent 子进程，无真实云端/模型）：

* **候选判定**（``harness_acp.sandbox_resume_target``）：租约新鲜（父 run
  自己泊的车）、pack/template 匹配、TTL 内 → 候选；过期、陈旧（别的 run）、
  包/模板不符、TTL=0 → 无候选（冷路径）；
* **adapter 面**（``AcpExecutionAdapter._open_sandbox``）：候选存在时先
  connect（``on_resume=\"reboot\"``）+ 校验沙箱代数标记；connect 失败或标记
  不一致 → 销毁租约沙箱（kill by id）、删租约、走 create 冷路径、发降级
  kernel_status；命中 → 跳过回灌、发热路径 kernel_status；
* **manager 端到端**（归档通道部署）：首轮 run 完成 → 回收发布 + 沙箱泊车
  （pause、租约、代数标记、终帧 ``cleanup.lease.parked``、不再 kill、不占
  run 槽位）；追问 → 同一沙箱被重连、状态连续（代号在会话里）、generation
  递增；租约沙箱消失 → 追问降级冷路径仍完成；
* **投影**：``acp_sandbox_resume`` 帧的热/冷两条 kernel_status。

沙箱是 ``test_kernel_acp_e2b.FakeSandbox`` 的泊车子类（真目录树 + 真子进程
agent + pause/connect/registry/remove 面）；回灌命令被拦截为「宿主对话状态
拷入沙箱树」（回灌链路本身已有 tests/test_kernel_acp_archive 的真跑验证）。
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from glink_harness.config import build_config
from glink_harness.events import EventBus
from glink_harness.kernel.acp_e2b_env import BACKFILL_TAR_PATH
from glink_harness.kernel.acp_projection import AcpProjector
from glink_harness.kernel.acp_sandbox_park import (
    read_generation_marker,
    write_generation_marker,
)
from glink_harness.kernel.conversation_lease import (
    ConversationLease,
    read_lease,
    write_lease,
)
from glink_harness.kernel.execution import ExecutionSpec
from glink_harness.kernel.harness_acp import AcpHarnessFamily
from glink_harness.kernel.run_views import RunStoreSessionSource
from glink_harness.kernel.sandbox import SandboxResume, open_sandbox
from glink_harness.redaction import SecretRedactor
from glink_harness.runs.runner import RunManager
from glink_harness.store import FINISHED, RunRecord, RunSpec, RunStore

from acp_pack import apply_acp_pack
from harness_helpers import family
from store_helpers import events_text
from test_kernel_acp_e2b import (
    FakeFiles,
    FakeSandbox,
    fake_agent_command,
    make_runtime,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_registry():
    """Keep the class-level fake registry test-local (ids never leak across)."""
    ParkableSandbox.REGISTRY.clear()
    yield
    ParkableSandbox.REGISTRY.clear()


# -- the parkable ACP sandbox ---------------------------------------------------


class ParkableFiles(FakeFiles):
    """The ACP fake's files face plus ``remove`` and the /tmp scratch paths.

    The host's reclaim/backfill scripts land in ``/tmp`` inside the sandbox;
    this fake maps those onto the host's own ``/tmp`` (the commands that use
    them are intercepted anyway, but the delivery writes must not fail).
    """

    def _real(self, sandbox_path: str) -> Path:
        if sandbox_path.startswith("/tmp/"):
            return Path(sandbox_path)
        return super()._real(sandbox_path)

    def remove(self, path: str, **_kwargs: Any) -> None:
        real = self._real(path)
        if not real.exists():
            from e2b import FileNotFoundException

            raise FileNotFoundException(path)
        if real.is_dir() and not real.is_symlink():
            real.rmdir()
        else:
            real.unlink()
        self._sandbox.operations.append(f"remove {path}")


class ParkableCommands:
    """The ACP fake's commands plus a real backfill (the GET direction).

    The reclaim direction (POST) stays a fake success, which makes the host
    degrade to the files walk — a real chain in this fake too (the tree is
    real). The backfill's ``curl | tar`` is replaced by its effect: copy the
    conversation's published state into the sandbox tree (excluding the
    manifest and credential destinations, exactly what the real server skips).
    """

    def __init__(self, sandbox: "ParkableSandbox", conversation_dir: Path | None) -> None:
        self._sandbox = sandbox
        self._conversation_dir = conversation_dir
        self.backfills = 0

    def run(self, cmd: str, **kwargs: Any) -> Any:
        if BACKFILL_TAR_PATH in cmd:
            self.backfills += 1
            self._sandbox.operations.append(f"backfill {cmd.split()[0]}")
            state = self._conversation_dir / "state" if self._conversation_dir else None
            if state is not None and state.is_dir():
                dest = self._sandbox.root / "home/user/acp/home"
                dest.mkdir(parents=True, exist_ok=True)
                skip = {"manifest.json", *self._sandbox.credential_dests}
                for source in sorted(state.rglob("*")):
                    if not source.is_file():
                        continue
                    if source.relative_to(state).as_posix() in skip:
                        continue
                    target = dest / source.relative_to(state)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
            from test_kernel_acp_e2b import _Result

            return _Result(0)
        return self._sandbox.acp_commands.run(cmd, **kwargs)


class ParkableSandbox(FakeSandbox):
    """The ACP fake plus the SDK's pause/connect/registry surface."""

    REGISTRY: dict[str, "ParkableSandbox"] = {}

    def __init__(self, tmp_path: Path, *, sandbox_id: str, report: str | None = None,
                 conversation_dir: Path | None = None,
                 credential_dests: tuple[str, ...] = ()) -> None:
        super().__init__(tmp_path / sandbox_id, sandbox_id=sandbox_id, report=report)
        self.state = "running"
        self.pause_calls: list[dict[str, Any]] = []
        self.connect_calls: list[dict[str, Any]] = []
        self.credential_dests = credential_dests
        # Replace the two faces with the parkable ones (files gains remove;
        # commands gains the intercepted backfill).
        self._acp_files = self.files
        self._acp_commands = self.commands
        self.files = ParkableFiles(self)
        self.commands = ParkableCommands(self, conversation_dir)
        ParkableSandbox.REGISTRY[sandbox_id] = self

    @property
    def acp_commands(self):
        return self._acp_commands

    def pause(self, keep_memory: bool | None = None, **_opts: Any) -> bool:
        if self.killed:
            from e2b import SandboxNotFoundException

            raise SandboxNotFoundException(self.sandbox_id)
        self.pause_calls.append({"keep_memory": keep_memory})
        if self.state == "paused":
            return False
        self.state = "paused"
        return True

    @classmethod
    def connect(cls, sandbox_id: str, timeout: int | None = None, *,
                on_resume: str = "restore", **_opts: Any) -> "ParkableSandbox":
        sandbox = cls.REGISTRY.get(sandbox_id)
        if sandbox is None or sandbox.killed:
            from e2b import SandboxNotFoundException

            raise SandboxNotFoundException(sandbox_id)
        sandbox.connect_calls.append(
            {"timeout": timeout, "on_resume": on_resume}
        )
        if sandbox.state == "paused":
            sandbox.state = "running"
        return sandbox

    @classmethod
    def kill_by_id(cls, sandbox_id: str, **_opts: Any) -> bool:
        sandbox = cls.REGISTRY.get(sandbox_id)
        if sandbox is None or sandbox.killed:
            return False
        sandbox.kill()
        return True

    def kill(self) -> bool:
        super().kill()
        self.state = "killed"
        ParkableSandbox.REGISTRY.pop(self.sandbox_id, None)
        return True


# -- fixtures and helpers ---------------------------------------------------------


def make_config(tmp_path: Path, **overrides: str):
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "GLINK_HARNESS": "pi",
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_EXECUTOR": "e2b",
        "E2B_API_KEY": "e2b-secret-key",
        "GLINK_EGRESS_PUBLIC_URL": "https://egress.example.test",
        "TEST_ACP_COMMAND": "dsh --profile acp",
    }
    values.update(overrides)
    apply_acp_pack(values, tmp_path)
    return build_config(values, REPO_ROOT)


class SandboxQueue:
    """Hands out one fresh parkable sandbox per create call, all registered."""

    def __init__(self, tmp_path: Path, sandboxes: list[ParkableSandbox], *,
                 conversation_dir: Path | None, report: str | None = None,
                 credential_dests: tuple[str, ...] = ()) -> None:
        self._tmp_path = tmp_path
        self._sandboxes = sandboxes
        self._conversation_dir = conversation_dir
        self._report = report
        self._credential_dests = credential_dests

    def __call__(self, **_kwargs: Any) -> ParkableSandbox:
        sandbox = ParkableSandbox(
            self._tmp_path,
            sandbox_id=f"fake-sandbox-{len(self._sandboxes) + 1}",
            report=self._report,
            conversation_dir=self._conversation_dir,
            credential_dests=self._credential_dests,
        )
        self._sandboxes.append(sandbox)
        return sandbox


def payloads(store: RunStore, run_id: str, event_type: str) -> list[dict[str, Any]]:
    events = [
        json.loads(line)
        for line in events_text(store, run_id).splitlines()
        if line.strip()
    ]
    return [event["payload"] for event in events if event["type"] == event_type]


def assistant_text(store: RunStore, run_id: str) -> str:
    return "".join(str(p.get("text") or "") for p in payloads(store, run_id, "assistant_delta"))


# -- candidate gate --------------------------------------------------------------


def a_family(config, store) -> AcpHarnessFamily:
    # R1-3：构造点只收 SessionSource 协议对象，仓外调用点自己包适配器。
    return AcpHarnessFamily(config=config, store=RunStoreSessionSource(store))


def a_record(run_id: str, *, executor: str = "e2b", pack: str = "dsh") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        prompt="p",
        status="finished",
        created_at=1.0,
        workspace="/tmp/ws",
        spec=RunSpec(family="acp", pack=pack, executor=executor),
        session_id="s",
    )


def write_a_lease(conversation_dir: Path, *, run_id: str, generation: int = 1,
                  expires_at: float | None = None, pack: str = "dsh",
                  template: str = "glink-acp-worker",
                  sandbox_id: str = "sbx-lease",
                  paused_ago: float = 0.0) -> None:
    now = time.time() - paused_ago
    write_lease(
        conversation_dir,
        ConversationLease(
            conversation_run=conversation_dir.name,
            sandbox_id=sandbox_id,
            generation=generation,
            template=template,
            pack=pack,
            run_id=run_id,
            paused_at=now,
            expires_at=expires_at if expires_at is not None else now + 3600,
        ),
    )


def test_resume_target_matches_a_fresh_lease(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    family = a_family(config, store)
    conversation = store.conversation_dir("r1")
    write_a_lease(conversation, run_id="r1", generation=4)
    target = family.sandbox_resume_target(a_record("r1"))
    assert target == SandboxResume(
        sandbox_id="sbx-lease", generation=4, conversation_run="r1",
        template="glink-acp-worker",
    )


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda c, d: write_a_lease(c, run_id="someone-else"), id="stale-run"),
        pytest.param(lambda c, d: write_a_lease(c, run_id="r1", pack="codex"), id="pack-mismatch"),
        pytest.param(
            lambda c, d: write_a_lease(c, run_id="r1", template="other-template"),
            id="template-mismatch",
        ),
        pytest.param(
            lambda c, d: write_a_lease(
                c, run_id="r1", paused_ago=3600.0, expires_at=time.time() - 1
            ),
            id="expired",
        ),
        pytest.param(lambda c, d: None, id="no-lease"),
    ],
)
def test_resume_target_refuses_every_mismatch(tmp_path: Path, mutate) -> None:
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    family = a_family(config, store)
    conversation = store.conversation_dir("r1")
    mutate(conversation, None)
    assert family.sandbox_resume_target(a_record("r1")) is None


def test_resume_target_refused_without_the_e2b_face_or_ttl(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    family = a_family(config, store)
    conversation = store.conversation_dir("r1")
    write_a_lease(conversation, run_id="r1")
    assert family.sandbox_resume_target(a_record("r1", executor="docker")) is None
    disabled = make_config(tmp_path, GLINK_CONVERSATION_LEASE_TTL_SECONDS="0")
    family_off = a_family(disabled, store)
    assert family_off.sandbox_resume_target(a_record("r1")) is None


# -- adapter face ------------------------------------------------------------------


def an_adapter(tmp_path: Path, config, sandbox: ParkableSandbox, *, resume=None,
                conversation_dir: Path | None = None):
    acp_family = family("acp", config, tmp_path / "face-store")
    runtime = make_runtime(config, tmp_path)
    spec = ExecutionSpec(
        config=config,
        runtime=runtime,
        run_id="hot1",
        prompt="p",
        system_prompt="",
        workspace=tmp_path / "ws",
        timeout_seconds=30,
        sandbox_resume=resume,
        conversation_dir=conversation_dir,
    )
    frames: list[dict[str, Any]] = []
    sandbox_face = open_sandbox(
        "e2b",
        config=config,
        run_id="hot1",
        workspace=spec.workspace,
        sandbox_factory=lambda **_: sandbox,
        resume_factory=lambda **kwargs: ParkableSandbox.connect(**kwargs),
    )
    launch = acp_family.launch(spec, sandbox_face)
    adapter = acp_family.driver(
        spec, sandbox=sandbox_face, launch=launch,
        on_frame=frames.append, client_factory=None,
    )
    return adapter, frames, sandbox_face


def test_adapter_hot_path_connects_and_skips_the_backfill(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    sandbox = ParkableSandbox(tmp_path, sandbox_id="sbx-hot")
    write_generation_marker(sandbox, conversation_run="r1", generation=2)
    resume = SandboxResume(
        sandbox_id="sbx-hot", generation=2, conversation_run="r1",
        template="glink-acp-worker",
    )
    adapter, frames, face = an_adapter(tmp_path, config, sandbox, resume=resume)
    assert adapter._open_sandbox(face) is True
    assert sandbox.connect_calls and sandbox.connect_calls[0]["on_resume"] == "reboot"
    assert frames[-1]["type"] == "acp_sandbox_resume" and frames[-1]["path"] == "hot"


def test_adapter_degrades_when_the_marker_disagrees(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    sandbox = ParkableSandbox(tmp_path, sandbox_id="sbx-marker-miss")
    write_generation_marker(sandbox, conversation_run="r1", generation=1)
    conversation = config.data_root / "conversations" / "r1"
    conversation.mkdir(parents=True, exist_ok=True)
    write_a_lease(conversation, run_id="hot1", sandbox_id="sbx-marker-miss", generation=99)
    resume = SandboxResume(
        sandbox_id="sbx-marker-miss", generation=99, conversation_run="r1",
        template="glink-acp-worker",
    )
    adapter, frames, face = an_adapter(
        tmp_path, config, sandbox, resume=resume, conversation_dir=conversation
    )
    assert adapter._open_sandbox(face) is False
    degraded = [f for f in frames if f.get("type") == "acp_sandbox_resume"]
    assert degraded and degraded[-1]["path"] == "cold" and "代数" in degraded[-1]["reason"]
    # The reconnected handle was destroyed (the fake's kill) and the lease —
    # whose sandbox is confirmed gone — is removed, not expired.
    assert sandbox.killed
    assert read_lease(conversation) is None


def test_adapter_degrades_when_connect_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import glink_harness.kernel.acp_execution as acp_execution

    config = make_config(tmp_path)
    gone = ParkableSandbox(tmp_path, sandbox_id="sbx-gone")
    gone.kill()  # the sandbox vanished between admission and start
    conversation = config.data_root / "conversations" / "r1"
    conversation.mkdir(parents=True, exist_ok=True)
    write_a_lease(conversation, run_id="hot1", sandbox_id="sbx-gone")
    resume = SandboxResume(
        sandbox_id="sbx-gone", generation=1, conversation_run="r1",
        template="glink-acp-worker",
    )
    adapter, frames, face = an_adapter(
        tmp_path, config, gone, resume=resume, conversation_dir=conversation
    )
    # The id-addressed destroy (no handle exists here) is faked: the sandbox
    # registry already says gone, so the destroy confirms.
    monkeypatch.setattr(
        acp_execution,
        "kill_sandbox_by_id",
        lambda sandbox_id, **_kwargs: (True, "fake confirm"),
    )
    fresh = ParkableSandbox(tmp_path, sandbox_id="sbx-after-gone", report=None)
    face._sandbox_factory = lambda **_: fresh  # type: ignore[attr-defined]
    assert adapter._open_sandbox(face) is False
    degraded = [f for f in frames if f.get("type") == "acp_sandbox_resume"]
    assert degraded and degraded[-1]["path"] == "cold"
    assert read_lease(conversation) is None
    assert face.handle is fresh




class _CleanReclaimTransfer:
    """Parks now require a post-close reclaim; this one always publishes OK."""

    def recover_state(self):
        from glink_harness.kernel.acp_e2b_env import AcpStateRecovery

        return AcpStateRecovery(ok=True, failures=())

# -- close-out parking (adapter face) ----------------------------------------------


def test_close_parks_a_recovered_sandbox(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    sandbox = ParkableSandbox(tmp_path, sandbox_id="sbx-close")
    conversation = config.data_root / "conversations" / "r1"
    (conversation / "state").mkdir(parents=True, exist_ok=True)
    (conversation / "state" / "manifest.json").write_text(
        json.dumps({"version": 1, "files": {}, "sourceRunId": "hot1"}),
        encoding="utf-8"
    )
    adapter, _frames, face = an_adapter(
        tmp_path, config, sandbox, conversation_dir=conversation
    )
    face.open(adapter.launch, timeout_seconds=30)  # the carrier holds the handle
    adapter._park_ready = True  # the recovery succeeded and published this run
    adapter._transfer = _CleanReclaimTransfer()  # post-close reclaim publishes OK (B1)
    report = adapter.close()
    assert report.released
    assert report.lease == {
        "parked": True, "sandbox": "sbx-close", "generation": 1,
        "expiresAt": read_lease(conversation).expires_at,  # type: ignore[union-attr]
    }
    assert sandbox.state == "paused" and not sandbox.killed


def test_close_reports_a_failed_parking_without_keeping_the_slot(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    sandbox = ParkableSandbox(tmp_path, sandbox_id="sbx-nopark")
    conversation = config.data_root / "conversations" / "r1"
    conversation.mkdir(parents=True, exist_ok=True)
    adapter, _frames, face = an_adapter(
        tmp_path, config, sandbox, conversation_dir=conversation
    )
    face.open(adapter.launch, timeout_seconds=30)
    adapter._park_ready = True
    adapter._transfer = _CleanReclaimTransfer()  # post-close reclaim publishes OK (B1)

    def refuse_pause(**_kwargs: object) -> bool:
        raise OSError("control plane said no")

    sandbox.pause = refuse_pause  # type: ignore[method-assign]
    report = adapter.close()
    # 泊车失败 → 销毁降级：资源已释放（不占槽位），事实随终帧可见。
    assert report.released
    assert report.lease is not None
    assert report.lease["parked"] is False and "pause" in report.lease["reason"]
    assert sandbox.killed
    assert read_lease(conversation) is None


def test_close_does_not_park_an_unpublished_run(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    sandbox = ParkableSandbox(tmp_path, sandbox_id="sbx-unpublished")
    conversation = config.data_root / "conversations" / "r1"
    conversation.mkdir(parents=True, exist_ok=True)
    adapter, _frames, face = an_adapter(
        tmp_path, config, sandbox, conversation_dir=conversation
    )
    face.open(adapter.launch, timeout_seconds=30)
    adapter._park_ready = False  # the recovery failed / did not publish
    report = adapter.close()
    assert report.released and report.lease is None
    assert sandbox.killed  # 阶段 2 之前的销毁行为


# -- the carrier's discard on a failed kill -----------------------------------------


def test_e2b_discard_survives_a_failed_kill(tmp_path: Path) -> None:
    """discard 的 kill 失败不抛：交对账兜底，返回 False 让调用方按 id 销毁。"""
    from glink_harness.kernel.sandbox import Launch

    config = make_config(tmp_path)
    stuck = ParkableSandbox(tmp_path, sandbox_id="sbx-stuck-handle")

    def refuse_kill() -> bool:
        raise OSError("transport wedged")

    stuck.kill = refuse_kill  # type: ignore[method-assign]
    launch = Launch(argv=(), template="glink-acp-worker")
    face = open_sandbox(
        "e2b",
        config=config,
        run_id="discard1",
        workspace=tmp_path / "ws",
        sandbox_factory=lambda **_: stuck,
        resume_factory=lambda **kwargs: ParkableSandbox.connect(**kwargs),
    )
    face.open(launch, timeout_seconds=30)
    assert face.discard() is False
    # discard 后状态已清空：同一 carrier 可以重走 create 路径（冷路径降级）。
    face.open(launch, timeout_seconds=30)
    assert face.handle is stuck


# -- projection --------------------------------------------------------------------


def test_projection_maps_the_hot_and_cold_frames() -> None:
    projector = AcpProjector(SecretRedactor(()))
    hot = projector.project(
        {"type": "acp_sandbox_resume", "path": "hot", "sandboxId": "s1", "generation": 2}
    )
    assert hot == [("kernel_status", {
        "level": "info",
        "message": "acp_sandbox_hot_path",
        "detail": "追问命中对话租约沙箱（热路径）：重连上一轮泊车的沙箱，未回灌状态；sandbox=s1；generation=2",
    })]
    cold = projector.project(
        {"type": "acp_sandbox_resume", "path": "cold", "sandboxId": "s1", "reason": "boom"}
    )
    assert cold[0][0] == "kernel_status"
    assert cold[0][1]["level"] == "warning"
    assert cold[0][1]["message"] == "acp_sandbox_hot_path_degraded"
    assert "s1" in cold[0][1]["detail"] and "boom" in cold[0][1]["detail"]


# -- manager end-to-end --------------------------------------------------------------


def fake_kill_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the adapter's id-addressed destroys at the fake registry.

    The production helper would call the real SDK class method; the fake
    registry answers the same contract (``False`` = not found = confirmed
    gone). Patched at the adapter module because both the degrade path and the
    parking retire path reach it through the same global name.
    """
    import glink_harness.kernel.acp_execution as acp_execution

    def confirm(sandbox_id: str, **_kwargs: Any):
        if ParkableSandbox.REGISTRY.get(sandbox_id) is None:
            return (True, "fake not-found confirm")
        ParkableSandbox.kill_by_id(sandbox_id)
        return (True, "fake kill-by-id")

    monkeypatch.setattr(acp_execution, "kill_sandbox_by_id", confirm)


def test_first_run_parks_and_followup_takes_the_hot_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_kill_by_id(monkeypatch)
    settings = {"FAKE_ACP_LOG": str(tmp_path / "agent.jsonl"), "FAKE_ACP_WRITE_REPORT": True}
    config = make_config(
        tmp_path,
        TEST_ACP_COMMAND=fake_agent_command(tmp_path, settings),
        GLINK_ACP_CREDENTIAL_FILES=f"{tmp_path / 'cred.yaml'}=.dsh/.credentials.yaml",
    )
    (tmp_path / "cred.yaml").write_text("DEEPSEEK_API_KEY: sk-live-cred-1234567890\n")
    store = RunStore(config.data_root / "runs")
    sandboxes: list[ParkableSandbox] = []
    queue_holder: dict[str, Any] = {}

    def factory(**kwargs: Any) -> ParkableSandbox:
        sandbox = ParkableSandbox(
            tmp_path,
            sandbox_id=f"fake-sandbox-{len(sandboxes) + 1}",
            report=None,
            conversation_dir=queue_holder.get("conversation"),
            credential_dests=(".dsh/.credentials.yaml",),
        )
        sandboxes.append(sandbox)
        return sandbox

    manager = RunManager(
        config, EventBus(), store=store,
        sandbox_factory=factory,
        sandbox_resume_factory=lambda **kwargs: ParkableSandbox.connect(**kwargs),
    )

    first = manager.start("第一轮：请记住代号 REMEMBER:HOT-991，然后写报告")
    first_final = manager.wait(first.run_id, timeout=180)
    assert first_final is not None and first_final.status == FINISHED, first_final.error

    # The conversation root directory now exists; the first sandbox's backfill
    # interceptor must see it for the (possible) degrade tests later.
    conversation = store.conversations_root() / first.run_id
    assert conversation.is_dir()
    queue_holder["conversation"] = conversation

    # Parked, not killed: the lease, the marker and the terminal frame agree.
    first_sandbox = sandboxes[0]
    assert not first_sandbox.killed
    assert first_sandbox.state == "paused"
    assert first_sandbox.pause_calls and first_sandbox.pause_calls[0]["keep_memory"] is False
    lease = read_lease(conversation)
    assert lease is not None
    assert lease.sandbox_id == first_sandbox.sandbox_id
    assert lease.run_id == first.run_id
    assert lease.generation == 1
    assert read_generation_marker(first_sandbox) == (first.run_id, 1)
    # 凭据副本不随沙箱持久化（ADR 0015/0016 的泊车形态）。
    assert not (
        first_sandbox.root / "home/user/acp/home/.dsh/.credentials.yaml"
    ).exists()

    # The terminal frame's cleanup carries the lease handover and no hold.
    terminal = payloads(store, first.run_id, "run_finished")[-1]
    assert terminal["cleanup"]["released"] is True
    assert terminal["cleanup"]["lease"]["parked"] is True
    assert terminal["cleanup"]["lease"]["sandbox"] == first_sandbox.sandbox_id
    assert manager._ledger.resource_holds() == []

    # The follow-up takes the hot path: the same sandbox is reconnected.
    second = manager.start(
        "RECALL 不要读文件，只用一行复述上一个会话里要求记住的代号",
        parent_run_id=first.run_id,
    )
    second_final = manager.wait(second.run_id, timeout=180)
    assert second_final is not None and second_final.status == FINISHED, second_final.error
    assert "REMEMBERED:HOT-991" in assistant_text(store, second.run_id)
    assert len(sandboxes) == 1, "the follow-up must not create a second sandbox"
    assert sandboxes[0].connect_calls, "the follow-up must reconnect by id"
    assert sandboxes[0].connect_calls[-1]["on_resume"] == "reboot"
    # No backfill ran: the hot path's state is the sandbox's own.
    assert sandboxes[0].commands.backfills == 0
    hot_notices = [
        p for p in payloads(store, second.run_id, "kernel_status")
        if p.get("message") == "acp_sandbox_hot_path"
    ]
    assert len(hot_notices) == 1
    ready = payloads(store, second.run_id, "session_ready")
    assert ready[0]["sandboxResume"] == {
        "path": "hot", "sandboxId": sandboxes[0].sandbox_id, "backfilled": False,
    }

    # The second turn parks again over the same sandbox: generation advanced.
    lease2 = read_lease(conversation)
    assert lease2 is not None
    assert lease2.generation == 2 and lease2.run_id == second.run_id
    assert read_generation_marker(sandboxes[0]) == (first.run_id, 2)


def test_hot_path_degrades_when_the_lease_sandbox_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_kill_by_id(monkeypatch)
    settings = {"FAKE_ACP_LOG": str(tmp_path / "agent.jsonl"), "FAKE_ACP_WRITE_REPORT": True}
    config = make_config(
        tmp_path,
        TEST_ACP_COMMAND=fake_agent_command(tmp_path, settings),
    )
    store = RunStore(config.data_root / "runs")
    sandboxes: list[ParkableSandbox] = []
    queue_holder: dict[str, Any] = {}

    def factory(**kwargs: Any) -> ParkableSandbox:
        sandbox = ParkableSandbox(
            tmp_path,
            sandbox_id=f"fake-sandbox-{len(sandboxes) + 1}",
            report=None,
            conversation_dir=queue_holder.get("conversation"),
            credential_dests=(".dsh/.credentials.yaml",),
        )
        sandboxes.append(sandbox)
        return sandbox

    manager = RunManager(
        config, EventBus(), store=store,
        sandbox_factory=factory,
        sandbox_resume_factory=lambda **kwargs: ParkableSandbox.connect(**kwargs),
    )
    first = manager.start("第一轮：请记住代号 REMEMBER:COLD-417，然后写报告")
    first_final = manager.wait(first.run_id, timeout=180)
    assert first_final is not None and first_final.status == FINISHED, first_final.error
    conversation = store.conversations_root() / first.run_id
    queue_holder["conversation"] = conversation

    # The parked sandbox vanishes before the follow-up (E2B reaped it, or the
    # operator killed it): connect must fail and the run must degrade.
    sandboxes[0].kill()

    second = manager.start(
        "RECALL 不要读文件，只用一行复述上一个会话里要求记住的代号",
        parent_run_id=first.run_id,
    )
    second_final = manager.wait(second.run_id, timeout=180)
    assert second_final is not None and second_final.status == FINISHED, second_final.error
    assert "REMEMBERED:COLD-417" in assistant_text(store, second.run_id)
    # A fresh sandbox carried the cold path, and the backfill really ran.
    assert len(sandboxes) == 2
    assert sandboxes[1].commands.backfills >= 1
    degraded = [
        p for p in payloads(store, second.run_id, "kernel_status")
        if p.get("message") == "acp_sandbox_hot_path_degraded"
    ]
    assert len(degraded) == 1 and sandboxes[0].sandbox_id in degraded[0]["detail"]
    # The dead lease is gone; the new sandbox parks in its place.
    lease = read_lease(conversation)
    assert lease is not None and lease.sandbox_id == sandboxes[1].sandbox_id
    assert not sandboxes[1].killed and sandboxes[1].state == "paused"



@pytest.mark.parametrize("parent_executor", ["e2b", "inproc"])
def test_non_archive_parent_keeps_its_admitted_state_when_archive_is_enabled(
    tmp_path, monkeypatch, parent_executor,
):
    """Exercise both real runs; only SDK/agent external inputs are fake."""
    fake_kill_by_id(monkeypatch)
    command = fake_agent_command(tmp_path, {})
    config = make_config(tmp_path, TEST_ACP_COMMAND=command, GLINK_EGRESS_PUBLIC_URL="",
                         LOCAL_WEB_EXECUTOR=parent_executor)
    store = RunStore(config.data_root / "runs")
    sandboxes = []

    def factory(**kw):
        sandbox = ParkableSandbox(tmp_path / f"face-{len(sandboxes)}",
                                  sandbox_id=f"legacy-{len(sandboxes)}", conversation_dir=None)
        sandboxes.append(sandbox)
        return sandbox

    first_manager = RunManager(config, EventBus(), store=store, sandbox_factory=factory)
    try:
        first = first_manager.start("REMEMBER:LEGACY-671")
        final = first_manager.wait(first.run_id, timeout=20)
        assert final and final.status == FINISHED, final
    finally:
        first_manager.shutdown()
    source = store.run_dir(first.run_id) / "acp-agent" / (
        "recovered" if parent_executor == "e2b" else "home"
    )
    assert source.is_dir() and list(source.rglob("*"))
    conversation = store.conversations_root() / first.run_id
    assert not (conversation / "state").exists()
    config = make_config(tmp_path, TEST_ACP_COMMAND=command)
    second_manager = RunManager(config, EventBus(), store=store, sandbox_factory=factory)
    try:
        second = second_manager.start("RECALL", parent_run_id=first.run_id)
        final = second_manager.wait(second.run_id, timeout=20)
        assert final and final.status == FINISHED, final.error if final else final
        assert "REMEMBERED:LEGACY-671" in assistant_text(store, second.run_id)
        assert sandboxes[-1].commands.backfills == 0
    finally:
        second_manager.shutdown()


@pytest.mark.parametrize("third_has_channel", [False, True])
def test_archive_then_files_only_recovery_can_be_followed_again(tmp_path, monkeypatch, third_has_channel):
    """Three real runs must resume the newest turn, not the stale root archive."""
    from glink_harness.kernel.archive_channel import read_state_manifest

    fake_kill_by_id(monkeypatch)
    command = fake_agent_command(tmp_path, {})
    config = make_config(tmp_path, TEST_ACP_COMMAND=command)
    store = RunStore(config.data_root / "runs")
    sandboxes = []
    conversation_holder = {}

    def factory(**kw):
        sandbox = ParkableSandbox(tmp_path, sandbox_id=f"mixed-{len(sandboxes)}",
                                  conversation_dir=conversation_holder.get("directory"))
        sandboxes.append(sandbox)
        return sandbox

    def run_turn(prompt, parent_id, public_url):
        configuration = make_config(tmp_path, TEST_ACP_COMMAND=command,
                                    GLINK_EGRESS_PUBLIC_URL=public_url)
        manager = RunManager(configuration, EventBus(), store, sandbox_factory=factory)
        try:
            record = manager.start(prompt, parent_run_id=parent_id)
            final = manager.wait(record.run_id, timeout=20)
            assert final and final.status == FINISHED, final.error if final else final
            return record
        finally:
            manager.shutdown()

    first = run_turn("REMEMBER:ARCHIVE-111", None, config.egress_public_url)
    conversation = store.conversations_root() / first.run_id
    conversation_holder["directory"] = conversation
    assert read_state_manifest(conversation)["sourceRunId"] == first.run_id
    second = run_turn("REMEMBER:FILES-222", first.run_id, "")
    recovered = store.run_dir(second.run_id) / "acp-agent" / "recovered"
    assert recovered.is_dir() and list(recovered.rglob("*"))
    assert read_state_manifest(conversation)["sourceRunId"] == first.run_id
    third = run_turn("RECALL", second.run_id,
                     config.egress_public_url if third_has_channel else "")
    assert "REMEMBERED:FILES-222" in assistant_text(store, third.run_id)
    assert sandboxes[-1].commands.backfills == 0, "the stale conversation archive must not be fetched"
