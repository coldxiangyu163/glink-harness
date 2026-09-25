"""ACP family on the E2B sandbox face (GLINK-ACP-04 / M3b), without a cloud call.

Three obligations are pinned here:

* **composition** — ``Sandbox.create`` receives the frozen ACP template and
  credential, the run's rendered ``HOME`` (credentials at 0600) is uploaded into
  the sandbox state root, and the agent command is the frozen argv running in
  the sandbox workspace with ``stdin=True`` as a background command;
* **bridging** — the sandbox command handle is handed to the *same*
  :class:`~pi_glink.kernel.acp_rpc.AcpRpcClient` the in-process and Docker faces
  use (the real fake ACP agent runs as a local subprocess behind a stand-in
  ``CommandHandle``), so ``session/new``'s ``cwd`` is the sandbox path, the run
  settles with a stop reason, artifacts are recovered before destruction and the
  sandbox identity is durable;
* **credential boundary** — the injected credential value reaches the sandbox
  only as a 0600 file: never the command line, never the command environment,
  never the event log.

A real sandbox run is covered separately by the task's live evidence.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from pi_glink.config import ConfigError, build_config
from pi_glink.events import EventBus
from pi_glink.kernel.acp_env import prepare_acp_runtime
from pi_glink.kernel.acp_execution import AcpSandboxExecutionAdapter
from pi_glink.kernel.e2b_env import (
    SANDBOX_ACP_HOME,
    SANDBOX_WORKSPACE,
    E2BAcpSandboxClient,
    SandboxCommandProcess,
)
from pi_glink.kernel.execution import ExecutionSpec, build_adapter
from pi_glink.kernel.rpc import PiRpcClient
from pi_glink.kernel.runner import RunManager
from pi_glink.store import FAILED, FINISHED, RunStore

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_ACP = Path(__file__).resolve().parent / "fixtures" / "fake_acp.py"
DOCKERFILE = REPO_ROOT / "docker" / "e2b" / "Dockerfile.acp-e2b"
NOTES = REPO_ROOT / "docs" / "acp-integration-notes.md"


# -- fakes --------------------------------------------------------------------


def translate(sandbox_path: str, root: Path) -> Path:
    """Map one ``/home/user/…`` sandbox path onto the fake sandbox's real tree."""
    relative = sandbox_path.lstrip("/")
    if not sandbox_path.startswith("/home/user"):
        raise AssertionError(f"unexpected sandbox path: {sandbox_path}")
    return root / relative


class _Info:
    def __init__(self, size: int) -> None:
        self.size = size


class _Stream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    def __iter__(self) -> Any:
        return iter(self._chunks)

    def close(self) -> None:
        pass


class _Entry:
    def __init__(self, sandbox_path: str, root: Path) -> None:
        real = translate(sandbox_path, root)
        self.name = real.name
        self.path = sandbox_path
        self.type = "dir" if real.is_dir() else "file"
        self.size = 0 if real.is_dir() else real.stat().st_size


class FakeFiles:
    def __init__(self, sandbox: "FakeSandbox") -> None:
        self._sandbox = sandbox

    def _real(self, sandbox_path: str) -> Path:
        return translate(sandbox_path, self._sandbox.root)

    def make_dir(self, path: str, **_kwargs: Any) -> bool:
        real = self._real(path)
        existed = real.is_dir()
        real.mkdir(parents=True, exist_ok=True)
        self._sandbox.made_dirs.append(path)
        return not existed

    def write(self, path: str, content: Any, **_kwargs: Any) -> None:
        if self._sandbox.write_failure is not None:
            raise OSError(self._sandbox.write_failure)
        real = self._real(path)
        real.parent.mkdir(parents=True, exist_ok=True)
        data = content if isinstance(content, bytes) else str(content).encode("utf-8")
        real.write_bytes(data)
        self._sandbox.written[path] = data

    def read(self, path: str, format: str | None = None, **_kwargs: Any) -> Any:
        self._sandbox.reads.append(path)
        raw = self._real(path).read_bytes()
        if format == "stream":
            return _Stream([raw])
        return raw

    def get_info(self, path: str, **_kwargs: Any) -> _Info:
        return _Info(self._real(path).stat().st_size)

    def list(self, path: str, **_kwargs: Any) -> list[_Entry]:
        self._sandbox.listed.append(path)
        real = self._real(path)
        if not real.is_dir():
            raise FileNotFoundError(path)
        return [_Entry(f"{path.rstrip('/')}/{child.name}", self._sandbox.root) for child in sorted(real.iterdir())]


class _Result:
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code
        self.stdout = ""
        self.stderr = ""


class _ExitException(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(f"command exited with {code}")
        self.exit_code = code


class FakeAgentHandle:
    """Stand-in for the SDK's ``CommandHandle`` over a real subprocess.

    Implements the surface the E2B face uses: ``send_stdin``/``close_stdin`` for
    input, iteration for streaming stdout/stderr chunks, ``kill`` and ``wait``.
    The chunks are deliberately split at a small size so the shared LF-only
    framer is exercised the way a real sandbox stream exercises it.
    """

    def __init__(self, process: subprocess.Popen[bytes], *, chunk_size: int = 64) -> None:
        self._process = process
        self._chunk_size = chunk_size
        self.pid = process.pid

    def __iter__(self) -> Any:
        assert self._process.stdout is not None
        while True:
            chunk = self._process.stdout.read(self._chunk_size)
            if not chunk:
                break
            yield chunk.decode("utf-8", "replace"), None, None
        assert self._process.stderr is not None
        for chunk in iter(lambda: self._process.stderr.read(self._chunk_size), b""):
            if chunk:
                yield None, chunk.decode("utf-8", "replace"), None

    def send_stdin(self, data: Any, request_timeout: float | None = None) -> None:
        assert self._process.stdin is not None
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        self._process.stdin.write(payload)
        self._process.stdin.flush()

    def close_stdin(self, request_timeout: float | None = None) -> None:
        assert self._process.stdin is not None
        self._process.stdin.close()

    def kill(self) -> bool:
        self._process.kill()
        return True

    def wait(self) -> _Result:
        code = self._process.wait()
        if code != 0:
            raise _ExitException(code)
        return _Result(code)


class FakeCommands:
    def __init__(self, sandbox: "FakeSandbox") -> None:
        self._sandbox = sandbox

    def run(self, cmd: str, **kwargs: Any) -> Any:
        self._sandbox.command = cmd
        self._sandbox.command_kwargs = dict(kwargs)
        if kwargs.get("background"):
            self._sandbox.agent_envs = dict(kwargs.get("envs") or {})
            self._sandbox.agent_cwd = kwargs.get("cwd")
            return self._spawn_agent(cmd, kwargs)
        self._sandbox.harden_command = cmd
        self._sandbox.harden_commands.append(cmd)
        # chmod and friends: the fake has no permissions to change.
        return _Result(0)

    def _spawn_agent(self, cmd: str, kwargs: dict[str, Any]) -> FakeAgentHandle:
        sandbox = self._sandbox
        if "&& exec " not in cmd:
            raise AssertionError(f"agent command is not the documented shape: {cmd!r}")
        argv = shlex.split(cmd.split("&& exec ", 1)[1])
        env = dict(os.environ)
        env.update({name: value for name, value in (kwargs.get("envs") or {}).items()})
        # The sandbox's HOME is a path in the sandbox, not on this host: map it
        # onto the fake tree so the agent can really write its state.
        if "HOME" in env:
            env["HOME"] = str(translate(env["HOME"], sandbox.root))
        cwd = translate(kwargs.get("cwd") or SANDBOX_WORKSPACE, sandbox.root)
        cwd.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        sandbox.agent_argv = argv
        sandbox.agent_process = process
        return FakeAgentHandle(process)


class FakeSandbox:
    """Minimal stand-in for ``e2b.Sandbox`` backed by a real temporary tree."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        sandbox_id: str = "fake-sandbox-id",
        report: str | None = "# 沙箱报告\n\n来自 ACP E2B 面。\n",
        write_failure: str | None = None,
    ) -> None:
        self.root = tmp_path / "sandbox-root"
        self.root.mkdir(parents=True, exist_ok=True)
        self.sandbox_id = sandbox_id
        self.killed = False
        self.written: dict[str, bytes] = {}
        self.made_dirs: list[str] = []
        self.reads: list[str] = []
        self.listed: list[str] = []
        self.create_kwargs: dict[str, Any] = {}
        self.command = ""
        self.command_kwargs: dict[str, Any] = {}
        self.harden_command = ""
        self.harden_commands: list[str] = []
        self.agent_envs: dict[str, str] = {}
        self.agent_cwd: str | None = None
        self.agent_argv: list[str] = []
        self.agent_process: subprocess.Popen[bytes] | None = None
        self.write_failure = write_failure
        self.files = FakeFiles(self)
        self.commands = FakeCommands(self)
        if report is not None:
            workspace = translate(SANDBOX_WORKSPACE, self.root)
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "report.md").write_text(report, encoding="utf-8")

    def kill(self) -> bool:
        if self.agent_process is not None and self.agent_process.poll() is None:
            self.agent_process.kill()
        self.killed = True
        return True


# -- helpers ------------------------------------------------------------------


def make_config(tmp_path: Path, **overrides: str):
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_EXECUTOR": "e2b",
        "E2B_API_KEY": "e2b-secret-key",
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent-web-search.json"),
        "PI_GLINK_HARNESS": "acp",
        "PI_GLINK_ACP_COMMAND": "dsh --profile acp",
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


def make_runtime(config, tmp_path: Path, name: str = "r1"):
    return prepare_acp_runtime(config, runtime_dir=tmp_path / name / "acp-agent")


def fake_agent_command(tmp_path: Path, settings: dict[str, Any], *, name: str = "settings.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
    return (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_ACP))}"
        f" --config {shlex.quote(str(path))}"
    )


def event_kinds(store: RunStore, run_id: str) -> list[str]:
    path = store.events_path(run_id)
    return [
        json.loads(line)["type"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def make_sandbox_client(
    config, runtime, tmp_path: Path, sandbox: FakeSandbox, *, workspace: Path | None = None
) -> E2BAcpSandboxClient:
    captured: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeSandbox:
        captured.update(kwargs)
        sandbox.create_kwargs = dict(kwargs)
        return sandbox

    return E2BAcpSandboxClient(
        config,
        runtime,
        run_id="abc123",
        workspace=workspace or (tmp_path / "ws"),
        sandbox_factory=factory,
    )


def make_spec(config, runtime, tmp_path: Path, *, prompt: str = "写一份报告") -> ExecutionSpec:
    return ExecutionSpec(
        config=config,
        runtime=runtime,
        run_id="abc123",
        prompt=prompt,
        system_prompt="",
        workspace=tmp_path / "ws",
        timeout_seconds=30,
    )


# -- composition --------------------------------------------------------------


def test_the_sandbox_uses_the_frozen_acp_template_and_credential(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        PI_GLINK_ACP_CREDENTIAL_FILES=f"{tmp_path / 'operator.json'}=.codex/auth.json",
    )
    (tmp_path / "operator.json").write_text('{"tokens": {"access_token": "opaque-1234567890"}}', encoding="utf-8")
    runtime = make_runtime(config, tmp_path)
    sandbox = FakeSandbox(tmp_path)
    client = make_sandbox_client(config, runtime, tmp_path, sandbox)

    client.start(timeout_seconds=120)
    created = sandbox.create_kwargs
    assert created["template"] == "glink-acp-worker"
    assert created["template"] != config.e2b_template, "ACP 模板与 pi 模板是两个旋钮"
    assert created["api_key"] == "e2b-secret-key"
    assert created["timeout"] == 120
    # The ACP sandbox has no pi runtime directory to deliver; the model key is
    # not part of any create-time environment.
    assert created["envs"] == {}
    assert client.sandbox_id == "fake-sandbox-id"
    client.close()
    assert sandbox.killed is True


def test_missing_credential_is_refused_before_any_cloud_call(tmp_path: Path) -> None:
    config = make_config(tmp_path, E2B_API_KEY="")
    runtime = make_runtime(config, tmp_path)
    client = E2BAcpSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws",
        sandbox_factory=lambda **_: FakeSandbox(tmp_path),
    )
    with pytest.raises(ConfigError) as excinfo:
        client.start()
    assert "E2B_API_KEY" in str(excinfo.value)


def test_credentials_are_uploaded_into_the_run_home_and_hardened(tmp_path: Path) -> None:
    source = tmp_path / "operator" / ".credentials.yaml"
    source.parent.mkdir()
    source.write_text("DEEPSEEK_API_KEY: sk-opaque-credential-1234567890\n", encoding="utf-8")
    config = make_config(
        tmp_path,
        PI_GLINK_ACP_CREDENTIAL_FILES=f"{source}=.dsh/.credentials.yaml",
    )
    runtime = make_runtime(config, tmp_path)
    sandbox = FakeSandbox(tmp_path)
    client = make_sandbox_client(config, runtime, tmp_path, sandbox)
    client.start()
    client.upload_home()

    target = f"{SANDBOX_ACP_HOME}/.dsh/.credentials.yaml"
    assert sandbox.written[target] == source.read_bytes()
    assert (f".dsh/.credentials.yaml",) == tuple(rel for rel, _digest in client.upload_manifest)
    # The boundary is re-applied inside the sandbox: the state root is 0700
    # *before* the upload (nothing lands in a traversable directory), then the
    # directories 0700 and the files 0600 — and only this run's paths appear.
    assert sandbox.harden_commands[0] == f"chmod 0700 {SANDBOX_ACP_HOME}"
    assert sandbox.harden_commands[-1].startswith("chmod 0600 ")
    assert target in sandbox.harden_commands[-1]
    assert shlex.split(sandbox.harden_commands[1])[1:4] == [
        "0700",
        SANDBOX_ACP_HOME,
        f"{SANDBOX_ACP_HOME}/.dsh",
    ]
    assert "sk-opaque-credential-1234567890" not in " ".join(sandbox.harden_commands)
    # The value stays in the file; the manifest makes delivery verifiable.
    assert client.upload_manifest[0][1]
    client.close()


def test_the_agent_command_is_the_frozen_argv_in_the_sandbox_workspace(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        PI_GLINK_ACP_COMMAND=fake_agent_command(
            tmp_path, {"FAKE_ACP_LOG": str(tmp_path / "agent.jsonl")}
        ),
        PI_GLINK_ACP_ENV="ACME_MODE=sandbox-mode-value",
    )
    runtime = make_runtime(config, tmp_path)
    sandbox = FakeSandbox(tmp_path)
    client = make_sandbox_client(config, runtime, tmp_path, sandbox)
    client.start()
    assert client.argv == shlex.split(config.acp_command)
    assert client.command() == f"cd {SANDBOX_WORKSPACE} && exec {shlex.join(client.argv)}"
    # HOME is the run's sandbox state root; extra entries are the explicit
    # process-environment channel (values never enter the command line).
    assert client.command_env() == {"HOME": SANDBOX_ACP_HOME, "ACME_MODE": "sandbox-mode-value"}
    assert "sandbox-mode-value" not in client.command()

    client.spawn()
    assert sandbox.command == client.command()
    assert sandbox.command_kwargs["background"] is True
    assert sandbox.command_kwargs["stdin"] is True
    assert sandbox.command_kwargs["cwd"] == SANDBOX_WORKSPACE
    # The command's lifetime is bounded by the sandbox's own lifetime.
    assert sandbox.command_kwargs["timeout"] == sandbox.create_kwargs["timeout"]
    # The fake executed exactly what the client asked for (the fake parses the
    # command line back into an argv), so quoting cannot silently change it.
    assert sandbox.agent_argv == client.argv
    client.close()


def test_an_argv_argument_with_spaces_stays_one_argument(tmp_path: Path) -> None:
    config = make_config(tmp_path, PI_GLINK_ACP_COMMAND="agent --config '/tmp/a b/c.json'")
    runtime = make_runtime(config, tmp_path)
    client = make_sandbox_client(config, runtime, tmp_path, FakeSandbox(tmp_path))
    assert client.command() == f"cd {SANDBOX_WORKSPACE} && exec agent --config '/tmp/a b/c.json'"


# -- bridging (real fake ACP agent behind a stand-in handle) ------------------


def build_acp_e2b_adapter(config, runtime, tmp_path: Path, sandbox: FakeSandbox):
    client = make_sandbox_client(config, runtime, tmp_path, sandbox)
    frames: list[dict[str, Any]] = []
    identities: list[dict[str, str]] = []
    adapter = build_adapter(
        make_spec(config, runtime, tmp_path),
        harness="acp",
        executor="e2b",
        client_factory=PiRpcClient,
        on_frame=frames.append,
        on_identity=identities.append,
        rpc_plan=lambda _spec: ([], {}, None),
        docker_plan=lambda _spec: ([], {}, "container"),
        docker_stop=lambda _name: [],
        acp_plan=lambda _spec: ([], {}, None),
        acp_e2b_plan=lambda _spec: client,
    )
    return adapter, frames, identities, client


def test_the_agent_is_bridged_over_the_sandbox_handle(tmp_path: Path) -> None:
    settings = {"sessionId": "sandbox-session", "log": str(tmp_path / "agent.jsonl")}
    config = make_config(
        tmp_path,
        PI_GLINK_ACP_COMMAND=fake_agent_command(tmp_path, settings),
    )
    runtime = make_runtime(config, tmp_path)
    sandbox = FakeSandbox(tmp_path)
    adapter, frames, identities, client = build_acp_e2b_adapter(config, runtime, tmp_path, sandbox)
    assert isinstance(adapter, AcpSandboxExecutionAdapter)

    adapter.start()
    # The sandbox identity is handed over as soon as the sandbox exists.
    assert identities == [{"sandbox": "fake-sandbox-id"}]
    session = adapter.session_info()
    assert session is not None
    payload = session.payload
    assert payload["harness"] == "acp"
    assert payload["protocolVersion"] == 1
    assert payload["agentInfo"]["name"] == "fake-acp"
    # The agent-visible workspace is the sandbox path, never a host path.
    assert payload["cwd"] == SANDBOX_WORKSPACE
    assert payload["sandboxId"] == "fake-sandbox-id"
    assert payload["template"] == "glink-acp-worker"

    try:
        adapter.deliver_input()
        outcome = adapter.wait(cancel=lambda: False, deadline=time.monotonic() + 60)
        assert outcome.settled is True, outcome.error
        assert outcome.cancelled is False
        # The turn boundaries the projector needs are produced on this face too.
        kinds = [frame.get("type") for frame in frames]
        assert "acp_turn_started" in kinds and "acp_turn_finished" in kinds
        assert any(frame.get("method") == "session/update" for frame in frames)
        prompt_result = [frame for frame in frames if frame.get("type") == "acp_prompt_result"]
        assert prompt_result and prompt_result[-1]["stopReason"] == "end_turn"

        # The agent's HOME is the sandbox state root: the fake writes its state
        # through the mapped path, proving the env plumbing reached the agent.
        assert sandbox.agent_envs["HOME"] == SANDBOX_ACP_HOME
        assert (translate(SANDBOX_ACP_HOME, sandbox.root) / "fake-acp-state.json").is_file()

        artifacts = adapter.recover_artifacts(tmp_path / "host-ws")
        assert [path.name for path in artifacts.files] == ["report.md"]
        assert (tmp_path / "host-ws" / "report.md").read_text(encoding="utf-8").startswith("# 沙箱报告")
    finally:
        report = adapter.close()
    assert report.released is True, report.failures
    assert report.external == {"sandbox": "fake-sandbox-id"}
    assert adapter.identity() == {"sandbox": "fake-sandbox-id"}
    assert sandbox.killed is True
    assert sandbox.agent_process is not None and sandbox.agent_process.poll() is not None


def test_the_bridge_wraps_the_handle_as_a_subprocess(tmp_path: Path) -> None:
    """``SandboxCommandProcess`` presents the surface the ACP client uses."""
    script = tmp_path / "echo.py"
    script.write_text(
        "import sys\n"
        "for line in sys.stdin.buffer:\n"
        "    sys.stdout.buffer.write(line)\n"
        "    sys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    config = make_config(tmp_path, PI_GLINK_ACP_COMMAND=f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")
    runtime = make_runtime(config, tmp_path)
    sandbox = FakeSandbox(tmp_path, report=None)
    client = make_sandbox_client(config, runtime, tmp_path, sandbox)
    client.start()
    process = client.spawn()
    assert isinstance(process, SandboxCommandProcess)
    try:
        assert process.poll() is None
        process.stdin.write(b"first\nsecond\n")
        assert process.stdout.read() == b"first\nsecond\n"
        # EOF signal, then the real exit code; the client reads both the way it
        # reads a subprocess.
        process.stdin.close()
        assert process.wait(timeout=10) == 0
        assert process.poll() == 0
        assert process.stdout.read() == b""
    finally:
        client.close()
    assert sandbox.killed is True


def test_a_broken_stream_reports_no_exit_code_instead_of_inventing_one(tmp_path: Path) -> None:
    """The adapter must not claim a settled run when the sandbox stream died."""

    class _BrokenHandle:
        pid = 4321

        def __iter__(self):
            raise RuntimeError("connection dropped")

        def send_stdin(self, data, request_timeout=None) -> None:
            pass

        def close_stdin(self, request_timeout=None) -> None:
            pass

        def kill(self) -> bool:
            return True

        def wait(self):
            raise RuntimeError("no end event")

    process = SandboxCommandProcess(_BrokenHandle())
    with pytest.raises(OSError):
        process.wait(timeout=10)
    assert process.poll() == -1, "poll() must not look like a live process"
    assert process.stream_error is not None


# -- run lifecycle ------------------------------------------------------------


def test_an_acp_run_finishes_on_the_sandbox_face(tmp_path: Path) -> None:
    log = tmp_path / "agent.jsonl"
    settings = {"FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True}
    config = make_config(tmp_path, PI_GLINK_ACP_COMMAND=fake_agent_command(tmp_path, settings))
    # No pre-seeded report: the agent writes it through the sandbox workspace,
    # so the recovered deliverable really travelled agent -> sandbox -> host.
    sandbox = FakeSandbox(tmp_path, report=None)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store=store, sandbox_factory=lambda **_: sandbox)

    record = manager.start("把结果写成 report.md")
    final = manager.wait(record.run_id, timeout=120)

    assert final is not None and final.status == FINISHED
    assert final.model_stop_reason == "end_turn"
    assert final.report_status == "ok"
    assert final.report_path is not None
    assert "fake acp" in Path(final.report_path).read_text(encoding="utf-8")
    assert final.sandbox_id == "fake-sandbox-id"
    assert final.parent_run_id is None and final.session_file is None
    assert sandbox.killed is True

    kinds = event_kinds(store, record.run_id)
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished"
    assert "session_ready" in kinds and "assistant_delta" in kinds and "turn_finished" in kinds
    events = [
        json.loads(line)
        for line in store.events_path(record.run_id).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    session_ready = next(event for event in events if event["type"] == "session_ready")
    assert session_ready["payload"]["cwd"] == SANDBOX_WORKSPACE
    assert events[-1]["payload"]["cleanup"]["released"] is True
    # The agent command is the sandbox command; the prompt never appears on it.
    assert "把结果写成 report.md" not in sandbox.command
    assert "OPENAI_API_KEY" not in sandbox.agent_envs
    agent_log = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    prompt_entries = [entry for entry in agent_log if entry["event"] == "prompt"]
    assert prompt_entries and "把结果写成 report.md" in prompt_entries[-1]["text"]
    # The agent wrote its report inside the sandbox workspace, and recovery is
    # what put it on the host (the file is not a host-side fixture).
    sandbox_report = translate(SANDBOX_WORKSPACE, sandbox.root) / "report.md"
    assert sandbox_report.is_file()


def test_the_credential_value_stays_in_the_sandbox_file(tmp_path: Path) -> None:
    secret = "sk-opaque-credential-1234567890"
    source = tmp_path / "operator.json"
    source.write_text(json.dumps({"tokens": {"access_token": secret}}), encoding="utf-8")
    settings = {"FAKE_ACP_LOG": str(tmp_path / "agent.jsonl"), "FAKE_ACP_WRITE_REPORT": True}
    config = make_config(
        tmp_path,
        PI_GLINK_ACP_COMMAND=fake_agent_command(tmp_path, settings),
        PI_GLINK_ACP_CREDENTIAL_FILES=f"{source}=.codex/auth.json",
    )
    sandbox = FakeSandbox(tmp_path)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store=store, sandbox_factory=lambda **_: sandbox)

    record = manager.start("写报告")
    final = manager.wait(record.run_id, timeout=120)
    assert final is not None and final.status == FINISHED

    target = f"{SANDBOX_ACP_HOME}/.codex/auth.json"
    assert sandbox.written[target] == source.read_bytes(), "凭据以文件形态进入沙箱"
    assert secret not in sandbox.command, "凭据值不得进入沙箱命令"
    assert secret not in " ".join(sandbox.agent_envs.values()), "凭据值不得进入命令环境"
    events_text = store.events_path(record.run_id).read_text(encoding="utf-8")
    assert secret not in events_text
    # The fake agent's HOME write lands inside the run's sandbox HOME.
    assert (translate(SANDBOX_ACP_HOME, sandbox.root) / "fake-acp-state.json").is_file()


def test_a_failed_upload_still_persists_the_sandbox_identity(tmp_path: Path) -> None:
    """A partial start must leave a named leftover the ledger can act on."""
    secret = tmp_path / "operator.json"
    secret.write_text('{"tokens": {"access_token": "opaque-1234567890"}}', encoding="utf-8")
    config = make_config(
        tmp_path,
        PI_GLINK_ACP_CREDENTIAL_FILES=f"{secret}=.codex/auth.json",
    )
    sandbox = FakeSandbox(tmp_path, write_failure="disk full")
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store=store, sandbox_factory=lambda **_: sandbox)

    record = manager.start("写报告")
    final = manager.wait(record.run_id, timeout=60)

    assert final is not None and final.status == FAILED
    assert final.sandbox_id == "fake-sandbox-id"
    assert sandbox.killed is True, "创建过的沙箱必须被销毁"
    events = event_kinds(store, record.run_id)
    assert events[-1] == "run_failed"


def test_a_cloud_failure_before_the_sandbox_exists_is_reported(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")

    def factory(**_kwargs: Any):
        raise RuntimeError("quota exceeded")

    manager = RunManager(config, EventBus(), store=store, sandbox_factory=factory)
    record = manager.start("写报告")
    final = manager.wait(record.run_id, timeout=60)
    assert final is not None and final.status == FAILED
    assert final.error is not None and "quota exceeded" in final.error
    assert final.sandbox_id is None


def test_the_runner_wires_the_acp_e2b_plan_from_the_frozen_configuration(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        PI_GLINK_ACP_COMMAND="dsh --profile acp",
        LOCAL_WEB_E2B_ACP_TEMPLATE="glink-acp-worker:pinned",
    )
    runtime = make_runtime(config, tmp_path)
    sandbox = FakeSandbox(tmp_path)
    created: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeSandbox:
        created.update(kwargs)
        return sandbox

    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store=store, sandbox_factory=factory)
    client = manager._acp_e2b_plan(make_spec(config, runtime, tmp_path))
    assert isinstance(client, E2BAcpSandboxClient)
    client.start()
    assert created["template"] == "glink-acp-worker:pinned"
    assert created["api_key"] == "e2b-secret-key"
    assert client.argv == ["dsh", "--profile", "acp"]


# -- image pin boundary -------------------------------------------------------


def test_the_e2b_acp_dockerfile_pins_what_the_notes_measured() -> None:
    """The template's ARGs must equal the versions the notes record as measured."""
    import re

    text = DOCKERFILE.read_text(encoding="utf-8")
    notes = NOTES.read_text(encoding="utf-8")

    def arg(name: str) -> str:
        match = re.search(rf"^ARG\s+{name}=(\S+)\s*$", text, re.MULTILINE)
        assert match is not None, f"missing ARG {name}"
        value = match.group(1)
        assert re.fullmatch(r"\d+\.\d+\.\d+(?:-[\w.]+)?", value), value
        return value

    def notes_pin(package: str) -> str:
        found = set(re.findall(rf"{re.escape(package)}@(\d+\.\d+\.\d+(?:-[\w.]+)?)", notes))
        assert len(found) == 1, f"{package} versions in the notes are inconsistent: {sorted(found)}"
        return next(iter(found))

    assert arg("DSH_VERSION") == notes_pin("@deepseek-ai/dsh")
    assert arg("CODEX_ACP_VERSION") == notes_pin("@agentclientprotocol/codex-acp")
    # M4: the same claude pair as the docker image, pinned in this template too.
    assert arg("CLAUDE_CODE_ACP_VERSION") == notes_pin("@zed-industries/claude-code-acp")
    assert arg("CLAUDE_CODE_VERSION") == notes_pin("@anthropic-ai/claude-code")
    # M5: opencode, pinned to the same version as the docker image.
    assert arg("OPENCODE_AI_VERSION") == notes_pin("opencode-ai")
    assert '"@deepseek-ai/dsh@${DSH_VERSION}"' in text
    assert '"@agentclientprotocol/codex-acp@${CODEX_ACP_VERSION}"' in text
    assert '"@zed-industries/claude-code-acp@${CLAUDE_CODE_ACP_VERSION}"' in text
    assert '"@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"' in text
    assert '"opencode-ai@${OPENCODE_AI_VERSION}"' in text
    # Self-check: the versions are verified at build time and by the default CMD.
    assert "acp-self-check" in text
    assert 'CMD ["acp-self-check"]' in text
    assert "grep -qx" in text
    # E2B's build rewrites the Dockerfile text, so the script is generated line
    # by line and claude-code-acp is verified through `npm ls` (no --version).
    assert "claude --version" in text
    assert "opencode --version" in text
    assert "npm ls -g --depth=0" in text


def test_the_acp_e2b_template_is_not_the_production_or_pi_template() -> None:
    """The dev template must stay independent: no instruction references others."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    instructions = [
        line for line in text.splitlines() if line and not line.lstrip().startswith("#")
    ]
    joined = "\n".join(instructions)
    # The禁触 note lives in the comments; no build instruction may reference the
    # production template or the pi template.
    assert "glink-frontier-e2b" not in joined
    assert "pi-glink-worker" not in joined
    config = make_config(Path("/tmp"))
    assert config.e2b_template == "pi-glink-worker"
    assert config.acp_e2b_template == "glink-acp-worker"
    assert config.e2b_template != config.acp_e2b_template
