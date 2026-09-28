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

from pi_glink.store import RunSpec
from pi_glink.app import PiGlinkServer, create_server
from pi_glink.config import build_config
from pi_glink.kernel.rpc import PiRpcClient
from store_helpers import finish

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
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
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


def _run_dirs(server: PiGlinkServer) -> list[str]:
    runs = server.config.data_root / "runs"
    return sorted(path.name for path in runs.glob("*")) if runs.is_dir() else []


@pytest.mark.parametrize(
    "prompt",
    [
        "/run researcher --bg 查跨境资金监管",
        "/subagents-detach",
        "   /chain scout -> planner",
        "\n\t/parallel researcher --bg",
        "/",
        "/DeepResearch 题目",
        "/skill:pi-subagents",
    ],
)
def test_slash_commands_outside_the_template_allowlist_are_refused(
    server: PiGlinkServer, prompt: str
) -> None:
    """GLINK-SUB-02 评审修复：客户端输入不能绕过 tool_call 守卫发起 detached run。

    pi 对以 `/` 开头的消息会先执行扩展命令（docs/rpc-commands.md），而载入的
    pi-subagents 注册了 `/run --bg`、`/subagents-detach` 等。准入层只接受从
    `methodology/blueprints/` 推导的蓝图命令（`/<蓝图名> <题目>`），并在任何 run 目录/执行面之前返回 400。
    """
    before = _run_dirs(server)
    status, _headers, raw = request(
        server, "/api/runs", method="POST", body={"prompt": prompt}
    )
    assert status == 400, raw
    body = json.loads(raw)
    assert body["error"] == "unsupported_command"
    assert body["allowed"] == ["deepresearch"]
    # 不得回声被拒的输入（错误体只给命令列表与固定说明）；裸 "/" 没有名字，
    # 只验证错误码。
    rejected = prompt.strip()
    if len(rejected) > 1:
        assert rejected not in raw.decode("utf-8")
    assert _run_dirs(server) == before, "拒绝必须早于 run 目录创建"


def test_template_commands_and_ordinary_prompts_still_run(server: PiGlinkServer) -> None:
    for prompt in (
        "/deepresearch 跨境资金监管新规",
        "正文里出现 /run --bg 也只是普通文本",
        "  前导空白的普通问题",
    ):
        status, _headers, raw = request(
            server, "/api/runs", method="POST", body={"prompt": prompt}
        )
        assert status == 201, raw
        run = json.loads(raw)["run"]
        assert wait_for_terminal(server, run["runId"])["status"] == "finished"


def test_followup_prompts_pass_the_same_gate(tmp_path: Path) -> None:
    """追问走同一个 start()：命令门禁对 parentRunId 同样生效。"""

    def factory(argv, *, cwd=None, env=None, on_event=None):
        return PiRpcClient(
            [sys.executable, str(FAKE_PI), *argv[1:]], cwd=cwd,
            env={**(env or {}), "FAKE_PI_PERSIST_SESSION": "1", "FAKE_PI_WRITE_REPORT": "1"},
            on_event=on_event,
        )

    instance = create_server(make_config(tmp_path), client_factory=factory)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        status, _headers, raw = request(
            instance, "/api/runs", method="POST", body={"prompt": "第一个问题"}
        )
        assert status == 201, raw
        parent = json.loads(raw)["run"]
        wait_for_terminal(instance, parent["runId"])

        before = _run_dirs(instance)
        status, _headers, raw = request(
            instance,
            "/api/runs",
            method="POST",
            body={"prompt": "  /subagents-detach", "parentRunId": parent["runId"]},
        )
        assert status == 400, raw
        assert json.loads(raw)["error"] == "unsupported_command"
        assert _run_dirs(instance) == before

        status, _headers, raw = request(
            instance,
            "/api/runs",
            method="POST",
            body={"prompt": "/deepresearch 合法追问", "parentRunId": parent["runId"]},
        )
        assert status == 201, raw
        child = json.loads(raw)["run"]
        assert wait_for_terminal(instance, child["runId"])["status"] == "finished"
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


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


def test_followup_continues_the_same_session_without_mutating_previous_run(tmp_path: Path) -> None:
    def factory(argv, *, cwd=None, env=None, on_event=None):
        return PiRpcClient(
            [sys.executable, str(FAKE_PI), *argv[1:]], cwd=cwd,
            env={**(env or {}), "FAKE_PI_PERSIST_SESSION": "1", "FAKE_PI_WRITE_REPORT": "1"},
            on_event=on_event,
        )

    instance = create_server(make_config(tmp_path), client_factory=factory)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, raw = request(instance, "/api/runs", method="POST", body={"prompt": "第一个问题"})
        assert status == 201
        parent = json.loads(raw)["run"]
        wait_for_terminal(instance, parent["runId"])
        parent_record = instance.store.get(parent["runId"])
        parent_bytes = Path(parent_record.session_file).read_bytes()

        status, _, raw = request(instance, "/api/runs", method="POST", body={
            "prompt": "追问细节", "parentRunId": parent["runId"],
        })
        assert status == 201, raw
        child = json.loads(raw)["run"]
        assert child["parentRunId"] == parent["runId"]
        final = wait_for_terminal(instance, child["runId"])
        assert final["status"] == "finished"
        child_record = instance.store.get(child["runId"])
        assert child_record.session_id == parent_record.session_id
        assert Path(parent_record.session_file).read_bytes() == parent_bytes
        assert b"\xe8\xbf\xbd\xe9\x97\xae" in Path(child_record.session_file).read_bytes()
        assert final["hasReport"] is True
        assert instance.store.get(parent["runId"]).event_count == parent_record.event_count

        status, _, raw = request(instance, "/api/runs", method="POST", body={
            "prompt": "不能从旧轮分叉", "parentRunId": parent["runId"],
        })
        assert status == 409
        assert json.loads(raw)["error"] == "stale_run"
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def test_followup_refuses_a_kernel_that_did_not_resume_parent_session(tmp_path: Path) -> None:
    launches = 0

    def factory(argv, *, cwd=None, env=None, on_event=None):
        nonlocal launches
        launches += 1
        return PiRpcClient(
            [sys.executable, str(FAKE_PI), *argv[1:]], cwd=cwd,
            env={**(env or {}), "FAKE_PI_PERSIST_SESSION": "1",
                 "FAKE_PI_SESSION_ID": "original" if launches == 1 else "unexpected"},
            on_event=on_event,
        )

    instance = create_server(make_config(tmp_path), client_factory=factory)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        _, _, raw = request(instance, "/api/runs", method="POST", body={"prompt": "原问题"})
        parent_id = json.loads(raw)["run"]["runId"]
        wait_for_terminal(instance, parent_id)
        _, _, raw = request(instance, "/api/runs", method="POST", body={
            "prompt": "不应交给错误的会话", "parentRunId": parent_id,
        })
        child_id = json.loads(raw)["run"]["runId"]
        final = wait_for_terminal(instance, child_id)
        assert final["status"] == "failed"
        assert instance.store.get(child_id).parent_run_id == parent_id
        # The child never prompted the wrong session; only the copied seed exists.
        copied = (instance.config.data_root / "runs" / child_id / "sessions") / "continued.jsonl"
        assert "不应交给错误的会话" not in copied.read_text(encoding="utf-8")
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def test_followup_without_a_run_owned_session_is_rejected(server: PiGlinkServer) -> None:
    status, _, raw = request(server, "/api/runs", method="POST", body={"prompt": "原问题"})
    parent_id = json.loads(raw)["run"]["runId"]
    wait_for_terminal(server, parent_id)
    status, _, raw = request(server, "/api/runs", method="POST", body={
        "prompt": "追问", "parentRunId": parent_id,
    })
    assert status == 409
    assert json.loads(raw)["error"] == "session_unavailable"
    assert len(server.store.list(limit=None)) == 1
    status, _, raw = request(server, "/api/runs", method="POST", body={
        "prompt": "非法父级", "parentRunId": "../other",
    })
    assert status == 400
    assert json.loads(raw)["error"] == "invalid_request"


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


def test_children_endpoint_lists_the_runs_child_documents(server: PiGlinkServer) -> None:
    """GLINK-SUB-R1: GET /api/runs/{id}/children serves the authoritative
    child.json documents (§3.5); a run without a children/ tree has none."""
    _status, _headers, raw = request(server, "/api/runs", method="POST", body={"prompt": "子代理"})
    run_id = json.loads(raw)["run"]["runId"]
    wait_for_terminal(server, run_id)

    # A run without children answers 200 with an empty list, not a 404.
    status, _headers, raw = request(server, f"/api/runs/{run_id}/children")
    assert status == 200
    assert json.loads(raw) == {"children": []}

    server.store.write_child(
        run_id,
        {
            "n": 0,
            "toolCallId": "call-1",
            "agent": "researcher",
            "task": "核查跨境资金监管",
            "status": "finished",
            "resultPreview": "核查完成",
            "enrichment": "none",
        },
    )
    server.store.write_child(
        run_id,
        {
            "n": 1,
            "toolCallId": "call-1",
            "agent": "researcher",
            "task": "比对两地口径",
            "status": "interrupted",
            "resultPreview": None,
            "enrichment": "none",
        },
    )
    status, _headers, raw = request(server, f"/api/runs/{run_id}/children")
    assert status == 200
    body = json.loads(raw)
    assert [child["n"] for child in body["children"]] == [0, 1]
    assert body["children"][0]["agent"] == "researcher"
    assert body["children"][0]["task"] == "核查跨境资金监管"
    assert body["children"][0]["status"] == "finished"
    assert body["children"][0]["resultPreview"] == "核查完成"
    assert body["children"][1]["status"] == "interrupted"


def test_children_endpoint_404s_like_the_run_detail(server: PiGlinkServer) -> None:
    status, _headers, raw = request(server, "/api/runs/nosuchrun/children")
    assert status == 404
    body = json.loads(raw)
    assert body["error"] == "unknown_run"
    assert body["runId"] == "nosuchrun"


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
    earlier process is replayed from its persisted event rows instead of
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
    from pi_glink.kernel.execution import (  # noqa: PLC0415
        ArtifactRecovery,
        CleanupReport,
        ExecutionAdapter,
        ExecutionOutcome,
    )

    class LeakingAdapter(ExecutionAdapter):
        def __init__(self):
            super().__init__(None, on_frame=lambda frame: None)

        def start(self): pass
        def identity(self): return {"sandbox": "synthetic-owned-resource"}
        def session_info(self): return None
        def deliver_input(self): pass
        def wait(self, *, cancel, deadline): return ExecutionOutcome(settled=True)
        def recover_artifacts(self, destination): return ArtifactRecovery()
        def close(self):
            return CleanupReport(released=False, failures=("synthetic kill failed",))
        def cancel(self): pass

    monkeypatch.setattr("pi_glink.kernel.harness_pi.PiExecutionAdapter", lambda spec, **kwargs: LeakingAdapter())
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


def _workspace_report_record(server: PiGlinkServer, run_id: str, *, report: str | None = None,
                             report_path: Path | None = None) -> Path:
    """A finished record whose report_path points outside the host artifact copy."""
    store = server.store
    workspace = server.config.workspace_root / run_id
    workspace.mkdir(parents=True, exist_ok=True)
    target = workspace / "report.md"
    if report is not None:
        target.write_text(report, encoding="utf-8")
    store.create("研究", run_id=run_id, workspace=workspace,
                 spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    finish(store, run_id, "finished", report_path=str(report_path or target), finished_at=time.time())
    return target


def _view(server: PiGlinkServer, run_id: str) -> dict:
    detail = json.loads(request(server, f"/api/runs/{run_id}")[2])["run"]
    listed = next(r for r in json.loads(request(server, "/api/runs")[2])["runs"] if r["runId"] == run_id)
    assert detail["hasReport"] == listed["hasReport"]
    return detail


def test_only_the_host_artifact_copy_is_served_as_the_report(
    server: PiGlinkServer, tmp_path: Path
) -> None:
    """report_path outside runs/<id>/artifacts/report.md never reopens a file.

    The workspace report, an external path and a workspace link all answer
    hasReport=false and 404 (docs/adr/0007).
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "report.md"
    external.write_text("# 外部文件\n", encoding="utf-8")

    # The agent's own workspace report.md.
    _workspace_report_record(server, "wsreport01", report="# 本体\n")
    # A path outside every run directory.
    _workspace_report_record(server, "external01", report="# 本体\n", report_path=external)
    # A workspace report.md that is a link to an external file.
    linked = _workspace_report_record(server, "wslink0001")
    linked.symlink_to(external)

    for run_id in ("wsreport01", "external01", "wslink0001"):
        assert _view(server, run_id)["hasReport"] is False, run_id
        assert request(server, f"/api/runs/{run_id}/report")[0] == 404, run_id
    assert external.read_text(encoding="utf-8") == "# 外部文件\n"


def test_a_host_artifact_behind_a_directory_link_is_not_served(server: PiGlinkServer, tmp_path: Path) -> None:
    """R2 consumer path: the artifacts directory swapped for a link."""
    outside = tmp_path / "outside-artifacts"
    outside.mkdir()
    (outside / "report.md").write_text("# 外部\n", encoding="utf-8")
    store = server.store
    store.create("产物目录被替换", run_id="swapartifact", workspace=tmp_path / "ws", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    (store.run_dir("swapartifact") / "artifacts").symlink_to(outside, target_is_directory=True)
    finish(store, "swapartifact", "finished", report_path=str(store.run_dir("swapartifact") / "artifacts" / "report.md"))
    assert _view(server, "swapartifact")["hasReport"] is False
    assert request(server, "/api/runs/swapartifact/report")[0] == 404


def test_child_artifact_download_serves_only_the_child_directory(server: PiGlinkServer) -> None:
    """GLINK-SUB-R2: GET /api/runs/{id}/children/{n}/{file} serves a regular
    file directly inside children/<n>/, redacted on the way out; anything else
    is the same 404, and the owner rules are the run detail's."""
    _status, _headers, raw = request(server, "/api/runs", method="POST", body={"prompt": "子代理"})
    run_id = json.loads(raw)["run"]["runId"]
    wait_for_terminal(server, run_id)
    child_dir = server.store.run_dir(run_id) / "children" / "0"
    child_dir.mkdir(parents=True)
    (child_dir / "output.md").write_text("# 调研笔记\n\n结论在此。\n", encoding="utf-8")

    status, headers, raw = request(server, f"/api/runs/{run_id}/children/0/output.md")
    assert status == 200
    assert "markdown" in headers.get("Content-Type", "")
    assert "结论在此" in raw.decode("utf-8")
    assert headers.get("X-Redacted") == "false"

    # Missing file / unknown run / path tricks all answer the same 404.
    status, _h, _r = request(server, f"/api/runs/{run_id}/children/0/absent.md")
    assert status == 404
    status, _h, _r = request(server, f"/api/runs/{run_id}/children/9/output.md")
    assert status == 404
    status, _h, r = request(server, "/api/runs/ffffffffffff/children/0/output.md")
    assert status == 404 and json.loads(r)["error"] in ("no_artifact", "unknown_run")
    status, _h, _r = request(server, f"/api/runs/{run_id}/children/0/..%2Fmeta.json")
    assert status == 404
    # 规则就是「children/<n>/ 内的普通文件」：child.json 同样可下载（内容与
    # children 列表端点一致，均为已脱敏事实）。
    (child_dir / "child.json").write_text('{"n": 0}', encoding="utf-8")
    status, _h, raw = request(server, f"/api/runs/{run_id}/children/0/child.json")
    assert status == 200 and json.loads(raw) == {"n": 0}
