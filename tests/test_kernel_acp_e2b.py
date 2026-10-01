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
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pi_glink.config import ConfigError, build_config

from acp_pack import apply_acp_pack
from pi_glink.events import EventBus
from pi_glink.kernel.acp_env import prepare_acp_runtime
from harness_helpers import compose_launch, family
from pi_glink.kernel.acp_e2b_env import SANDBOX_ACP_HOME, AcpSandboxTransfer
from pi_glink.contracts import FollowupUnavailable
from pi_glink.kernel.acp_execution import RESUME_METHOD, AcpExecutionAdapter
from pi_glink.kernel.e2b_env import SANDBOX_WORKSPACE, SandboxCommandProcess
from pi_glink.kernel.execution import ExecutionSpec
from pi_glink.kernel.sandbox import E2BSandbox, open_sandbox
from pi_glink.runs.runner import RunManager
from pi_glink.store import FAILED, FINISHED, RunStore
from pi_glink.tenancy import TenantContext
from store_helpers import events_text, stored_record

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_ACP = Path(__file__).resolve().parent / "fixtures" / "fake_acp.py"
DOCKERFILE = REPO_ROOT / "docker" / "e2b" / "Dockerfile.acp-e2b"
NOTES = REPO_ROOT / "docs" / "acp-integration-notes.md"
#: Multi-tenant fixture constants (same shape the inproc continuation tests use).
AUDIENCE = "pi-glink-test"
SECRET = b"0123456789abcdef0123456789abcdef"


# -- fakes --------------------------------------------------------------------


def translate(sandbox_path: str, root: Path) -> Path:
    """Map one ``/home/user/…`` sandbox path onto the fake sandbox's real tree."""
    relative = sandbox_path.lstrip("/")
    if not sandbox_path.startswith("/home/user") and sandbox_path not in ("/", "/home"):
        raise AssertionError(f"unexpected sandbox path: {sandbox_path}")
    return root / relative


def untranslate(real: Path, root: Path) -> str:
    """The inverse of :func:`translate`: one real path as its sandbox path."""
    return "/" + real.relative_to(root).as_posix()


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
        self.type = "symlink" if real.is_symlink() else ("dir" if real.is_dir() else "file")
        self.size = real.stat().st_size if self.type == "file" else 0


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
        self._sandbox.operations.append(f"make_dir {path}")
        return not existed

    def write(self, path: str, content: Any, **_kwargs: Any) -> None:
        if self._sandbox.write_failure is not None:
            raise OSError(self._sandbox.write_failure)
        real = self._real(path)
        real.parent.mkdir(parents=True, exist_ok=True)
        data = content if isinstance(content, bytes) else str(content).encode("utf-8")
        real.write_bytes(data)
        self._sandbox.written[path] = data
        self._sandbox.operations.append(f"write {path}")

    def read(self, path: str, format: str | None = None, **_kwargs: Any) -> Any:
        self._sandbox.reads.append(path)
        raw = self._real(path).read_bytes()
        if format == "stream":
            return _Stream([raw])
        return raw

    def get_info(self, path: str, **_kwargs: Any) -> _Info:
        return _Info(self._real(path).stat().st_size)

    def list(self, path: str, depth: int = 1, **_kwargs: Any) -> list[_Entry]:
        self._sandbox.listed.append(path)
        if self._sandbox.list_failure is not None and path == SANDBOX_ACP_HOME:
            raise OSError(self._sandbox.list_failure)
        real = self._real(path)
        if not real.is_dir():
            raise FileNotFoundError(path)
        levels = depth if isinstance(depth, int) and depth >= 1 else 1

        def walk(directory: Path, remaining: int) -> list[_Entry]:
            found: list[_Entry] = []
            for child in sorted(directory.iterdir()):
                sandbox_path = untranslate(child, self._sandbox.root)
                found.append(_Entry(sandbox_path, self._sandbox.root))
                if remaining > 1 and child.is_dir() and not child.is_symlink():
                    found.extend(walk(child, remaining - 1))
            return found

        return walk(real, levels)


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
        self._sandbox.operations.append(f"run {cmd}")
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
        list_failure: str | None = None,
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
        self.list_failure = list_failure
        self.files = FakeFiles(self)
        #: Ordered filesystem/command operations, for ordering assertions
        #: (``make_dir`` -> ``chmod 0700`` -> inherited upload -> 0600 harden).
        self.operations: list[str] = []
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
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_EXECUTOR": "e2b",
        "E2B_API_KEY": "e2b-secret-key",
        "PI_GLINK_HARNESS": "acp",
        "TEST_ACP_COMMAND": "dsh --profile acp",
    }
    values.update(overrides)
    apply_acp_pack(values, tmp_path)
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
    text = events_text(store, run_id)
    return [
        json.loads(line)["type"]
        for line in text.splitlines()
        if line.strip()
    ]


class _AcpFace:
    """The ACP family on one E2B sandbox, composed as ``AcpExecutionAdapter.start`` does.

    ``sandbox`` is the resource and transport (:class:`E2BSandbox`: create,
    command, kill); ``transfer`` is the ACP state root on the opened sandbox
    (:class:`AcpSandboxTransfer`: ``HOME`` upload and recovery). The launch is
    the family's real one, so the command checked here is the command run.
    """

    def __init__(self, config, runtime, tmp_path: Path, *, sandbox_factory, workspace: Path | None = None):
        self.runtime = runtime
        self.workspace = workspace or (tmp_path / "ws")
        spec = make_spec(config, runtime, tmp_path)
        spec = replace(spec, workspace=self.workspace)
        launch, sandbox = compose_launch(
            "acp", "e2b", spec, store_root=tmp_path / "face-store", sandbox_factory=sandbox_factory
        )
        assert isinstance(sandbox, E2BSandbox)
        self.launch = launch
        self.sandbox = sandbox
        self.transfer: AcpSandboxTransfer | None = None

    def open(self, *, timeout_seconds: int = 0) -> None:
        self.sandbox.open(self.launch, timeout_seconds=timeout_seconds)
        self.transfer = AcpSandboxTransfer(
            self.sandbox.handle, self.runtime, workspace=self.workspace
        )

    def command_env(self) -> dict[str, str]:
        """What ``E2BSandbox.spawn`` passes as the command's ``envs``."""
        return {**self.launch.env, **self.launch.secret_env}

    def spawn(self) -> SandboxCommandProcess:
        spawn = self.sandbox.spawn(self.launch)
        assert spawn.process is not None
        return spawn.process()


def make_sandbox_client(
    config, runtime, tmp_path: Path, sandbox: FakeSandbox, *, workspace: Path | None = None
) -> _AcpFace:
    def factory(**kwargs: Any) -> FakeSandbox:
        sandbox.create_kwargs = dict(kwargs)
        return sandbox

    return _AcpFace(config, runtime, tmp_path, sandbox_factory=factory, workspace=workspace)


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

    client.open(timeout_seconds=120)
    created = sandbox.create_kwargs
    assert created["template"] == "glink-acp-worker"
    assert created["template"] != config.e2b_template, "ACP 模板与 pi 模板是两个旋钮"
    assert created["api_key"] == "e2b-secret-key"
    assert created["timeout"] == 120
    # The ACP sandbox has no pi runtime directory to deliver; the model key is
    # not part of any create-time environment.
    assert created["envs"] == {}
    assert client.sandbox.sandbox_id == "fake-sandbox-id"
    client.sandbox.close()
    assert sandbox.killed is True


def test_missing_credential_is_refused_before_any_cloud_call(tmp_path: Path) -> None:
    config = make_config(tmp_path, E2B_API_KEY="")
    runtime = make_runtime(config, tmp_path)
    client = _AcpFace(
        config, runtime, tmp_path, sandbox_factory=lambda **_: FakeSandbox(tmp_path)
    )
    with pytest.raises(ConfigError) as excinfo:
        client.open()
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
    client.open()
    client.transfer.upload_home()

    target = f"{SANDBOX_ACP_HOME}/.dsh/.credentials.yaml"
    assert sandbox.written[target] == source.read_bytes()
    assert (".dsh/.credentials.yaml",) == tuple(rel for rel, _digest in client.transfer.upload_manifest)
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
    assert client.transfer.upload_manifest[0][1]
    client.sandbox.close()


def test_the_agent_command_is_the_frozen_argv_in_the_sandbox_workspace(tmp_path: Path) -> None:
    config = make_config(
        tmp_path,
        TEST_ACP_COMMAND=fake_agent_command(
            tmp_path, {"FAKE_ACP_LOG": str(tmp_path / "agent.jsonl")}
        ),
        PI_GLINK_ACP_ENV="ACME_MODE=sandbox-mode-value",
    )
    runtime = make_runtime(config, tmp_path)
    sandbox = FakeSandbox(tmp_path)
    client = make_sandbox_client(config, runtime, tmp_path, sandbox)
    client.open()
    argv = list(client.launch.argv)
    assert argv == list(config.pack.command)
    assert client.sandbox.command(client.launch) == f"cd {SANDBOX_WORKSPACE} && exec {shlex.join(argv)}"
    # HOME is the run's sandbox state root; extra entries are the explicit
    # process-environment channel (values never enter the command line).
    assert client.command_env() == {"HOME": SANDBOX_ACP_HOME, "ACME_MODE": "sandbox-mode-value"}
    assert "sandbox-mode-value" not in client.sandbox.command(client.launch)

    client.spawn()
    assert sandbox.command == client.sandbox.command(client.launch)
    assert sandbox.command_kwargs["background"] is True
    assert sandbox.command_kwargs["stdin"] is True
    assert sandbox.command_kwargs["cwd"] == SANDBOX_WORKSPACE
    # The command's lifetime is bounded by the sandbox's own lifetime.
    assert sandbox.command_kwargs["timeout"] == sandbox.create_kwargs["timeout"]
    # The fake executed exactly what the client asked for (the fake parses the
    # command line back into an argv), so quoting cannot silently change it.
    assert sandbox.agent_argv == argv
    client.sandbox.close()


def test_an_argv_argument_with_spaces_stays_one_argument(tmp_path: Path) -> None:
    config = make_config(tmp_path, TEST_ACP_COMMAND="agent --config '/tmp/a b/c.json'")
    runtime = make_runtime(config, tmp_path)
    client = make_sandbox_client(config, runtime, tmp_path, FakeSandbox(tmp_path))
    assert (
        client.sandbox.command(client.launch)
        == f"cd {SANDBOX_WORKSPACE} && exec agent --config '/tmp/a b/c.json'"
    )


# -- bridging (real fake ACP agent behind a stand-in handle) ------------------


def build_acp_e2b_adapter(config, runtime, tmp_path: Path, sandbox: FakeSandbox):
    """``family.build_adapter(spec, open_sandbox("e2b"))`` — the production composition."""
    frames: list[dict[str, Any]] = []
    identities: list[dict[str, str]] = []

    def factory(**kwargs: Any) -> FakeSandbox:
        sandbox.create_kwargs = dict(kwargs)
        return sandbox

    spec = make_spec(config, runtime, tmp_path)
    e2b = open_sandbox(
        "e2b",
        config=config,
        run_id=spec.run_id,
        workspace=spec.workspace,
        sandbox_factory=factory,
        on_identity=identities.append,
    )
    adapter = family("acp", config, tmp_path / "adapter-store").build_adapter(
        spec, e2b, on_frame=frames.append
    )
    return adapter, frames, identities, e2b


def test_the_agent_is_bridged_over_the_sandbox_handle(tmp_path: Path) -> None:
    settings = {"sessionId": "sandbox-session", "log": str(tmp_path / "agent.jsonl")}
    config = make_config(
        tmp_path,
        TEST_ACP_COMMAND=fake_agent_command(tmp_path, settings),
    )
    runtime = make_runtime(config, tmp_path)
    sandbox = FakeSandbox(tmp_path)
    adapter, frames, identities, client = build_acp_e2b_adapter(config, runtime, tmp_path, sandbox)
    assert isinstance(adapter, AcpExecutionAdapter) and adapter.kind == "e2b"

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
    config = make_config(tmp_path, TEST_ACP_COMMAND=f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}")
    runtime = make_runtime(config, tmp_path)
    sandbox = FakeSandbox(tmp_path, report=None)
    client = make_sandbox_client(config, runtime, tmp_path, sandbox)
    client.open()
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
        client.sandbox.close()
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
    config = make_config(tmp_path, TEST_ACP_COMMAND=fake_agent_command(tmp_path, settings))
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
        for line in events_text(store, record.run_id).splitlines()
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


# -- continuation across sandboxes (GLINK-ACP-13 / M8) ------------------------
#
# Original obligation (M7): the E2B face refused every ACP follow-up because the
# sandbox destroyed the agent state with it. New obligation (M8): the run's
# state root is recovered to the host before the sandbox is destroyed and the
# follow-up inherits it into its own sandbox. The tests below pin the recovery
# contract (bounded, whole-or-nothing, credential-aware), the restore order the
# follow-up uploads it with, the admission matrix that keeps every other case
# refused, and the full fake-SDK chain.


def _sandbox_sequence(
    tmp_path: Path, sandboxes: list[FakeSandbox], *, report: str | None = None, **kwargs: Any
):
    """A ``sandbox_factory`` handing out a fresh fake sandbox per call."""

    def factory(**_ignored: Any) -> FakeSandbox:
        sandbox = FakeSandbox(
            tmp_path, report=report, sandbox_id=f"fake-sandbox-{len(sandboxes) + 1}", **kwargs
        )
        sandboxes.append(sandbox)
        return sandbox

    return factory


def _state_path(sandbox: FakeSandbox, relative: str) -> Path:
    return translate(f"{SANDBOX_ACP_HOME}/{relative}", sandbox.root)


def _payloads(store: RunStore, run_id: str, event_type: str) -> list[dict[str, Any]]:
    events = [
        json.loads(line)
        for line in events_text(store, run_id).splitlines()
        if line.strip()
    ]
    return [event["payload"] for event in events if event["type"] == event_type]


def _assistant_text(store: RunStore, run_id: str) -> str:
    return "".join(
        str(payload.get("text") or "")
        for payload in _payloads(store, run_id, "assistant_delta")
    )


def test_the_sandbox_face_reports_the_agent_stderr_tail_on_a_kernel_exit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """E2B 面同样把 agent stderr 尾部带进 kernel_exit 失败详情与宿主日志（GLINK-OBS-01 项2）。

    The ACP client is shared by the three faces; what this pins is that the
    sandbox carrier's stderr queue really reaches the client's history before
    ``close()`` completes the report — the gap the dsh r2 failure exposed.
    """
    secret = "sk-host-boundary-0123456789abcdef"
    settings = {"FAKE_ACP_STDERR": f"dsh: 沙箱内崩溃 OPENAI_API_KEY={secret}"}
    config = make_config(
        tmp_path,
        TEST_ACP_COMMAND=fake_agent_command(tmp_path, settings),
        OPENAI_API_KEY=secret,
    )
    sandbox = FakeSandbox(tmp_path, report=None)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store=store, sandbox_factory=lambda **_: sandbox)

    with caplog.at_level(logging.WARNING, logger="pi_glink.runs.runner"):
        record = manager.start("CRASH 掉这个 agent")
        final = manager.wait(record.run_id, timeout=120)

    assert final is not None
    assert final.status == FAILED and final.stop_reason == "kernel_exit"
    error = final.error or ""
    assert "dsh: 沙箱内崩溃" in error, "the sandbox agent's stderr must reach the report"
    assert secret not in error
    assert "exit code 3" in error
    assert sandbox.killed is True

    logs = "\n".join(entry.getMessage() for entry in caplog.records)
    assert "dsh: 沙箱内崩溃" in logs
    assert secret not in logs


def test_acp_followup_resumes_the_parent_session_across_sandboxes(tmp_path: Path) -> None:
    """The full chain: first turn, bounded recovery, new sandbox, same session.

    One sandbox per run (the follow-up really starts a new one), the parent's
    state root really travelled host-ward before the first sandbox was killed,
    the follow-up uploaded it into the new sandbox before starting the agent,
    and the resumed answer carries the first turn's codename — which appears in
    neither the follow-up's prompt nor a fresh state.
    """
    log = tmp_path / "agent.jsonl"
    settings = {"FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True}
    config = make_config(tmp_path, TEST_ACP_COMMAND=fake_agent_command(tmp_path, settings))
    sandboxes: list[FakeSandbox] = []
    store = RunStore(config.data_root / "runs")
    manager = RunManager(
        config, EventBus(), store=store, sandbox_factory=_sandbox_sequence(tmp_path, sandboxes, report=None)
    )

    parent = manager.start("第一轮：请记住代号 REMEMBER:ZETA-7319，然后写报告")
    parent_final = manager.wait(parent.run_id, timeout=120)
    assert parent_final is not None and parent_final.status == FINISHED
    assert parent_final.session_id == "fake-acp-session"

    # The state root was recovered to the host before the sandbox was killed,
    # and it is a mirror of what the agent wrote inside the sandbox.
    parent_recovered = store.run_dir(parent.run_id) / "acp-agent" / "recovered"
    recovered_state = json.loads((parent_recovered / "fake-acp-state.json").read_text(encoding="utf-8"))
    assert recovered_state["remembered"] == "ZETA-7319"
    assert sandboxes[0].killed is True

    child = manager.start(
        "RECALL 不要读文件，只用一行复述上一个会话里要求记住的代号",
        parent_run_id=parent.run_id,
    )
    child_final = manager.wait(child.run_id, timeout=120)
    assert child_final is not None and child_final.status == FINISHED, child_final.error

    # A new sandbox carried the follow-up; the session id is the parent's own.
    assert len(sandboxes) == 2
    assert sandboxes[1].sandbox_id == "fake-sandbox-2"
    assert child_final.session_id == parent_final.session_id
    # Original obligation (ACP first cut): every run got its own workspace. New
    # obligation (M7): the session records its cwd, so the continuation reuses
    # the conversation's workspace and the child's artifacts stay its own.
    assert Path(child.workspace) == Path(parent.workspace)

    # Restore order (M3b): the sandbox state root is made 0700 *before* the
    # inherited files are written, and the fresh 0600 harden covers them.
    inherited_target = f"{SANDBOX_ACP_HOME}/fake-acp-state.json"
    operations = sandboxes[1].operations
    assert f"make_dir {SANDBOX_ACP_HOME}" in operations
    assert f"run chmod 0700 {SANDBOX_ACP_HOME}" in operations
    assert f"write {inherited_target}" in operations
    assert operations.index(f"run chmod 0700 {SANDBOX_ACP_HOME}") < operations.index(
        f"write {inherited_target}"
    )
    assert sandboxes[1].written[inherited_target] == (
        sandboxes[0].root / "home/user/acp/home/fake-acp-state.json"
    ).read_bytes()
    assert inherited_target in sandboxes[1].harden_commands[-1]
    assert sandboxes[1].command_kwargs["envs"]["HOME"] == SANDBOX_ACP_HOME
    # The conversation's workspace half travelled too: the prior deliverable is
    # in the new sandbox before the agent starts (the pi face does the same).
    parent_report = (store.run_dir(parent.run_id) / "artifacts" / "report.md").read_bytes()
    assert sandboxes[1].written[f"{SANDBOX_WORKSPACE}/report.md"] == parent_report

    # The follow-up's own record proves the continuation.
    ready = _payloads(store, child.run_id, "session_ready")
    assert ready[0]["sandboxId"] == "fake-sandbox-2"
    assert ready[0]["resumed"] is True
    assert ready[0]["resumeMethod"] == RESUME_METHOD
    assert ready[-1]["stateRootRecovered"] is True
    noticed = [
        payload
        for payload in _payloads(store, child.run_id, "kernel_status")
        if payload.get("message") == "acp_session_resumed"
    ]
    assert len(noticed) == 1
    # Continuity: the answer carries the first turn's codename, and a fresh
    # session (or the child's own prompt) could not produce it.
    assert "REMEMBERED:ZETA-7319" in _assistant_text(store, child.run_id)
    assert "ZETA-7319" not in child.prompt
    assert "ZETA-7319" not in str(_payloads(store, child.run_id, "session_ready")[0])

    # The child's own state root is recovered too, so a third turn can continue
    # the same conversation; the cleanup acted on the two known sandbox ids.
    child_recovered = store.run_dir(child.run_id) / "acp-agent" / "recovered"
    child_state = json.loads((child_recovered / "fake-acp-state.json").read_text(encoding="utf-8"))
    assert child_state["sessionId"] == "fake-acp-session"
    assert [sandbox.killed for sandbox in sandboxes] == [True, True]
    grandfather = manager.start("RECALL 复述代号", parent_run_id=child.run_id)
    grandfather_final = manager.wait(grandfather.run_id, timeout=120)
    assert grandfather_final is not None and grandfather_final.status == FINISHED
    assert grandfather_final.session_id == parent_final.session_id
    assert len(sandboxes) == 3 and sandboxes[2].killed is True

    entries = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [entry["event"] for entry in entries].count("session_new") == 1
    assert [entry["event"] for entry in entries].count("session_resume") == 2


def test_acp_followup_refused_when_the_state_recovery_failed(tmp_path: Path) -> None:
    """A failed recovery leaves nothing to inherit, so the follow-up stays refused.

    The sandbox destroys the state with it; the host records the failure and
    publishes no recovered root. The refusal happens before a second sandbox is
    created — the host does not pay for a resume it knows cannot work.
    """
    settings = {"FAKE_ACP_WRITE_REPORT": True}
    config = make_config(tmp_path, TEST_ACP_COMMAND=fake_agent_command(tmp_path, settings))
    sandboxes: list[FakeSandbox] = []
    store = RunStore(config.data_root / "runs")
    manager = RunManager(
        config,
        EventBus(),
        store=store,
        sandbox_factory=_sandbox_sequence(tmp_path, sandboxes, report=None, list_failure="state listing broke"),
    )
    record = manager.start("第一轮")
    final = manager.wait(record.run_id, timeout=120)
    assert final is not None and final.status == FINISHED

    # The failure is recorded honestly in the run's terminal frame, the
    # post-recovery frame carries the negative verdict, and no recovered root
    # (nor a leftover staging directory) exists.
    assert stored_record(store, record.run_id)["status"] == FINISHED
    ready = _payloads(store, record.run_id, "session_ready")
    assert ready[-1]["stateRootRecovered"] is False
    run_acp_dir = store.run_dir(record.run_id) / "acp-agent"
    assert not (run_acp_dir / "recovered").exists()
    assert not (run_acp_dir / ".recovered.staging").exists()

    before = len(store.list(limit=None))
    with pytest.raises(FollowupUnavailable) as excinfo:
        manager.start("追问", parent_run_id=record.run_id)
    assert excinfo.value.code == "session_unavailable"
    assert len(store.list(limit=None)) == before
    assert len(sandboxes) == 1, "refusal happens before a second sandbox is created"


def test_state_recovery_bounds_refuse_the_whole_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any bound violation refuses the follow-up instead of truncating the state.

    The bounds are the inheritance bounds (one file / file count / total), so a
    state root that would be undeliverable to the next sandbox is refused at
    the source. A violation must leave no recovered root at all.
    """
    settings = {"FAKE_ACP_WRITE_REPORT": True}
    config = make_config(tmp_path, TEST_ACP_COMMAND=fake_agent_command(tmp_path, settings))
    for constant, value, seed in (
        ("INHERIT_MAX_FILE_BYTES", 1024, {"big.bin": b"x" * 4096}),
        ("INHERIT_MAX_FILES", 1, {"a.bin": b"a", "b.bin": b"b"}),
        ("INHERIT_MAX_TOTAL_BYTES", 16, {"a.bin": b"a" * 40}),
    ):
        monkeypatch.setattr(f"pi_glink.kernel.acp_e2b_env.{constant}", value)
        sandboxes: list[FakeSandbox] = []

        class SeedingFactory:
            """A sandbox whose state root already holds the seeded entries.

            ``upload_home`` only writes the host-rendered files, so entries
            that exist before the run are exactly extra sandbox state the
            recovery has to judge — the same shape a long agent session has.

            ``seed`` / ``sandboxes`` are bound at construction, so the loop
            variable is not captured through the call (B023).
            """

            def __init__(self, seed: dict[str, bytes], sandboxes: list[FakeSandbox]):
                self._seed = seed
                self._sandboxes = sandboxes

            def __call__(self, **_kwargs: Any) -> FakeSandbox:
                sandbox = FakeSandbox(
                    tmp_path, report=None, sandbox_id=f"fake-sandbox-{len(self._sandboxes) + 1}"
                )
                for name, data in self._seed.items():
                    target = translate(f"{SANDBOX_ACP_HOME}/{name}", sandbox.root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                self._sandboxes.append(sandbox)
                return sandbox

        store = RunStore(config.data_root / f"runs-{constant}")
        manager = RunManager(config, EventBus(), store=store, sandbox_factory=SeedingFactory(seed, sandboxes))
        record = manager.start(f"第一轮-{constant}")
        final = manager.wait(record.run_id, timeout=120)
        assert final is not None and final.status == FINISHED
        run_acp_dir = store.run_dir(record.run_id) / "acp-agent"
        assert not (run_acp_dir / "recovered").exists(), constant
        assert not (run_acp_dir / ".recovered.staging").exists(), constant
        # The failure is recorded, and it is the bound (not some other reason),
        # that refused the mirror.
        failures = [
            event["payload"]["artifactFailures"]
            for event in (
                json.loads(line)
                for line in events_text(store, record.run_id).splitlines()
                if line.strip()
            )
            if event["type"] == "run_finished"
        ]
        assert failures and any("上限" in item for item in failures[-1]), (constant, failures)
        with pytest.raises(FollowupUnavailable) as excinfo:
            manager.start("追问", parent_run_id=record.run_id)
        assert excinfo.value.code == "session_unavailable"
        assert len(sandboxes) == 1


def test_inherited_state_secrets_join_the_child_redaction_boundary(tmp_path: Path) -> None:
    """A credential-shaped value in the sandbox state is masked in the child.

    The value is not one of the operator's configured credentials: the host
    only learns it from the recovered sandbox state (and the follow-up's
    inheritance scan). The fake agent echoes it on ``RECALL``; the child's
    stream must show no readable copy. The recovered state files themselves are
    not rewritten — that would corrupt the session a resume is meant to
    continue.
    """
    inherited_secret = "sk-live-inherited-credential-0123456789"
    log = tmp_path / "agent.jsonl"
    settings = {"FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True}
    config = make_config(tmp_path, TEST_ACP_COMMAND=fake_agent_command(tmp_path, settings))
    sandboxes: list[FakeSandbox] = []

    class SeedingFactory:
        def __call__(self, **_kwargs: Any) -> FakeSandbox:
            sandbox = FakeSandbox(
                tmp_path, report=None, sandbox_id=f"fake-sandbox-{len(sandboxes) + 1}"
            )
            target = translate(f"{SANDBOX_ACP_HOME}/fake-acp-state.json", sandbox.root)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"remembered": inherited_secret}), encoding="utf-8")
            sandboxes.append(sandbox)
            return sandbox

    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store=store, sandbox_factory=SeedingFactory())
    parent = manager.start("第一轮：不用记住任何东西，直接写报告")
    parent_final = manager.wait(parent.run_id, timeout=120)
    assert parent_final is not None and parent_final.status == FINISHED
    recovered = store.run_dir(parent.run_id) / "acp-agent" / "recovered" / "fake-acp-state.json"
    assert inherited_secret in recovered.read_text(encoding="utf-8")

    child = manager.start("RECALL 复述代号", parent_run_id=parent.run_id)
    child_final = manager.wait(child.run_id, timeout=120)
    assert child_final is not None and child_final.status == FINISHED
    raw_events = events_text(store, child.run_id)
    assert inherited_secret not in raw_events
    assert "[REDACTED]" in raw_events
    assert inherited_secret not in _assistant_text(store, child.run_id)


def test_acp_e2b_followup_admission_matrix(tmp_path: Path) -> None:
    """Every e2b refusal besides the admitted case stays in force.

    * no declared continuation capability: refused before a sandbox exists;
    * a parent that already has a follow-up turn (not the latest turn);
    * a missing recovered state root (the parent record is forged to look
      resumable): refused, never inherited from an absent root.
    """
    settings = {"FAKE_ACP_WRITE_REPORT": True}
    config = make_config(tmp_path, TEST_ACP_COMMAND=fake_agent_command(tmp_path, settings))
    store = RunStore(config.data_root / "runs")
    sandboxes: list[FakeSandbox] = []
    manager = RunManager(
        config, EventBus(), store=store, sandbox_factory=_sandbox_sequence(tmp_path, sandboxes, report=None)
    )
    parent = manager.start("第一轮")
    assert manager.wait(parent.run_id, timeout=120) is not None
    child = manager.start("追问", parent_run_id=parent.run_id)
    assert manager.wait(child.run_id, timeout=120) is not None

    # Not the latest turn: the parent already has a follow-up.
    with pytest.raises(FollowupUnavailable) as excinfo:
        manager.start("再追问", parent_run_id=parent.run_id)
    assert excinfo.value.code == "stale_run"

    # Missing recovered root: a parent that never recovered it is refused even
    # when the capability and session facts look resumable.
    import shutil

    shutil.rmtree(store.run_dir(child.run_id) / "acp-agent" / "recovered")
    with pytest.raises(FollowupUnavailable) as excinfo:
        manager.start("追问", parent_run_id=child.run_id)
    assert excinfo.value.code == "session_unavailable"
    assert len(sandboxes) == 2, "refusals happen before a further sandbox exists"

    # No continuation capability in the parent's recorded matrix: refused.
    no_resume = {
        "FAKE_ACP_WRITE_REPORT": True,
        "FAKE_ACP_CAPABILITIES": {"loadSession": False, "sessionCapabilities": {}},
    }
    config2 = make_config(
        tmp_path,
        TEST_ACP_COMMAND=fake_agent_command(tmp_path, no_resume, name="no-resume.json"),
        LOCAL_WEB_DATA_ROOT=str(tmp_path / "capability-data"),
    )
    store2 = RunStore(config2.data_root / "runs")
    manager2 = RunManager(config2, EventBus(), store=store2, sandbox_factory=_sandbox_sequence(tmp_path, []))
    parent2 = manager2.start("第一轮")
    assert manager2.wait(parent2.run_id, timeout=120) is not None
    before = len(store2.list(limit=None))
    with pytest.raises(FollowupUnavailable) as excinfo:
        manager2.start("追问", parent_run_id=parent2.run_id)
    assert excinfo.value.code == "session_unavailable"
    assert len(store2.list(limit=None)) == before

    # No recorded session id: refused even when the recovered root and the
    # capability matrix look resumable. (Simulated by clearing the record, the
    # same shape a pre-session-id ACP run would have.)
    parent3 = manager.start("第一轮-无会话")
    assert manager.wait(parent3.run_id, timeout=120) is not None
    store.update(parent3.run_id, session_id=None)
    sandboxes_before = len(sandboxes)
    with pytest.raises(FollowupUnavailable) as excinfo:
        manager.start("追问", parent_run_id=parent3.run_id)
    assert excinfo.value.code == "session_unavailable"
    assert len(sandboxes) == sandboxes_before, "拒绝发生在起新沙箱之前"


def test_restoring_the_inherited_state_must_succeed_before_the_agent_starts(
    tmp_path: Path,
) -> None:
    """A failed restore into the new sandbox fails the run closed.

    ``upload_home`` is the "灌回" step: the follow-up's HOME (inherited state +
    fresh credentials) is written into the new sandbox before the agent starts.
    A write failure must fail the run — never start the agent against a partial
    state root — and the just-created sandbox must still be destroyed.
    """
    settings = {"FAKE_ACP_WRITE_REPORT": True}
    config = make_config(tmp_path, TEST_ACP_COMMAND=fake_agent_command(tmp_path, settings))
    sandboxes: list[FakeSandbox] = []

    def factory(**_kwargs: Any) -> FakeSandbox:
        sandbox = FakeSandbox(
            tmp_path,
            report=None,
            sandbox_id=f"fake-sandbox-{len(sandboxes) + 1}",
            # The *second* sandbox refuses writes: the first turn must finish
            # normally, and the follow-up must fail while restoring.
            write_failure="disk full" if sandboxes else None,
        )
        sandboxes.append(sandbox)
        return sandbox

    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store=store, sandbox_factory=factory)
    parent = manager.start("第一轮")
    parent_final = manager.wait(parent.run_id, timeout=120)
    assert parent_final is not None and parent_final.status == FINISHED

    child = manager.start("追问", parent_run_id=parent.run_id)
    child_final = manager.wait(child.run_id, timeout=120)
    assert child_final is not None and child_final.status == FAILED
    assert child_final.error and "disk full" in child_final.error
    assert child_final.session_id is None
    assert len(sandboxes) == 2 and sandboxes[1].killed is True
    assert sandboxes[1].agent_process is None, "灌回失败时不得启动 agent"
    assert not any(
        payload.get("sessionId") for payload in _payloads(store, child.run_id, "session_ready")
    ), "灌回失败不得留下已建立的会话"


def test_acp_followup_refuses_a_parent_owned_by_another_tenant_on_the_sandbox_face(
    tmp_path: Path,
) -> None:
    """The owner gate runs before anything else on the e2b face too.

    A parent owned by another tenant is invisible: no session, no state root,
    no existence leak, and no second sandbox is created. shared 政策环境：ADR 0015
    后 multitenant tenant 政策下 ACP run 在准入即拒；本测试关注 owner 边界，
    与凭据政策正交，故用 shared + ACP 通道搭建环境。
    """
    secret_file = tmp_path / "gateway.key"
    secret_file.write_bytes(SECRET)
    secret_file.chmod(0o600)
    config = make_config(
        tmp_path,
        TEST_ACP_COMMAND=fake_agent_command(
            tmp_path, {"FAKE_ACP_WRITE_REPORT": True}, name="tenant-settings.json"
        ),
        PI_GLINK_TENANCY="multitenant",
        PI_GLINK_MODEL_CREDENTIALS="shared",
        PI_GLINK_ACP_ENV="FAKE_ACP_KEY=sk-acp-shared-000000000000",
        PI_GLINK_GATEWAY_SECRET_FILE=str(secret_file),
        PI_GLINK_TENANCY_AUDIENCE=AUDIENCE,
        LOCAL_WEB_TASK_TIMEOUT_SECONDS="60",
    )
    sandboxes: list[FakeSandbox] = []
    store = RunStore(config.data_root / "runs")
    manager = RunManager(
        config,
        EventBus(),
        store,
        sandbox_factory=_sandbox_sequence(tmp_path, sandboxes, report=None),
    )
    alpha = TenantContext(
        tenant_id="tenant-alpha",
        user_id="user-a",
        role="member",
        audience=AUDIENCE,
        expires_at=time.time() + 60,
    )
    beta = TenantContext(
        tenant_id="tenant-beta",
        user_id="user-b",
        role="member",
        audience=AUDIENCE,
        expires_at=time.time() + 60,
    )
    parent = manager.start("第一轮", alpha)
    assert manager.wait(parent.run_id, timeout=120) is not None

    before = len(store.list(limit=None))
    with pytest.raises(FollowupUnavailable) as excinfo:
        manager.start("追问", beta, parent_run_id=parent.run_id)
    assert excinfo.value.code == "unknown_run"
    assert len(store.list(limit=None)) == before
    assert len(sandboxes) == 1, "异租户拒绝发生在起新沙箱之前"


def test_recovery_never_mirrors_a_path_outside_the_state_root() -> None:
    """Sandbox entry paths are validated, never trusted.

    A recursive listing is untrusted input: a path that is not exactly below
    the sandbox state root refuses the whole recovery (``None``) instead of
    being re-interpreted by the host's path model.
    """
    from pi_glink.kernel.acp_e2b_env import _state_relative

    assert _state_relative(f"{SANDBOX_ACP_HOME}/.dsh/sessions/a.json") == ".dsh/sessions/a.json"
    for hostile in (
        "/home/user/workspace/report.md",
        f"{SANDBOX_ACP_HOME}/../escape",
        f"{SANDBOX_ACP_HOME}/a/../../escape",
        f"{SANDBOX_ACP_HOME}/a\\b",
        f"{SANDBOX_ACP_HOME}/",
        f"{SANDBOX_ACP_HOME}",
        "",
    ):
        assert _state_relative(hostile) is None, hostile


def test_the_credential_value_stays_in_the_sandbox_file(tmp_path: Path) -> None:
    secret = "sk-opaque-credential-1234567890"
    source = tmp_path / "operator.json"
    source.write_text(json.dumps({"tokens": {"access_token": secret}}), encoding="utf-8")
    settings = {"FAKE_ACP_LOG": str(tmp_path / "agent.jsonl"), "FAKE_ACP_WRITE_REPORT": True}
    config = make_config(
        tmp_path,
        TEST_ACP_COMMAND=fake_agent_command(tmp_path, settings),
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
    stored_events = events_text(store, record.run_id)
    assert secret not in stored_events
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
        TEST_ACP_COMMAND="dsh --profile acp",
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
    spec = make_spec(config, runtime, tmp_path)
    # The runner's own selection: the sandbox from the frozen executor, the
    # launch from the run's family (no per-face plan callback).
    e2b = open_sandbox(
        config.executor,
        config=config,
        run_id=spec.run_id,
        workspace=spec.workspace,
        sandbox_factory=factory,
    )
    assert isinstance(e2b, E2BSandbox)
    adapter = manager._admission.family_for("acp").build_adapter(  # noqa: SLF001
        spec, e2b, on_frame=lambda _frame: None
    )
    assert isinstance(adapter, AcpExecutionAdapter)
    e2b.open(adapter.launch, timeout_seconds=0)
    assert created["template"] == "glink-acp-worker:pinned"
    assert created["api_key"] == "e2b-secret-key"
    assert list(adapter.launch.argv) == ["dsh", "--profile", "acp"]
    e2b.close()


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
    # The M4/M6 claude pair was removed from the product (GLINK-ARCH-6); the
    # template must not carry it any more, and the absence assertion keeps the
    # removal from silently regressing.
    assert "claude" not in text.lower(), "claude 层已随 GLINK-ARCH-6 移除，不得重新出现在模板里"
    # M5: opencode, pinned to the same version as the docker image.
    assert arg("OPENCODE_AI_VERSION") == notes_pin("opencode-ai")
    assert '"@deepseek-ai/dsh@${DSH_VERSION}"' in text
    assert '"@agentclientprotocol/codex-acp@${CODEX_ACP_VERSION}"' in text
    assert '"opencode-ai@${OPENCODE_AI_VERSION}"' in text
    # Self-check: the versions are verified at build time and by the default CMD.
    assert "acp-self-check" in text
    assert 'CMD ["acp-self-check"]' in text
    assert "grep -qx" in text
    # E2B's build rewrites the Dockerfile text, so the script is generated line
    # by line; every component reports its version directly.
    assert "dsh --version" in text
    assert "codex-acp --version" in text
    assert "opencode --version" in text


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


def test_the_sandbox_pump_applies_backpressure_instead_of_buffering_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原义务：沙箱 stdout/stderr 走无界队列，远端输出快于宿主消费时全部堆在内存。
    新义务：队列有界（背压阻塞 SDK 迭代）；kill 之后泵不再阻塞，退出码仍可得。"""
    from pi_glink.kernel import e2b_env

    monkeypatch.setattr(e2b_env, "_PUMP_QUEUE_CHUNKS", 4, raising=False)
    monkeypatch.setattr(e2b_env, "_PUMP_PUT_POLL_SECONDS", 0.05, raising=False)
    produced: list[int] = []

    class _FloodHandle:
        pid = 7

        def __iter__(self):
            for index in range(10_000):
                produced.append(index)
                yield (b"x" * 1024).decode(), None, None

        def send_stdin(self, data, request_timeout=None) -> None:
            pass

        def close_stdin(self, request_timeout=None) -> None:
            pass

        def kill(self) -> bool:
            return True

        def wait(self):
            return type("R", (), {"exit_code": 0})()

    process = SandboxCommandProcess(_FloodHandle())
    time.sleep(0.5)
    # Nobody reads: the pump is held at the bound, not racing through 10k chunks.
    assert len(produced) <= 4 + 2
    process.kill()
    assert process.wait(timeout=10) == 0
    assert process.poll() == 0


def test_the_real_sdk_handle_does_not_retain_the_whole_stream(tmp_path: Path) -> None:
    """原义务：有界队列只限制 SDK→读取线程的缓冲；真实 e2b ``CommandHandle`` 自身把
    每块 stdout/stderr 追加进 ``_stdout_chunks``/``_stderr_chunks``，结束时再拼出一份
    同体量的 ``CommandResult.stdout``——agent 输出总量仍全部驻留宿主内存。
    新义务：宿主只读退出码；泵转交每块后清空 SDK 累计缓存，结果不携带整段输出。
    （只替换线上事件输入，``CommandHandle`` 是已安装 SDK 的真实实现。）"""
    pb = pytest.importorskip("e2b.envd.process.process_pb")
    from e2b.sandbox_sync.commands.command_handle import CommandHandle
    from protobuf import Oneof

    record = (json.dumps({"type": "probe", "text": "x" * 4000}) + "\n").encode()
    count = 600

    def events():
        for index in range(count):
            field = "stdout" if index % 3 else "stderr"
            yield pb.StartResponse(event=pb.ProcessEvent(event=Oneof(
                field="data", value=pb.ProcessEvent.DataEvent(output=Oneof(field=field, value=record)))))
        yield pb.StartResponse(event=pb.ProcessEvent(event=Oneof(
            field="end", value=pb.ProcessEvent.EndEvent(exit_code=0, exited=True))))

    handle = CommandHandle(pid=1, handle_kill=lambda: True, events=events())
    process = SandboxCommandProcess(handle)
    stderr_lines: list[bytes] = []
    reader = threading.Thread(target=lambda: stderr_lines.extend(process.stderr), daemon=True)
    reader.start()
    consumed = 0
    while True:
        data = process.stdout.read(65536)
        if not data:
            break
        consumed += len(data)
    assert process.wait(timeout=10) == 0
    reader.join(10)
    assert consumed == len(record) * (count - count // 3), "the host still receives every chunk"
    assert len(stderr_lines) == count // 3
    retained = sum(len(chunk) for chunk in (*handle._stdout_chunks, *handle._stderr_chunks))
    assert retained < len(record) * 4, f"SDK retained {retained} bytes of output"
