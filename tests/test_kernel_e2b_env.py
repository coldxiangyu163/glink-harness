"""Regressions for the E2B execution face (no cloud calls).

They pin the behaviours the completion audit flagged:

* the credential read from ``.env`` reaches ``Sandbox.create`` (``api_key``);
* a run replays its JSONL frames and pulls the report back into the host workspace;
* a non-zero sandbox exit is a failure even when a report file exists.

They also pin the delivery contract added with the architecture hardening:
LF-only framing (U+2028 stays inside a frame), a verifiable configuration
manifest, shell-safe command composition, and bounded/safe artifact recovery.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from pi_glink.config import build_config
from pi_glink.events import EventBus
from pi_glink.kernel import agent_env, e2b_env
from pi_glink.kernel.e2b_env import (
    ARTIFACT_MAX_BYTES,
    EVENTS_MAX_BYTES,
    STDERR_MAX_BYTES,
    E2BSandboxClient,
    EventCatchUpFailed,
    SandboxError,
    parse_json_stream_line,
)
from pi_glink.kernel.runner import RunManager
from pi_glink.store import RunStore

REPO_ROOT = Path(__file__).resolve().parents[1]
FRAMING_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "lf-framing.json"
FRAMING_FIXTURE_TEXT = FRAMING_FIXTURE.read_text(encoding="utf-8")

_FAKE_EVENTS = [
    {"type": "agent_start"},
    {"type": "turn_start"},
    {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "沙箱里的一句话"}},
    {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "write", "args": {"path": "notes.md"}},
    {"type": "tool_execution_end", "toolCallId": "c1", "toolName": "write", "result": "ok", "isError": False},
    {"type": "turn_end", "message": {"role": "assistant", "usage": {"input": 5, "output": 7, "totalTokens": 12}}, "toolResults": []},
    {"type": "agent_settled"},
]


class _Stream:
    """Stand-in for the SDK's ``format="stream"`` reader (chunks + close)."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.closed = False

    def __iter__(self) -> Any:
        return iter(self._chunks)

    def close(self) -> None:
        self.closed = True


class FakeFiles:
    def __init__(self, sandbox: "FakeSandbox") -> None:
        self._sandbox = sandbox

    def make_dir(self, path: str) -> None:
        self._sandbox.dirs.append(path)

    def write(self, path: str, content: Any) -> None:
        text = content.decode("utf-8") if isinstance(content, bytes) else str(content)
        self._sandbox.written[path] = text

    def read(self, path: str, format: str | None = None) -> Any:
        self._sandbox.reads.append(path)
        if path in self._sandbox.readable:
            value = self._sandbox.readable[path]
            data = value if isinstance(value, bytes) else value.encode("utf-8")
        else:
            raise FileNotFoundError(path)
        if format == "stream":
            return _Stream([data])
        return data

    def get_info(self, path: str) -> Any:
        """The SDK's entry metadata: the host bounds the read by ``size`` first."""
        if path not in self._sandbox.readable:
            raise FileNotFoundError(path)
        value = self._sandbox.readable[path]
        size = self._sandbox.sizes.get(
            path, len(value) if isinstance(value, bytes) else len(value.encode("utf-8"))
        )

        class Info:
            pass

        info = Info()
        info.size = size
        return info

    def list(self, path: str) -> list[Any]:
        readable = self._sandbox.readable
        sizes = self._sandbox.sizes
        types = self._sandbox.entry_types

        class Entry:
            def __init__(self, name: str) -> None:
                self.name = name
                self.path = f"{path}/{name}"
                # The real SDK reports an EntryInfo.type (an enum); absent
                # means a regular file.
                self.type = types.get(self.path, "file")
                content = readable.get(self.path)
                if self.path in sizes:
                    self.size = sizes[self.path]
                elif content is None:
                    self.size = 0
                elif isinstance(content, bytes):
                    self.size = len(content)
                else:
                    self.size = len(content.encode("utf-8"))

        names = self._sandbox.session_files if path == "/home/user/sessions" else self._sandbox.workspace_files
        return [Entry(name) for name in names]


class _Result:
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code


class _Handle:
    def __init__(self, exit_code: int) -> None:
        self._exit_code = exit_code

    def wait(self) -> _Result:
        return _Result(self._exit_code)


class FakeCommands:
    def __init__(self, sandbox: "FakeSandbox") -> None:
        self._sandbox = sandbox

    def run(
        self,
        cmd: str,
        *,
        background: bool = False,
        envs: dict[str, str] | None = None,
        timeout: float | None = None,
        **_: Any,
    ) -> _Handle:
        self._sandbox.command = cmd
        self._sandbox.command_envs = envs or {}
        self._sandbox.command_timeout = timeout
        return _Handle(self._sandbox.exit_code)


class FakeSandbox:
    """Minimal stand-in for ``e2b.Sandbox`` driven by the tests."""

    def __init__(
        self,
        *,
        exit_code: int = 0,
        report: str | None = "# 报告\n\n来自沙箱。\n",
        workspace_files: tuple[str, ...] = ("report.md",),
        events: list[dict[str, Any]] | None = None,
        events_text: str | None = None,
    ) -> None:
        self.sandbox_id = "fake-sandbox"
        self.exit_code = exit_code
        self.dirs: list[str] = []
        self.written: dict[str, str] = {}
        self.readable: dict[str, Any] = {}
        self.workspace_files = workspace_files
        self.session_files: tuple[str, ...] = ()
        self.command = ""
        self.command_envs: dict[str, str] = {}
        #: ``timeout`` the host passed to ``commands.run`` (``None`` when the
        #: caller left the SDK default in place).
        self.command_timeout: float | None = None
        self.killed = False
        #: Paths the host asked to read (the bounded-read regressions).
        self.reads: list[str] = []
        #: Entry sizes the SDK reports instead of the real content length.
        self.sizes: dict[str, int] = {}
        #: Entry kinds the SDK reports (path -> "file"/"dir"/"symlink"/enum).
        self.entry_types: dict[str, Any] = {}
        if events is not None:
            self.readable["/home/user/events.jsonl"] = "\n".join(
                json.dumps(event) for event in events
            )
        elif events_text is not None:
            self.readable["/home/user/events.jsonl"] = events_text
        else:
            # A real sandbox always writes the file (the command tees into it),
            # so a run whose face never streamed still finds an empty timeline
            # instead of an unresolved one. Tests that own the fail-closed case
            # delete this entry explicitly.
            self.readable["/home/user/events.jsonl"] = ""
        if report is not None:
            self.readable["/home/user/workspace/report.md"] = report
        self.files = FakeFiles(self)
        self.commands = FakeCommands(self)

    def kill(self) -> None:
        self.killed = True


def make_config(tmp_path: Path, **overrides: str):
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_EXECUTOR": "e2b",
        "E2B_API_KEY": "e2b-secret-key",
        "LOCAL_WEB_E2B_TEMPLATE": "pi-glink-worker",
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent-web-search.json"),
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


def _prepare(config, tmp_path: Path, name: str = "run-1"):
    run_dir = tmp_path / "runs" / name
    return agent_env.prepare_agent_dir(
        config,
        runtime_dir=run_dir / "pi-agent",
        session_dir=run_dir / "sessions",
    )


# -- credential plumbing ------------------------------------------------------


def test_e2b_key_reaches_create_and_model_key_stays_out_of_env(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    captured: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeSandbox:
        captured.update(kwargs)
        return FakeSandbox()

    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=factory
    )
    client.start()
    assert captured["template"] == "pi-glink-worker"
    # The *E2B* key authenticates the sandbox API…
    assert captured["api_key"] == "e2b-secret-key"
    # …while the *model* key travels in auth.json, never as an env var.
    assert "OPENAI_API_KEY" not in captured["envs"]
    client.close()


def test_missing_credential_is_refused_before_any_cloud_call(tmp_path: Path) -> None:
    config = make_config(tmp_path, E2B_API_KEY="")
    runtime = _prepare(config, tmp_path)
    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=lambda **_: FakeSandbox()
    )
    with pytest.raises(Exception) as excinfo:
        client.start()
    assert "E2B_API_KEY" in str(excinfo.value)
    assert config.has_e2b_key is False


def test_config_repr_hides_the_cloud_credential(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    assert "e2b-secret-key" not in repr(config)
    assert config.has_e2b_key is True


# -- configuration delivery ---------------------------------------------------


def test_sandbox_followup_uploads_and_recovers_a_private_session(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    sandbox.session_files = ("continued.jsonl",)
    sandbox.readable["/home/user/sessions/continued.jsonl"] = b'{"id":"same-session"}\n'
    runtime.session_dir.mkdir(parents=True, exist_ok=True)
    (runtime.session_dir / "continued.jsonl").write_bytes(b'{"id":"same-session"}\n')
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "report.md").write_text("# previous report", encoding="utf-8")
    client = E2BSandboxClient(config, runtime, run_id="child", workspace=workspace,
                              sandbox_factory=lambda **_: sandbox)
    client.start()
    client.upload_config()
    client.upload_continuation("continued.jsonl", workspace)
    assert sandbox.written["/home/user/sessions/continued.jsonl"] == '{"id":"same-session"}\n'
    assert sandbox.written["/home/user/workspace/report.md"] == "# previous report"
    command = client._command("追问", "", resume_session="continued.jsonl")
    assert "--session /home/user/sessions/continued.jsonl" in command
    saved, error = client.recover_session()
    assert error is None
    assert saved == runtime.session_dir / "continued.jsonl"
    assert saved.read_bytes() == b'{"id":"same-session"}\n'


def test_sandbox_session_recovery_rejects_a_path_escape(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    sandbox.session_files = ("../outside.jsonl",)
    client = E2BSandboxClient(config, runtime, run_id="root", workspace=tmp_path / "ws",
                              sandbox_factory=lambda **_: sandbox)
    client.start()
    path, error = client.recover_session()
    assert path is None and error
    assert not (tmp_path / "outside.jsonl").exists()



def test_upload_config_delivers_the_runtime_directory_with_a_manifest(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=lambda **_: sandbox
    )
    client.start()
    client.upload_config()

    assert "/home/user/config/models.json" in sandbox.written
    assert "/home/user/config/settings.json" in sandbox.written
    assert "/home/user/config/auth.json" in sandbox.written
    expected_dirs = {
        "/home/user/config", "/home/user/workspace", "/home/user/sessions",
        *(f"/home/user/config/{entry.relative_to(runtime.directory).as_posix()}"
          for entry in runtime.directory.rglob("*") if entry.is_dir()),
    }
    assert set(sandbox.dirs) == expected_dirs
    assert len(sandbox.dirs) == len(expected_dirs)

    # The manifest names every delivered path with its content digest, so the
    # delivery is verifiable instead of "the directory happened to be empty".
    manifest = dict(client.upload_manifest)
    for relative in ("models.json", "settings.json", "auth.json"):
        assert relative in manifest
        digest = hashlib.sha256((runtime.directory / relative).read_bytes()).hexdigest()
        assert manifest[relative] == digest
    assert client.uploaded, "the uploaded path list must not be empty"
    client.close()


def test_upload_manifest_covers_reviewable_template_entries(tmp_path: Path) -> None:
    """Extensions/skills/prompts are delivered with the same path semantics."""
    template = tmp_path / "template"
    (template / "extensions").mkdir(parents=True)
    (template / "settings.json").write_text('{"defaultThinkingLevel": "high"}\n', encoding="utf-8")
    (template / "AGENTS.md").write_text("# agent\n", encoding="utf-8")
    (template / "extensions" / "hello.js").write_text("export default () => {};\n", encoding="utf-8")
    (template / "prompts").mkdir()
    (template / "prompts" / "research.md").write_text("prompt\n", encoding="utf-8")
    config = make_config(tmp_path, PI_CODING_AGENT_DIR=str(template))
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=lambda **_: sandbox
    )
    client.start()
    client.upload_config()

    delivered = {relative for relative, _digest in client.upload_manifest}
    assert {"AGENTS.md", "settings.json", "extensions/hello.js", "prompts/research.md"} <= delivered
    assert "/home/user/config/extensions/hello.js" in sandbox.written
    assert "/home/user/config/prompts/research.md" in sandbox.written
    client.close()


# -- sandbox run --------------------------------------------------------------


def _manager(config, sandbox, tmp_path: Path):
    storage = RunStore(config.data_root / "runs")
    manager = RunManager(
        config, EventBus(), store=storage, sandbox_factory=lambda **_: sandbox
    )
    return manager, storage


def test_run_replays_frames_and_pulls_the_report(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    sandbox = FakeSandbox(events=_FAKE_EVENTS)
    manager, storage = _manager(config, sandbox, tmp_path)
    record = manager.start("把结果写成 report.md")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == "finished"
    assert final.report_path is not None
    assert "来自沙箱" in Path(final.report_path).read_text(encoding="utf-8")
    # The served report is the host-owned artifact copy inside the run directory.
    assert Path(final.report_path).is_relative_to(storage.run_dir(record.run_id))
    assert final.report_status == "ok"

    kinds = [json.loads(line)["type"] for line in storage.events_path(record.run_id).read_text(encoding="utf-8").splitlines() if line.strip()]
    assert "assistant_delta" in kinds
    assert "tool_started" in kinds and "tool_finished" in kinds
    assert kinds[-1] == "run_finished"
    assert sandbox.killed is True, "the sandbox must be destroyed after the run"
    # Neither the prompt nor the model credential travels through the sandbox env.
    assert "OPENAI_API_KEY" not in sandbox.command_envs
    assert "把结果写成 report.md" not in sandbox.command
    # The sandbox identity is durable for restart reconciliation.
    assert final.sandbox_id == "fake-sandbox"


def test_sandbox_followup_through_runner_keeps_session_and_run_boundaries(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    first = FakeSandbox(events=[{"type": "session", "id": "same-session"}, *_FAKE_EVENTS])
    first.session_files = ("research.jsonl",)
    first.readable["/home/user/sessions/research.jsonl"] = b'{"id":"same-session","turn":1}\n'
    second = FakeSandbox(events=[{"type": "session", "id": "same-session"}, *_FAKE_EVENTS])
    second.session_files = ("continued.jsonl",)
    second.readable["/home/user/sessions/continued.jsonl"] = b'{"id":"same-session","turn":2}\n'
    sandboxes = iter([first, second])
    storage = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store=storage, sandbox_factory=lambda **_: next(sandboxes))
    parent = manager.start("初始问题")
    original = manager.wait(parent.run_id, timeout=30)
    assert original.status == "finished" and original.session_id == "same-session"
    assert Path(original.session_file).read_bytes() == first.readable["/home/user/sessions/research.jsonl"]
    deadline = time.monotonic() + 5
    while manager.active_run_id is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.active_run_id is None
    child = manager.start("追问细节", parent_run_id=parent.run_id)
    final = manager.wait(child.run_id, timeout=30)
    assert final.status == "finished" and final.parent_run_id == parent.run_id
    assert final.report_path is None, "an unchanged seeded report is not a new deliverable"
    assert final.session_id == original.session_id
    assert "--session /home/user/sessions/continued.jsonl" in second.command
    assert second.written["/home/user/sessions/continued.jsonl"] == '{"id":"same-session","turn":1}\n'
    assert second.written["/home/user/workspace/report.md"] == "# 报告\n\n来自沙箱。\n"
    assert Path(original.session_file).read_bytes() == first.readable["/home/user/sessions/research.jsonl"]
    assert Path(final.session_file).read_bytes() == second.readable["/home/user/sessions/continued.jsonl"]
    assert _event_kinds(storage, parent.run_id)[-1] == "run_finished"
    assert _event_kinds(storage, child.run_id)[-1] == "run_finished"


def test_nonzero_exit_fails_the_run_even_with_a_report(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    sandbox = FakeSandbox(exit_code=3, report="# 仍然有报告\n")
    manager, storage = _manager(config, sandbox, tmp_path)
    record = manager.start("强制失败")
    final = manager.wait(record.run_id, timeout=30)
    assert final.status == "failed"
    assert final.error is not None and "exit" in final.error.lower()
    kinds = [json.loads(line)["type"] for line in storage.events_path(record.run_id).read_text(encoding="utf-8").splitlines() if line.strip()]
    assert kinds[-1] == "run_failed"


# -- cancellation -------------------------------------------------------------
#
# The completion audit flagged this path: stopping an E2B run closes the
# sandbox, so the in-flight command either raises or returns normally. Both
# must end as ``cancelled`` — never ``failed`` and never ``finished``.


def _event_kinds(storage: RunStore, run_id: str) -> list[str]:
    path = storage.events_path(run_id)
    return [
        json.loads(line)["type"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class BlockingHandle:
    """Handle whose command blocks until the sandbox is killed."""

    def __init__(self, sandbox: "BlockingSandbox") -> None:
        self._sandbox = sandbox

    def wait(self) -> _Result:
        self._sandbox.command_started.set()
        if not self._sandbox.released.wait(timeout=10):
            raise AssertionError("sandbox was never killed")
        if self._sandbox.raise_on_release:
            # The E2B SDK surfaces a killed sandbox as an error on the handle.
            raise SandboxError("sandbox was killed")
        return _Result(self._sandbox.exit_code)


class BlockingCommands(FakeCommands):
    def run(
        self,
        cmd: str,
        *,
        background: bool = False,
        envs: dict[str, str] | None = None,
        **_: Any,
    ) -> BlockingHandle:
        self._sandbox.command = cmd
        self._sandbox.command_envs = envs or {}
        return BlockingHandle(self._sandbox)


class BlockingSandbox(FakeSandbox):
    """FakeSandbox whose command blocks until ``kill()`` — a real E2B stop."""

    def __init__(self, *, raise_on_release: bool = True, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.command_started = threading.Event()
        self.released = threading.Event()
        self.raise_on_release = raise_on_release
        self.commands = BlockingCommands(self)

    def kill(self) -> None:
        super().kill()
        self.released.set()


@pytest.mark.parametrize("raise_on_release", [True, False])
def test_stop_during_a_sandbox_run_is_cancelled_not_failed(
    tmp_path: Path, raise_on_release: bool
) -> None:
    config = make_config(tmp_path)
    sandbox = BlockingSandbox(raise_on_release=raise_on_release, events=_FAKE_EVENTS)
    manager, storage = _manager(config, sandbox, tmp_path)
    record = manager.start("取消正在执行的沙箱 run")
    assert sandbox.command_started.wait(timeout=10), "the sandbox command never started"
    assert manager.stop(record.run_id).accepted is True

    final = manager.wait(record.run_id, timeout=30)
    assert final is not None, "the run never reached a terminal state"
    assert final.status == "cancelled", final.error
    assert final.stop_reason == "abort"
    kinds = _event_kinds(storage, record.run_id)
    assert kinds[-1] == "run_cancelled"
    assert "run_failed" not in kinds and "run_finished" not in kinds
    assert sandbox.killed is True, "a cancelled run must still destroy its sandbox"


def test_stop_that_lands_before_the_sandbox_starts_is_still_cancelled(
    tmp_path: Path,
) -> None:
    """A stop during ``Sandbox.create`` returns the sandbox and returns normally.

    ``stop()`` only closes the client once it has a sandbox; if the stop lands
    while the factory is still running, the runner must notice the cancellation
    after ``start()`` and end as cancelled without prompting.
    """
    config = make_config(tmp_path)
    sandbox = FakeSandbox(events=_FAKE_EVENTS)
    created = threading.Event()
    release = threading.Event()

    def factory(**_: Any) -> FakeSandbox:
        created.set()
        assert release.wait(timeout=10), "the factory was never released"
        return sandbox

    manager, storage = _manager(config, sandbox, tmp_path)
    manager._sandbox_factory = factory  # noqa: SLF001 - the injected face
    record = manager.start("沙箱启动前就取消")
    assert created.wait(timeout=10), "the sandbox factory was never called"
    assert manager.stop(record.run_id).accepted is True
    release.set()

    final = manager.wait(record.run_id, timeout=30)
    assert final is not None, "the run never reached a terminal state"
    assert final.status == "cancelled", final.error
    assert final.stop_reason == "abort"
    kinds = _event_kinds(storage, record.run_id)
    assert kinds[-1] == "run_cancelled"
    assert sandbox.command == "", "a cancelled run must not run the prompt"
    assert sandbox.killed is True, "a cancelled run must still destroy its sandbox"


# -- the deadline's late-write race -------------------------------------------
#
# GLINK-ACP-11 fixed this family on the ACP face: the outcome is a shared,
# mutable object, and a driver write that lands after ``wait()`` decided the
# deadline could rewrite the decided error text. The E2B pi face had no freeze:
# its ``_drive`` checkpoint (``if self._cancelled.is_set(): return``) refuses
# writes the cancellation got to see first, but a driver that already passed
# that checkpoint before the deadline was decided still writes afterwards.
# This regression holds that window open deterministically.


class LateWriteGate:
    """The window between the driver's checkpoint and its outcome write."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.open = threading.Event()


class _GatedExitCode:
    """An exit code whose ``int()`` blocks inside the late-write window.

    ``_drive`` converts the result's exit code with ``int()`` after its
    cancellation checkpoint and before writing the outcome, so blocking
    exactly there makes the race deterministic instead of hoping for a
    scheduling coincidence.
    """

    def __init__(self, code: int, gate: LateWriteGate) -> None:
        self._code = code
        self._gate = gate

    def __int__(self) -> int:
        self._gate.entered.set()
        # A bounded wait keeps a failing test from hanging the suite.
        self._gate.open.wait(timeout=10)
        return self._code

    def __eq__(self, other: object) -> bool:
        return self._code == other

    def __ne__(self, other: object) -> bool:
        return self._code != other

    def __hash__(self) -> int:
        return hash(self._code)

    def __str__(self) -> str:
        return str(self._code)


class _GatedResult:
    """The real run result with its exit code replaced by the gated one."""

    def __init__(self, result: Any, gate: LateWriteGate) -> None:
        self._result = result
        self.exit_code = _GatedExitCode(result.exit_code, gate)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._result, name)


class DeadlineRaceSandbox(FakeSandbox):
    """FakeSandbox that opens the late-write window when the deadline kills it."""

    def __init__(self, *, gate: LateWriteGate, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._gate = gate

    def kill(self) -> None:
        super().kill()
        self._gate.open.set()


def test_e2b_timeout_error_text_survives_a_late_driver_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline's error text is final; the driver's late write cannot rewrite it.

    The window is deterministic: the driver's exit code is converted with a
    gated ``int()`` that releases when the deadline path kills the sandbox, so
    the ``kernel_exit`` write lands after ``wait()`` wrote the timeout verdict.
    Without a freeze the run keeps ``stop_reason=timeout`` but its error text
    becomes the kernel-exit report.
    """
    gate = LateWriteGate()
    config = make_config(tmp_path, LOCAL_WEB_TASK_TIMEOUT_SECONDS="1")
    sandbox = DeadlineRaceSandbox(gate=gate, exit_code=1, events=_FAKE_EVENTS)
    original = E2BSandboxClient.run_prompt

    def gated_run_prompt(self: Any, *args: Any, **kwargs: Any) -> _GatedResult:
        return _GatedResult(original(self, *args, **kwargs), gate)

    monkeypatch.setattr(E2BSandboxClient, "run_prompt", gated_run_prompt)
    manager, storage = _manager(config, sandbox, tmp_path)
    record = manager.start("超时后 driver 才写完 outcome")
    # The driver passed its cancellation checkpoint and is held before its
    # outcome write, so the deadline cannot settle the run while it is held.
    assert gate.entered.wait(timeout=10), "the driver never reached the late-write window"

    final = manager.wait(record.run_id, timeout=30)
    assert final is not None
    assert (final.status, final.stop_reason) == ("failed", "timeout")
    assert "run did not settle within" in (final.error or "")
    assert sandbox.killed is True, "the deadline must still destroy its sandbox"
    assert _event_kinds(storage, record.run_id)[-1] == "run_failed"


# -- stream parsing -----------------------------------------------------------


def test_json_stream_parser_ignores_non_json_noise() -> None:
    assert parse_json_stream_line("") is None
    assert parse_json_stream_line("npm warn something") is None
    assert parse_json_stream_line("[]") is None
    frame = parse_json_stream_line('{"type": "agent_start"}')
    assert frame == {"type": "agent_start"}


def test_event_frames_are_split_on_lf_only(tmp_path: Path) -> None:
    """U+2028 inside a JSON string must not tear a frame (shared corpus)."""
    corpus = json.loads(FRAMING_FIXTURE_TEXT)
    separator = "\u2028"
    wire = "".join(
        json.dumps(frame, ensure_ascii=False) + ("\r\n" if index % 2 else "\n")
        for index, frame in enumerate(corpus["frames"])
    )
    assert separator in wire, "the corpus must contain the separator inside a string"

    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    sandbox.readable["/home/user/events.jsonl"] = wire
    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=lambda **_: sandbox
    )
    client.start()
    records = [parse_json_stream_line(line) for line in client.fetch_events()]
    assert records == corpus["frames"]
    client.close()


# -- command composition ------------------------------------------------------


def test_sandbox_command_is_shell_safe_for_hostile_prompts(tmp_path: Path) -> None:
    """Quotes, newlines, backticks, ``$(…)`` and Unicode are base64-encoded."""
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=lambda **_: FakeSandbox()
    )
    prompt = 'echo "$(rm -rf /)" `whoami` \'quote\' \\ backslash\n第二行 \u2028 line'
    command = client._command(prompt, "系统提示 with 'quotes' and $vars")  # noqa: SLF001
    assert prompt not in command
    assert "$(rm -rf /)" not in command
    assert "`whoami`" not in command
    # Both payloads survive the shell line exactly.
    encoded_prompt = base64.b64encode(prompt.encode("utf-8")).decode("ascii")
    encoded_system = base64.b64encode("系统提示 with 'quotes' and $vars".encode("utf-8")).decode("ascii")
    assert f"'{encoded_prompt}'" in command
    assert f"'{encoded_system}'" in command
    assert base64.b64decode(encoded_prompt).decode("utf-8") == prompt


def test_sandbox_command_guards_the_prebaked_packages(tmp_path: Path) -> None:
    """沙箱面同样从镜像内路径加载包；模板早于宿主/缺包时必须明确失败。

    E2B 没有 docker 的 ``--entrypoint``，所以守卫是命令链的一环：先验证
    守卫脚本存在（旧模板立刻 exit 78），再由守卫验证包存在，然后才 exec pi。
    """
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    client = E2BSandboxClient(
        config,
        runtime,
        run_id="abc123",
        workspace=tmp_path / "ws",
        sandbox_factory=lambda **_: FakeSandbox(),
    )
    command = client._command("问题", "")  # noqa: SLF001
    guard = e2b_env.SANDBOX_PACKAGE_GUARD
    assert f"test -x {guard}" in command
    assert "exit 78" in command
    assert command.index(f"test -x {guard}") < command.index(f"{guard} pi --mode json")
    settings = json.loads((runtime.directory / "settings.json").read_text(encoding="utf-8"))
    assert settings["packages"] == [
        f"{agent_env.IMAGE_PACKAGE_NODE_MODULES}/pi-web-access",
        f"{agent_env.IMAGE_PACKAGE_NODE_MODULES}/pi-subagents",
    ]


# -- artifact recovery --------------------------------------------------------


def test_sandbox_lifetime_is_bounded_by_configuration(tmp_path: Path) -> None:
    """The configured ceiling wins over the adapter's larger bound.

    E2B's SDK path exposes no CPU/memory controls, so lifetime is the one
    resource limit the host can hand the cloud provider; it never claims more
    than the configuration allows.
    """
    config = make_config(tmp_path, LOCAL_WEB_E2B_TIMEOUT_SECONDS="120")
    runtime = _prepare(config, tmp_path)
    captured: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeSandbox:
        captured.update(kwargs)
        return FakeSandbox()

    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=factory
    )
    client.start(timeout_seconds=99_999)
    assert captured["timeout"] == 120
    client.close()


def test_sandbox_lifetime_defaults_to_the_task_timeout_plus_headroom(tmp_path: Path) -> None:
    config = make_config(tmp_path, LOCAL_WEB_TASK_TIMEOUT_SECONDS="900")
    runtime = _prepare(config, tmp_path)
    captured: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeSandbox:
        captured.update(kwargs)
        return FakeSandbox()

    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=factory
    )
    client.start()
    assert captured["timeout"] == 1200
    client.close()


def test_the_kernel_command_is_allowed_to_run_for_the_whole_sandbox_lifetime(
    tmp_path: Path,
) -> None:
    """The SDK default of 60 s must never be what bounds a research run.

    A real run (retrieval + synthesis + report) takes minutes, while the sandbox
    is created with the run's full budget. Leaving ``timeout`` unset let E2B kill
    the kernel after 60 seconds and the run failed with ``TimeoutException`` even
    though the sandbox was healthy; the host therefore passes the sandbox's own
    lifetime explicitly.
    """
    config = make_config(tmp_path, LOCAL_WEB_TASK_TIMEOUT_SECONDS="900")
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=lambda **_: sandbox
    )
    client.start()
    client.run_prompt("把结果写成 report.md")

    assert sandbox.command_timeout is not None, "the SDK default (60 s) must not be left in place"
    # The command may run as long as the sandbox it runs in; never longer.
    assert sandbox.command_timeout == client.sandbox_timeout == 1200
    client.close()


def test_the_command_timeout_follows_a_tighter_configured_sandbox_ceiling(
    tmp_path: Path,
) -> None:
    config = make_config(
        tmp_path, LOCAL_WEB_TASK_TIMEOUT_SECONDS="900", LOCAL_WEB_E2B_TIMEOUT_SECONDS="300"
    )
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=lambda **_: sandbox
    )
    client.start()
    client.run_prompt("把结果写成 report.md")

    assert sandbox.command_timeout == 300
    client.close()


def test_recovered_artifacts_are_bounded_and_path_safe(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox(workspace_files=("report.md", "notes.md", "../escape.md", "huge.bin"))
    sandbox.readable["/home/user/workspace/report.md"] = "# 报告\n"
    sandbox.readable["/home/user/workspace/notes.md"] = "记录\n"
    sandbox.readable["/home/user/workspace/../escape.md"] = "越界\n"
    sandbox.readable["/home/user/workspace/huge.bin"] = b"x" * (ARTIFACT_MAX_BYTES + 1)
    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=lambda **_: sandbox
    )
    client.start()
    files, failures = client.recover_artifacts(tmp_path / "ws")

    names = {path.name for path in files}
    assert {"report.md", "notes.md"} <= names
    assert not (tmp_path / "escape.md").exists(), "a traversal name must not escape"
    assert any("非法产物名" in failure for failure in failures)
    assert any("上限" in failure for failure in failures)
    client.close()


# -- live streaming (GLINK-E2B-LIVE-01) --------------------------------------
#
# The e2b 2.x background handle *is* the streaming interface: iterating it
# yields ``(stdout, stderr, pty)`` chunks while the command runs. The old face
# was built on the misdiagnosis that no callback fires for
# ``commands.run(background=True)``, so it redirected stdout to a file and
# replayed the file once the command settled. These fakes model the real handle
# contract (chunks, end event, drop without end) and never contact E2B.

_EVENTS_PATH = "/home/user/events.jsonl"
_EXIT_PATH = "/home/user/pi-exit-code.txt"


def _record_lines(records: list[dict[str, Any]]) -> list[str]:
    """The exact LF-terminated wire records for ``records``."""
    return [json.dumps(record, ensure_ascii=False) + "\n" for record in records]


def _delivered(lines: list[str]) -> list[str]:
    """The wire records as ``on_line`` sees them: LF/CR removed."""
    return [line.removesuffix("\n").removesuffix("\r") for line in lines]


class FakeCommandExit(Exception):
    """Stand-in for the SDK's ``CommandExitException`` (carries the code)."""

    def __init__(self, exit_code: int, stderr: str) -> None:
        super().__init__(f"Command exited with code {exit_code} and error:\n{stderr}")
        self.exit_code = exit_code
        self.stderr = stderr


class StreamedHandle:
    """The e2b 2.x ``CommandHandle`` contract: iterate for output, then wait().

    ``drop_after`` makes the stream raise after that many chunks, before the
    end event, which is the "connection lost without end" case. ``gate_after``
    blocks after delivering that chunk so a test can observe the run *while the
    command is running*.
    """

    def __init__(self, sandbox: "StreamingSandbox") -> None:
        self.pid = 4242
        self._sandbox = sandbox
        self.ended = False
        self.dropped = False

    def __iter__(self):
        sandbox = self._sandbox
        for index, chunk in enumerate(sandbox.stream_chunks):
            yield chunk, None, None
            if index == 0:
                sandbox.stream_started.set()
            if sandbox.gate_after == index:
                assert sandbox.released.wait(timeout=10), "the test never released the stream"
        if sandbox.drop_after is not None and sandbox.drop_after <= len(sandbox.stream_chunks):
            self.dropped = True
            raise SandboxError("stream disconnected before the end event")
        self.ended = True

    def wait(self) -> "_Result":
        if not self.ended and not self.dropped:
            for _ in self:
                pass
        if self.dropped:
            # The SDK raises a generic error when the stream ends without an
            # end event: "Command ended without an end event".
            raise Exception("Command ended without an end event")
        if self._sandbox.exit_code != 0:
            raise FakeCommandExit(
                self._sandbox.exit_code,
                str(self._sandbox.readable.get("/home/user/pi-stderr.txt", "")),
            )
        return _Result(self._sandbox.exit_code)


class StreamingCommands(FakeCommands):
    def run(
        self,
        cmd: str,
        *,
        background: bool = False,
        envs: dict[str, str] | None = None,
        timeout: float | None = None,
        **_: Any,
    ) -> StreamedHandle:
        self._sandbox.command = cmd
        self._sandbox.command_envs = envs or {}
        self._sandbox.command_timeout = timeout
        return StreamedHandle(self._sandbox)


class StreamingSandbox(FakeSandbox):
    """FakeSandbox whose command streams stdout chunks like the real SDK."""

    def __init__(
        self,
        *,
        events_text: str | None = None,
        chunks: tuple[str | bytes, ...] = (),
        exit_code: int = 0,
        drop_after: int | None = None,
        gate_after: int | None = None,
        marker: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(exit_code=exit_code, events_text=events_text, **kwargs)
        if marker is not None:
            self.readable[_EXIT_PATH] = marker
        self.stream_chunks = chunks
        self.drop_after = drop_after
        self.gate_after = gate_after
        self.stream_started = threading.Event()
        self.released = threading.Event()
        self.commands = StreamingCommands(self)

    def kill(self) -> None:
        super().kill()
        # Killing the sandbox ends the command; a gated stream unblocks.
        self.released.set()


def test_live_frames_reach_the_event_bus_before_the_sandbox_command_ends(
    tmp_path: Path,
) -> None:
    """The behavioural red line: frames must be committed while pi still runs.

    The file-replay mechanism had produced nothing here yet, because every
    frame only arrived after ``wait()`` returned.
    """
    config = make_config(tmp_path)
    records = [
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "第一帧"},
        },
        {"type": "session", "id": "live-session"},
        {"type": "agent_settled"},
    ]
    lines = _record_lines(records)
    sandbox = StreamingSandbox(
        events_text="".join(lines),
        chunks=tuple(lines),
        gate_after=0,
    )
    sandbox.session_files = ("live.jsonl",)
    sandbox.readable["/home/user/sessions/live.jsonl"] = b'{"id":"live-session"}\n'
    manager, storage = _manager(config, sandbox, tmp_path)
    record = manager.start("实时投递")
    try:
        assert sandbox.stream_started.wait(timeout=10), "the sandbox never streamed a chunk"
        # The command is still blocked before its final chunk, so nothing has
        # settled; the first frame must already be committed regardless.
        assert "assistant_delta" in _event_kinds(storage, record.run_id)
    finally:
        sandbox.released.set()
    final = manager.wait(record.run_id, timeout=30)
    assert final.status == "finished"
    assert final.session_id == "live-session", "the streamed session frame still sets the id"
    assert _event_kinds(storage, record.run_id)[-1] == "run_finished"


def test_run_prompt_delivers_lines_while_the_command_is_still_running(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    records = [
        {"type": "session", "id": "s"},
        {"type": "agent_settled"},
    ]
    lines = _record_lines(records)
    sandbox = StreamingSandbox(events_text="".join(lines), chunks=tuple(lines), gate_after=0)
    client = E2BSandboxClient(
        config,
        _prepare(config, tmp_path),
        run_id="abc123",
        workspace=tmp_path / "ws",
        sandbox_factory=lambda **_: sandbox,
    )
    client.start()
    outcome: dict[str, Any] = {}
    seen: list[str] = []

    def drive() -> None:
        outcome["result"] = client.run_prompt("实时投递", on_line=seen.append)

    worker = threading.Thread(target=drive, daemon=True)
    worker.start()
    try:
        assert sandbox.stream_started.wait(timeout=10), "the stream never delivered a chunk"
        assert _delivered(seen) == [lines[0].removesuffix("\n")], (
            "the first record must reach on_line before the end event"
        )
    finally:
        sandbox.released.set()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert outcome["result"].exit_code == 0
    assert _delivered(seen) == _delivered(lines)
    client.close()


def test_lf_framer_splits_on_lf_only_and_survives_byte_boundaries() -> None:
    framer = e2b_env.LFLineFramer()
    frame = json.dumps(
        {"type": "message_update", "delta": "你\u2028好\u2029尾"},
        ensure_ascii=False,
    )
    wire = (frame + "\n" + '{"type": "agent_settled"}\n').encode("utf-8")
    # Cut inside the multi-byte CJK character: the decoder must reassemble it.
    cut = wire.index("你".encode("utf-8")) + 1
    assert framer.feed(wire[:cut]) == []
    records = framer.feed(wire[cut : wire.index(b"\n") + 1])
    assert records == [frame], "U+2028/U+2029 inside a JSON string are not separators"
    assert framer.feed(wire[wire.index(b"\n") + 1 :]) == ['{"type": "agent_settled"}']
    # CRLF writers leave one CR before the LF; the caller strips it.
    assert framer.feed('{"type":"x"}\r\n') == ['{"type":"x"}\r']


def test_streamed_records_survive_chunk_and_utf8_boundaries(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    records = [
        {"type": "session", "id": "s"},
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "你\u2028好"},
        },
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "跨块"},
        },
        {"type": "agent_settled"},
    ]
    wire = "".join(_record_lines(records))
    raw = wire.encode("utf-8")
    head = raw.index("你".encode("utf-8"))
    chunks = (raw[: head + 1], raw[head + 1 : head + 4], raw[head + 4 :])
    sandbox = StreamingSandbox(events_text=wire, chunks=chunks, marker="0")
    client = E2BSandboxClient(
        config,
        _prepare(config, tmp_path),
        run_id="abc123",
        workspace=tmp_path / "ws",
        sandbox_factory=lambda **_: sandbox,
    )
    client.start()
    seen: list[str] = []
    result = client.run_prompt("分块", on_line=seen.append)
    client.close()

    assert [json.loads(line) for line in seen] == records
    assert result.delivered_lines == len(records)
    assert result.exit_code == 0
    assert result.stream_error is None


def test_a_dropped_stream_is_caught_up_from_the_file_exactly_once(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    records = [
        {"type": "session", "id": "s"},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "一"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "二"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "三"}},
        {"type": "agent_settled"},
    ]
    lines = _record_lines(records)
    sandbox = StreamingSandbox(
        events_text="".join(lines),
        chunks=(lines[0], lines[1]),
        drop_after=2,
        marker="0",
    )
    client = E2BSandboxClient(
        config,
        _prepare(config, tmp_path),
        run_id="abc123",
        workspace=tmp_path / "ws",
        sandbox_factory=lambda **_: sandbox,
    )
    client.start()
    seen: list[str] = []
    result = client.run_prompt("断流补齐", on_line=seen.append)
    client.close()

    assert seen == _delivered(lines), "every record exactly once, in order"
    assert result.delivered_lines == len(lines)
    assert result.exit_code == 0
    assert result.stream_error, "the drop is still reported as a stream problem"
    # The same run without streaming delivers the same frames (no duplicate, no loss).
    assert [json.loads(line) for line in seen] == records


def test_a_dropped_stream_waits_for_the_command_then_reads_the_finished_file(
    tmp_path: Path,
) -> None:
    """A drop must not read a half-written file: wait for the command's end."""
    config = make_config(tmp_path)
    records = [
        {"type": "session", "id": "s"},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "前半"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "后半"}},
        {"type": "agent_settled"},
    ]
    lines = _record_lines(records)
    # The command is still running at the drop: only its first record exists.
    sandbox = StreamingSandbox(events_text=lines[0], chunks=(lines[0],), drop_after=1)

    def finish_command() -> None:
        time.sleep(0.1)
        sandbox.readable[_EVENTS_PATH] = "".join(lines)
        sandbox.readable[_EXIT_PATH] = "0"

    finisher = threading.Thread(target=finish_command, daemon=True)
    finisher.start()

    client = E2BSandboxClient(
        config,
        _prepare(config, tmp_path),
        run_id="abc123",
        workspace=tmp_path / "ws",
        sandbox_factory=lambda **_: sandbox,
    )
    client.start()
    seen: list[str] = []
    result = client.run_prompt("等待结束再补齐", on_line=seen.append)
    client.close()
    finisher.join(timeout=10)

    assert seen == _delivered(lines)
    assert result.exit_code == 0


def test_a_final_record_the_stream_never_terminated_comes_from_the_file(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    records = [
        {"type": "session", "id": "s"},
        {"type": "agent_settled"},
    ]
    lines = _record_lines(records)
    unterminated = lines[1].removesuffix("\n")
    sandbox = StreamingSandbox(
        events_text=lines[0] + unterminated,
        chunks=(lines[0],),
        drop_after=1,
        marker="0",
    )
    client = E2BSandboxClient(
        config,
        _prepare(config, tmp_path),
        run_id="abc123",
        workspace=tmp_path / "ws",
        sandbox_factory=lambda **_: sandbox,
    )
    client.start()
    seen: list[str] = []
    result = client.run_prompt("末行无换行", on_line=seen.append)
    client.close()

    assert [json.loads(line) for line in seen] == records
    assert result.delivered_lines == len(records)


def test_cancel_during_a_live_stream_stops_delivery(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    records = [
        {"type": "session", "id": "s"},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "已送达"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "不应送达"}},
        {"type": "agent_settled"},
    ]
    lines = _record_lines(records)
    sandbox = StreamingSandbox(
        events_text="".join(lines),
        chunks=tuple(lines),
        gate_after=1,
        marker="0",
    )
    manager, storage = _manager(config, sandbox, tmp_path)
    record = manager.start("取消实时流")
    assert sandbox.stream_started.wait(timeout=10), "the sandbox never streamed"
    assert manager.stop(record.run_id).accepted is True

    final = manager.wait(record.run_id, timeout=30)
    assert final.status == "cancelled", final.error
    assert sandbox.killed is True, "a cancelled stream still kills the sandbox"
    kinds = _event_kinds(storage, record.run_id)
    assert kinds[-1] == "run_cancelled"
    log = storage.events_path(record.run_id).read_text(encoding="utf-8")
    assert "已送达" in log, "frames delivered before the cancel stay"
    assert "不应送达" not in log, "no frame may be delivered after the cancel"


def test_a_dropped_stream_run_matches_a_full_replay_frame_for_frame(tmp_path: Path) -> None:
    records = [
        {"type": "session", "id": "s"},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "一"}},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "二"}},
        {"type": "agent_settled"},
    ]
    lines = _record_lines(records)
    config = make_config(tmp_path)
    baseline_manager, baseline_store = _manager(config, FakeSandbox(events=records), tmp_path)
    baseline = baseline_manager.start("完整回放")
    baseline_final = baseline_manager.wait(baseline.run_id, timeout=30)
    assert baseline_final.status == "finished"
    baseline_kinds = _event_kinds(baseline_store, baseline.run_id)

    sandbox = StreamingSandbox(events_text="".join(lines), chunks=(lines[0],), drop_after=1, marker="0")
    manager, storage = _manager(config, sandbox, tmp_path)
    record = manager.start("断流补齐")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == "finished", final.error
    assert _event_kinds(storage, record.run_id) == baseline_kinds, (
        "the catch-up must neither lose nor repeat a frame"
    )


def test_nonzero_exit_is_reported_as_an_exit_code_not_a_stream_error(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    records = [{"type": "agent_settled"}]
    lines = _record_lines(records)
    sandbox = StreamingSandbox(events_text="".join(lines), chunks=tuple(lines), exit_code=3, marker="3")
    sandbox.readable["/home/user/pi-stderr.txt"] = "pi exploded\n"
    client = E2BSandboxClient(
        config,
        _prepare(config, tmp_path),
        run_id="abc123",
        workspace=tmp_path / "ws",
        sandbox_factory=lambda **_: sandbox,
    )
    client.start()
    result = client.run_prompt("强制失败", on_line=lambda line: None)
    client.close()

    assert result.exit_code == 3
    assert "pi exploded" in result.stderr_tail


def test_a_nonzero_sandbox_exit_is_kernel_exit_not_execution_error(tmp_path: Path) -> None:
    """The SDK raises ``CommandExitException`` for a non-zero command exit."""
    config = make_config(tmp_path)
    lines = _record_lines([{"type": "agent_settled"}])
    sandbox = StreamingSandbox(events_text="".join(lines), chunks=tuple(lines), exit_code=7, marker="7")
    sandbox.readable["/home/user/pi-stderr.txt"] = "sdkerror\n"
    manager, storage = _manager(config, sandbox, tmp_path)
    record = manager.start("非零退出码")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == "failed"
    assert final.stop_reason == "kernel_exit", final.error
    assert final.error is not None and "exit" in final.error.lower()
    assert "sdkerror" in final.error
    assert "execution_error" not in (final.error or "")
    assert _event_kinds(storage, record.run_id)[-1] == "run_failed"


def test_sandbox_command_streams_stdout_and_records_the_pipeline_exit_code(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    client = E2BSandboxClient(
        config,
        _prepare(config, tmp_path),
        run_id="abc123",
        workspace=tmp_path / "ws",
        sandbox_factory=lambda **_: FakeSandbox(),
    )
    prompt = 'echo "$(rm -rf /)" `whoami` \'quote\' \\ backslash\n第二行 \u2028 line'
    command = client._command(prompt, "系统提示 with 'quotes' and $vars")  # noqa: SLF001

    assert command.startswith("set -o pipefail && "), "a pipeline must not swallow pi's exit code"
    assert "| tee /home/user/events.jsonl" in command, "stdout must stream *and* persist"
    assert "2> /home/user/pi-stderr.txt" in command
    assert 'rc=$?' in command and "> /home/user/pi-exit-code.txt" in command
    assert 'exit "$rc"' in command, "the command's own exit code must survive the marker write"
    # The base64 contract is unchanged: no prompt text reaches the shell line.
    assert prompt not in command
    assert "$(rm -rf /)" not in command
    assert "`whoami`" not in command
    assert f"'{base64.b64encode(prompt.encode('utf-8')).decode('ascii')}'" in command


# -- bounded reads and fail-closed catch-up (GLINK-INTEG-01) -------------------
#
# The sandbox's event file is the completeness authority when the live stream
# dropped; an unreadable or oversized file must make the run unresolved rather
# than produce a conclusion nobody can prove. Every sandbox read is bounded by
# entry metadata (or a streamed read) *before* a byte is loaded.


def test_a_dropped_stream_with_an_unreadable_event_file_fails_the_run(
    tmp_path: Path,
) -> None:
    """The file decided the outcome and could not be read: fail, not finish."""
    config = make_config(tmp_path)
    records = [
        {"type": "session", "id": "s"},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "一"}},
        {"type": "agent_settled"},
    ]
    lines = _record_lines(records)
    sandbox = StreamingSandbox(
        events_text="".join(lines),
        chunks=(lines[0],),
        drop_after=1,
        marker="0",
    )
    del sandbox.readable[_EVENTS_PATH]  # the drop means only the file could prove the rest

    manager, storage = _manager(config, sandbox, tmp_path)
    record = manager.start("断流且补读失败")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == "failed"
    assert final.stop_reason == "execution_error", final.stop_reason
    assert final.error and "补读失败" in final.error
    assert _event_kinds(storage, record.run_id)[-1] == "run_failed"


def test_an_oversized_event_file_fails_closed_without_loading_it(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    records = [{"type": "session", "id": "s"}, {"type": "agent_settled"}]
    lines = _record_lines(records)
    sandbox = StreamingSandbox(
        events_text="".join(lines),
        chunks=(lines[0],),
        drop_after=1,
        marker="0",
    )
    sandbox.sizes[_EVENTS_PATH] = EVENTS_MAX_BYTES + 1  # the metadata says "too big"
    sandbox.reads.clear()

    client = E2BSandboxClient(
        config,
        _prepare(config, tmp_path),
        run_id="abc123",
        workspace=tmp_path / "ws",
        sandbox_factory=lambda **_: sandbox,
    )
    client.start()
    with pytest.raises(EventCatchUpFailed) as refusal:
        client.run_prompt("断流且事件文件超限")
    client.close()

    assert "超过上限" in str(refusal.value) or "补读失败" in str(refusal.value)
    assert _EVENTS_PATH not in sandbox.reads, "超限的事件文件必须在读取前被拒绝"


def test_a_healthy_stream_does_not_need_the_event_file(tmp_path: Path) -> None:
    """A stream that ended on its own proved the timeline; the file is redundant."""
    config = make_config(tmp_path)
    records = [
        {"type": "session", "id": "s"},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "完整"}},
        {"type": "agent_settled"},
    ]
    lines = _record_lines(records)
    sandbox = StreamingSandbox(
        events_text="".join(lines), chunks=tuple(lines), marker="0"
    )
    del sandbox.readable[_EVENTS_PATH]

    client = E2BSandboxClient(
        config,
        _prepare(config, tmp_path),
        run_id="abc123",
        workspace=tmp_path / "ws",
        sandbox_factory=lambda **_: sandbox,
    )
    client.start()
    seen: list[str] = []
    result = client.run_prompt("健康流", on_line=seen.append)
    client.close()

    assert seen == _delivered(lines)
    assert result.exit_code == 0 and result.stream_error is None
    assert result.delivered_lines == len(lines)


def test_artifacts_are_bounded_by_listing_metadata_before_reading(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    sandbox = FakeSandbox(workspace_files=("report.md", "huge.bin"))
    sandbox.readable["/home/user/workspace/report.md"] = "# 报告\n"
    sandbox.readable["/home/user/workspace/huge.bin"] = b"small"
    sandbox.sizes["/home/user/workspace/huge.bin"] = ARTIFACT_MAX_BYTES + 1  # metadata lies, read must not
    sandbox.reads.clear()
    client = E2BSandboxClient(
        config,
        _prepare(config, tmp_path),
        run_id="abc123",
        workspace=tmp_path / "ws",
        sandbox_factory=lambda **_: sandbox,
    )
    client.start()
    files, failures = client.recover_artifacts(tmp_path / "ws")
    client.close()

    assert {path.name for path in files} == {"report.md"}
    assert any("上限" in failure for failure in failures)
    assert "/home/user/workspace/huge.bin" not in sandbox.reads


def test_a_sandbox_without_size_metadata_or_streaming_refuses_the_read(
    tmp_path: Path,
) -> None:
    """Neither bound available: refuse the load instead of guessing."""
    config = make_config(tmp_path)
    sandbox = FakeSandbox()
    sandbox.readable[_EVENTS_PATH] = "{}"

    class PlainFiles:
        def read(self, path: str) -> bytes:
            raise AssertionError("不存在上限依据时不得整份载入沙箱文件")

    sandbox.files = PlainFiles()  # type: ignore[assignment]
    client = E2BSandboxClient(
        config,
        _prepare(config, tmp_path),
        run_id="abc123",
        workspace=tmp_path / "ws",
        sandbox_factory=lambda **_: sandbox,
    )
    client.start()
    try:
        assert client.fetch_event_lines() == []
        with pytest.raises(EventCatchUpFailed) as refusal:
            client.fetch_event_lines(required=True)
        assert "拒绝整份载入" in str(refusal.value)
    finally:
        client.close()


def test_the_stderr_tail_stays_empty_when_the_read_is_refused(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    sandbox = FakeSandbox()
    sandbox.sizes["/home/user/pi-stderr.txt"] = STDERR_MAX_BYTES + 1
    sandbox.readable["/home/user/pi-stderr.txt"] = "trace"
    sandbox.reads.clear()
    client = E2BSandboxClient(
        config,
        _prepare(config, tmp_path),
        run_id="abc123",
        workspace=tmp_path / "ws",
        sandbox_factory=lambda **_: sandbox,
    )
    client.start()
    try:
        assert client.fetch_stderr_tail() == ""
        assert "/home/user/pi-stderr.txt" not in sandbox.reads
    finally:
        client.close()


def _recover_from(sandbox: FakeSandbox, tmp_path: Path):
    config = make_config(tmp_path)
    client = E2BSandboxClient(
        config,
        _prepare(config, tmp_path),
        run_id="abc123",
        workspace=tmp_path / "ws",
        sandbox_factory=lambda **_: sandbox,
    )
    client.start()
    try:
        return client.recover_artifacts(tmp_path / "ws")
    finally:
        client.close()


def test_directories_and_symlinks_are_skipped_without_a_false_failure(tmp_path: Path) -> None:
    """The audit blocker: a directory entry must not become an artifact failure.

    The real SDK reports ``EntryInfo.type`` as the ``FileType`` enum, so an
    agent that wrote a ``notes/`` directory (very common) must not turn every
    E2B run's terminal record into a false alarm.
    """
    sandbox = FakeSandbox(workspace_files=("report.md", "notes", "linked.md"))
    sandbox.readable["/home/user/workspace/report.md"] = "# 报告\n"
    sandbox.entry_types["/home/user/workspace/notes"] = "dir"
    sandbox.entry_types["/home/user/workspace/linked.md"] = "symlink"
    sandbox.reads.clear()

    files, failures = _recover_from(sandbox, tmp_path)

    assert {path.name for path in files} == {"report.md"}
    assert failures == [], "目录/符号链接不是产物的失败，不得写进终态 artifactFailures"
    assert "/home/user/workspace/notes" not in sandbox.reads, "目录不得被读取"
    assert "/home/user/workspace/linked.md" not in sandbox.reads, "链接不得被跟随"


def test_the_real_sdk_file_type_enum_is_understood(tmp_path: Path) -> None:
    e2b = pytest.importorskip("e2b")
    from e2b import FileType  # noqa: PLC0415 - optional dependency

    assert FileType.DIR.value == "dir" and str(FileType.DIR) == "FileType.DIR"
    sandbox = FakeSandbox(workspace_files=("report.md", "notes", "linked.md"))
    sandbox.readable["/home/user/workspace/report.md"] = "# 报告\n"
    sandbox.entry_types["/home/user/workspace/notes"] = FileType.DIR
    sandbox.entry_types["/home/user/workspace/linked.md"] = FileType.SYMLINK

    files, failures = _recover_from(sandbox, tmp_path)

    assert {path.name for path in files} == {"report.md"}
    assert failures == []
    assert sandbox.reads.count("/home/user/workspace/report.md") >= 1
    assert "/home/user/workspace/notes" not in sandbox.reads
    assert e2b is not None
