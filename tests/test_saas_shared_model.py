"""GLINK-SAAS-01: shared SaaS model credentials with execution isolation.

The approved SaaS deployment contract: the cloud sandbox isolates the kernel,
the in-app account surface owns registration/login/org membership, and the
model credential is provided **once by the host** (``shared`` policy) instead
of by each tenant. This module pins that contract and the unchanged fallbacks:

* the ``shared`` policy is explicit, requires frozen ``OPENAI_*`` material, and
  refuses tenant entries that try to mix their own credentials in;
* the default ``tenant`` policy keeps its per-tenant key semantics and errors;
* the host credential reaches each run only through the per-run ``auth.json``
  (and, for search, the host-level shared search key) — never the sandbox
  environment, command line, HTTP payload, event log or report;
* the account/owner/quota surface is untouched: two tenants share one model
  configuration but never see each other's runs, and the quota stays per tenant;
* invite bootstrap in ``shared`` mode validates the host configuration instead
  of a tenant entry.

Everything here is synthetic: temporary data roots and account databases,
in-tmp tenant files, a fake in-process execution face and a fake E2B sandbox.
No pi process, container, cloud call, real ``.env`` or ``~/.pi`` file is used.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from pi_glink.accounts import AccountsStore
from pi_glink.app import PiGlinkServer, create_server
from pi_glink.app import main as app_main
from pi_glink.config import ConfigError, build_config
from pi_glink.events import EventBus
from pi_glink.kernel.agent_env import RunCredentials, prepare_agent_dir
from pi_glink.kernel.docker_env import build_plan as build_docker_plan
from pi_glink.kernel.e2b_env import SANDBOX_CONFIG, E2BSandboxClient
from pi_glink.kernel.execution import (
    ArtifactRecovery,
    CleanupReport,
    ExecutionOutcome,
)
from pi_glink.kernel.runner import RunManager
from pi_glink.store import RunStore
from pi_glink.tenancy import (
    TenantContext,
    TenantCredentialStore,
    build_tenancy,
    issue_assertion,
    load_identity_material,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The one host-level model credential every shared-mode run renders.
SHARED_KEY = "sk-shared-0000000000000000"
#: The optional host-level search credential (explicitly shared, not per tenant).
SHARED_SEARCH_KEY = "search-shared-000000000000"
#: A synthetic operator file value that must never substitute for the host key.
OPERATOR_SEARCH_KEY = "search-operator-0000000000"
ALPHA_KEY = "sk-alpha-0000000000000000"
AUDIENCE = "pi-glink-test"
SECRET = b"0123456789abcdef0123456789abcdef"
PASSWORD = "correct horse battery"
CSRF = "X-Pi-Glink-CSRF"


# -- synthetic configuration ---------------------------------------------------


def write_tenant_file(tmp_path: Path, tenants: dict[str, Any] | None = None, name: str = "tenants.json") -> Path:
    path = tmp_path / name
    path.write_text(
        json.dumps({"tenants": tenants if tenants is not None else {}}), encoding="utf-8"
    )
    path.chmod(0o600)
    return path


def shared_values(tmp_path: Path, **overrides: str) -> dict[str, str]:
    """A synthetic ``shared`` SaaS configuration (accounts + docker by default).

    The shared policy has **no** tenant-credential-file entry: every credential
    (model and the optional search key) comes from the frozen host values.
    """
    values: dict[str, str] = {
        "OPENAI_API_KEY": SHARED_KEY,
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_PORT": "0",
        "LOCAL_WEB_EXECUTOR": "docker",
        "PI_GLINK_TENANCY": "multitenant",
        "PI_GLINK_AUTH_MODE": "accounts",
        "PI_GLINK_MODEL_CREDENTIALS": "shared",
        "PI_GLINK_WEB_SEARCH_KEY": SHARED_SEARCH_KEY,
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent-web-search.json"),
        "PI_GLINK_ACCOUNTS_DB": str(tmp_path / "data" / "accounts" / "accounts.db"),
        "PI_GLINK_COOKIE_SECURE": "0",
        "LOCAL_WEB_TASK_TIMEOUT_SECONDS": "60",
    }
    values.update(overrides)
    return values


def tenant_values(tmp_path: Path, tenants: dict[str, Any] | None = None) -> dict[str, str]:
    """The original per-tenant policy, for the compatibility half of the matrix."""
    document = {"tenant-alpha": {"modelApiKey": ALPHA_KEY}, **(tenants or {})}
    values: dict[str, str] = {
        "OPENAI_API_KEY": SHARED_KEY,
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_PORT": "0",
        "LOCAL_WEB_EXECUTOR": "docker",
        "PI_GLINK_TENANCY": "multitenant",
        "PI_GLINK_AUTH_MODE": "accounts",
        "PI_GLINK_TENANT_CONFIG_FILE": str(write_tenant_file(tmp_path, document)),
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent-web-search.json"),
        "PI_GLINK_ACCOUNTS_DB": str(tmp_path / "data" / "accounts" / "accounts.db"),
        "PI_GLINK_COOKIE_SECURE": "0",
        "LOCAL_WEB_TASK_TIMEOUT_SECONDS": "60",
    }
    return values


def _runtime_for(config, tmp_path: Path, name: str, credentials: RunCredentials):
    run_dir = tmp_path / "runs" / name
    return prepare_agent_dir(
        config,
        runtime_dir=run_dir / "pi-agent",
        session_dir=run_dir / "sessions",
        credentials=credentials,
    )


# -- config gate ---------------------------------------------------------------


def test_shared_policy_requires_frozen_host_model_credentials(tmp_path: Path) -> None:
    for missing in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        with pytest.raises(ConfigError, match=missing):
            build_config(shared_values(tmp_path, **{missing: ""}), REPO_ROOT)


def test_shared_policy_is_explicit_and_multitenant_only(tmp_path: Path) -> None:
    config = build_config(shared_values(tmp_path), REPO_ROOT)
    assert config.model_credentials == "shared"
    assert config.multi_tenant is True

    with pytest.raises(ConfigError, match="PI_GLINK_MODEL_CREDENTIALS"):
        build_config(shared_values(tmp_path, PI_GLINK_TENANCY="local"), REPO_ROOT)
    with pytest.raises(ConfigError, match="PI_GLINK_MODEL_CREDENTIALS"):
        build_config(shared_values(tmp_path, PI_GLINK_MODEL_CREDENTIALS="magic"), REPO_ROOT)

    # The default stays the original per-tenant contract.
    default = build_config(tenant_values(tmp_path), REPO_ROOT)
    assert default.model_credentials == "tenant"


def test_shared_policy_refuses_the_tenant_credential_file_entry(tmp_path: Path) -> None:
    """``shared`` reads no tenant configuration at all: any file entry is refused.

    This is stricter than the original relaxation: the host credential is the
    only source, so an entry that could label a second credential set (or even
    a credential-free registry) fails closed at configuration time.
    """
    keyed = write_tenant_file(tmp_path, {"tenant-alpha": {"modelApiKey": ALPHA_KEY}}, name="keyed.json")
    with pytest.raises(ConfigError, match="PI_GLINK_TENANT_CONFIG_FILE"):
        build_config(shared_values(tmp_path, PI_GLINK_TENANT_CONFIG_FILE=str(keyed)), REPO_ROOT)

    empty = write_tenant_file(tmp_path, {}, name="empty.json")
    with pytest.raises(ConfigError, match="PI_GLINK_TENANT_CONFIG_FILE"):
        build_config(shared_values(tmp_path, PI_GLINK_TENANT_CONFIG_FILE=str(empty)), REPO_ROOT)

    # An empty value is the same as not setting it: no file entry exists.
    config = build_config(shared_values(tmp_path, PI_GLINK_TENANT_CONFIG_FILE=""), REPO_ROOT)
    assert config.tenant_config_file is None

    # The invariant also holds for a directly constructed HostConfig.
    sneak = replace(
        build_config(shared_values(tmp_path), REPO_ROOT),
        tenant_config_file=write_tenant_file(tmp_path, {}, name="sneak.json"),
    )
    with pytest.raises(ConfigError, match="PI_GLINK_TENANT_CONFIG_FILE"):
        load_identity_material(sneak)


def test_shared_mode_has_no_tenant_credential_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``TenantCredentialStore`` may be constructed or consulted in shared mode."""
    config = build_config(shared_values(tmp_path), REPO_ROOT)
    assert config.tenant_config_file is None

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("shared 模式不得读取租户凭据配置")

    monkeypatch.setattr(TenantCredentialStore, "from_file", classmethod(forbidden))
    material = load_identity_material(config)
    assert material is not None
    try:
        assert material.shared_model_credentials is True
        assert material.credentials is None
    finally:
        material.close()

    server = create_server(build_config(shared_values(tmp_path), REPO_ROOT))
    try:
        assert server.tenancy is not None
        assert server.tenancy.credentials is None
        assert server.manager._credential_store is None
    finally:
        server.server_close()


def test_tenant_policy_keeps_the_tenant_file_entry(tmp_path: Path) -> None:
    """The default policy still reads the tenant credential file (unchanged)."""
    config = build_config(tenant_values(tmp_path), REPO_ROOT)
    assert config.tenant_config_file is not None
    material = load_identity_material(config)
    assert material is not None
    try:
        assert material.shared_model_credentials is False
        assert material.credentials is not None
        assert material.credentials.tenant_ids() == frozenset({"tenant-alpha"})
    finally:
        material.close()


def test_missing_shared_credentials_are_a_startup_refusal(tmp_path: Path) -> None:
    config = build_config(shared_values(tmp_path), REPO_ROOT)
    broken = replace(config, openai_api_key="")
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        load_identity_material(broken)

    # ``create_server`` must not fall back to a per-tenant read either.
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        create_server(broken)


def test_tenant_policy_still_requires_per_tenant_keys(tmp_path: Path) -> None:
    config = build_config(tenant_values(tmp_path, {"tenant-beta": {"webSearchKey": "search-only"}}), REPO_ROOT)
    assert config.model_credentials == "tenant"
    with pytest.raises(ConfigError, match="modelApiKey"):
        build_tenancy(config)

    path = write_tenant_file(tmp_path, {"tenant-beta": {}}, name="strict.json")
    with pytest.raises(ConfigError, match="modelApiKey"):
        TenantCredentialStore.from_file(path, repo_root=REPO_ROOT)


# -- rendering: the credential lives in the run's own auth.json ----------------


def test_shared_credentials_render_only_into_the_per_run_auth_file(tmp_path: Path) -> None:
    config = build_config(
        shared_values(tmp_path, LOCAL_WEB_EXECUTOR="e2b", E2B_API_KEY="e2b-secret"), REPO_ROOT
    )
    runtime = _runtime_for(config, tmp_path, "run-1", RunCredentials.from_shared_host(config))
    auth_path = runtime.directory / "auth.json"
    assert SHARED_KEY in auth_path.read_text(encoding="utf-8")
    assert auth_path.stat().st_mode & 0o777 == 0o600
    search = json.loads((runtime.directory / "web-search.json").read_text(encoding="utf-8"))
    assert search == {"exaApiKey": SHARED_SEARCH_KEY}

    # Docker delivers the credential only through the mounted config directory;
    # neither the container argv nor the CLI environment carries it.
    plan = build_docker_plan(
        config,
        runtime,
        run_id="run-1",
        workspace=tmp_path / "ws",
        session_dir=runtime.session_dir,
    )
    assert plan.argv.count(f"{runtime.directory}:/config") == 1
    for element in plan.argv:
        assert SHARED_KEY not in element
    assert SHARED_KEY not in "\n".join(plan.env.values())


def test_shared_mode_never_substitutes_the_operator_search_file(tmp_path: Path) -> None:
    operator = tmp_path / "operator-web-search.json"
    operator.write_text(json.dumps({"exaApiKey": OPERATOR_SEARCH_KEY}), encoding="utf-8")
    operator.chmod(0o600)
    config = build_config(
        shared_values(
            tmp_path, PI_GLINK_WEB_SEARCH_KEY="", PI_GLINK_WEB_SEARCH_CONFIG=str(operator)
        ),
        REPO_ROOT,
    )
    runtime = _runtime_for(config, tmp_path, "run-2", RunCredentials.from_shared_host(config))
    assert not (runtime.directory / "web-search.json").exists()
    assert OPERATOR_SEARCH_KEY not in (runtime.directory / "auth.json").read_text(encoding="utf-8")


# -- fake E2B sandbox ----------------------------------------------------------


class _Result:
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code


class _Handle:
    def __init__(self, exit_code: int) -> None:
        self._exit_code = exit_code

    def wait(self) -> _Result:
        return _Result(self._exit_code)


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
        self._sandbox.written[path] = (
            content.decode("utf-8") if isinstance(content, bytes) else str(content)
        )

    def read(self, path: str, format: str | None = None) -> Any:
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
        size = len(value) if isinstance(value, bytes) else len(value.encode("utf-8"))

        class Info:
            pass

        info = Info()
        info.size = size
        return info

    def list(self, path: str) -> list[Any]:
        readable = self._sandbox.readable

        class Entry:
            def __init__(self, name: str) -> None:
                self.name = name
                self.path = f"{path}/{name}"
                self.type = "file"
                content = readable.get(self.path)
                if content is None:
                    self.size = 0
                elif isinstance(content, bytes):
                    self.size = len(content)
                else:
                    self.size = len(content.encode("utf-8"))

        names = (
            self._sandbox.session_files
            if path == "/home/user/sessions"
            else self._sandbox.workspace_files
        )
        return [Entry(name) for name in names]


class FakeCommands:
    def __init__(self, sandbox: "FakeSandbox") -> None:
        self._sandbox = sandbox

    def run(self, cmd: str, *, background: bool = False, envs: dict[str, str] | None = None, **_: Any) -> _Handle:
        self._sandbox.command = cmd
        self._sandbox.command_envs = envs or {}
        return _Handle(0)


class FakeSandbox:
    def __init__(self) -> None:
        self.sandbox_id = "fake-sandbox"
        self.dirs: list[str] = []
        self.written: dict[str, str] = {}
        # A real sandbox always writes its event file (the command tees into it).
        self.readable: dict[str, Any] = {"/home/user/events.jsonl": ""}
        self.workspace_files: tuple[str, ...] = []
        self.session_files: tuple[str, ...] = ()
        self.command = ""
        self.command_envs: dict[str, str] = {}
        self.killed = False
        self.files = FakeFiles(self)
        self.commands = FakeCommands(self)

    def kill(self) -> None:
        self.killed = True


def _e2b_config(tmp_path: Path):
    return build_config(
        shared_values(tmp_path, LOCAL_WEB_EXECUTOR="e2b", E2B_API_KEY="e2b-secret"), REPO_ROOT
    )


def test_shared_model_key_stays_out_of_the_e2b_environment_and_command(tmp_path: Path) -> None:
    """The E2B face may receive the key only through the uploaded auth.json."""
    config = _e2b_config(tmp_path)
    runtime = _runtime_for(config, tmp_path, "run-3", RunCredentials.from_shared_host(config))
    sandbox = FakeSandbox()
    captured: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeSandbox:
        captured.update(kwargs)
        return sandbox

    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=factory
    )
    client.start()
    client.upload_config()
    client.run_prompt("合成问题")
    assert SHARED_KEY in sandbox.written[f"{SANDBOX_CONFIG}/auth.json"]
    assert SHARED_KEY not in sandbox.command
    assert "OPENAI_API_KEY" not in sandbox.command_envs
    assert SHARED_KEY not in json.dumps(sandbox.command_envs)
    assert "OPENAI_API_KEY" not in captured["envs"]
    assert SHARED_KEY not in json.dumps(captured)
    assert captured["api_key"] == "e2b-secret"
    assert any(path.endswith("/config/auth.json") for path in client.uploaded)
    client.close()
    assert sandbox.killed is True, "the sandbox (and its auth.json) must be destroyed"


_E2B_EVENTS = [
    {"type": "agent_start"},
    {"type": "turn_start"},
    {
        "type": "message_update",
        "assistantMessageEvent": {"type": "text_delta", "delta": f"工具回显 OPENAI_API_KEY={SHARED_KEY}"},
    },
    {
        "type": "turn_end",
        "message": {"role": "assistant", "usage": {"input": 5, "output": 7, "totalTokens": 12}},
        "toolResults": [],
    },
    {"type": "agent_settled"},
]


def _context(tenant_id: str = "tenant-alpha", user_id: str = "user-a") -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        user_id=user_id,
        role="member",
        audience=AUDIENCE,
        expires_at=time.time() + 600,
        auth_mode="accounts",
    )


def test_shared_e2b_run_redacts_the_host_key_and_reconciles_the_sandbox_id(tmp_path: Path) -> None:
    config = _e2b_config(tmp_path)
    sandbox = FakeSandbox()
    sandbox.readable["/home/user/events.jsonl"] = "\n".join(json.dumps(event) for event in _E2B_EVENTS)
    sandbox.readable["/home/user/workspace/report.md"] = f"# 报告\n\n共享 key: {SHARED_KEY}\n"
    sandbox.workspace_files = ("report.md",)
    storage = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store=storage, sandbox_factory=lambda **_: sandbox)
    record = manager.start("合成问题", _context())
    final = manager.wait(record.run_id, timeout=30)

    assert final is not None and final.status == "finished"
    assert final.sandbox_id == "fake-sandbox"
    assert final.usage and final.usage.get("requests") == 1
    events = storage.events_path(record.run_id).read_text(encoding="utf-8")
    assert SHARED_KEY not in events
    assert "[REDACTED]" in events
    assert sandbox.killed is True
    assert final.report_path is not None
    report = Path(final.report_path).read_text(encoding="utf-8")
    assert SHARED_KEY not in report and "[REDACTED]" in report


def test_shared_e2b_followup_keeps_the_session_and_resource_boundaries(tmp_path: Path) -> None:
    """The E2B continuation path is unchanged under the ``shared`` policy."""
    config = _e2b_config(tmp_path)
    first = FakeSandbox()
    first.sandbox_id = "fake-sandbox-1"
    first.readable["/home/user/events.jsonl"] = "\n".join(
        json.dumps(event) for event in [{"type": "session", "id": "same-session"}, *_E2B_EVENTS]
    )
    first.readable["/home/user/workspace/report.md"] = "# 报告\n\n来自沙箱。\n"
    first.workspace_files = ("report.md",)
    first.session_files = ("research.jsonl",)
    first.readable["/home/user/sessions/research.jsonl"] = b'{"id":"same-session","turn":1}\n'

    second = FakeSandbox()
    second.sandbox_id = "fake-sandbox-2"
    second.readable["/home/user/events.jsonl"] = "\n".join(
        json.dumps(event) for event in [{"type": "session", "id": "same-session"}, *_E2B_EVENTS]
    )
    second.session_files = ("continued.jsonl",)
    second.readable["/home/user/sessions/continued.jsonl"] = b'{"id":"same-session","turn":2}\n'

    sandboxes = iter([first, second])
    storage = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store=storage, sandbox_factory=lambda **_: next(sandboxes))
    parent = manager.start("初始问题", _context())
    original = manager.wait(parent.run_id, timeout=30)
    assert original is not None and original.status == "finished"
    assert original.session_id == "same-session" and original.sandbox_id == "fake-sandbox-1"

    deadline = time.monotonic() + 5
    while manager.active_run_id is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.active_run_id is None

    child = manager.start("追问细节", _context(), parent_run_id=parent.run_id)
    final = manager.wait(child.run_id, timeout=30)
    assert final is not None and final.status == "finished"
    assert final.parent_run_id == parent.run_id and final.sandbox_id == "fake-sandbox-2"
    assert final.session_id == original.session_id
    assert "--session /home/user/sessions/continued.jsonl" in second.command
    assert SHARED_KEY not in second.command
    assert second.written["/home/user/sessions/continued.jsonl"] == '{"id":"same-session","turn":1}\n'
    assert Path(final.session_file).read_bytes() == second.readable["/home/user/sessions/continued.jsonl"]
    assert first.killed is True and second.killed is True


# -- account-mode HTTP surface -------------------------------------------------


class AdapterBehavior:
    def __init__(self) -> None:
        self.hold = False
        self.frames: tuple[dict, ...] = ()
        self.report: str | None = None
        self.created: list["FakeAdapter"] = []


class FakeAdapter:
    def __init__(self, spec, *, on_frame, behavior: AdapterBehavior) -> None:
        self.spec = spec
        self._on_frame = on_frame
        self._behavior = behavior
        self.runtime_dir = spec.runtime.directory
        self.auth_key = _auth_key(spec.runtime.directory)
        self.web_search_present = (spec.runtime.directory / "web-search.json").is_file()
        behavior.created.append(self)

    def start(self) -> None:
        if self._behavior.report is not None:
            self.spec.workspace.mkdir(parents=True, exist_ok=True)
            (self.spec.workspace / "report.md").write_text(self._behavior.report, encoding="utf-8")
        for frame in self._behavior.frames:
            self._on_frame(frame)

    def deliver_input(self) -> None:
        pass

    def session_info(self):
        return None

    def identity(self):
        return {}

    def wait(self, *, cancel, deadline):
        if not self._behavior.hold:
            return ExecutionOutcome(settled=True)
        while time.monotonic() < deadline:
            if cancel():
                break
            time.sleep(0.01)
        return ExecutionOutcome(settled=True, cancelled=cancel())

    def cancel(self) -> None:
        pass

    def recover_artifacts(self, destination):
        return ArtifactRecovery()

    def close(self):
        return CleanupReport(released=True)


def _auth_key(directory: Path) -> str:
    try:
        document = json.loads((directory / "auth.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    for entry in document.values():
        if isinstance(entry, dict) and isinstance(entry.get("key"), str):
            return entry["key"]
    return ""


@pytest.fixture()
def behavior(monkeypatch: pytest.MonkeyPatch) -> AdapterBehavior:
    state = AdapterBehavior()

    def build(spec, **kwargs):
        return FakeAdapter(spec, on_frame=kwargs["on_frame"], behavior=state)

    monkeypatch.setattr("pi_glink.kernel.runner.build_adapter", build)
    return state


def start_server(instance: PiGlinkServer) -> threading.Thread:
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    return thread


def stop_server(instance: PiGlinkServer, thread: threading.Thread) -> None:
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=5)


@pytest.fixture()
def shared_server(tmp_path: Path, behavior: AdapterBehavior):
    instance = create_server(build_config(shared_values(tmp_path), REPO_ROOT))
    thread = start_server(instance)
    try:
        yield instance
    finally:
        stop_server(instance, thread)


def raw_request(
    server: PiGlinkServer,
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}{path}", data=payload, method=method
    )
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


class Client:
    def __init__(self, server: PiGlinkServer) -> None:
        self.server = server
        self.cookie: str | None = None
        self.csrf: str | None = None

    def call(self, path, *, method="GET", body=None, headers=None):
        status, response_headers, raw = self.raw(path, method=method, body=body, headers=headers)
        payload = json.loads(raw) if raw else None
        return status, payload, response_headers

    def raw(self, path, *, method="GET", body=None, headers=None):
        sent = dict(headers or {})
        if self.cookie:
            sent.setdefault("Cookie", self.cookie)
        if method == "POST" and self.csrf:
            sent.setdefault(CSRF, self.csrf)
        status, response_headers, raw = raw_request(
            self.server, path, method=method, body=body, headers=sent
        )
        set_cookie = response_headers.get("Set-Cookie")
        if set_cookie:
            self.cookie = set_cookie.split(";", 1)[0]
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("csrfToken"), str):
                self.csrf = payload["csrfToken"]
        return status, response_headers, raw

    def login(self, email: str) -> None:
        status, payload, _headers = self.call(
            "/api/auth/login", method="POST", body={"email": email, "password": PASSWORD}
        )
        assert status == 200, payload

    def start_run(self, prompt: str, **extra) -> tuple[int, dict]:
        return self.call("/api/runs", method="POST", body={"prompt": prompt, **extra})[:2]


def bootstrap_user(server: PiGlinkServer, tenant: str, email: str, *, name: str = "成员") -> None:
    store = server.accounts
    store.upsert_tenant(tenant, f"组织 {tenant}")
    store.create_user(
        tenant_id=tenant, email=email, name=name, role="member", password=PASSWORD
    )


def wait_for_terminal(server: PiGlinkServer, run_id: str, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = server.store.get(run_id)
        if record is not None and record.is_terminal:
            return record
        time.sleep(0.02)
    return server.store.get(run_id)


def test_shared_mode_serves_two_tenants_from_one_host_model(
    shared_server: PiGlinkServer, behavior: AdapterBehavior
) -> None:
    server = shared_server
    bootstrap_user(server, "tenant-alpha", "alpha@example.test")
    bootstrap_user(server, "tenant-beta", "beta@example.test")
    behavior.frames = (
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "delta": f"工具输出 OPENAI_API_KEY={SHARED_KEY}",
            },
        },
        {
            "type": "turn_end",
            "message": {"role": "assistant", "usage": {"input": 3, "output": 4, "totalTokens": 7}},
            "toolResults": [],
        },
    )
    behavior.report = f"# 合成报告\n\n共享 key: {SHARED_KEY}\n"

    alpha = Client(server)
    beta = Client(server)
    alpha.login("alpha@example.test")
    beta.login("beta@example.test")

    # Both organisations are told the shared model is configured, without any
    # per-tenant key and without echoing the host credential.
    for client in (alpha, beta):
        status, config, _headers = client.call("/api/config")
        assert status == 200
        assert config["configured"]["model"] is True
        assert SHARED_KEY not in json.dumps(config)

    status, payload = alpha.start_run(f"研究 {SHARED_KEY}")
    assert status == 201, payload
    run_id = payload["run"]["runId"]
    final = wait_for_terminal(server, run_id)
    assert final is not None and final.status == "finished"
    assert final.usage and final.usage.get("requests") == 1

    # The run used the host credential, not a tenant entry.
    assert behavior.created[-1].auth_key == SHARED_KEY
    assert behavior.created[-1].web_search_present is True
    # The credential is redacted on every product surface.
    events = server.store.events_path(run_id).read_text(encoding="utf-8")
    assert SHARED_KEY not in events and "[REDACTED]" in events
    assert SHARED_KEY not in final.prompt
    status, _headers, raw = alpha.raw(f"/api/runs/{run_id}/report")
    assert status == 200
    # The artifact copy was already redacted at recovery, so the download path
    # has nothing left to change; either way the served bytes are clean.
    assert _headers.get("X-Redacted") == "false"
    assert SHARED_KEY not in raw.decode("utf-8") and "[REDACTED]" in raw.decode("utf-8")
    status, listing, _headers = alpha.call("/api/runs")
    assert status == 200
    assert run_id in json.dumps(listing) and SHARED_KEY not in json.dumps(listing)

    # Cross-tenant reads stay invisible: same 404 as an unknown run.
    for path in (
        f"/api/runs/{run_id}",
        f"/api/runs/{run_id}/report",
        f"/api/runs/{run_id}/events",
    ):
        status, cross, _headers = beta.call(path)
        assert status == 404, (path, status)
        assert run_id in json.dumps(cross)
    status, stop, _headers = beta.call(f"/api/runs/{run_id}/stop", method="POST")
    assert status == 404 and stop["error"] == "unknown_run"
    status, followup = beta.start_run("追问", parentRunId=run_id)
    assert status == 404 and followup["error"] == "unknown_run"
    status, listing, _headers = beta.call("/api/runs")
    assert status == 200 and listing["runs"] == []


def test_shared_mode_keeps_the_quota_per_tenant(
    shared_server: PiGlinkServer, behavior: AdapterBehavior
) -> None:
    server = shared_server
    bootstrap_user(server, "tenant-alpha", "alpha2@example.test")
    bootstrap_user(server, "tenant-beta", "beta2@example.test")
    alpha = Client(server)
    beta = Client(server)
    alpha.login("alpha2@example.test")
    beta.login("beta2@example.test")
    behavior.hold = True

    alpha_runs = [alpha.start_run(f"alpha {index}") for index in range(2)]
    assert [status for status, _payload in alpha_runs] == [201, 201]
    status, payload = alpha.start_run("alpha 第三条")
    assert status == 429
    assert payload["scope"] == "tenant" and payload["limit"] == 2

    beta_runs = [beta.start_run(f"beta {index}") for index in range(2)]
    assert [status for status, _payload in beta_runs] == [201, 201]
    status, payload = beta.start_run("beta 第三条")
    assert status == 429

    for (status, payload), client in [
        (alpha_runs[0], alpha),
        (alpha_runs[1], alpha),
        (beta_runs[0], beta),
        (beta_runs[1], beta),
    ]:
        assert status == 201
        run_id = payload["run"]["runId"]
        stop_status, _stop, _headers = client.call(f"/api/runs/{run_id}/stop", method="POST")
        assert stop_status == 200
        assert wait_for_terminal(server, run_id).status == "cancelled"


def test_shared_mode_refuses_client_supplied_model_fields(
    shared_server: PiGlinkServer, behavior: AdapterBehavior
) -> None:
    server = shared_server
    bootstrap_user(server, "tenant-alpha", "alpha3@example.test")
    client = Client(server)
    client.login("alpha3@example.test")

    for field, value in (
        ("modelApiKey", SHARED_KEY),
        ("model", "evil/model"),
        ("apiKey", "evil-key"),
        ("baseUrl", "https://evil.invalid/v1"),
        ("webSearchKey", "evil-search"),
    ):
        status, payload, _headers = client.call(
            "/api/runs", method="POST", body={"prompt": "x", field: value}
        )
        assert status == 400, (field, status, payload)
        assert payload["error"] == "invalid_request"
    assert behavior.created == [], "a refused request must not start an execution face"
    assert server.store.list(limit=None) == []

    # A query parameter or a header cannot relabel the model either: the run
    # still renders the frozen host credential.
    status, payload = client.call(
        "/api/runs?model=evil/model",
        method="POST",
        body={"prompt": "合法请求"},
        headers={"X-Pi-Glink-Model": "evil/model"},
    )[:2]
    assert status == 201, payload
    run_id = payload["run"]["runId"]
    final = wait_for_terminal(server, run_id)
    assert final is not None and final.status == "finished"
    assert behavior.created[0].auth_key == SHARED_KEY


def test_shared_mode_bootstrap_uses_the_host_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--create-invite`` validates the shared host config, not a tenant entry."""
    config = build_config(shared_values(tmp_path), REPO_ROOT)
    monkeypatch.setattr("pi_glink.app.load_config", lambda: config)
    code = app_main(
        [
            "--create-invite",
            "--tenant",
            "tenant-new",
            "--tenant-name",
            "新组织",
            "--email",
            "admin@example.test",
            "--role",
            "admin",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "邀请已创建" in captured.out
    store = AccountsStore(config.accounts_db)
    try:
        assert store.get_tenant("tenant-new").name == "新组织"
    finally:
        store.close()

    # The defensive refusal: a shared config that somehow lost its key must not
    # bootstrap an organisation that cannot run.
    broken = replace(config, openai_api_key="")
    monkeypatch.setattr("pi_glink.app.load_config", lambda: broken)
    code = app_main(["--create-invite", "--tenant", "tenant-other", "--email", "other@example.test"])
    captured = capsys.readouterr()
    assert code == 2
    assert "OPENAI_API_KEY" in captured.err


def test_shared_mode_invite_registration_then_run_uses_host_credentials(
    shared_server: PiGlinkServer, behavior: AdapterBehavior
) -> None:
    """The full SaaS onboarding: invite -> register -> login -> shared run."""
    server = shared_server
    server.accounts.upsert_tenant("tenant-alpha", "组织甲")
    _record, code = server.accounts.create_invitation(
        tenant_id="tenant-alpha", email="invitee@example.test", role="member", created_by="operator-cli"
    )
    client = Client(server)
    status, payload, _headers = client.call(
        "/api/invitations/check", method="POST", body={"token": code}
    )
    assert status == 200 and payload["valid"] is True
    assert payload["invitation"]["tenant"]["name"] == "组织甲"
    status, payload, _headers = client.call(
        "/api/auth/register",
        method="POST",
        body={"invite": code, "email": "invitee@example.test", "name": "新人", "password": PASSWORD},
    )
    assert status == 201, payload
    client.login("invitee@example.test")
    status, payload = client.start_run("注册后的研究")
    assert status == 201, payload
    final = wait_for_terminal(server, payload["run"]["runId"])
    assert final is not None and final.status == "finished"
    assert behavior.created[-1].auth_key == SHARED_KEY


def test_shared_gateway_mode_is_equally_explicit_and_uses_host_credentials(
    tmp_path: Path, behavior: AdapterBehavior
) -> None:
    secret = tmp_path / "gateway.key"
    secret.write_bytes(SECRET)
    secret.chmod(0o600)
    values = shared_values(
        tmp_path,
        PI_GLINK_AUTH_MODE="gateway",
        PI_GLINK_GATEWAY_SECRET_FILE=str(secret),
        PI_GLINK_TENANCY_AUDIENCE=AUDIENCE,
    )
    server = create_server(build_config(values, REPO_ROOT))
    thread = start_server(server)
    try:
        token = issue_assertion(
            SECRET, tenant_id="tenant-x", user_id="user-x", audience=AUDIENCE, ttl_seconds=120
        )
        status, _headers, raw = raw_request(
            server,
            "/api/runs",
            method="POST",
            body={"prompt": "网关共享模式"},
            headers={"X-Pi-Glink-Identity": token},
        )
        assert status == 201, raw
        run_id = json.loads(raw)["run"]["runId"]
        final = wait_for_terminal(server, run_id)
        assert final is not None and final.status == "finished"
        assert behavior.created[-1].auth_key == SHARED_KEY
    finally:
        stop_server(server, thread)
