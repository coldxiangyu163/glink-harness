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


def test_event_stream_replays_runs_from_an_earlier_host_process(tmp_path: Path) -> None:
    """History replay must survive a host restart.

    The in-memory bus only knows runs this process started; a run from an
    earlier process is replayed from its persisted events.jsonl instead of
    answering 404 (which left the browser with an empty timeline).
    """
    first = create_server(make_config(tmp_path), client_factory=fake_factory())
    thread = threading.Thread(target=first.serve_forever, daemon=True)
    thread.start()
    try:
        _status, _headers, raw = request(first, "/api/runs", method="POST", body={"prompt": "重启后重放"})
        run_id = json.loads(raw)["run"]["runId"]
        wait_for_terminal(first, run_id)
    finally:
        first.shutdown()
        first.server_close()
        thread.join(timeout=5)

    second = create_server(make_config(tmp_path), client_factory=fake_factory())
    thread = threading.Thread(target=second.serve_forever, daemon=True)
    thread.start()
    try:
        assert not second.bus.is_open(run_id)
        connection = HTTPConnection("127.0.0.1", second.server_address[1], timeout=10)
        try:
            connection.request("GET", f"/api/runs/{run_id}/events")
            response = connection.getresponse()
            assert response.status == 200
            assert response.getheader("Content-Type").startswith("text/event-stream")
            body = response.read().decode("utf-8")
        finally:
            connection.close()
        assert "event: run_started" in body
        assert "event: run_finished" in body
        # Resume offsets still apply to persisted frames.
        connection = HTTPConnection("127.0.0.1", second.server_address[1], timeout=10)
        try:
            connection.request("GET", f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "1"})
            resumed = connection.getresponse().read().decode("utf-8")
        finally:
            connection.close()
        assert "event: run_started" not in resumed
        assert "event: run_finished" in resumed

        status, _headers, _raw = request(second, "/api/runs/does-not-exist/events")
        assert status == 404
    finally:
        second.shutdown()
        second.server_close()
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


# -- R5: the stop endpoint reports the run's commit point -------------------------


def test_stop_answers_follow_the_committed_state(server: PiGlinkServer) -> None:
    """R5 consumer contract: accepted → 200; past the commit → 409 with the
    committed status; unknown run → 404. The WIP answered 200 for all three."""
    _status, _headers, raw = request(server, "/api/runs", method="POST", body={"prompt": "已结束的 run"})
    run_id = json.loads(raw)["run"]["runId"]
    assert wait_for_terminal(server, run_id)["status"] == "finished"

    status, _headers, raw = request(server, f"/api/runs/{run_id}/stop", method="POST")
    assert status == 409
    body = json.loads(raw)
    assert body == {
        "error": "run_not_cancellable",
        "requested": False,
        "status": "finished",
        "reason": "terminal",
    }
    status, _headers, raw = request(server, "/api/runs/does-not-exist/stop", method="POST")
    assert status == 404
    assert json.loads(raw)["error"] == "unknown_run"


# -- R4: admission follows resource release -------------------------------------


def test_start_is_refused_while_a_release_is_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pi_glink.kernel.execution import ArtifactRecovery, CleanupReport, ExecutionOutcome  # noqa: PLC0415

    class LeakingAdapter:
        def start(self): pass
        def identity(self): return {"sandbox": "synthetic-owned-resource"}
        def session_info(self): return None
        def deliver_input(self): pass
        def wait(self, *, cancel, deadline): return ExecutionOutcome(settled=True)
        def recover_artifacts(self, destination): return ArtifactRecovery()
        def close(self):
            return CleanupReport(released=False, failures=("synthetic kill failed",))
        def cancel(self): pass

    monkeypatch.setattr("pi_glink.kernel.runner.build_adapter", lambda spec, **kwargs: LeakingAdapter())
    instance = create_server(make_config(tmp_path), client_factory=fake_factory())
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        _status, _headers, raw = request(instance, "/api/runs", method="POST", body={"prompt": "泄漏"})
        run_id = json.loads(raw)["run"]["runId"]
        assert wait_for_terminal(instance, run_id)["status"] == "finished"
        status, _headers, raw = request(instance, "/api/runs", method="POST", body={"prompt": "下一个"})
        assert status == 409
        body = json.loads(raw)
        assert body["error"] == "resources_unreleased" and body["runIds"] == [run_id]
        health = json.loads(request(instance, "/api/health")[2])
        assert health["activeRunId"] is None, "no run executes; capacity is held by the resource"
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


# -- R6: reports written before host copies existed stay readable ----------------


def _legacy_record(server: PiGlinkServer, run_id: str, *, report: str | None = None,
                   report_path: Path | None = None, workspace: Path | None = None) -> Path:
    """An old-schema record: report_path points into workspaces/<id>/report.md.

    The meta.json fields introduced later are removed, as an upgraded host
    finds them on disk.
    """
    store = server.store
    workspace = workspace or (server.config.workspace_root / run_id)
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / "report.md"
    if report is not None:
        target.write_text(report, encoding="utf-8")
    store.create("历史研究", run_id=run_id, workspace=workspace)
    store.update(run_id, status="finished", report_path=str(report_path or target),
                 finished_at=time.time(), event_count=0)
    meta_path = store.run_dir(run_id) / "meta.json"
    fields = json.loads(meta_path.read_text(encoding="utf-8"))
    for key in ("model_stop_reason", "report_status", "cleanup", "container_name",
                "sandbox_id", "resource_adjudication"):
        fields.pop(key, None)
    meta_path.write_text(json.dumps(fields), encoding="utf-8")
    return target


def _view(server: PiGlinkServer, run_id: str) -> dict:
    detail = json.loads(request(server, f"/api/runs/{run_id}")[2])["run"]
    listed = next(r for r in json.loads(request(server, "/api/runs")[2])["runs"] if r["runId"] == run_id)
    assert detail["hasReport"] == listed["hasReport"]
    return detail


def test_a_legacy_workspace_report_is_downloadable_read_only(server: PiGlinkServer) -> None:
    """R6: the upgrade kept the file but the endpoint answered 404."""
    secret_line = "OPENAI_API_KEY=sk-legacy0123456789abcdef"
    original = f"# 历史报告\n\n{secret_line}\n"
    target = _legacy_record(server, "legacyrun01", report=original)
    before = target.stat()

    assert _view(server, "legacyrun01")["hasReport"] is True
    status, headers, body = request(server, "/api/runs/legacyrun01/report")
    assert status == 200
    assert body.decode("utf-8").startswith("# 历史报告")
    assert "sk-legacy0123456789abcdef" not in body.decode("utf-8")
    assert headers.get("X-Redacted") == "true"
    after = target.stat()
    assert target.read_text(encoding="utf-8") == original, "history is never rewritten"
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_legacy_layout_does_not_reopen_arbitrary_report_paths(
    server: PiGlinkServer, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "report.md"
    external.write_text("# 外部文件\n", encoding="utf-8")
    workspace_root = server.config.workspace_root

    # A path outside the run's workspace.
    _legacy_record(server, "legacyout01", report="# 本体\n", report_path=external)
    # A path deeper inside the workspace than the legacy layout allows.
    nested = workspace_root / "legacynest1" / "sub"
    nested.mkdir(parents=True)
    (nested / "report.md").write_text("# 嵌套\n", encoding="utf-8")
    _legacy_record(server, "legacynest1", report="# 本体\n", report_path=nested / "report.md")
    # A workspace that is not the run's own (another run's directory).
    _legacy_record(server, "legacyother", report_path=workspace_root / "legacyrun99" / "report.md",
                   workspace=workspace_root / "legacyrun99")
    (workspace_root / "legacyrun99" / "report.md").write_text("# 别人的\n", encoding="utf-8")
    # A legacy report.md that is a link to an external file.
    linked = _legacy_record(server, "legacylink1")
    linked.symlink_to(external)
    # A legacy workspace directory replaced by a link to an external directory.
    swapped = workspace_root / "legacyswap1"
    _legacy_record(server, "legacyswap1", report="# 本体\n")
    (swapped / "report.md").unlink()
    swapped.rmdir()
    swapped.symlink_to(outside, target_is_directory=True)

    for run_id in ("legacyout01", "legacynest1", "legacyother", "legacylink1", "legacyswap1"):
        assert _view(server, run_id)["hasReport"] is False, run_id
        assert request(server, f"/api/runs/{run_id}/report")[0] == 404, run_id
    assert external.read_text(encoding="utf-8") == "# 外部文件\n"


def test_a_host_artifact_behind_a_directory_link_is_not_served(server: PiGlinkServer, tmp_path: Path) -> None:
    """R2 consumer path: the artifacts directory swapped for a link."""
    outside = tmp_path / "outside-artifacts"
    outside.mkdir()
    (outside / "report.md").write_text("# 外部\n", encoding="utf-8")
    store = server.store
    store.create("产物目录被替换", run_id="swapartifact", workspace=tmp_path / "ws")
    (store.run_dir("swapartifact") / "artifacts").symlink_to(outside, target_is_directory=True)
    store.update("swapartifact", status="finished",
                 report_path=str(store.run_dir("swapartifact") / "artifacts" / "report.md"))
    assert _view(server, "swapartifact")["hasReport"] is False
    assert request(server, "/api/runs/swapartifact/report")[0] == 404
