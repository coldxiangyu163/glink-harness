"""B1 regressions: one execution interface for inproc / Docker / E2B.

The three faces differ in how a kernel is launched, spoken to, cancelled and
released. Those differences belong behind :class:`ExecutionAdapter`; the run
manager owns the product lifecycle and must not carry a second copy of "wait for
settle", "pull the report back" or "kill on cancel" per face.

Two properties are pinned here:

* a recording adapter shows the manager drives every face through exactly the
  same ordered protocol (start → session → input → wait → recover → close);
* the same kernel event script produces the same product event vocabulary and
  the same run verdict on all three faces.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from harness_helpers import test_egress
from pi_glink.config import build_config
from pi_glink.events import EventBus
from pi_glink.kernel.execution import (
    ArtifactRecovery,
    CleanupReport,
    ExecutionAdapter,
    ExecutionOutcome,
    ExecutionSpec,
    PiExecutionAdapter,
    SessionInfo,
)
from pi_glink.kernel.harness import select_family
from pi_glink.kernel.sandbox import DockerSandbox, E2BSandbox, open_sandbox
from pi_glink.kernel.rpc import PiRpcClient
from pi_glink.runs.runner import RunManager
from pi_glink.store import FINISHED, RunStore

from e2b_fake import FakeSandbox

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_PI = Path(__file__).resolve().parent / "fixtures" / "fake_pi.py"


def make_config(tmp_path: Path, **overrides: str):
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


@dataclass
class RecordingAdapter(ExecutionAdapter):
    """Records the manager's calls and returns a scripted outcome."""

    calls: list[str] = field(default_factory=list)
    report: bool = True

    def start(self) -> None:
        self.calls.append("start")

    def session_info(self) -> SessionInfo | None:
        self.calls.append("session_info")
        return SessionInfo(payload={"sessionId": "recording"})

    def deliver_input(self) -> None:
        self.calls.append("deliver_input")

    def wait(self, *, cancel: Callable[[], bool], deadline: float) -> ExecutionOutcome:
        self.calls.append("wait")
        return ExecutionOutcome(settled=True)

    def cancel(self) -> None:
        self.calls.append("cancel")

    def recover_artifacts(self, destination: Path) -> ArtifactRecovery:
        self.calls.append("recover_artifacts")
        if self.report:
            destination.mkdir(parents=True, exist_ok=True)
            path = destination / "report.md"
            path.write_text("# 录制报告\n", encoding="utf-8")
            return ArtifactRecovery(files=(path,))
        return ArtifactRecovery()

    def close(self) -> CleanupReport:
        self.calls.append("close")
        return CleanupReport(released=True, external={"container": "recording"})

    def identity(self) -> Mapping[str, str]:
        return {"container": "recording"}


def make_manager(tmp_path: Path, **overrides: str):
    config = make_config(tmp_path, **overrides)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    return config, store, manager


def test_the_manager_drives_every_face_through_the_same_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, manager = make_manager(tmp_path)
    adapter = RecordingAdapter()
    monkeypatch.setattr(
        "pi_glink.kernel.harness_pi.PiExecutionAdapter", lambda spec, **kwargs: adapter
    )

    record = manager.start("统一接口")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == FINISHED
    assert final.report_status == "ok", "the artifact recovery result must be used"
    assert final.cleanup is not None and final.cleanup["released"] is True
    assert adapter.calls == [
        "start",
        "session_info",
        "deliver_input",
        "wait",
        "recover_artifacts",
        "close",
    ], adapter.calls
    assert final.container_name == "recording", "identity must be persisted for reconciliation"


def test_cancellation_goes_through_the_adapter_not_a_face_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, store, manager = make_manager(tmp_path)
    adapter = RecordingAdapter()
    adapter.report = False
    entered = threading.Event()
    release = threading.Event()

    def slow_wait(*, cancel, deadline):
        adapter.calls.append("wait")
        entered.set()
        assert release.wait(timeout=10)
        return ExecutionOutcome(cancelled=True, stop_reason="abort")

    adapter.wait = slow_wait  # type: ignore[method-assign]
    monkeypatch.setattr(
        "pi_glink.kernel.harness_pi.PiExecutionAdapter", lambda spec, **kwargs: adapter
    )

    record = manager.start("取消走适配器")
    assert entered.wait(timeout=10)
    assert manager.stop(record.run_id).accepted is True
    assert "cancel" in adapter.calls, "stop must call the adapter's cancel"
    release.set()
    final = manager.wait(record.run_id, timeout=30)
    assert final.status == "cancelled"
    assert adapter.calls[-1] == "close"


# -- cross-face equivalence ---------------------------------------------------


def _run_face(tmp_path: Path, executor: str, monkeypatch: pytest.MonkeyPatch) -> tuple:
    overrides: dict[str, str] = {"LOCAL_WEB_EXECUTOR": executor}
    if executor == "e2b":
        # The sandbox face runs the same fake pi as a real subprocess behind
        # the stand-in CommandHandle (the ACP face's FakeAgentHandle shape);
        # only the carrier differs, the protocol drive is shared.
        overrides["E2B_API_KEY"] = "e2b-secret"
        overrides["PI_GLINK_EGRESS_PUBLIC_URL"] = "https://egress.example.test"
        sandbox_factory = lambda **_: FakeSandbox(  # noqa: E731
            tmp_path / "sandbox-root",
            report=None,
            fake_pi_env={"FAKE_PI_WRITE_REPORT": "1"},
        )
    else:
        sandbox_factory = None
        monkeypatch.setattr("pi_glink.kernel.sandbox.docker_available", lambda: True)
        monkeypatch.setattr(
            "pi_glink.kernel.sandbox.subprocess.run",
            lambda *args, **kwargs: type("R", (), {"returncode": 0, "stderr": ""})(),
        )
    config = make_config(tmp_path, **overrides)

    def fake_factory(argv, *, cwd=None, env=None, on_event=None, popen=None):
        if popen is not None:
            # The E2B adapter injects the already-started sandbox process
            # facade; the client is the shared one, unchanged.
            return PiRpcClient(argv, cwd=cwd, env=env, on_event=on_event, popen=popen)
        merged = dict(env or {})
        merged["FAKE_PI_WRITE_REPORT"] = "1"
        if executor == "docker":
            # The container face has no host cwd; the stand-in must write into
            # the run's workspace so the recovery path sees a real artifact.
            match = re.search(r"pi-glink-([0-9a-f]{12})", " ".join(argv))
            assert match is not None, argv
            cwd = config.workspace_root / match.group(1)
            cwd.mkdir(parents=True, exist_ok=True)
            merged["FAKE_PI_SESSION_FILE"] = "/sessions/fake-session.jsonl"
        return PiRpcClient(
            [sys.executable, str(FAKE_PI), *argv[1:]],
            cwd=cwd,
            env=merged,
            on_event=on_event,
        )

    store = RunStore(config.data_root / "runs")
    manager = RunManager(
        config,
        EventBus(),
        store,
        client_factory=fake_factory,
        sandbox_factory=sandbox_factory,
    )
    record = manager.start("同一套输入契约")
    final = manager.wait(record.run_id, timeout=30)
    events = [
        json.loads(line)
        for line in store.events_path(record.run_id).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return final, [event["type"] for event in events]


@pytest.mark.parametrize("executor", ["inproc", "docker", "e2b"])
def test_every_face_produces_the_same_product_verdict(
    tmp_path: Path, executor: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    final, types = _run_face(tmp_path, executor, monkeypatch)
    assert final.status == FINISHED
    assert final.report_status == "ok"
    assert types[0] == "run_started"
    assert types[-1] == "run_finished"
    for expected in (
        "session_ready",
        "turn_started",
        "assistant_delta",
        "tool_started",
        "tool_updated",
        "tool_finished",
        "usage_reported",
        "turn_finished",
    ):
        assert expected in types, f"{executor} is missing {expected}"


def test_the_three_faces_agree_frame_for_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same kernel script must project to the same vocabulary on every face.

    Payloads differ where they describe the face (session id, sandbox id); the
    event *sequence* the browser consumes must not.
    """
    _, inproc_types = _run_face(tmp_path / "inproc", "inproc", monkeypatch)
    _, docker_types = _run_face(tmp_path / "docker", "docker", monkeypatch)
    _, e2b_types = _run_face(tmp_path / "e2b", "e2b", monkeypatch)
    assert inproc_types == docker_types == e2b_types


def test_the_adapter_is_the_family_driver_over_the_frozen_executor_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``family.build_adapter(spec, open_sandbox(executor))`` is the only composition.

    The pi family's driver is the same :class:`PiExecutionAdapter` on every
    sandbox; only the sandbox (chosen from the frozen executor, not the host's
    current configuration) differs, and the adapter reports its kind and
    identity from it.
    """
    # The host is configured for docker; the frozen executor decides the face.
    config = make_config(tmp_path, LOCAL_WEB_EXECUTOR="docker")
    from pi_glink.kernel import agent_env

    runtime = agent_env.prepare_agent_dir(
        config,
        runtime_dir=tmp_path / "runtime",
        session_dir=tmp_path / "sessions",
        egress=test_egress(),
    )
    spec = ExecutionSpec(
        config=config,
        runtime=runtime,
        run_id="abc123",
        prompt="x",
        system_prompt="",
        workspace=tmp_path / "ws",
        timeout_seconds=60,
    )
    family = select_family("pi", config=config, store=RunStore(tmp_path / "store"))

    def sandbox(executor: str, **kwargs: Any):
        return open_sandbox(
            executor, config=config, run_id="abc123", workspace=tmp_path / "ws", **kwargs
        )

    docker = sandbox("docker")
    adapter = family.build_adapter(
        spec, docker, client_factory=PiRpcClient, on_frame=lambda frame: None
    )
    assert isinstance(docker, DockerSandbox)
    assert isinstance(adapter, PiExecutionAdapter)
    assert adapter.kind == "docker"
    assert adapter.identity() == {"container": "pi-glink-abc123"}

    e2b = sandbox("e2b", sandbox_factory=lambda **_: FakeSandbox())
    e2b_adapter = family.build_adapter(
        spec, e2b, client_factory=PiRpcClient, on_frame=lambda frame: None
    )
    assert isinstance(e2b, E2BSandbox)
    assert isinstance(e2b_adapter, PiExecutionAdapter)
    assert e2b_adapter.kind == "e2b"
    # No sandbox exists before start(): nothing to name yet.
    assert e2b_adapter.identity() == {}

    inproc_adapter = family.build_adapter(
        spec, sandbox("inproc"), client_factory=PiRpcClient, on_frame=lambda frame: None
    )
    assert isinstance(inproc_adapter, PiExecutionAdapter)
    assert inproc_adapter.kind == "inproc"
    assert inproc_adapter.identity() == {}
