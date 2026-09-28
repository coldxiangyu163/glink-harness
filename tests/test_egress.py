"""Credential egress proxy (decision D4, docs/adr/0008-egress-proxy.md).

The real model/search keys stay in the host process: a run gets only the
proxy URL plus a short-lived token bound to run and tenant. Everything here is
synthetic — a fake OpenAI/Exa-shaped upstream on a random loopback port, the
fake pi kernel, temporary data roots. No real model, search, Docker or E2B.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from pi_glink.config import ConfigError, build_config
from pi_glink.egress import EgressProxy, EgressQuota, normalize_usage
from pi_glink.events import EventBus
from pi_glink.kernel.harness import select_family
from pi_glink.kernel.sandbox import SANDBOXES
from pi_glink.kernel.rpc import PiRpcClient
from pi_glink.runs.runner import RunManager
from pi_glink.store import RunStore

from harness_helpers import compose_launch, execution_spec, test_egress

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_PI = Path(__file__).resolve().parent / "fixtures" / "fake_pi.py"

MODEL_KEY = "sk-real-model-key-0000000000"
SEARCH_KEY = "exa-real-search-key-000000000"
USAGE = {
    "prompt_tokens": 11,
    "completion_tokens": 5,
    "total_tokens": 16,
    "prompt_tokens_details": {"cached_tokens": 3},
}


# -- fake upstream -------------------------------------------------------------


class FakeUpstream:
    """OpenAI-compatible ``/v1/chat/completions`` plus an Exa-shaped ``/exa/search``."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.usage: dict[str, Any] | None = USAGE
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or "0")
                body = self.rfile.read(length) if length else b""
                upstream.requests.append(
                    {"path": self.path, "headers": dict(self.headers.items()), "body": body}
                )
                if self.path.startswith("/redirect"):
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/elsewhere")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path.startswith("/exa/"):
                    self._json({"results": [{"title": "合成结果"}]})
                    return
                try:
                    stream = json.loads(body or b"{}").get("stream") is True
                except ValueError:
                    stream = False
                if stream:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    chunks = [
                        {"choices": [{"delta": {"content": "你"}}]},
                        {"choices": [{"delta": {"content": "好"}}]},
                    ]
                    if upstream.usage is not None:
                        chunks.append({"choices": [], "usage": upstream.usage})
                    for chunk in chunks:
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                document: dict[str, Any] = {"choices": [{"message": {"content": "ok"}}]}
                if upstream.usage is not None:
                    document["usage"] = upstream.usage
                self._json(document)

            def _json(self, document: dict[str, Any]) -> None:
                raw = json.dumps(document).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def upstream():
    fake = FakeUpstream()
    try:
        yield fake
    finally:
        fake.close()


def make_proxy(upstream: FakeUpstream, **quota: int) -> EgressProxy:
    return EgressProxy(
        model_upstream=f"{upstream.origin}/v1",
        search_upstream=f"{upstream.origin}/exa",
        quota_for=lambda _tenant: EgressQuota(**quota),
    )


@pytest.fixture()
def proxy(upstream: FakeUpstream):
    instance = make_proxy(upstream)
    try:
        yield instance
    finally:
        instance.close()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def post(url: str, *, headers: dict[str, str] | None = None, body: dict | None = None):
    request = urllib.request.Request(
        url,
        data=json.dumps(body or {}).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=10) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def base(proxy: EgressProxy) -> str:
    assert proxy.port is not None
    return f"http://127.0.0.1:{proxy.port}"


# -- forwarding and authentication ----------------------------------------------


def test_the_upstream_sees_only_the_real_key_and_the_kernel_only_the_token(
    proxy: EgressProxy, upstream: FakeUpstream
) -> None:
    grant = proxy.issue(run_id="run-1", tenant="tenant-a", model_key=MODEL_KEY)
    status, _headers, raw = post(
        f"{base(proxy)}/model/chat/completions",
        headers={"Authorization": f"Bearer {grant.token}", "Cookie": "c=1"},
        body={"model": "m", "messages": []},
    )
    assert status == 200
    assert json.loads(raw)["usage"]["total_tokens"] == 16
    [seen] = upstream.requests
    assert seen["path"] == "/v1/chat/completions"
    assert seen["headers"]["Authorization"] == f"Bearer {MODEL_KEY}"
    assert grant.token not in json.dumps(seen["headers"])
    assert "Cookie" not in seen["headers"]
    assert grant.usage() == {
        "source": "egress",
        "requests": 1,
        "searchRequests": 0,
        "input": 11,
        "output": 5,
        "cacheRead": 3,
        "totalTokens": 16,
        "unmetered": 0,
        "refused": 0,
    }
    assert "sk-real" not in repr(grant) and grant.token not in repr(grant)


def test_a_missing_or_foreign_token_is_refused_before_the_upstream(
    proxy: EgressProxy, upstream: FakeUpstream
) -> None:
    proxy.issue(run_id="run-1", tenant=None, model_key=MODEL_KEY)
    for headers in ({}, {"Authorization": "Bearer pge_not-issued"}, {"Authorization": f"Bearer {MODEL_KEY}"}):
        status, _headers, raw = post(f"{base(proxy)}/model/chat/completions", headers=headers)
        assert status == 401, headers
        assert json.loads(raw)["error"]["code"] == "invalid_token"
    assert upstream.requests == []


def test_a_revoked_token_is_refused_and_the_idle_listener_closes(
    proxy: EgressProxy, upstream: FakeUpstream
) -> None:
    first = proxy.issue(run_id="run-1", tenant="a", model_key=MODEL_KEY)
    second = proxy.issue(run_id="run-2", tenant="b", model_key="sk-other-tenant-key-000")
    url = f"{base(proxy)}/model/chat/completions"
    final = proxy.revoke(first.token)
    assert final is not None and final["requests"] == 0
    assert post(url, headers={"Authorization": f"Bearer {first.token}"})[0] == 401
    # The other run's token is unaffected.
    assert post(url, headers={"Authorization": f"Bearer {second.token}"})[0] == 200
    assert upstream.requests[-1]["headers"]["Authorization"] == "Bearer sk-other-tenant-key-000"
    assert proxy.revoke(second.token) is not None
    assert proxy.port is None, "no live grant, no open egress port"
    assert proxy.revoke(second.token) is None


def test_a_fixed_port_rebinds_right_after_the_last_grant_is_revoked(
    upstream: FakeUpstream,
) -> None:
    # A public URL pins PI_GLINK_EGRESS_PORT, so back-to-back runs revoke and
    # re-issue on the same port; the old socket must be closed by then.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    fixed = probe.getsockname()[1]
    probe.close()
    proxy = EgressProxy(
        model_upstream=f"{upstream.origin}/v1",
        search_upstream=f"{upstream.origin}/exa",
        port=fixed,
    )
    try:
        for round_no in range(5):
            grant = proxy.issue(run_id=f"run-{round_no}", tenant=None, model_key=MODEL_KEY)
            assert proxy.port == fixed
            url = f"http://127.0.0.1:{fixed}/model/chat/completions"
            assert post(url, headers={"Authorization": f"Bearer {grant.token}"})[0] == 200
            assert proxy.revoke(grant.token) is not None
            assert proxy.port is None
    finally:
        proxy.close()


def test_search_needs_its_own_grant_and_forwards_the_search_key(
    proxy: EgressProxy, upstream: FakeUpstream
) -> None:
    plain = proxy.issue(run_id="run-1", tenant=None, model_key=MODEL_KEY)
    status, _headers, raw = post(
        f"{base(proxy)}/search/search", headers={"x-api-key": plain.token}, body={"query": "q"}
    )
    assert status == 403 and json.loads(raw)["error"]["code"] == "search_not_granted"
    assert upstream.requests == []

    searching = proxy.issue(run_id="run-2", tenant=None, model_key=MODEL_KEY, search_key=SEARCH_KEY)
    status, _headers, _raw = post(
        f"{base(proxy)}/search/search", headers={"x-api-key": searching.token}, body={"query": "q"}
    )
    assert status == 200
    [seen] = upstream.requests
    assert seen["path"] == "/exa/search"
    assert seen["headers"]["X-Api-Key"] == SEARCH_KEY
    assert "Authorization" not in seen["headers"]
    usage = searching.usage()
    assert (usage["requests"], usage["searchRequests"]) == (0, 1)


def test_unknown_routes_and_path_tricks_never_reach_the_upstream(
    proxy: EgressProxy, upstream: FakeUpstream
) -> None:
    grant = proxy.issue(run_id="run-1", tenant=None, model_key=MODEL_KEY)
    auth = {"Authorization": f"Bearer {grant.token}"}
    for path in ("/v1/chat/completions", "/model", "/model/../search/x", "/model//x", "/other/x"):
        status, _headers, _raw = post(f"{base(proxy)}{path}", headers=auth)
        assert status == 404, path
    assert upstream.requests == []


def test_an_upstream_redirect_is_returned_not_followed(
    upstream: FakeUpstream,
) -> None:
    proxy = EgressProxy(model_upstream=f"{upstream.origin}/redirect", search_upstream=upstream.origin)
    try:
        grant = proxy.issue(run_id="run-1", tenant=None, model_key=MODEL_KEY)
        status, _headers, _raw = post(
            f"{base(proxy)}/model/chat/completions",
            headers={"Authorization": f"Bearer {grant.token}"},
        )
        assert status == 302
        assert [request["path"] for request in upstream.requests] == ["/redirect/chat/completions"]
    finally:
        proxy.close()


# -- metering ------------------------------------------------------------------


def test_a_streamed_response_is_passed_through_and_metered(
    proxy: EgressProxy, upstream: FakeUpstream
) -> None:
    grant = proxy.issue(run_id="run-1", tenant=None, model_key=MODEL_KEY)
    status, headers, raw = post(
        f"{base(proxy)}/model/chat/completions",
        headers={"Authorization": f"Bearer {grant.token}"},
        body={"stream": True},
    )
    assert status == 200
    assert headers["Content-Type"] == "text/event-stream"
    text = raw.decode("utf-8")
    assert '"content": "\\u4f60"' in text and text.rstrip().endswith("data: [DONE]")
    usage = grant.usage()
    assert (usage["input"], usage["output"], usage["cacheRead"], usage["totalTokens"]) == (11, 5, 3, 16)
    assert usage["unmetered"] == 0


def test_a_response_without_usage_is_counted_as_unmetered(
    proxy: EgressProxy, upstream: FakeUpstream
) -> None:
    upstream.usage = None
    grant = proxy.issue(run_id="run-1", tenant=None, model_key=MODEL_KEY)
    for stream in (False, True):
        status, _headers, _raw = post(
            f"{base(proxy)}/model/chat/completions",
            headers={"Authorization": f"Bearer {grant.token}"},
            body={"stream": stream},
        )
        assert status == 200
    usage = grant.usage()
    assert (usage["requests"], usage["unmetered"], usage["totalTokens"]) == (2, 2, 0)


def test_normalize_usage_ignores_malformed_counters() -> None:
    assert normalize_usage({"prompt_tokens": 2, "completion_tokens": 3}) == {
        "input": 2,
        "output": 3,
        "cacheRead": 0,
        "totalTokens": 5,
    }
    assert normalize_usage({"prompt_tokens": -1, "completion_tokens": True, "total_tokens": "9"}) == {
        "input": 0,
        "output": 0,
        "cacheRead": 0,
        "totalTokens": 0,
    }


def test_the_quota_hook_refuses_requests_past_the_per_run_ceiling(upstream: FakeUpstream) -> None:
    by_requests = make_proxy(upstream, max_requests=1)
    try:
        grant = by_requests.issue(run_id="run-1", tenant="a", model_key=MODEL_KEY)
        url = f"{base(by_requests)}/model/chat/completions"
        auth = {"Authorization": f"Bearer {grant.token}"}
        assert post(url, headers=auth)[0] == 200
        status, _headers, raw = post(url, headers=auth)
        assert status == 429 and json.loads(raw)["error"]["code"] == "quota_exceeded"
        assert (grant.usage()["requests"], grant.usage()["refused"]) == (1, 1)
    finally:
        by_requests.close()

    by_tokens = make_proxy(upstream, max_tokens=10)
    try:
        grant = by_tokens.issue(run_id="run-2", tenant="a", model_key=MODEL_KEY)
        url = f"{base(by_tokens)}/model/chat/completions"
        auth = {"Authorization": f"Bearer {grant.token}"}
        # The crossing request completes (its usage is only known afterwards)…
        assert post(url, headers=auth)[0] == 200
        # …the next one is refused.
        assert post(url, headers=auth)[0] == 429
        assert grant.usage()["totalTokens"] == 16
    finally:
        by_tokens.close()
    assert len(upstream.requests) == 2


# -- configuration and faces (fail closed) --------------------------------------


def _values(tmp_path: Path, **overrides: str) -> dict[str, str]:
    values = {
        "OPENAI_API_KEY": MODEL_KEY,
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
    }
    values.update(overrides)
    return values


def test_config_fails_closed_when_a_face_cannot_reach_the_proxy(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="PI_GLINK_EGRESS_PUBLIC_URL"):
        build_config(_values(tmp_path, LOCAL_WEB_EXECUTOR="e2b", E2B_API_KEY="e2b-x"), REPO_ROOT)
    with pytest.raises(ConfigError, match="https"):
        build_config(
            _values(tmp_path, LOCAL_WEB_EXECUTOR="docker", PI_GLINK_WEB_SEARCH_KEY=SEARCH_KEY),
            REPO_ROOT,
        )
    with pytest.raises(ConfigError, match="https"):
        build_config(
            _values(tmp_path, PI_GLINK_EGRESS_PUBLIC_URL="http://egress.example.test"), REPO_ROOT
        )
    with pytest.raises(ConfigError, match="PI_GLINK_EGRESS_PORT"):
        build_config(_values(tmp_path, PI_GLINK_EGRESS_PORT="70000"), REPO_ROOT)
    with pytest.raises(ConfigError, match="PI_GLINK_WEB_SEARCH_UPSTREAM"):
        build_config(
            _values(tmp_path, PI_GLINK_WEB_SEARCH_UPSTREAM="https://user:pw@exa.example"), REPO_ROOT
        )
    # Adjacent legal inputs: Docker without a search key, Docker with an https
    # public URL, E2B with one, and a loopback http public URL.
    build_config(_values(tmp_path, LOCAL_WEB_EXECUTOR="docker"), REPO_ROOT)
    build_config(
        _values(
            tmp_path,
            LOCAL_WEB_EXECUTOR="docker",
            PI_GLINK_WEB_SEARCH_KEY=SEARCH_KEY,
            PI_GLINK_EGRESS_PUBLIC_URL="https://egress.example.test",
        ),
        REPO_ROOT,
    )
    e2b = build_config(
        _values(
            tmp_path,
            LOCAL_WEB_EXECUTOR="e2b",
            E2B_API_KEY="e2b-x",
            PI_GLINK_EGRESS_PUBLIC_URL="https://egress.example.test/",
        ),
        REPO_ROOT,
    )
    assert e2b.egress_public_url == "https://egress.example.test"
    local = build_config(_values(tmp_path, PI_GLINK_EGRESS_PUBLIC_URL="http://127.0.0.1:8900"), REPO_ROOT)
    assert local.egress_public_url == "http://127.0.0.1:8900"


def _sandbox(config, kind: str, tmp_path: Path):
    return SANDBOXES[kind](config=config, run_id="run-1", workspace=tmp_path / "ws")


def test_each_face_names_its_own_route_to_the_proxy(tmp_path: Path) -> None:
    config = build_config(_values(tmp_path), REPO_ROOT)
    assert _sandbox(config, "inproc", tmp_path).egress_origin(4321) == "http://127.0.0.1:4321"
    assert _sandbox(config, "docker", tmp_path).egress_origin(4321) == "http://host.docker.internal:4321"
    # A config built without the public URL (bypassing the startup gate) still
    # cannot hand an E2B run a route: the sandbox layer refuses on its own.
    with pytest.raises(ConfigError, match="PI_GLINK_EGRESS_PUBLIC_URL"):
        _sandbox(config, "e2b", tmp_path).egress_origin(4321)
    public = build_config(
        _values(tmp_path, PI_GLINK_EGRESS_PUBLIC_URL="https://egress.example.test"), REPO_ROOT
    )
    for kind in ("inproc", "docker", "e2b"):
        assert _sandbox(public, kind, tmp_path).egress_origin(4321) == "https://egress.example.test"


def test_the_docker_run_maps_the_host_gateway_and_carries_no_key(tmp_path: Path) -> None:
    config = build_config(_values(tmp_path, LOCAL_WEB_EXECUTOR="docker"), REPO_ROOT)
    family = select_family(config.harness, config=config, store=RunStore(tmp_path / "store-runs"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    run_dir = tmp_path / "runs" / "run-1"
    from pi_glink.kernel.agent_env import prepare_agent_dir

    runtime = prepare_agent_dir(
        config,
        runtime_dir=run_dir / "pi-agent",
        session_dir=run_dir / "sessions",
        egress=test_egress(origin="http://host.docker.internal:4321"),
        image_packages=True,
    )
    spec = execution_spec(config, runtime, workspace=workspace, run_id="run-1")
    launch, docker = compose_launch("pi", "docker", spec, store_root=tmp_path / "store")
    argv = docker.spawn(launch).argv
    index = argv.index("--add-host")
    assert argv[index + 1] == "host.docker.internal:host-gateway"
    assert MODEL_KEY not in " ".join(argv)
    models = json.loads((runtime.directory / "models.json").read_text(encoding="utf-8"))
    assert models["providers"]["frontier"]["baseUrl"] == "http://host.docker.internal:4321/model"
    assert family.uses_egress is True


def test_the_pi_family_refuses_to_render_without_a_grant(tmp_path: Path) -> None:
    config = build_config(_values(tmp_path), REPO_ROOT)
    family = select_family(config.harness, config=config, store=RunStore(tmp_path / "store-runs"))
    with pytest.raises(ConfigError, match="egress grant"):
        family.prepare_runtime(
            "run-1",
            tmp_path / "runs" / "run-1",
            parent=None,
            session_seed=None,
            egress=None,
            sandbox=_sandbox(config, "inproc", tmp_path),
        )
    assert not (tmp_path / "runs" / "run-1" / "pi-agent" / "auth.json").exists()


# -- end to end through the runner ------------------------------------------------


def test_a_run_calls_the_model_and_search_through_the_proxy_end_to_end(
    tmp_path: Path, upstream: FakeUpstream, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", MODEL_KEY)  # an operator shell export
    call_log = tmp_path / "calls.jsonl"
    seen_env: dict[str, str] = {}

    def factory(argv, *, cwd=None, env=None, on_event=None):
        merged = dict(env or {})
        seen_env.update(merged)
        merged.update(
            {
                "FAKE_PI_WRITE_REPORT": "1",
                "FAKE_PI_CALL_MODEL": "1",
                "FAKE_PI_CALL_SEARCH": "1",
                "FAKE_PI_STREAM": "1",
                "FAKE_PI_CALL_LOG": str(call_log),
            }
        )
        return PiRpcClient(
            [sys.executable, str(FAKE_PI), *argv[1:]], cwd=cwd, env=merged, on_event=on_event
        )

    config = build_config(
        _values(
            tmp_path,
            OPENAI_BASE_URL=f"{upstream.origin}/v1",
            PI_GLINK_WEB_SEARCH_KEY=SEARCH_KEY,
            PI_GLINK_WEB_SEARCH_UPSTREAM=f"{upstream.origin}/exa",
        ),
        REPO_ROOT,
    )
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store, client_factory=factory)
    issued: list = []
    original = manager.egress.issue

    def recording(**kwargs: Any):
        grant = original(**kwargs)
        issued.append(grant)
        return grant

    manager.egress.issue = recording  # type: ignore[method-assign]
    try:
        record = manager.start("经由出口代理的合成研究")
        final = manager.wait(record.run_id, timeout=30)
    finally:
        manager.shutdown()

    assert final is not None and final.status == "finished", final
    calls = [json.loads(line) for line in call_log.read_text(encoding="utf-8").splitlines()]
    assert [(call["route"], call["status"]) for call in calls] == [("model", 200), ("search", 200)]
    # The upstream saw the real keys; the kernel saw only the token.
    [grant] = issued
    model_call, search_call = upstream.requests
    assert model_call["headers"]["Authorization"] == f"Bearer {MODEL_KEY}"
    assert search_call["headers"]["X-Api-Key"] == SEARCH_KEY
    assert "OPENAI_API_KEY" not in seen_env
    for value in seen_env.values():
        assert MODEL_KEY not in value and SEARCH_KEY not in value
    # The proxy's meter is the run's authoritative usage.
    assert final.usage is not None
    assert final.usage["source"] == "egress"
    assert (final.usage["requests"], final.usage["searchRequests"]) == (1, 1)
    assert final.usage["totalTokens"] == 16 and final.usage["input"] == 11
    # The token died with the verdict and the listener closed.
    assert manager.egress.grant_for(grant.token) is None
    assert manager.egress.port is None
    # No real key anywhere in the run's files (events, report, runtime dir).
    for path in store.run_dir(record.run_id).rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            assert MODEL_KEY not in text and SEARCH_KEY not in text, path
    for path in ((config.data_root / "runs" / record.run_id / "sessions")).rglob("*"):
        if path.is_file():
            assert MODEL_KEY not in path.read_text(encoding="utf-8", errors="replace")
