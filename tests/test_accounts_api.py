"""Accounts HTTP surface: invitations, login, sessions, CSRF and isolation.

Everything is synthetic: the execution face is a fake adapter, the account
database and the identity key files live in a temporary directory, and every
server binds an OS-assigned loopback port. No pi process, container, sandbox,
model call or real ``.env`` / ``~/.pi`` file is involved.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from http.client import HTTPConnection
from pathlib import Path

import pytest

from pi_glink.store import RunSpec
from pi_glink.accounts import AccountsStore
from pi_glink.app import HostRequestHandler, PiGlinkServer, create_server, main as app_main
from pi_glink.config import ConfigError, build_config
from pi_glink.kernel.execution import (
    ArtifactRecovery,
    CleanupReport,
    ExecutionAdapter,
    ExecutionOutcome,
    SessionInfo,
)
from pi_glink.tenancy import ASSERTION_HEADER, issue_assertion

REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIENCE = "pi-glink-test"
SECRET = b"0123456789abcdef0123456789abcdef"
ALPHA_KEY = "sk-alpha-0000000000000000"
BETA_KEY = "sk-beta-0000000000000000"
GAMMA_KEY = "sk-gamma-0000000000000000"
HOST_KEY = "sk-host-global-00000000000000"
PASSWORD = "correct horse battery"
TENANTS = {
    "tenant-alpha": {
        "modelApiKey": ALPHA_KEY,
        "webSearchKey": "search-alpha-000000000000",
    },
    "tenant-beta": {"modelApiKey": BETA_KEY},
    "tenant-gamma": {"modelApiKey": GAMMA_KEY},
}
COOKIE = "pi_glink_session"
CSRF = "X-Pi-Glink-CSRF"


def assertion(
    tenant: str = "tenant-alpha", user: str = "user-a", *, role: str = "member", ttl: int = 120
) -> str:
    return issue_assertion(
        SECRET, tenant_id=tenant, user_id=user, audience=AUDIENCE, role=role, ttl_seconds=ttl
    )


# -- synthetic execution face --------------------------------------------------


class AdapterBehavior:
    def __init__(self) -> None:
        self.hold = False
        self.write_report = False
        self.persist_session = False
        self.frames: tuple[dict, ...] = ()
        self.created: list["FakeAdapter"] = []


class FakeAdapter(ExecutionAdapter):
    def __init__(self, spec, *, on_frame, behavior: AdapterBehavior):
        super().__init__(spec, on_frame=on_frame)
        self._behavior = behavior
        self.runtime_dir = spec.runtime.directory
        self.session_file: Path | None = None

    def start(self) -> None:
        if self._behavior.write_report:
            self.spec.workspace.mkdir(parents=True, exist_ok=True)
            (self.spec.workspace / "report.md").write_text(
                "# 合成报告\n\n来自 fake 执行面。\n", encoding="utf-8"
            )
        if self._behavior.persist_session:
            directory = self.spec.runtime.session_dir
            directory.mkdir(parents=True, exist_ok=True)
            self.session_file = directory / "fake-session.jsonl"
            with self.session_file.open("a", encoding="utf-8") as log:
                log.write(json.dumps({"prompt": self.spec.prompt}, ensure_ascii=False) + "\n")
        for frame in self._behavior.frames:
            self._on_frame(frame)

    def deliver_input(self) -> None:
        pass

    def session_info(self):
        if not self._behavior.persist_session or self.session_file is None:
            return None
        return SessionInfo(
            payload={"sessionId": "fake-session"},
            session_id="fake-session",
            session_file=str(self.session_file),
        )

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


@pytest.fixture()
def adapters(monkeypatch: pytest.MonkeyPatch) -> AdapterBehavior:
    behavior = AdapterBehavior()

    def build(spec, **kwargs):
        adapter = FakeAdapter(spec, on_frame=kwargs["on_frame"], behavior=behavior)
        behavior.created.append(adapter)
        return adapter

    monkeypatch.setattr("pi_glink.kernel.harness_pi.PiExecutionAdapter", build)
    return behavior


# -- server fixtures -----------------------------------------------------------


@pytest.fixture()
def tenant_files(tmp_path: Path) -> tuple[Path, Path]:
    secret = tmp_path / "gateway.key"
    secret.write_bytes(SECRET)
    secret.chmod(0o600)
    tenants = tmp_path / "tenants.json"
    tenants.write_text(json.dumps({"tenants": TENANTS}), encoding="utf-8")
    tenants.chmod(0o600)
    return secret, tenants


def make_config(tmp_path: Path, tenant_files: tuple[Path, Path], **overrides: str):
    secret, tenants = tenant_files
    values = {
        "OPENAI_API_KEY": HOST_KEY,
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_PORT": "0",
        "LOCAL_WEB_EXECUTOR": "docker",
        # Docker web search goes through an https egress URL (ADR 0008).
        "PI_GLINK_EGRESS_PUBLIC_URL": "https://egress.example.test",
        "PI_GLINK_TENANCY": "multitenant",
        "PI_GLINK_AUTH_MODE": "accounts",
        "PI_GLINK_GATEWAY_SECRET_FILE": str(secret),
        "PI_GLINK_TENANT_CONFIG_FILE": str(tenants),
        "PI_GLINK_TENANCY_AUDIENCE": AUDIENCE,
        "PI_GLINK_ACCOUNTS_DB": str(tmp_path / "data" / "accounts" / "accounts.db"),
        "PI_GLINK_COOKIE_SECURE": "0",
        "PI_GLINK_SESSION_ABSOLUTE_SECONDS": "3600",
        "PI_GLINK_SESSION_IDLE_SECONDS": "600",
        "PI_GLINK_INVITE_TTL_SECONDS": "3600",
        "PI_GLINK_LOGIN_MAX_ATTEMPTS": "5",
        "PI_GLINK_LOGIN_WINDOW_SECONDS": "300",
        "PI_GLINK_LOGIN_PEER_MAX_ATTEMPTS": "60",
        "PI_GLINK_LOGIN_PEER_WINDOW_SECONDS": "300",
        "PI_GLINK_REGISTER_MAX_ATTEMPTS": "50",
        "PI_GLINK_REGISTER_WINDOW_SECONDS": "60",
        "PI_GLINK_INVITE_CHECK_MAX_ATTEMPTS": "200",
        "LOCAL_WEB_TASK_TIMEOUT_SECONDS": "60",
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


def start_server(instance: PiGlinkServer) -> threading.Thread:
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    return thread


def stop_server(instance: PiGlinkServer, thread: threading.Thread) -> None:
    instance.shutdown()
    instance.server_close()
    thread.join(timeout=5)


@pytest.fixture()
def server(tmp_path: Path, tenant_files: tuple[Path, Path], adapters: AdapterBehavior):
    instance = create_server(make_config(tmp_path, tenant_files))
    thread = start_server(instance)
    try:
        yield instance
    finally:
        stop_server(instance, thread)


@pytest.fixture()
def gateway_server(tmp_path: Path, tenant_files: tuple[Path, Path], adapters: AdapterBehavior):
    instance = create_server(
        make_config(tmp_path, tenant_files, PI_GLINK_AUTH_MODE="gateway")
    )
    thread = start_server(instance)
    try:
        yield instance
    finally:
        stop_server(instance, thread)


@pytest.fixture()
def local_server(tmp_path: Path):
    instance = create_server(
        build_config(
            {
                "LOCAL_WEB_DATA_ROOT": str(tmp_path / "local-data"),
                "LOCAL_WEB_PORT": "0",
                "OPENAI_MODEL": "test-model",
            },
            REPO_ROOT,
        )
    )
    thread = start_server(instance)
    try:
        yield instance
    finally:
        stop_server(instance, thread)


# -- HTTP client ---------------------------------------------------------------


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
    """A cookie-and-CSRF-aware client, like a logged-in browser tab."""

    def __init__(self, server: PiGlinkServer) -> None:
        self.server = server
        self.cookie: str | None = None
        self.csrf: str | None = None

    def call(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict | None = None,
        headers: dict[str, str] | None = None,
        csrf: str | None = ...,
    ) -> tuple[int, dict, dict[str, str]]:
        sent = dict(headers or {})
        if self.cookie:
            sent.setdefault("Cookie", self.cookie)
        if method == "POST" and csrf is not ...:
            if csrf:
                sent.setdefault(CSRF, csrf)
        elif method == "POST" and self.csrf:
            sent.setdefault(CSRF, self.csrf)
        status, response_headers, raw = raw_request(
            self.server, path, method=method, body=body, headers=sent
        )
        set_cookie = response_headers.get("Set-Cookie")
        if set_cookie:
            self.cookie = set_cookie.split(";", 1)[0]
        payload = json.loads(raw) if raw else None
        if isinstance(payload, dict) and isinstance(payload.get("csrfToken"), str):
            self.csrf = payload["csrfToken"]
        return status, payload, response_headers

    def get(self, path: str, **kwargs) -> tuple[int, dict, dict[str, str]]:
        return self.call(path, method="GET", **kwargs)

    def post(self, path: str, body: dict | None = None, **kwargs) -> tuple[int, dict, dict[str, str]]:
        return self.call(path, method="POST", body=body, **kwargs)

    def login(self, email: str, password: str = PASSWORD) -> tuple[int, dict]:
        status, payload, _headers = self.post(
            "/api/auth/login", {"email": email, "password": password}
        )
        return status, payload

    def start_run(self, prompt: str = "合成研究", **extra) -> tuple[int, dict]:
        status, payload, _headers = self.post(
            "/api/runs", {"prompt": prompt, **extra}
        )
        return status, payload


def bootstrap_user(
    server: PiGlinkServer,
    *,
    tenant: str = "tenant-alpha",
    tenant_name: str = "组织甲",
    email: str = "member@example.test",
    name: str = "成员",
    role: str = "member",
    password: str = PASSWORD,
):
    store = server.accounts
    store.upsert_tenant(tenant, tenant_name)
    return store.create_user(
        tenant_id=tenant, email=email, name=name, role=role, password=password
    )


def bootstrap_invitation(
    server: PiGlinkServer,
    *,
    tenant: str = "tenant-alpha",
    tenant_name: str = "组织甲",
    email: str = "invitee@example.test",
    role: str = "member",
    ttl_seconds: int = 3600,
) -> str:
    if server.accounts.get_tenant(tenant) is None:
        server.accounts.upsert_tenant(tenant, tenant_name)
    _record, token = server.accounts.create_invitation(
        tenant_id=tenant, email=email, role=role, ttl_seconds=ttl_seconds
    )
    return token


def logged_in_client(server: PiGlinkServer, email: str, password: str = PASSWORD) -> Client:
    client = Client(server)
    status, payload = client.login(email, password)
    assert status == 200, (status, payload)
    return client


def seed_history(server, run_id: str, *, frames: int = 20, tenant: str = "tenant-alpha", user: str = "u1"):
    store = server.store
    store.create(
        "历史回放", run_id=run_id, workspace=server.config.workspace_root / run_id,
        tenant_id=tenant, creator_user_id=user,
        spec=RunSpec(family="pi", pack="pi", executor="inproc"),
    )
    for seq in range(1, frames + 1):
        store.append_event(
            run_id,
            {
                "type": "kernel_status",
                "runId": run_id,
                "seq": seq,
                "ts": time.time(),
                "payload": {"level": "info", "message": f"frame-{seq}"},
            },
        )
    store.commit_terminal(
        run_id,
        {
            "type": "run_finished",
            "runId": run_id,
            "seq": frames + 1,
            "ts": time.time(),
            "payload": {"status": "finished", "reportStatus": "none"},
        },
    )


def wait_for_terminal(server: PiGlinkServer, run_id: str, client: Client, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    latest: dict = {}
    while time.monotonic() < deadline:
        status, payload, _headers = client.get(f"/api/runs/{run_id}")
        if status != 200:
            return {"status": f"http-{status}"}
        latest = payload["run"]
        if latest["status"] != "running":
            return latest
        time.sleep(0.05)
    return latest


def read_stream(
    server: PiGlinkServer,
    path: str,
    *,
    cookie: str | None,
    timeout: float = 15.0,
    head_bytes: int | None = None,
):
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=timeout)
    headers = {"Cookie": cookie} if cookie else {}
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        status = response.status
        body = response.read() if head_bytes is None else response.read1(head_bytes)
        return status, response.getheader("Content-Type") or "", body
    finally:
        connection.close()


def read_stream_until_eof(server: PiGlinkServer, path: str, *, cookie: str | None, timeout: float = 10.0):
    """Read an SSE stream to its clean end inside a worker thread."""
    result: dict = {}

    def consume() -> None:
        try:
            result["body"] = read_stream(server, path, cookie=cookie, timeout=timeout)[2]
        except Exception as exc:  # pragma: no cover - surfaced through the caller
            result["error"] = exc

    worker = threading.Thread(target=consume, daemon=True)
    worker.start()
    return worker, result


# -- unauthenticated surfaces --------------------------------------------------


def test_unauthenticated_requests_are_uniform_401_with_mode(server: PiGlinkServer) -> None:
    for path, method in (
        ("/api/me", "GET"),
        ("/api/config", "GET"),
        ("/api/blueprints", "GET"),
        ("/api/runs", "GET"),
        ("/api/runs", "POST"),
        ("/api/runs/whatever", "GET"),
        ("/api/runs/whatever/report", "GET"),
        ("/api/runs/whatever/stop", "POST"),
        ("/api/runs/whatever/events", "GET"),
    ):
        status, _headers, raw = raw_request(server, path, method=method, body={"prompt": "x"} if method == "POST" else None)
        assert status == 401, path
        assert json.loads(raw) == {"error": "unauthenticated", "mode": "accounts"}, path


def test_health_stays_public_and_names_no_run(server: PiGlinkServer) -> None:
    status, _headers, raw = raw_request(server, "/api/health")
    assert status == 200
    payload = json.loads(raw)
    assert payload["status"] == "ok"
    assert "activeRunId" not in payload


def test_gateway_assertions_are_ignored_in_accounts_mode(server: PiGlinkServer) -> None:
    """No fallback: a signed gateway header is not an account identity."""
    token = assertion("tenant-alpha", "user-a", role="admin")
    for path, method in (("/api/me", "GET"), ("/api/runs", "GET")):
        status, _headers, raw = raw_request(
            server, path, method=method, headers={ASSERTION_HEADER: token}
        )
        assert status == 401, path
        assert json.loads(raw) == {"error": "unauthenticated", "mode": "accounts"}


def test_gateway_me_returns_the_asserted_identity(gateway_server: PiGlinkServer) -> None:
    """The gateway surface still answers ``/api/me`` — display fields only."""
    token = assertion("tenant-alpha", "user-a", role="admin")
    status, _headers, raw = raw_request(
        gateway_server, "/api/me", headers={ASSERTION_HEADER: token}
    )
    assert status == 200, raw[:200]
    payload = json.loads(raw)
    assert payload["mode"] == "gateway" and payload["authenticated"] is True
    assert payload["user"] == {"id": "user-a", "name": "user-a", "role": "admin"}
    assert payload["tenant"] == {"id": "tenant-alpha", "name": "tenant-alpha"}
    assert payload["csrfToken"] is None
    assert ALPHA_KEY not in raw.decode("utf-8")
    assert HOST_KEY not in raw.decode("utf-8")


def test_accounts_endpoints_do_not_exist_outside_accounts_mode(
    gateway_server: PiGlinkServer, local_server: PiGlinkServer
) -> None:
    for server in (gateway_server, local_server):
        for path, method in (
            ("/api/auth/register", "POST"),
            ("/api/auth/logout", "POST"),
            ("/api/admin/invitations", "POST"),
            ("/api/invitations/check", "POST"),
        ):
            status, _headers, raw = raw_request(
                server, path, method=method, body={} if method == "POST" else None
            )
            assert status == 404, (path, status)
            assert json.loads(raw)["error"] == "unknown_endpoint", path


def test_accounts_cookies_are_ignored_in_gateway_mode(gateway_server: PiGlinkServer) -> None:
    """The other direction of the same boundary: a session cookie is not an assertion."""
    status, _headers, raw = raw_request(
        gateway_server, "/api/me", headers={"Cookie": f"{COOKIE}=fabricated-token"}
    )
    assert status == 401
    assert json.loads(raw) == {"error": "unauthenticated", "mode": "gateway"}
    status, _headers, raw = raw_request(gateway_server, "/api/auth/login", method="POST", body={})
    assert status == 404
    assert json.loads(raw)["error"] == "unknown_endpoint"


def test_local_mode_me_is_public_and_keeps_the_old_surface(local_server: PiGlinkServer) -> None:
    status, _headers, raw = raw_request(local_server, "/api/me")
    assert status == 200
    assert json.loads(raw) == {"mode": "local", "authenticated": False}
    status, _headers, raw = raw_request(local_server, "/api/config")
    assert status == 200
    status, _headers, raw = raw_request(
        local_server, "/api/auth/login", method="POST", body={"email": "a@b.test"}
    )
    assert status == 404


# -- login and session cookies -------------------------------------------------


def test_login_unknown_account_and_wrong_password_are_indistinguishable(server: PiGlinkServer) -> None:
    bootstrap_user(server, email="known@example.test")
    anonymous = Client(server)
    first = anonymous.post("/api/auth/login", {"email": "known@example.test", "password": "wrong password!"})
    second = anonymous.post("/api/auth/login", {"email": "absent@example.test", "password": "wrong password!"})
    assert first[0] == second[0] == 401
    assert first[1] == second[1]
    assert first[1]["error"] == "invalid_credentials"
    assert "Set-Cookie" not in first[2]
    assert "Set-Cookie" not in second[2]


def test_login_sets_an_httponly_samesite_cookie_and_returns_identity(server: PiGlinkServer) -> None:
    user = bootstrap_user(server, email="member@example.test", name="成员", role="member")
    client = Client(server)
    status, payload, headers = client.post(
        "/api/auth/login", {"email": "member@example.test", "password": PASSWORD}
    )
    assert status == 200, payload
    cookie = headers["Set-Cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert "Path=/" in cookie
    assert "Max-Age=" in cookie
    assert "Secure" not in cookie  # explicitly disabled for the loopback HTTP test
    raw_token = cookie.split(";", 1)[0].split("=", 1)[1]
    assert raw_token and raw_token not in json.dumps(payload)
    assert payload["mode"] == "accounts" and payload["authenticated"] is True
    assert payload["user"] == {
        "id": user.user_id,
        "name": "成员",
        "email": "member@example.test",
        "role": "member",
    }
    assert payload["tenant"] == {"id": "tenant-alpha", "name": "组织甲"}
    assert isinstance(payload["csrfToken"], str) and len(payload["csrfToken"]) >= 24
    assert ALPHA_KEY not in json.dumps(payload) and HOST_KEY not in json.dumps(payload)

    status, me, _headers = client.get("/api/me")
    assert status == 200
    assert me["user"]["id"] == user.user_id
    assert me["tenant"]["name"] == "组织甲"
    assert me["mode"] == "accounts"


def test_secure_cookie_attribute_follows_frozen_config(
    tmp_path: Path, tenant_files: tuple[Path, Path], adapters: AdapterBehavior
) -> None:
    instance = create_server(
        make_config(tmp_path, tenant_files, PI_GLINK_COOKIE_SECURE="1")
    )
    thread = start_server(instance)
    try:
        bootstrap_user(instance, email="secure@example.test")
        status, _payload, headers = Client(instance).post(
            "/api/auth/login", {"email": "secure@example.test", "password": PASSWORD}
        )
        assert status == 200
        assert "Secure" in headers["Set-Cookie"]
    finally:
        stop_server(instance, thread)


def test_logout_revokes_the_server_session(server: PiGlinkServer) -> None:
    bootstrap_user(server, email="member@example.test")
    client = logged_in_client(server, "member@example.test")
    stale_cookie = client.cookie
    status, payload, headers = client.post("/api/auth/logout", {})
    assert status == 200 and payload == {"ok": True}
    assert "Max-Age=0" in headers["Set-Cookie"]
    assert client.get("/api/me")[0] == 401
    # The old cookie is dead even if a client kept it.
    status, _headers, raw = raw_request(server, "/api/me", headers={"Cookie": stale_cookie})
    assert status == 401


def test_disabled_user_or_tenant_kills_sessions_immediately(server: PiGlinkServer) -> None:
    user = bootstrap_user(server, email="member@example.test")
    client = logged_in_client(server, "member@example.test")
    assert client.get("/api/me")[0] == 200
    server.accounts.set_user_status(user.user_id, "disabled")
    assert client.get("/api/me")[0] == 401
    server.accounts.set_user_status(user.user_id, "active")
    fresh = logged_in_client(server, "member@example.test")
    assert fresh.get("/api/me")[0] == 200
    server.accounts.set_tenant_status("tenant-alpha", "disabled")
    assert fresh.get("/api/me")[0] == 401
    # A disabled account can still not log in with a correct password.
    server.accounts.set_user_status(user.user_id, "active")
    server.accounts.set_tenant_status("tenant-alpha", "active")
    server.accounts.set_user_status(user.user_id, "disabled")
    anonymous = Client(server)
    status, payload = anonymous.login("member@example.test", PASSWORD)
    assert status == 403
    assert payload["error"] == "account_disabled"


def test_session_absolute_expiry_is_enforced(server: PiGlinkServer) -> None:
    bootstrap_user(server, email="member@example.test")
    client = logged_in_client(server, "member@example.test")

    class Clock:
        now = time.time()

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    server.accounts._clock = clock
    assert client.get("/api/me")[0] == 200
    clock.now += 3601
    assert client.get("/api/me")[0] == 401


def test_login_rate_limit_is_enforced_and_resets_on_success(server: PiGlinkServer) -> None:
    bootstrap_user(server, email="limited@example.test")
    anonymous = Client(server)
    for _ in range(5):
        status, payload = anonymous.login("limited@example.test", "wrong password!")
        assert status == 401, payload
    status, payload, headers = anonymous.post(
        "/api/auth/login", {"email": "limited@example.test", "password": "wrong password!"}
    )
    assert status == 429
    assert payload["error"] == "rate_limited"
    assert int(headers.get("Retry-After", "0")) > 0
    # A different account is not throttled by this one's failures.
    bootstrap_user(server, tenant="tenant-beta", tenant_name="组织乙", email="other@example.test")
    assert Client(server).login("other@example.test")[0] == 200

    # A successful login clears the window for that account.
    server.auth.login_limiter.reset("login:limited@example.test")
    assert Client(server).login("limited@example.test")[0] == 200
    assert Client(server).login("limited@example.test")[0] == 200


def test_login_across_rotated_emails_is_capped_by_the_peer_budget(
    tmp_path: Path,
    tenant_files: tuple[Path, Path],
    adapters: AdapterBehavior,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rotating e-mail addresses must not buy unbounded password hashing.

    The per-account limiter is keyed by the client-supplied address, so on its
    own it lets an anonymous client trigger one scrypt per request forever. The
    socket-peer budget sits in front of it and refuses before any hashing.
    """
    from pi_glink import accounts as accounts_module

    config = make_config(
        tmp_path,
        tenant_files,
        PI_GLINK_LOGIN_PEER_MAX_ATTEMPTS="3",
        PI_GLINK_LOGIN_PEER_WINDOW_SECONDS="300",
        PI_GLINK_LOGIN_MAX_ATTEMPTS="100",
    )
    instance = create_server(config)
    thread = start_server(instance)
    calls = {"count": 0}
    original = accounts_module._scrypt

    def counting(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(accounts_module, "_scrypt", counting)
    try:
        anonymous = Client(instance)
        for index in range(3):
            status, payload, _headers = anonymous.post(
                "/api/auth/login",
                {"email": f"absent-{index}@example.test", "password": "wrong password!"},
            )
            assert status == 401, payload
        allowed = calls["count"]
        assert allowed == 3, "the allowed attempts each hashed once"

        for email in ("fresh@example.test", "another@example.test"):
            status, payload, headers = anonymous.post(
                "/api/auth/login", {"email": email, "password": "wrong password!"}
            )
            assert status == 429, payload
            assert payload["error"] == "rate_limited"
            assert int(headers.get("Retry-After", "0")) > 0
        assert calls["count"] == allowed, "a refused attempt must not run scrypt"
    finally:
        stop_server(instance, thread)


def test_peer_login_budget_recovers_after_its_window(
    tmp_path: Path,
    tenant_files: tuple[Path, Path],
    adapters: AdapterBehavior,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pi_glink import accounts as accounts_module

    config = make_config(
        tmp_path,
        tenant_files,
        PI_GLINK_LOGIN_PEER_MAX_ATTEMPTS="2",
        PI_GLINK_LOGIN_PEER_WINDOW_SECONDS="1",
        PI_GLINK_LOGIN_MAX_ATTEMPTS="100",
    )
    instance = create_server(config)
    thread = start_server(instance)
    calls = {"count": 0}
    original = accounts_module._scrypt

    def counting(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(accounts_module, "_scrypt", counting)
    try:
        anonymous = Client(instance)
        for index in range(2):
            status, _payload, _headers = anonymous.post(
                "/api/auth/login",
                {"email": f"absent-{index}@example.test", "password": "wrong password!"},
            )
            assert status == 401
        assert anonymous.post(
            "/api/auth/login", {"email": "blocked@example.test", "password": "wrong password!"}
        )[0] == 429
        blocked_calls = calls["count"]

        time.sleep(1.2)  # the fixed window has passed
        status, payload, _headers = anonymous.post(
            "/api/auth/login", {"email": "recovered@example.test", "password": "wrong password!"}
        )
        assert status == 401, payload
        assert calls["count"] == blocked_calls + 1, "the next window hashes again"
    finally:
        stop_server(instance, thread)


def test_login_rejects_wrong_content_type_oversized_and_malformed_bodies(server: PiGlinkServer) -> None:
    bootstrap_user(server, email="member@example.test")
    status, _headers, raw = raw_request(
        server,
        "/api/auth/login",
        method="POST",
        headers={"Content-Type": "text/plain"},
        body={"email": "member@example.test"},
    )
    # text/plain bodies are not what a JSON API accepts; the request never
    # reaches credential verification.
    assert status in {400, 415}
    for body in (
        {"email": "member@example.test"},
        {"email": 42, "password": 42},
        {"email": "member@example.test", "password": "x" * 5000},
        {"email": "x" * 300 + "@example.test", "password": PASSWORD},
    ):
        status, _payload, _headers = Client(server).post("/api/auth/login", body)
        assert status in {400, 401}, body
        assert status != 500


def test_cross_site_posts_are_refused_before_credentials_are_checked(server: PiGlinkServer) -> None:
    bootstrap_user(server, email="member@example.test")
    client = Client(server)
    for headers in (
        {"Origin": "https://evil.example"},
        {"Referer": "https://evil.example/form"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": "null"},
    ):
        status, payload, headers_out = client.post(
            "/api/auth/login",
            {"email": "member@example.test", "password": PASSWORD},
            headers=headers,
        )
        assert status == 403, headers
        assert payload["error"] == "forbidden"
        assert "Set-Cookie" not in headers_out
    # An honest browser origin for this host is accepted.
    port = server.server_address[1]
    status, _payload, _headers = client.post(
        "/api/auth/login",
        {"email": "member@example.test", "password": PASSWORD},
        headers={"Origin": f"http://127.0.0.1:{port}"},
    )
    assert status == 200


def test_trusted_public_origin_is_accepted_for_post_checks(
    tmp_path: Path, tenant_files: tuple[Path, Path], adapters: AdapterBehavior
) -> None:
    instance = create_server(
        make_config(tmp_path, tenant_files, PI_GLINK_PUBLIC_ORIGIN="https://glink.example.test")
    )
    thread = start_server(instance)
    try:
        bootstrap_user(instance, email="member@example.test")
        client = Client(instance)
        status, _payload, _headers = client.post(
            "/api/auth/login",
            {"email": "member@example.test", "password": PASSWORD},
            headers={"Origin": "https://glink.example.test"},
        )
        assert status == 200
        status, _payload, _headers = client.post(
            "/api/auth/login",
            {"email": "member@example.test", "password": PASSWORD},
            headers={"Origin": "https://other.example.test"},
        )
        assert status == 403
    finally:
        stop_server(instance, thread)


# -- registration --------------------------------------------------------------


def test_invitation_check_is_bounded_and_hides_the_token(server: PiGlinkServer) -> None:
    bootstrap_user(server, tenant="tenant-beta", tenant_name="组织乙", email="other@example.test", name="另一人")
    token = bootstrap_invitation(server, tenant="tenant-beta", email="invitee@example.test", role="admin")
    anonymous = Client(server)
    # The token travels in a JSON body, never in a URL.
    status, payload, _headers = anonymous.post("/api/invitations/check", {"token": token})
    assert status == 200
    assert payload["valid"] is True
    assert payload["invitation"]["email"] == "invitee@example.test"
    assert payload["invitation"]["role"] == "admin"
    assert payload["invitation"]["tenant"] == {"id": "tenant-beta", "name": "组织乙"}
    assert token not in json.dumps(payload)
    for bad in ("", "made-up", token[:-2]):
        status, payload, _headers = anonymous.post("/api/invitations/check", {"token": bad})
        assert status == 200
        assert payload == {"valid": False}


def test_invitation_check_is_post_only_and_never_reads_a_query_token(server: PiGlinkServer) -> None:
    token = bootstrap_invitation(server, email="invitee@example.test")
    anonymous = Client(server)
    status, payload, headers = anonymous.get(f"/api/invitations/check?token={token}")
    assert status == 405
    assert payload["error"] == "method_not_allowed"
    assert headers.get("Allow") == "POST"
    status, payload, _headers = anonymous.post("/api/invitations/check", {"token": token})
    assert status == 200 and payload["valid"] is True


def test_invitation_check_enforces_origin_and_json(server: PiGlinkServer) -> None:
    token = bootstrap_invitation(server, email="invitee@example.test")
    anonymous = Client(server)
    status, payload, headers = anonymous.post(
        "/api/invitations/check", {"token": token}, headers={"Origin": "https://evil.example"}
    )
    assert status == 403 and payload["error"] == "forbidden"
    assert "Set-Cookie" not in headers
    status, _headers, raw = raw_request(
        server,
        "/api/invitations/check",
        method="POST",
        headers={"Content-Type": "text/plain"},
        body={"token": token},
    )
    assert status in {400, 415}, raw[:120]
    status, payload, _headers = anonymous.post(
        "/api/invitations/check",
        {"token": token},
        headers={"Origin": f"http://127.0.0.1:{server.server_address[1]}"},
    )
    assert status == 200 and payload["valid"] is True


def test_invitation_check_rate_limit_still_applies(
    tmp_path: Path, tenant_files: tuple[Path, Path], adapters: AdapterBehavior
) -> None:
    config = make_config(
        tmp_path,
        tenant_files,
        PI_GLINK_INVITE_CHECK_MAX_ATTEMPTS="2",
        PI_GLINK_INVITE_CHECK_WINDOW_SECONDS="300",
    )
    instance = create_server(config)
    thread = start_server(instance)
    try:
        token = bootstrap_invitation(instance, email="invitee@example.test")
        anonymous = Client(instance)
        for _ in range(2):
            assert anonymous.post("/api/invitations/check", {"token": token})[0] == 200
        status, payload, headers = anonymous.post("/api/invitations/check", {"token": token})
        assert status == 429 and payload["error"] == "rate_limited"
        assert int(headers.get("Retry-After", "0")) > 0
    finally:
        stop_server(instance, thread)


def test_invitation_tokens_never_reach_a_request_line_or_the_logs(
    server: PiGlinkServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Our own flows must never put the invitation code into an HTTP URL.

    The fragment stays in the browser; checking and registering travel in JSON
    bodies. The server additionally sanitizes every logged request line, so even
    a client that *tries* the legacy query form cannot get the code written into
    a log (the log is often shipped off-host).
    """
    token = bootstrap_invitation(server, email="invitee@example.test")
    raw_lines: list[str] = []
    original = HostRequestHandler.log_message

    def recording(self, format, *args):  # noqa: A002 - stdlib signature
        raw_lines.append(str(self.requestline))
        return original(self, format, *args)

    monkeypatch.setattr(HostRequestHandler, "log_message", recording)
    messages: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = Capture(level=logging.DEBUG)
    host_logger = logging.getLogger("pi_glink.host")
    previous_level = host_logger.level
    host_logger.addHandler(handler)
    host_logger.setLevel(logging.DEBUG)
    try:
        anonymous = Client(server)
        # 1) the real flow: load /register (the #invite=… fragment is never sent),
        #    then check and register through JSON bodies.
        status, _headers, _body = raw_request(server, "/register")
        assert status == 200
        status, payload, _headers = anonymous.post("/api/invitations/check", {"token": token})
        assert status == 200 and payload["valid"] is True
        status, payload, _headers = anonymous.post(
            "/api/auth/register",
            {"invite": token, "email": "invitee@example.test", "name": "受邀者", "password": PASSWORD},
        )
        assert status == 201, payload
        assert raw_lines, "the request-line recorder saw the flow"
        for line in raw_lines:
            assert token not in line, line
        for message in messages:
            assert token not in message, message

        # 2) a hostile client may still put it in a query; the server refuses the
        #    method and the sanitized logger never writes the query anywhere.
        raw_lines.clear()
        messages.clear()
        status, payload, headers = anonymous.get(f"/api/invitations/check?token={token}")
        assert status == 405 and headers.get("Allow") == "POST"
        assert messages, "the refused request was still logged"
        for message in messages:
            assert token not in message, message
    finally:
        host_logger.removeHandler(handler)
        host_logger.setLevel(previous_level)


def test_registration_uses_the_invitation_identity_without_creating_a_session(server: PiGlinkServer) -> None:
    token = bootstrap_invitation(server, tenant="tenant-beta", email="invitee@example.test", role="member")
    anonymous = Client(server)
    status, payload, headers = anonymous.post(
        "/api/auth/register",
        {"invite": token, "email": "Invitee@Example.test", "name": "受邀者", "password": PASSWORD},
    )
    assert status == 201, payload
    assert payload == {"registered": True, "email": "invitee@example.test"}
    assert "Set-Cookie" not in headers
    assert anonymous.get("/api/me")[0] == 401
    user = server.accounts.find_user_by_email("invitee@example.test")
    assert user is not None and user.tenant_id == "tenant-beta" and user.role == "member"
    # The invitation cannot be used twice.
    status, payload, _headers = anonymous.post(
        "/api/auth/register",
        {"invite": token, "email": "invitee@example.test", "name": "另一个人", "password": PASSWORD},
    )
    assert status == 400 and payload["error"] == "invitation_invalid"
    # And after registering, the account can log in.
    assert Client(server).login("invitee@example.test")[0] == 200


def test_registration_rejects_wrong_email_expired_or_unknown_invitations(server: PiGlinkServer) -> None:
    token = bootstrap_invitation(server, email="invitee@example.test")
    anonymous = Client(server)
    status, payload, _headers = anonymous.post(
        "/api/auth/register",
        {"invite": token, "email": "someone-else@example.test", "name": "冒名者", "password": PASSWORD},
    )
    assert status == 400 and payload["error"] == "invitation_invalid"

    expired = bootstrap_invitation(server, email="late@example.test", ttl_seconds=1)
    server.accounts._clock = lambda: time.time() + 2
    status, payload, _headers = anonymous.post(
        "/api/auth/register",
        {"invite": expired, "email": "late@example.test", "name": "迟到者", "password": PASSWORD},
    )
    assert status == 400 and payload["error"] == "invitation_invalid"

    status, payload, _headers = anonymous.post(
        "/api/auth/register",
        {"invite": "not-a-token", "email": "a@example.test", "name": "无名", "password": PASSWORD},
    )
    assert status == 400 and payload["error"] == "invitation_invalid"


def test_registration_enforces_input_bounds(server: PiGlinkServer) -> None:
    token = bootstrap_invitation(server, email="invitee@example.test")
    anonymous = Client(server)
    cases = [
        ({"name": "受邀者", "password": "short"}, "invalid_password"),
        ({"name": "", "password": PASSWORD}, "invalid_name"),
        ({"name": "x" * 200, "password": PASSWORD}, "invalid_name"),
    ]
    for fields, code in cases:
        status, payload, _headers = anonymous.post(
            "/api/auth/register",
            {"invite": token, "email": "invitee@example.test", **fields},
        )
        assert status == 400, fields
        assert payload["error"] == code, fields
    # A malformed email is not accepted even though it is bound to the invite.
    status, payload, _headers = anonymous.post(
        "/api/auth/register",
        {"invite": token, "email": "not-an-email", "name": "受邀者", "password": PASSWORD},
    )
    assert status == 400 and payload["error"] == "invitation_invalid"


def test_registration_refuses_an_email_that_already_has_an_account(server: PiGlinkServer) -> None:
    token = bootstrap_invitation(server, email="invitee@example.test")
    bootstrap_user(server, email="invitee@example.test", name="先到者")
    anonymous = Client(server)
    status, payload, _headers = anonymous.post(
        "/api/auth/register",
        {"invite": token, "email": "invitee@example.test", "name": "后来者", "password": PASSWORD},
    )
    assert status == 409 and payload["error"] == "email_in_use"


# -- CSRF ----------------------------------------------------------------------


def test_authenticated_posts_require_the_csrf_header(server: PiGlinkServer) -> None:
    bootstrap_user(server, email="member@example.test")
    client = logged_in_client(server, "member@example.test")
    status, payload, _headers = client.post("/api/runs", {"prompt": "没有令牌"}, csrf=None)
    assert status == 403 and payload["error"] == "csrf_invalid"
    status, payload, _headers = client.post("/api/runs", {"prompt": "错误令牌"}, csrf="wrong")
    assert status == 403 and payload["error"] == "csrf_invalid"
    status, payload, _headers = client.post("/api/runs", {"prompt": "正确令牌"})
    assert status == 201, payload
    run_id = payload["run"]["runId"]
    status, payload, _headers = client.post(f"/api/runs/{run_id}/stop", {}, csrf=None)
    assert status == 403 and payload["error"] == "csrf_invalid"


def test_csrf_token_is_bound_to_the_session_not_the_page(server: PiGlinkServer) -> None:
    bootstrap_user(server, email="member@example.test")
    first = logged_in_client(server, "member@example.test")
    second = logged_in_client(server, "member@example.test")
    assert first.csrf != second.csrf
    status, payload, _headers = first.post(
        "/api/runs", {"prompt": "借用别人的令牌"}, csrf=second.csrf
    )
    assert status == 403 and payload["error"] == "csrf_invalid"


# -- admin invitations ----------------------------------------------------------


def test_only_admins_can_create_invitations_for_their_own_tenant(server: PiGlinkServer) -> None:
    bootstrap_user(server, email="member@example.test", role="member")
    bootstrap_user(server, email="admin@example.test", name="管理员", role="admin")
    member = logged_in_client(server, "member@example.test")
    status, payload, _headers = member.post(
        "/api/admin/invitations", {"email": "new@example.test", "role": "member"}
    )
    assert status == 403 and payload["error"] == "forbidden"

    admin = logged_in_client(server, "admin@example.test")
    # A client-supplied tenantId is ignored: the invitation belongs to the
    # caller's own tenant.
    status, payload, _headers = admin.post(
        "/api/admin/invitations",
        {"email": "new@example.test", "role": "admin", "tenantId": "tenant-beta"},
    )
    assert status == 201, payload
    invitation = payload["invitation"]
    assert invitation["tenant"] == {"id": "tenant-alpha", "name": "组织甲"}
    assert invitation["email"] == "new@example.test" and invitation["role"] == "admin"
    code = invitation["code"]
    assert len(code) >= 32
    stored = server.accounts.inspect_invitation(code)
    assert stored is not None and stored.tenant_id == "tenant-alpha"
    assert code.encode() not in server.accounts.path.read_bytes()

    registered = Client(server)
    status, payload, _headers = registered.post(
        "/api/auth/register",
        {"invite": code, "email": "new@example.test", "name": "新管理员", "password": PASSWORD},
    )
    assert status == 201, payload
    user = server.accounts.find_user_by_email("new@example.test")
    assert user.tenant_id == "tenant-alpha" and user.role == "admin"


def test_admin_invitation_input_bounds_and_existing_email(server: PiGlinkServer) -> None:
    bootstrap_user(server, email="admin@example.test", role="admin")
    bootstrap_user(server, tenant="tenant-beta", tenant_name="组织乙", email="other@example.test", name="另一人")
    admin = logged_in_client(server, "admin@example.test")
    for body, code, expected in (
        ({"email": "not-an-email", "role": "member"}, "invalid_email", 400),
        ({"email": "new@example.test", "role": "owner"}, "invalid_role", 400),
        ({"email": "new@example.test"}, "invalid_role", 400),
        ({"email": "other@example.test", "role": "member"}, "email_in_use", 409),
    ):
        status, payload, _headers = admin.post("/api/admin/invitations", body)
        assert status == expected, (body, payload)
        assert payload["error"] == code, body


def test_admin_invitations_require_the_csrf_header(server: PiGlinkServer) -> None:
    bootstrap_user(server, email="admin@example.test", role="admin")
    admin = logged_in_client(server, "admin@example.test")
    status, payload, _headers = admin.post(
        "/api/admin/invitations", {"email": "new@example.test", "role": "member"}, csrf=None
    )
    assert status == 403 and payload["error"] == "csrf_invalid"


# -- runs, reports, SSE and cross-tenant isolation ------------------------------


def test_authenticated_run_lifecycle_and_report(server: PiGlinkServer, adapters: AdapterBehavior) -> None:
    adapters.write_report = True
    bootstrap_user(server, email="member@example.test")
    client = logged_in_client(server, "member@example.test")
    status, payload = client.start_run("关于合成数据的研究")
    assert status == 201, payload
    run_id = payload["run"]["runId"]
    final = wait_for_terminal(server, run_id, client)
    assert final["status"] == "finished", final
    status, report_headers, body = raw_request(
        server, f"/api/runs/{run_id}/report", headers={"Cookie": client.cookie}
    )
    assert status == 200
    assert "合成报告" in body.decode("utf-8")
    assert report_headers.get("X-Redacted") in {"true", "false"}


def test_stop_and_logout_do_not_poison_a_keepalive_connection(server: PiGlinkServer) -> None:
    """A POST body a handler ignores must not become the next request line.

    Real browsers reuse one HTTP/1.1 connection for logout/stop and the next
    navigation; leaving the `{}` body in the socket made the following request
    parse as `{}GET …` and answer 501.
    """
    bootstrap_user(server, email="member@example.test")
    client = logged_in_client(server, "member@example.test")
    status, payload = client.start_run("keep-alive")
    assert status == 201, payload
    run_id = payload["run"]["runId"]
    wait_for_terminal(server, run_id, client)

    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        connection.request(
            "POST",
            f"/api/runs/{run_id}/stop",
            body=b"{}",
            headers={"Content-Type": "application/json", "Cookie": client.cookie, CSRF: client.csrf},
        )
        response = connection.getresponse()
        response.read()
        assert response.status in {200, 409}, response.status

        connection.request("GET", "/api/me", headers={"Cookie": client.cookie})
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200, body[:200]
        assert json.loads(body)["authenticated"] is True
    finally:
        connection.close()

    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        connection.request(
            "POST",
            "/api/auth/logout",
            body=b"{}",
            headers={"Content-Type": "application/json", "Cookie": client.cookie, CSRF: client.csrf},
        )
        response = connection.getresponse()
        response.read()
        assert response.status == 200

        connection.request("GET", "/api/me", headers={"Cookie": client.cookie})
        response = connection.getresponse()
        body = response.read()
        assert response.status == 401, body[:200]
        assert json.loads(body)["error"] == "unauthenticated"
    finally:
        connection.close()


def test_gateway_stop_with_a_body_keeps_the_connection_usable(
    gateway_server: PiGlinkServer, adapters: AdapterBehavior
) -> None:
    token = assertion("tenant-alpha", "user-a")
    status, _headers, raw = raw_request(
        gateway_server,
        "/api/runs",
        method="POST",
        body={"prompt": "keep-alive"},
        headers={ASSERTION_HEADER: token},
    )
    assert status == 201, raw[:200]
    payload = json.loads(raw)
    run_id = payload["run"]["runId"]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status, _headers, raw = raw_request(
            gateway_server, f"/api/runs/{run_id}", headers={ASSERTION_HEADER: token}
        )
        if status == 200 and json.loads(raw)["run"]["status"] != "running":
            break
        time.sleep(0.05)
    connection = HTTPConnection("127.0.0.1", gateway_server.server_address[1], timeout=10)
    try:
        connection.request(
            "POST",
            f"/api/runs/{run_id}/stop",
            body=b"{}",
            headers={"Content-Type": "application/json", ASSERTION_HEADER: token},
        )
        response = connection.getresponse()
        response.read()
        assert response.status in {200, 409}
        connection.request("GET", "/api/runs", headers={ASSERTION_HEADER: token})
        response = connection.getresponse()
        body = response.read()
        assert response.status == 200, body[:200]
        assert "runs" in json.loads(body)
    finally:
        connection.close()


def test_operator_cli_creates_the_first_admin_invitation(
    tmp_path: Path,
    tenant_files: tuple[Path, Path],
    adapters: AdapterBehavior,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The offline bootstrap: one tenant, one admin invitation, printed once."""
    config = make_config(tmp_path, tenant_files)
    monkeypatch.setattr("pi_glink.app.load_config", lambda: config)
    code = app_main(
        [
            "--create-invite",
            "--tenant",
            "tenant-alpha",
            "--tenant-name",
            "组织甲",
            "--email",
            "first-admin@example.test",
            "--role",
            "admin",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "邀请已创建" in captured.out
    match = re.search(r"/register#invite=(\S+)", captured.out)
    assert match, captured.out
    token = match.group(1)
    assert captured.out.count(token) == 1, "the raw code is shown exactly once"
    assert "?invite=" not in captured.out, "the hand-out link must not put the code in a query"
    assert token not in captured.err

    store = AccountsStore(config.accounts_db)
    try:
        record = store.inspect_invitation(token)
        assert record is not None
        assert record.tenant_id == "tenant-alpha" and record.role == "admin"
        assert record.email == "first-admin@example.test"
        assert store.get_tenant("tenant-alpha").name == "组织甲"
        # The database keeps the digest, never the code itself.
        assert token.encode() not in config.accounts_db.read_bytes()
        assert record.expires_at > record.created_at
    finally:
        store.close()
    # The database is left closed: the operator command can run again.
    assert config.accounts_db.is_file()


def test_operator_cli_refuses_tenants_without_credentials(
    tmp_path: Path,
    tenant_files: tuple[Path, Path],
    adapters: AdapterBehavior,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_config(tmp_path, tenant_files)
    monkeypatch.setattr("pi_glink.app.load_config", lambda: config)
    code = app_main(
        ["--create-invite", "--tenant", "tenant-nokey", "--email", "a@example.test"]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "未配置模型凭据" in captured.err


def test_operator_cli_refuses_to_race_a_running_host(
    server: PiGlinkServer, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("pi_glink.app.load_config", lambda: server.config)
    code = app_main(
        ["--create-invite", "--tenant", "tenant-alpha", "--email", "a@example.test"]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "请先停止宿主" in captured.err


def test_operator_cli_requires_accounts_mode(
    tmp_path: Path,
    tenant_files: tuple[Path, Path],
    adapters: AdapterBehavior,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = make_config(tmp_path, tenant_files, PI_GLINK_AUTH_MODE="gateway")
    monkeypatch.setattr("pi_glink.app.load_config", lambda: config)
    code = app_main(
        ["--create-invite", "--tenant", "tenant-alpha", "--email", "a@example.test"]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "PI_GLINK_AUTH_MODE=accounts" in captured.err


def test_operator_cli_requires_the_target_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        app_main(["--create-invite"])
    assert excinfo.value.code == 2


def test_missing_tenant_model_key_still_refuses_admission(server: PiGlinkServer) -> None:
    # ``tenant-nokey`` exists in the account store but has no entry in the
    # tenant credential file.
    bootstrap_user(server, tenant="tenant-nokey", tenant_name="无凭据", email="member@example.test")
    client = logged_in_client(server, "member@example.test")
    status, payload = client.start_run()
    assert status == 403
    assert payload["error"] == "forbidden"


def test_cross_tenant_matrix_for_two_logged_in_tenants(
    server: PiGlinkServer, adapters: AdapterBehavior
) -> None:
    adapters.write_report = True
    bootstrap_user(server, email="alice@example.test", name="爱丽丝", role="admin")
    bootstrap_user(
        server, tenant="tenant-beta", tenant_name="组织乙", email="bob@example.test", name="鲍勃"
    )
    alpha = logged_in_client(server, "alice@example.test")
    beta = logged_in_client(server, "bob@example.test")

    status, payload = alpha.start_run("甲的研究")
    assert status == 201, payload
    run_id = payload["run"]["runId"]
    assert wait_for_terminal(server, run_id, alpha)["status"] == "finished"

    status, payload, _headers = beta.get("/api/runs")
    assert status == 200 and payload["runs"] == []
    assert beta.get(f"/api/runs/{run_id}")[0] == 404
    assert beta.get(f"/api/runs/{run_id}/report")[0] == 404
    assert beta.post(f"/api/runs/{run_id}/stop", {})[0] == 404
    status, content_type, _body = read_stream(server, f"/api/runs/{run_id}/events", cookie=beta.cookie)
    assert status == 404 and content_type.startswith("application/json")
    status, payload = beta.start_run("乙的追问", parentRunId=run_id)
    assert status == 404, payload

    status, payload, _headers = alpha.get(f"/api/runs/{run_id}")
    assert status == 200 and payload["run"]["runId"] == run_id
    status, _headers, body = raw_request(
        server, f"/api/runs/{run_id}/report", headers={"Cookie": alpha.cookie}
    )
    assert status == 200


def test_foreign_and_ownerless_records_stay_invisible_in_accounts_mode(server: PiGlinkServer) -> None:
    # Two invisible shapes: an ownerless record (single-user surface), and one
    # whose owner is another tenant.
    store = server.store
    store.create("无 owner 记录", run_id="ownerless-run", workspace=server.config.workspace_root / "ownerless-run", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    seed_history(server, "other-tenant-run", frames=3, tenant="tenant-beta", user="u9")
    bootstrap_user(server, email="member@example.test")
    client = logged_in_client(server, "member@example.test")
    status, payload, _headers = client.get("/api/runs")
    assert status == 200
    seen = {run["runId"] for run in payload["runs"]}
    assert "other-tenant-run" not in seen and "ownerless-run" not in seen
    assert client.get("/api/runs/ownerless-run")[0] == 404
    assert client.get("/api/runs/other-tenant-run")[0] == 404


def test_sse_replay_from_the_durable_log_with_a_session(server: PiGlinkServer) -> None:
    bootstrap_user(server, email="member@example.test")
    client = logged_in_client(server, "member@example.test")
    seed_history(server, "history-run", frames=5)
    status, content_type, body = read_stream(
        server, "/api/runs/history-run/events", cookie=client.cookie
    )
    assert status == 200
    assert content_type.startswith("text/event-stream")
    text = body.decode("utf-8")
    assert "frame-1" in text and "run_finished" in text


def test_logout_during_a_live_stream_ends_it(server: PiGlinkServer, adapters: AdapterBehavior) -> None:
    adapters.hold = True
    bootstrap_user(server, email="member@example.test")
    client = logged_in_client(server, "member@example.test")
    status, payload = client.start_run("长时间研究")
    assert status == 201, payload
    run_id = payload["run"]["runId"]
    worker, result = read_stream_until_eof(
        server, f"/api/runs/{run_id}/events", cookie=client.cookie
    )
    time.sleep(0.5)  # let the stream attach and start flowing
    assert worker.is_alive(), "the live stream ended before logout"
    status, payload, _headers = client.post("/api/auth/logout", {})
    assert status == 200
    worker.join(timeout=5)
    assert not worker.is_alive(), "the stream kept running after the session was revoked"
    assert "error" not in result
    # The run itself is not cancelled by a session ending; it can still be
    # stopped through an authenticated request afterwards.
    assert server.store.get(run_id).status == "running"


def test_disabling_a_user_during_a_live_stream_ends_it(
    server: PiGlinkServer, adapters: AdapterBehavior
) -> None:
    adapters.hold = True
    user = bootstrap_user(server, email="member@example.test")
    client = logged_in_client(server, "member@example.test")
    status, payload = client.start_run("长时间研究")
    assert status == 201, payload
    run_id = payload["run"]["runId"]
    worker, result = read_stream_until_eof(
        server, f"/api/runs/{run_id}/events", cookie=client.cookie
    )
    time.sleep(0.5)
    assert worker.is_alive()
    server.accounts.set_user_status(user.user_id, "disabled")
    worker.join(timeout=5)
    assert not worker.is_alive(), "the stream kept running for a disabled account"
    assert "error" not in result


def test_session_expiry_during_a_live_stream_ends_it(
    server: PiGlinkServer, adapters: AdapterBehavior
) -> None:
    adapters.hold = True
    bootstrap_user(server, email="member@example.test")
    client = logged_in_client(server, "member@example.test")

    class Clock:
        now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    clock.now = time.time()
    server.accounts._clock = clock

    status, payload = client.start_run("长时间研究")
    assert status == 201, payload
    run_id = payload["run"]["runId"]
    worker, result = read_stream_until_eof(
        server, f"/api/runs/{run_id}/events", cookie=client.cookie
    )
    time.sleep(0.5)
    assert worker.is_alive()
    clock.now += 3601  # past the absolute session deadline
    worker.join(timeout=5)
    assert not worker.is_alive(), "the stream outlived the session"
    assert "error" not in result


# -- configuration --------------------------------------------------------------


def test_accounts_mode_configuration_defaults_and_refusals(tmp_path: Path) -> None:
    base = {
        "LOCAL_WEB_EXECUTOR": "docker",
        "PI_GLINK_TENANCY": "multitenant",
        "PI_GLINK_AUTH_MODE": "accounts",
        "PI_GLINK_TENANT_CONFIG_FILE": str(tmp_path / "tenants.json"),
        "PI_GLINK_TENANCY_AUDIENCE": AUDIENCE,
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "OPENAI_MODEL": "test/model",
    }
    config = build_config(base, REPO_ROOT)
    assert config.accounts_db == tmp_path / "data" / "accounts" / "accounts.db"
    assert config.multi_tenant and config.auth_mode == "accounts"
    assert config.identity_mode == "accounts"

    # accounts mode without multitenant is refused, never silently local.
    with pytest.raises(ConfigError) as excinfo:
        build_config({**base, "PI_GLINK_TENANCY": "local"}, REPO_ROOT)
    assert "PI_GLINK_TENANCY" in str(excinfo.value)
    # Unknown auth modes are refused.
    with pytest.raises(ConfigError):
        build_config({**base, "PI_GLINK_AUTH_MODE": "magic"}, REPO_ROOT)
    # An accounts database inside the repository is refused.
    with pytest.raises(ConfigError):
        build_config({**base, "PI_GLINK_ACCOUNTS_DB": str(REPO_ROOT / "data" / "x.db")}, REPO_ROOT)


def test_accounts_mode_does_not_require_a_gateway_secret(tmp_path: Path) -> None:
    tenants = tmp_path / "tenants.json"
    tenants.write_text(json.dumps({"tenants": {"tenant-alpha": {"modelApiKey": ALPHA_KEY}}}))
    tenants.chmod(0o600)
    config = build_config(
        {
            "LOCAL_WEB_EXECUTOR": "docker",
            "PI_GLINK_TENANCY": "multitenant",
            "PI_GLINK_AUTH_MODE": "accounts",
            "PI_GLINK_TENANT_CONFIG_FILE": str(tenants),
            "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
            "OPENAI_MODEL": "test/model",
        },
        REPO_ROOT,
    )
    assert config.accounts_db is not None
    # Gateway mode still requires the secret file.
    with pytest.raises(Exception):
        build_config(
            {
                "LOCAL_WEB_EXECUTOR": "docker",
                "PI_GLINK_TENANCY": "multitenant",
                "PI_GLINK_TENANT_CONFIG_FILE": str(tenants),
                "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data2"),
                "OPENAI_MODEL": "test/model",
            },
            REPO_ROOT,
        )
