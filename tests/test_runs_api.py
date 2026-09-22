"""Regressions for the run HTTP surface (create, list, report, stop, stream)."""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.client import HTTPConnection
from pathlib import Path

import pytest

from pi_glink.app import PiGlinkServer, create_server
from pi_glink.config import build_config
from pi_glink.kernel.rpc import PiRpcClient

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_PI = Path(__file__).resolve().parent / "fixtures" / "fake_pi.py"


def fake_factory(*, write_report: bool = True):
    def factory(argv, *, cwd=None, env=None, on_event=None):
        merged = dict(env or {})
        merged["FAKE_PI_WRITE_REPORT"] = "1" if write_report else "0"
        return PiRpcClient(
            [sys.executable, str(FAKE_PI), *argv[1:]],
            cwd=cwd,
            env=merged,
            on_event=on_event,
        )

    return factory


def make_config(tmp_path: Path, **overrides: str):
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_PORT": "0",
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


@pytest.fixture()
def server(tmp_path: Path):
    instance = create_server(make_config(tmp_path), client_factory=fake_factory())
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def request(
    server: PiGlinkServer, path: str, *, method: str = "GET", body: dict | None = None
) -> tuple[int, dict[str, str], bytes]:
    port = server.server_address[1]
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=payload, method=method
    )
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def wait_for_terminal(server: PiGlinkServer, run_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    latest: dict = {}
    while time.monotonic() < deadline:
        status, _headers, raw = request(server, f"/api/runs/{run_id}")
        latest = json.loads(raw)["run"]
        if latest["status"] != "running":
            return latest
        time.sleep(0.05)
    return latest


def test_start_run_returns_a_run_view(server: PiGlinkServer) -> None:
    status, _headers, raw = request(
        server, "/api/runs", method="POST", body={"prompt": "写一份最小报告"}
    )
    assert status == 201
    run = json.loads(raw)["run"]
    assert run["status"] == "running"
    assert run["prompt"] == "写一份最小报告"
    assert "workspace" not in run and "reportPath" not in run  # no host paths leak

    final = wait_for_terminal(server, run["runId"])
    assert final["status"] == "finished"
    assert final["hasReport"] is True
    assert final["sessionId"] == "fake-session"
    assert final["eventCount"] > 0


def test_runs_are_listed_and_readable(server: PiGlinkServer) -> None:
    _status, _headers, raw = request(server, "/api/runs", method="POST", body={"prompt": "列我"})
    run_id = json.loads(raw)["run"]["runId"]
    wait_for_terminal(server, run_id)

    status, _headers, raw = request(server, "/api/runs")
    assert status == 200
    runs = json.loads(raw)["runs"]
    assert [item["runId"] for item in runs] == [run_id]

    status, _headers, raw = request(server, f"/api/runs/{run_id}")
    assert status == 200
    assert json.loads(raw)["run"]["runId"] == run_id


def test_report_is_served_as_markdown(server: PiGlinkServer) -> None:
    _status, _headers, raw = request(server, "/api/runs", method="POST", body={"prompt": "报告"})
    run_id = json.loads(raw)["run"]["runId"]
    wait_for_terminal(server, run_id)

    status, headers, body = request(server, f"/api/runs/{run_id}/report")
    assert status == 200
    assert headers["Content-Type"].startswith("text/markdown")
    assert "fake pi" in body.decode("utf-8")


def test_missing_report_is_a_404(tmp_path: Path) -> None:
    instance = create_server(
        make_config(tmp_path), client_factory=fake_factory(write_report=False)
    )
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        _status, _headers, raw = request(instance, "/api/runs", method="POST", body={"prompt": "无报告"})
        run_id = json.loads(raw)["run"]["runId"]
        final = wait_for_terminal(instance, run_id)
        assert final["hasReport"] is False
        status, _headers, raw = request(instance, f"/api/runs/{run_id}/report")
        assert status == 404
        assert json.loads(raw)["error"] == "no_report"
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def test_empty_prompt_is_rejected(server: PiGlinkServer) -> None:
    status, _headers, raw = request(server, "/api/runs", method="POST", body={"prompt": "   "})
    assert status == 400
    assert json.loads(raw)["error"] == "invalid_request"


def test_non_json_body_is_rejected(server: PiGlinkServer) -> None:
    port = server.server_address[1]
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/runs", data=b"not json", method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        raise AssertionError("expected HTTP 400")
    except urllib.error.HTTPError as error:
        assert error.code == 400


def test_second_run_while_busy_is_a_conflict(server: PiGlinkServer) -> None:
    _status, _headers, raw = request(
        server, "/api/runs", method="POST", body={"prompt": "这一轮不要结束 NOSETTLE"}
    )
    run_id = json.loads(raw)["run"]["runId"]
    status, _headers, raw = request(server, "/api/runs", method="POST", body={"prompt": "第二个"})
    assert status == 409
    payload = json.loads(raw)
    assert payload["error"] == "run_busy"
    assert payload["activeRunId"] == run_id

    # Wait until the kernel is actually attached before cancelling: the point of
    # this test is the 409 and the accepted stop, not a race with process start.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        _status, _headers, raw = request(server, f"/api/runs/{run_id}")
        if json.loads(raw)["run"]["eventCount"] >= 2:  # run_started + session_ready
            break
        time.sleep(0.1)
    else:
        raise AssertionError("run never reported its session as ready")

    status, _headers, raw = request(server, f"/api/runs/{run_id}/stop", method="POST")
    assert status == 200
    assert json.loads(raw)["requested"] is True
    assert wait_for_terminal(server, run_id)["status"] == "cancelled"


def test_missing_credential_is_reported_as_not_configured(tmp_path: Path) -> None:
    instance = create_server(make_config(tmp_path, OPENAI_API_KEY=""), client_factory=fake_factory())
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        status, _headers, raw = request(instance, "/api/runs", method="POST", body={"prompt": "x"})
        assert status == 400
        assert json.loads(raw)["error"] == "not_configured"
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def test_event_stream_replays_finished_runs(server: PiGlinkServer) -> None:
    _status, _headers, raw = request(server, "/api/runs", method="POST", body={"prompt": "流"})
    run_id = json.loads(raw)["run"]["runId"]
    wait_for_terminal(server, run_id)

    port = server.server_address[1]
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request("GET", f"/api/runs/{run_id}/events")
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type").startswith("text/event-stream")
        head = response.read(120)
        assert b"event: run_started" in head
    finally:
        connection.close()
