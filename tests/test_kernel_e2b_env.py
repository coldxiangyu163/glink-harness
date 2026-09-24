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
from pi_glink.kernel import agent_env
from pi_glink.kernel.e2b_env import (
    ARTIFACT_MAX_BYTES,
    E2BSandboxClient,
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


class FakeFiles:
    def __init__(self, sandbox: "FakeSandbox") -> None:
        self._sandbox = sandbox

    def make_dir(self, path: str) -> None:
        self._sandbox.dirs.append(path)

    def write(self, path: str, content: Any) -> None:
        text = content.decode("utf-8") if isinstance(content, bytes) else str(content)
        self._sandbox.written[path] = text

    def read(self, path: str) -> bytes:
        if path in self._sandbox.readable:
            value = self._sandbox.readable[path]
            return value if isinstance(value, bytes) else value.encode("utf-8")
        raise FileNotFoundError(path)

    def list(self, path: str) -> list[Any]:
        class Entry:
            def __init__(self, name: str) -> None:
                self.name = name
                self.path = f"{path}/{name}"

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
        if events is not None:
            self.readable["/home/user/events.jsonl"] = "\n".join(
                json.dumps(event) for event in events
            )
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
