"""Regressions for the E2B execution face (no cloud calls).

They pin the three behaviours the completion audit flagged:

* the credential read from ``.env`` reaches ``Sandbox.create`` (``api_key``);
* a run replays its JSONL frames and pulls the report back into the host workspace;
* a non-zero sandbox exit is a failure even when a report file exists.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from pi_glink.config import build_config
from pi_glink.events import EventBus
from pi_glink.kernel import agent_env
from pi_glink.kernel.e2b_env import E2BSandboxClient, SandboxError, parse_json_stream_line
from pi_glink.kernel.runner import RunManager
from pi_glink.store import RunStore

REPO_ROOT = Path(__file__).resolve().parents[1]

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

    def write(self, path: str, content: str) -> None:
        self._sandbox.written[path] = content

    def read(self, path: str) -> bytes:
        if path in self._sandbox.readable:
            return self._sandbox.readable[path].encode("utf-8")
        raise FileNotFoundError(path)

    def list(self, path: str) -> list[Any]:
        class Entry:
            def __init__(self, name: str) -> None:
                self.name = name
                self.path = f"{path}/{name}"

        return [Entry(name) for name in self._sandbox.workspace_files]


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

    def run(self, cmd: str, *, background: bool = False, envs: dict[str, str] | None = None, **_: Any) -> _Handle:
        self._sandbox.command = cmd
        self._sandbox.command_envs = envs or {}
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
        self.readable: dict[str, str] = {}
        self.workspace_files = workspace_files
        self.command = ""
        self.command_envs: dict[str, str] = {}
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
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


# -- credential plumbing ------------------------------------------------------


def test_api_key_from_env_reaches_sandbox_create(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime = agent_env.prepare_agent_dir(config)
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
    runtime = agent_env.prepare_agent_dir(config)
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


# -- sandbox run --------------------------------------------------------------


def test_upload_config_includes_models_settings_and_search_credential(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime = agent_env.prepare_agent_dir(config)
    sandbox = FakeSandbox()
    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=lambda **_: sandbox
    )
    client.start()
    client.upload_config()
    assert "/home/user/config/models.json" in sandbox.written
    assert "/home/user/config/settings.json" in sandbox.written
    assert "/home/user/config/auth.json" in sandbox.written
    assert len(sandbox.dirs) == 3
    client.close()


def test_run_replays_frames_and_pulls_the_report(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime = agent_env.prepare_agent_dir(config)
    sandbox = FakeSandbox(events=_FAKE_EVENTS)
    storage = RunStore(config.data_root / "runs")
    bus = EventBus()
    manager = RunManager(
        config, bus, store=storage, sandbox_factory=lambda **_: sandbox
    )
    record = manager.start("把结果写成 report.md")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == "finished"
    assert final.report_path is not None
    assert "来自沙箱" in Path(final.report_path).read_text(encoding="utf-8")

    kinds = [json.loads(line)["type"] for line in storage.events_path(record.run_id).read_text(encoding="utf-8").splitlines() if line.strip()]
    assert "assistant_delta" in kinds
    assert "tool_started" in kinds and "tool_finished" in kinds
    assert kinds[-1] == "run_finished"
    assert sandbox.killed is True, "the sandbox must be destroyed after the run"
    # Neither the prompt nor the model credential travels through the sandbox env.
    assert "OPENAI_API_KEY" not in sandbox.command_envs
    assert "把结果写成 report.md" not in sandbox.command


def test_nonzero_exit_fails_the_run_even_with_a_report(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    sandbox = FakeSandbox(exit_code=3, report="# 仍然有报告\n")
    storage = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store=storage, sandbox_factory=lambda **_: sandbox)
    record = manager.start("强制失败")
    final = manager.wait(record.run_id, timeout=30)
    assert final.status == "failed"
    assert final.error is not None and "exit" in final.error.lower()
    kinds = [json.loads(line)["type"] for line in storage.events_path(record.run_id).read_text(encoding="utf-8").splitlines() if line.strip()]
    assert kinds[-1] == "run_failed"


# -- stream parsing -----------------------------------------------------------


def test_json_stream_parser_ignores_non_json_noise() -> None:
    assert parse_json_stream_line("") is None
    assert parse_json_stream_line("npm warn something") is None
    assert parse_json_stream_line("[]") is None
    frame = parse_json_stream_line('{"type": "agent_start"}')
    assert frame == {"type": "agent_start"}
