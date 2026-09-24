"""Regressions for the bounded SSE surface (GLINK-HTTP-01).

The stream budget is a per-host ceiling (plus a per-tenant one in multi-tenant
mode). It is checked before any stream header is written, so an over-limit
client gets a JSON error instead of a torn stream, and every way a stream can
end — normal end, client disconnect, expired identity, slow consumer — must
return its slot.
"""

from __future__ import annotations

import json
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

import pytest

from pi_glink.app import PiGlinkServer, create_server
from pi_glink.config import ConfigError, build_config
from pi_glink.kernel.execution import (
    ArtifactRecovery,
    CleanupReport,
    ExecutionOutcome,
    SessionInfo,
)
from pi_glink.web_sse import StreamLimits

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_config(tmp_path: Path, **overrides: str):
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_PORT": "0",
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent-web-search.json"),
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


class _Adapter:
    """A run that stays open until cancelled (``forever=False`` settles at once)."""

    kind = "inproc"

    def __init__(self, *, forever: bool = True) -> None:
        self._cancelled = threading.Event()
        self._forever = forever

    def start(self) -> None:
        return None

    def session_info(self) -> SessionInfo | None:
        return None

    def deliver_input(self) -> None:
        return None

    def wait(self, *, cancel, deadline: float) -> ExecutionOutcome:
        while self._forever and not self._cancelled.is_set():
            if cancel():
                break
            time.sleep(0.01)
        return ExecutionOutcome(settled=True, cancelled=self._cancelled.is_set())

    def cancel(self) -> None:
        self._cancelled.set()

    def recover_artifacts(self, destination: Path) -> ArtifactRecovery:
        return ArtifactRecovery()

    def close(self) -> CleanupReport:
        return CleanupReport(released=True)

    def identity(self) -> dict[str, str]:
        return {}


@pytest.fixture()
def stream_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A local host whose execution face is a fake adapter (no pi process)."""
    created: list[PiGlinkServer] = []

    def build(adapter: _Adapter, **overrides: str):
        monkeypatch.setattr("pi_glink.kernel.runner.build_adapter", lambda spec, **kwargs: adapter)
        instance = create_server(make_config(tmp_path, **overrides))
        created.append(instance)
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        return instance, thread

    yield build
    for instance in created:
        instance.shutdown()
        instance.server_close()


def start_run(server: PiGlinkServer, prompt: str = "流") -> str:
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        body = json.dumps({"prompt": prompt}).encode("utf-8")
        connection.request(
            "POST", "/api/runs", body=body, headers={"Content-Type": "application/json"}
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        assert response.status == 201, payload
        return payload["run"]["runId"]
    finally:
        connection.close()


def open_stream(server: PiGlinkServer, run_id: str, *, timeout: float = 10.0):
    """Open the SSE request and return the live connection plus its response."""
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=timeout)
    connection.request("GET", f"/api/runs/{run_id}/events")
    return connection, connection.getresponse()


def read_all(server: PiGlinkServer, run_id: str, *, timeout: float = 10.0) -> tuple[int, str, bytes]:
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=timeout)
    try:
        connection.request("GET", f"/api/runs/{run_id}/events")
        response = connection.getresponse()
        return response.status, response.getheader("Content-Type") or "", response.read()
    finally:
        connection.close()


def wait_for_terminal(server: PiGlinkServer, run_id: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = server.store.get(run_id)
        if record is not None and record.is_terminal:
            return
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} did not reach a terminal state")


def test_the_global_ceiling_refuses_before_any_stream_header(tmp_path: Path, stream_server) -> None:
    adapter = _Adapter(forever=True)
    server, _thread = stream_server(adapter, PI_GLINK_MAX_GLOBAL_STREAMS="1")
    run_id = start_run(server)

    connection, response = open_stream(server, run_id)
    try:
        assert response.status == 200
        assert response.getheader("Content-Type").startswith("text/event-stream")
        assert b"event: run_started" in response.read1(256)

        status, content_type, body = read_all(server, run_id)
        assert status == 429
        assert content_type.startswith("application/json")
        assert "text/event-stream" not in content_type, "拒绝必须在写出任何流响应头之前"
        payload = json.loads(body)
        assert payload["error"] == "stream_limit"
        assert payload["scope"] == "global"
        assert payload["limit"] == 1
        assert run_id not in body.decode("utf-8")
    finally:
        connection.close()

    # The reader left without an end-of-stream marker. The server only notices
    # on the next write — which must return the slot through the stream's
    # ``finally`` instead of leaking it.
    released = False
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        server.bus.publish(run_id, "assistant_delta", {"text": "after disconnect"})
        time.sleep(0.05)
        probe, response = open_stream(server, run_id, timeout=5.0)
        try:
            if response.status == 200:
                released = True
                break
            assert response.status == 429
        finally:
            probe.close()
    assert released, "断开连接后必须释放流额度"


def test_a_stream_that_ends_normally_releases_its_slot(tmp_path: Path, stream_server) -> None:
    adapter = _Adapter(forever=False)
    server, _thread = stream_server(adapter, PI_GLINK_MAX_GLOBAL_STREAMS="1")
    run_id = start_run(server)
    wait_for_terminal(server, run_id)

    status, content_type, body = read_all(server, run_id)
    assert status == 200 and content_type.startswith("text/event-stream")
    assert b"event: run_finished" in body

    # The first stream ended by itself: the same ceiling accepts the next one.
    status, _content_type, body = read_all(server, run_id)
    assert status == 200
    assert b"event: run_finished" in body


def test_stream_limits_account_per_tenant_and_globally() -> None:
    limits = StreamLimits(global_limit=2, tenant_limit=1)
    assert limits.acquire("tenant-a") is None
    assert limits.acquire("tenant-a") == "tenant", "同一租户的第二个连接超出该租户额度"
    assert limits.acquire("tenant-b") is None
    assert limits.acquire("tenant-b") == "global", "宿主总额度先于租户额度被检查"
    assert limits.active() == (2, {"tenant-a": 1, "tenant-b": 1})

    # Freeing one slot makes room for that tenant again; the host ceiling still
    # bounds the total, and a tenant already at its own ceiling is told so.
    limits.release("tenant-a")
    assert limits.acquire("tenant-b") == "tenant"
    assert limits.acquire("tenant-a") is None
    assert limits.acquire("tenant-a") == "global"
    limits.release("tenant-a")
    limits.release("tenant-b")
    assert limits.active() == (0, {})
    assert limits.limit_for("global") == 2 and limits.limit_for("tenant") == 1

    # An unknown tenant never leaks a counter entry.
    limits.acquire(None)
    limits.release(None)
    assert limits.active() == (0, {})


def test_stream_limits_require_a_positive_ceiling() -> None:
    with pytest.raises(ValueError):
        StreamLimits(global_limit=0, tenant_limit=1)
    with pytest.raises(ValueError):
        StreamLimits(global_limit=1, tenant_limit=0)


def test_the_stream_ceiling_config_rejects_zero(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="PI_GLINK_MAX_GLOBAL_STREAMS"):
        make_config(tmp_path, PI_GLINK_MAX_GLOBAL_STREAMS="0")
    with pytest.raises(ConfigError, match="PI_GLINK_MAX_TENANT_STREAMS"):
        make_config(tmp_path, PI_GLINK_MAX_TENANT_STREAMS="-1")


def test_the_default_ceilings_are_the_documented_ones(tmp_path: Path) -> None:
    config: Any = make_config(tmp_path)
    assert (config.max_global_streams, config.max_tenant_streams) == (64, 16)
