"""Cross-tenant matrix for the single-host multi-tenant HTTP surface.

Every run in these tests is synthetic: the execution face is replaced, the
gateway assertion is minted locally with a key file inside a temporary
directory, and each server binds an OS-assigned loopback port. No pi process,
container, sandbox, model call or real .env / ~/.pi file is involved.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.client import HTTPConnection
from pathlib import Path

import pytest

from pi_glink.app import PiGlinkServer, create_server
from pi_glink.config import build_config
from pi_glink.kernel.execution import (
    ArtifactRecovery,
    CleanupReport,
    ExecutionOutcome,
)
from pi_glink.tenancy import ASSERTION_HEADER, issue_assertion

REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIENCE = "pi-glink-test"
SECRET = b"0123456789abcdef0123456789abcdef"
ALPHA_KEY = "sk-alpha-0000000000000000"
BETA_KEY = "sk-beta-0000000000000000"
GAMMA_KEY = "sk-gamma-0000000000000000"
HOST_KEY = "sk-host-global-00000000000000"
TENANTS = {
    "tenant-alpha": {
        "modelApiKey": ALPHA_KEY,
        "webSearchKey": "search-alpha-000000000000",
    },
    "tenant-beta": {"modelApiKey": BETA_KEY},
    "tenant-gamma": {"modelApiKey": GAMMA_KEY},
}


def assertion(
    tenant: str = "tenant-alpha",
    user: str = "user-a",
    *,
    role: str = "member",
    ttl: int = 120,
) -> str:
    return issue_assertion(
        SECRET,
        tenant_id=tenant,
        user_id=user,
        audience=AUDIENCE,
        role=role,
        ttl_seconds=ttl,
    )


def _auth_key(directory: Path) -> str:
    try:
        document = json.loads((directory / "auth.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    for entry in document.values():
        if isinstance(entry, dict) and isinstance(entry.get("key"), str):
            return entry["key"]
    return ""


class FakeAdapter:
    """Synthetic execution face: records rendered credentials, then settles."""

    def __init__(
        self, spec, *, on_frame, settle=True, release=True, frames=(), write_report=False
    ):
        self.spec = spec
        self._on_frame = on_frame
        self._settle = settle
        self._release = release
        self._frames = tuple(frames)
        self._write_report = write_report
        self._cancelled = False
        self.runtime_dir = spec.runtime.directory
        self.auth_key = _auth_key(spec.runtime.directory)
        self.web_search_present = (spec.runtime.directory / "web-search.json").is_file()

    def start(self) -> None:
        if self._write_report:
            self.spec.workspace.mkdir(parents=True, exist_ok=True)
            (self.spec.workspace / "report.md").write_text(
                "# 合成报告\n\n来自 fake 执行面。\n", encoding="utf-8"
            )
        for frame in self._frames:
            self._on_frame(frame)

    def deliver_input(self) -> None:
        pass

    def session_info(self):
        return None

    def identity(self):
        return {}

    def wait(self, *, cancel, deadline):
        if self._settle:
            return ExecutionOutcome(settled=True)
        while time.monotonic() < deadline:
            if cancel() or self._cancelled:
                break
            time.sleep(0.01)
        return ExecutionOutcome(settled=True, cancelled=cancel() or self._cancelled)

    def cancel(self) -> None:
        self._cancelled = True

    def recover_artifacts(self, destination):
        return ArtifactRecovery()

    def close(self):
        if self._release:
            return CleanupReport(released=True)
        return CleanupReport(released=False, failures=("synthetic close failure",))


class AdapterBehavior:
    def __init__(self) -> None:
        self.settle = True
        self.release = True
        self.frames: tuple[dict, ...] = ()
        self.write_report = False
        self.created: list[FakeAdapter] = []


@pytest.fixture()
def adapters(monkeypatch: pytest.MonkeyPatch) -> AdapterBehavior:
    behavior = AdapterBehavior()

    def build(spec, **kwargs):
        adapter = FakeAdapter(
            spec,
            on_frame=kwargs["on_frame"],
            settle=behavior.settle,
            release=behavior.release,
            frames=behavior.frames,
            write_report=behavior.write_report,
        )
        behavior.created.append(adapter)
        return adapter

    monkeypatch.setattr("pi_glink.kernel.runner.build_adapter", build)
    return behavior


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
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_PORT": "0",
        "LOCAL_WEB_EXECUTOR": "docker",
        "PI_GLINK_TENANCY": "multitenant",
        "PI_GLINK_GATEWAY_SECRET_FILE": str(secret),
        "PI_GLINK_TENANT_CONFIG_FILE": str(tenants),
        "PI_GLINK_TENANCY_AUDIENCE": AUDIENCE,
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent-web-search.json"),
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


def request(
    server: PiGlinkServer,
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    token: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    port = server.server_address[1]
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=payload, method=method
    )
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header(ASSERTION_HEADER, token)
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def start_run(
    server: PiGlinkServer, prompt: str, *, token: str | None, **body_extra
) -> dict:
    body = {"prompt": prompt, **body_extra}
    status, _headers, raw = request(server, "/api/runs", method="POST", body=body, token=token)
    assert status == 201, (status, raw.decode("utf-8", "replace"))
    return json.loads(raw)["run"]


def wait_for_terminal(server: PiGlinkServer, run_id: str, token: str | None, timeout: float = 30.0):
    deadline = time.monotonic() + timeout
    latest: dict = {}
    while time.monotonic() < deadline:
        status, _headers, raw = request(server, f"/api/runs/{run_id}", token=token)
        if status != 200:
            latest = {"status": f"http-{status}"}
            break
        latest = json.loads(raw)["run"]
        if latest["status"] != "running":
            return latest
        time.sleep(0.05)
    return latest


def read_stream(
    server: PiGlinkServer,
    path: str,
    *,
    token: str | None,
    timeout: float = 15.0,
    head_bytes: int | None = None,
):
    """Read an SSE response; ``head_bytes`` returns after that many bytes.

    An open run's stream never ends on its own, so a test that only wants to
    prove the headers and the first frame must bound the read explicitly.
    """
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=timeout)
    headers = {ASSERTION_HEADER: token} if token else {}
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        status = response.status
        content_type = response.getheader("Content-Type") or ""
        body = response.read() if head_bytes is None else response.read1(head_bytes)
        return status, content_type, body
    finally:
        connection.close()


# -- identity -----------------------------------------------------------------


def test_unauthenticated_api_requests_are_refused_without_details(server: PiGlinkServer) -> None:
    for path, method in (
        ("/api/config", "GET"),
        ("/api/runs", "GET"),
        ("/api/runs", "POST"),
        ("/api/runs/whatever", "GET"),
        ("/api/runs/whatever/report", "GET"),
        ("/api/runs/whatever/stop", "POST"),
        ("/api/runs/whatever/events", "GET"),
    ):
        status, _headers, raw = request(server, path, method=method)
        assert status == 401, path
        assert json.loads(raw) == {"error": "unauthenticated"}, path


def test_health_needs_no_identity_but_names_no_run(server: PiGlinkServer) -> None:
    status, _headers, raw = request(server, "/api/health")
    assert status == 200
    payload = json.loads(raw)
    assert payload["status"] == "ok"
    assert "activeRunId" not in payload


def test_forged_expired_wrong_audience_and_duplicate_assertions_are_refused(
    server: PiGlinkServer,
) -> None:
    valid = assertion()
    _version, payload, signature = valid.split(".")
    forged_role = issue_assertion(
        SECRET, tenant_id="tenant-alpha", user_id="user-a", audience=AUDIENCE, role="admin"
    ).split(".")[1]
    cases = [
        # Payload replaced (member → admin) while the old signature is kept.
        "v1." + forged_role + "." + signature,
        # A valid signature for another audience.
        issue_assertion(SECRET, tenant_id="tenant-alpha", user_id="user-a", audience="other"),
        # A valid signature that violates the short-lived contract.
        issue_assertion(
            SECRET, tenant_id="tenant-alpha", user_id="user-a", audience=AUDIENCE, ttl_seconds=3600
        ),
        "not-a-token",
    ]
    for candidate in cases:
        status, _headers, raw = request(server, "/api/runs", token=candidate)
        assert status == 401, candidate[:24]
        assert json.loads(raw) == {"error": "unauthenticated"}
    # An explicitly expired token (its expiry is in the past for the verifier).
    expired = issue_assertion(
        SECRET,
        tenant_id="tenant-alpha",
        user_id="user-a",
        audience=AUDIENCE,
        ttl_seconds=1,
        now=time.time() - 3600,
    )
    assert request(server, "/api/runs", token=expired)[0] == 401

    # Two assertion headers: a gateway that failed to strip the client's copy
    # must not have one of them chosen for it.
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        connection.putrequest("GET", "/api/runs")
        connection.putheader(ASSERTION_HEADER, valid)
        connection.putheader(ASSERTION_HEADER, valid)
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 401
        assert json.loads(response.read()) == {"error": "unauthenticated"}
    finally:
        connection.close()


def test_public_config_reports_the_callers_own_model_credential(server: PiGlinkServer) -> None:
    alpha = assertion("tenant-alpha", "user-a")
    delta = assertion("tenant-delta", "user-d")
    alpha_view = json.loads(request(server, "/api/config", token=alpha)[2])
    assert alpha_view["multiTenant"] is True
    assert alpha_view["configured"]["model"] is True
    delta_view = json.loads(request(server, "/api/config", token=delta)[2])
    assert delta_view["configured"]["model"] is False
    # The public view never names another tenant or a credential.
    for name in ("tenant-alpha", "tenant-beta", ALPHA_KEY, BETA_KEY, HOST_KEY):
        assert name not in json.dumps(alpha_view)


def test_client_supplied_owner_fields_are_ignored(server: PiGlinkServer) -> None:
    token = assertion("tenant-alpha", "user-a")
    run = start_run(
        server,
        "自报 owner",
        token=token,
        tenantId="tenant-beta",
        creatorUserId="user-evil",
    )
    record = server.store.get(run["runId"])
    assert record is not None
    assert record.tenant_id == "tenant-alpha"
    assert record.creator_user_id == "user-a"


# -- cross-tenant read/cancel matrix ------------------------------------------


def test_tenant_scope_lists_and_reads_only_its_own_runs(server: PiGlinkServer, adapters) -> None:
    alpha_token = assertion("tenant-alpha", "user-a")
    beta_token = assertion("tenant-beta", "user-b")
    alpha_run = start_run(server, "alpha run", token=alpha_token)
    beta_run = start_run(server, "beta run", token=beta_token)
    assert wait_for_terminal(server, alpha_run["runId"], alpha_token)["status"] == "finished"
    assert wait_for_terminal(server, beta_run["runId"], beta_token)["status"] == "finished"

    alpha_list = json.loads(request(server, "/api/runs", token=alpha_token)[2])["runs"]
    assert [item["runId"] for item in alpha_list] == [alpha_run["runId"]]
    beta_list = json.loads(request(server, "/api/runs", token=beta_token)[2])["runs"]
    assert [item["runId"] for item in beta_list] == [beta_run["runId"]]

    status, _headers, raw = request(server, f"/api/runs/{alpha_run['runId']}", token=beta_token)
    assert status == 404 and json.loads(raw)["error"] == "unknown_run"
    status, _headers, raw = request(
        server, f"/api/runs/{alpha_run['runId']}/report", token=beta_token
    )
    assert status == 404 and json.loads(raw)["error"] == "no_report"
    status, content_type, raw = read_stream(
        server, f"/api/runs/{alpha_run['runId']}/events", token=beta_token
    )
    assert status == 404
    assert content_type.startswith("application/json")
    assert json.loads(raw)["error"] == "unknown_run"
    status, _headers, raw = request(
        server, f"/api/runs/{alpha_run['runId']}/stop", method="POST", token=beta_token
    )
    assert status == 404 and json.loads(raw)["error"] == "unknown_run"
    # A same-tenant member can still read it: the refusal is scoped to owners.
    status, _headers, _raw = request(server, f"/api/runs/{alpha_run['runId']}", token=alpha_token)
    assert status == 200


def test_stop_requires_the_creator_or_a_tenant_admin(
    server: PiGlinkServer, adapters: AdapterBehavior
) -> None:
    adapters.settle = False  # keep the run open so cancellation is meaningful
    creator = assertion("tenant-alpha", "user-a")
    peer = assertion("tenant-alpha", "user-b")
    admin = assertion("tenant-alpha", "user-admin", role="admin")
    run = start_run(server, "长跑", token=creator)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if json.loads(request(server, "/api/runs", token=creator)[2])["runs"][0]["eventCount"] >= 1:
            break
        time.sleep(0.05)

    status, _headers, raw = request(
        server, f"/api/runs/{run['runId']}/stop", method="POST", token=peer
    )
    assert status == 403 and json.loads(raw)["error"] == "forbidden"
    assert (
        json.loads(request(server, f"/api/runs/{run['runId']}", token=creator)[2])["run"]["status"]
        == "running"
    )

    status, _headers, raw = request(
        server, f"/api/runs/{run['runId']}/stop", method="POST", token=admin
    )
    assert status == 200 and json.loads(raw)["requested"] is True
    assert wait_for_terminal(server, run["runId"], creator)["status"] == "cancelled"

    second = start_run(server, "creator 取消", token=creator)
    status, _headers, _raw = request(
        server, f"/api/runs/{second['runId']}/stop", method="POST", token=creator
    )
    assert status == 200
    assert wait_for_terminal(server, second["runId"], creator)["status"] == "cancelled"


def test_same_tenant_members_share_read_access(
    server: PiGlinkServer, adapters: AdapterBehavior
) -> None:
    """A member of the run's tenant may read detail, SSE and the report."""
    adapters.write_report = True
    creator = assertion("tenant-alpha", "user-a")
    peer = assertion("tenant-alpha", "user-b")
    run = start_run(server, "成员可读", token=creator)
    assert wait_for_terminal(server, run["runId"], creator)["status"] == "finished"

    detail = json.loads(request(server, f"/api/runs/{run['runId']}", token=peer)[2])["run"]
    assert detail["runId"] == run["runId"] and detail["hasReport"] is True
    status, _headers, body = request(server, f"/api/runs/{run['runId']}/report", token=peer)
    assert status == 200 and "合成报告" in body.decode("utf-8")

    stream_status, content_type, stream = read_stream(
        server, f"/api/runs/{run['runId']}/events", token=peer
    )
    assert stream_status == 200 and content_type.startswith("text/event-stream")
    assert b"event: run_started" in stream and b"event: run_finished" in stream


def test_sse_reconnect_resumes_after_the_cursor(
    server: PiGlinkServer, adapters: AdapterBehavior
) -> None:
    """A dropped reader reconnects with ``Last-Event-ID`` and loses nothing."""
    adapters.settle = False
    alpha = assertion("tenant-alpha", "user-a")
    run = start_run(server, "断线重连", token=alpha)
    path = f"/api/runs/{run['runId']}/events"
    status, _content_type, _head = read_stream(server, path, token=alpha, head_bytes=64)
    assert status == 200

    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=15)
    try:
        connection.request(
            "GET", path, headers={ASSERTION_HEADER: alpha, "Last-Event-ID": "1"}
        )
        response = connection.getresponse()
        assert response.status == 200
        # Ending the run closes the resumed stream so the read below returns.
        assert (
            request(server, f"/api/runs/{run['runId']}/stop", method="POST", token=alpha)[0]
            == 200
        )
        body = response.read()
    finally:
        connection.close()
    assert b"event: run_started" not in body, "游标之前不重发"
    assert b"event: run_cancelled" in body


def test_legacy_ownerless_records_are_invisible_to_every_tenant(
    server: PiGlinkServer, tmp_path: Path
) -> None:
    workspace = server.config.workspace_root / "legacy01"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "report.md").write_text("# 旧报告\n", encoding="utf-8")
    server.store.create("历史研究", run_id="legacy01", workspace=workspace)
    server.store.update(
        "legacy01",
        status="finished",
        report_path=str(workspace / "report.md"),
        finished_at=time.time(),
    )
    token = assertion("tenant-alpha", "user-a")
    assert json.loads(request(server, "/api/runs", token=token)[2])["runs"] == []
    assert request(server, "/api/runs/legacy01", token=token)[0] == 404
    assert request(server, "/api/runs/legacy01/report", token=token)[0] == 404
    assert request(server, "/api/runs/legacy01/events", token=token)[0] == 404
    assert request(server, "/api/runs/legacy01/stop", method="POST", token=token)[0] == 404
    assert (workspace / "report.md").read_text(encoding="utf-8") == "# 旧报告\n"


# -- credentials --------------------------------------------------------------


def test_each_run_renders_only_its_own_tenants_credentials(
    server: PiGlinkServer, adapters: AdapterBehavior
) -> None:
    alpha = start_run(server, "alpha", token=assertion("tenant-alpha", "user-a"))
    beta = start_run(server, "beta", token=assertion("tenant-beta", "user-b"))
    assert wait_for_terminal(server, alpha["runId"], assertion("tenant-alpha", "user-a"))
    assert wait_for_terminal(server, beta["runId"], assertion("tenant-beta", "user-b"))

    alpha_adapter = adapters.created[0]
    beta_adapter = adapters.created[1]
    assert alpha_adapter.auth_key == ALPHA_KEY
    assert beta_adapter.auth_key == BETA_KEY
    assert alpha_adapter.web_search_present is True
    assert beta_adapter.web_search_present is False, "缺搜索 key 时应回退无 key 检索"
    for adapter in (alpha_adapter, beta_adapter):
        assert HOST_KEY not in adapter.auth_key, "宿主全局 key 不得回退给租户"
        assert ALPHA_KEY not in beta_adapter.auth_key
        assert BETA_KEY not in alpha_adapter.auth_key


def test_a_tenant_without_configured_model_key_is_refused_admission(
    server: PiGlinkServer, adapters: AdapterBehavior
) -> None:
    token = assertion("tenant-delta", "user-d")
    status, _headers, raw = request(
        server, "/api/runs", method="POST", body={"prompt": "x"}, token=token
    )
    assert status == 403
    assert json.loads(raw)["error"] == "forbidden"
    assert adapters.created == [], "被拒绝的租户不得启动执行面"
    assert server.store.list(limit=None) == []


def test_tenant_secrets_do_not_cross_runs_or_tenants(
    server: PiGlinkServer, adapters: AdapterBehavior
) -> None:
    adapters.frames = (
        {
            "type": "message_update",
            "assistantMessageEvent": {
                "type": "text_delta",
                "contentIndex": 1,
                "delta": f"工具输出 OPENAI_API_KEY={ALPHA_KEY}",
            },
        },
    )
    alpha_token = assertion("tenant-alpha", "user-a")
    alpha = start_run(server, "alpha", token=alpha_token)
    assert wait_for_terminal(server, alpha["runId"], alpha_token)["status"] == "finished"
    events = "\n".join(
        json.dumps(event, ensure_ascii=False)
        for event in server.store.iter_events(alpha["runId"])
    )
    assert ALPHA_KEY not in events
    assert "[REDACTED]" in events

    adapters.frames = ()
    beta_token = assertion("tenant-beta", "user-b")
    beta = start_run(server, "beta", token=beta_token)
    assert wait_for_terminal(server, beta["runId"], beta_token)["status"] == "finished"
    alpha_record = server.store.get(alpha["runId"])
    beta_record = server.store.get(beta["runId"])
    assert alpha_record is not None and beta_record is not None
    assert ALPHA_KEY not in server.manager.redactor_for_record(beta_record).known_values
    assert ALPHA_KEY in server.manager.redactor_for_record(alpha_record).known_values


# -- concurrency and quota ----------------------------------------------------


def test_two_tenants_run_concurrently_up_to_the_ceilings(
    server: PiGlinkServer, adapters: AdapterBehavior
) -> None:
    adapters.settle = False
    alpha = assertion("tenant-alpha", "user-a")
    beta = assertion("tenant-beta", "user-b")
    alpha_runs = [start_run(server, f"alpha {index}", token=alpha) for index in range(2)]
    beta_runs = [start_run(server, f"beta {index}", token=beta) for index in range(2)]
    assert len({run["runId"] for run in alpha_runs + beta_runs}) == 4

    status, _headers, raw = request(
        server, "/api/runs", method="POST", body={"prompt": "alpha 第三条"}, token=alpha
    )
    assert status == 429
    payload = json.loads(raw)
    assert payload["error"] == "quota_exceeded" and payload["scope"] == "tenant"
    assert payload["limit"] == 2
    assert not any(run["runId"] in raw.decode("utf-8") for run in alpha_runs)

    for run in alpha_runs + beta_runs:
        request(server, f"/api/runs/{run['runId']}/stop", method="POST",
                token=alpha if run in alpha_runs else beta)
    for run in alpha_runs:
        assert wait_for_terminal(server, run["runId"], alpha)["status"] == "cancelled"
    for run in beta_runs:
        assert wait_for_terminal(server, run["runId"], beta)["status"] == "cancelled"

    # Capacity returns once every terminal run confirmed its resource released.
    start_run(server, "恢复", token=alpha)


def test_the_global_ceiling_refuses_without_naming_other_tenants(
    server: PiGlinkServer, adapters: AdapterBehavior
) -> None:
    adapters.settle = False
    tokens = {
        tenant: assertion(tenant, f"user-{tenant}")
        for tenant in ("tenant-alpha", "tenant-beta")
    }
    created = []
    for tenant, token in tokens.items():
        for index in range(2):
            created.append(start_run(server, f"{tenant} {index}", token=token))

    gamma = assertion("tenant-gamma", "user-g")
    status, _headers, raw = request(
        server, "/api/runs", method="POST", body={"prompt": "第五条"}, token=gamma
    )
    assert status == 429
    payload = json.loads(raw)
    assert payload["error"] == "quota_exceeded" and payload["scope"] == "global"
    assert payload["limit"] == 4
    text = raw.decode("utf-8")
    for run in created:
        assert run["runId"] not in text, "全局配额拒绝不得泄露其他 run 标识"

    for run in created:
        tenant = "tenant-alpha" if run in created[:2] else "tenant-beta"
        request(server, f"/api/runs/{run['runId']}/stop", method="POST", token=tokens[tenant])
    for run in created:
        tenant = "tenant-alpha" if run in created[:2] else "tenant-beta"
        wait_for_terminal(server, run["runId"], tokens[tenant])


def test_concurrent_admissions_never_exceed_the_global_ceiling(
    server: PiGlinkServer, adapters: AdapterBehavior
) -> None:
    """Nine racing requests fill exactly the four slots, never more.

    Admission checks and slot reservation happen under one lock, so a burst must
    not overshoot the ceiling even when every request arrives at once; refused
    requests are told about the quota, not about other tenants.
    """
    adapters.settle = False
    tenants = ("tenant-alpha", "tenant-beta", "tenant-gamma")
    tokens = {tenant: assertion(tenant, f"user-{tenant}") for tenant in tenants}
    results: list[tuple[int, dict | None]] = []
    lock = threading.Lock()

    def fire(index: int) -> None:
        tenant = tenants[index % len(tenants)]
        status, _headers, raw = request(
            server,
            "/api/runs",
            method="POST",
            body={"prompt": f"race {index}"},
            token=tokens[tenant],
        )
        payload = json.loads(raw) if status != 201 else None
        with lock:
            results.append((status, payload))

    threads = [threading.Thread(target=fire, args=(index,)) for index in range(9)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 9
    statuses = sorted(status for status, _payload in results)
    assert statuses.count(201) == 4, "全局上限 4 必须原子生效并被打满"
    assert statuses.count(429) == 5
    for status, payload in results:
        if status == 429:
            assert payload is not None and payload["error"] == "quota_exceeded"
            assert payload.get("scope") in {"global", "tenant"}


def test_unconfirmed_resources_hold_capacity_without_naming_the_run(
    server: PiGlinkServer, adapters: AdapterBehavior
) -> None:
    adapters.release = False
    alpha = assertion("tenant-alpha", "user-a")
    beta = assertion("tenant-beta", "user-b")
    run = start_run(server, "泄漏", token=alpha)
    assert wait_for_terminal(server, run["runId"], alpha)["status"] == "finished"

    status, _headers, raw = request(
        server, "/api/runs", method="POST", body={"prompt": "下一条"}, token=beta
    )
    assert status == 409
    payload = json.loads(raw)
    assert payload["error"] == "resources_unreleased"
    assert "runIds" not in payload, "异租户资源标识不得出现在准入拒绝里"
    assert run["runId"] not in raw.decode("utf-8")


def test_unreleased_resources_block_admission_inside_the_commit_window(
    server: PiGlinkServer, adapters: AdapterBehavior, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit-to-table-removal window must not leak unreleased capacity.

    A run whose terminal frame is already committed — readers see ``finished`` —
    but which has not left the in-memory execution table yet still holds its
    resources when the cleanup did not confirm release. The next admission must
    see that hold instead of treating the run as merely active.
    """
    adapters.release = False
    original_settle = server.store.settle
    gate = threading.Event()
    entered = threading.Event()

    def gated_settle(run_id: str):
        entered.set()
        assert gate.wait(timeout=10.0)
        return original_settle(run_id)

    monkeypatch.setattr(server.store, "settle", gated_settle)
    alpha = assertion("tenant-alpha", "user-a")
    beta = assertion("tenant-beta", "user-b")
    run = start_run(server, "窗口内未释放", token=alpha)
    try:
        assert entered.wait(timeout=10.0), "run 没有走到落定副本阶段"
        assert wait_for_terminal(server, run["runId"], alpha)["status"] == "finished"
        status, _headers, raw = request(
            server, "/api/runs", method="POST", body={"prompt": "窗口内下一条"}, token=beta
        )
        assert status == 409, raw.decode("utf-8", "replace")
        assert json.loads(raw)["error"] == "resources_unreleased"
    finally:
        gate.set()


def test_accepted_cancellation_still_wins_under_concurrency(
    server: PiGlinkServer, adapters: AdapterBehavior
) -> None:
    adapters.settle = False
    alpha = assertion("tenant-alpha", "user-a")
    beta = assertion("tenant-beta", "user-b")
    first = start_run(server, "alpha 长跑", token=alpha)
    second = start_run(server, "beta 长跑", token=beta)
    assert (
        request(server, f"/api/runs/{first['runId']}/stop", method="POST", token=alpha)[0] == 200
    )
    assert (
        request(server, f"/api/runs/{second['runId']}/stop", method="POST", token=beta)[0] == 200
    )
    assert wait_for_terminal(server, first["runId"], alpha)["status"] == "cancelled"
    assert wait_for_terminal(server, second["runId"], beta)["status"] == "cancelled"


# -- SSE ----------------------------------------------------------------------


def test_event_stream_authorizes_before_writing_headers(
    server: PiGlinkServer, adapters: AdapterBehavior
) -> None:
    adapters.settle = False
    alpha = assertion("tenant-alpha", "user-a")
    beta = assertion("tenant-beta", "user-b")
    run = start_run(server, "流", token=alpha)
    status, content_type, raw = read_stream(
        server, f"/api/runs/{run['runId']}/events", token=beta
    )
    assert status == 404
    assert content_type.startswith("application/json")
    assert "text/event-stream" not in content_type
    assert json.loads(raw)["error"] == "unknown_run"

    status, content_type, body = read_stream(
        server, f"/api/runs/{run['runId']}/events", token=alpha, head_bytes=256
    )
    assert status == 200
    assert content_type.startswith("text/event-stream")
    assert b"event: run_started" in body
    request(server, f"/api/runs/{run['runId']}/stop", method="POST", token=alpha)


def test_event_stream_ends_when_the_assertion_expires(
    server: PiGlinkServer, adapters: AdapterBehavior
) -> None:
    adapters.settle = False
    short = assertion("tenant-alpha", "user-a", ttl=2)
    run = start_run(server, "短时断言", token=short)
    started = time.monotonic()
    status, content_type, _body = read_stream(
        server, f"/api/runs/{run['runId']}/events", token=short, timeout=15.0
    )
    elapsed = time.monotonic() - started
    assert status == 200 and content_type.startswith("text/event-stream")
    assert elapsed >= 1.0, "流不得在断言过期前结束"
    assert elapsed < 10.0, "断言过期后流必须被终止"
    # The run itself is unaffected; a fresh assertion can read it again.
    request(server, f"/api/runs/{run['runId']}/stop", method="POST", token=short)


def test_restart_replay_keeps_tenant_isolation(
    tmp_path: Path, tenant_files: tuple[Path, Path], adapters: AdapterBehavior
) -> None:
    first = create_server(make_config(tmp_path, tenant_files))
    thread = start_server(first)
    alpha = assertion("tenant-alpha", "user-a")
    try:
        run = start_run(first, "重启前", token=alpha)
        assert wait_for_terminal(first, run["runId"], alpha)["status"] == "finished"
    finally:
        stop_server(first, thread)

    second = create_server(make_config(tmp_path, tenant_files))
    thread = start_server(second)
    try:
        status, content_type, body = read_stream(
            second, f"/api/runs/{run['runId']}/events", token=alpha
        )
        assert status == 200 and content_type.startswith("text/event-stream")
        assert b"event: run_started" in body and b"event: run_finished" in body
        beta = assertion("tenant-beta", "user-b")
        assert request(second, f"/api/runs/{run['runId']}", token=beta)[0] == 404
        assert read_stream(second, f"/api/runs/{run['runId']}/events", token=beta)[0] == 404
    finally:
        stop_server(second, thread)


# -- request origin (CSRF) ----------------------------------------------------


def test_cross_site_posts_are_refused_before_any_work(server: PiGlinkServer, adapters) -> None:
    token = assertion("tenant-alpha", "user-a")
    body = {"prompt": "csrf"}
    status, _headers, raw = request(
        server,
        "/api/runs",
        method="POST",
        body=body,
        token=token,
        headers={"Origin": "https://evil.example"},
    )
    assert status == 403 and json.loads(raw)["error"] == "forbidden"
    status, _headers, _raw = request(
        server,
        "/api/runs",
        method="POST",
        body=body,
        token=token,
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert status == 403
    status, _headers, _raw = request(
        server,
        "/api/runs",
        method="POST",
        body=body,
        token=token,
        headers={"Referer": "https://evil.example/page"},
    )
    assert status == 403
    assert adapters.created == [], "跨站请求不得启动任何执行面"

    port = server.server_address[1]
    status, _headers, raw = request(
        server,
        "/api/runs",
        method="POST",
        body=body,
        token=token,
        headers={"Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"},
    )
    assert status == 201, raw.decode("utf-8", "replace")
