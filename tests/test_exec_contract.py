"""R1-5 执行服务 HTTP 契约测试：与 docs/exec-contract.md 逐条对应。

每条测试的 docstring 以契约条款编号开头（如「契约 1.1」）；条款变化时文档与
本文件同步修改。全部使用合成输入、临时目录、fake pi 与随机 loopback 端口，
不触达真实模型、真实配置或既有运行数据。
"""

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

from glink_harness import event_schema, events
from glink_harness.app import GlinkHarnessServer, create_server
from glink_harness.config import build_config
from glink_harness.kernel.rpc import PiRpcClient
from glink_harness.store import RunSpec
from glink_harness.web_routes import FORBIDDEN_RUN_BODY_FIELDS
from store_helpers import finish

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_PI = Path(__file__).resolve().parent / "fixtures" / "fake_pi.py"


def fake_factory(*, write_report: bool = True, persist_session: bool = False):
    def factory(argv, *, cwd=None, env=None, on_event=None):
        merged = dict(env or {})
        merged["FAKE_PI_WRITE_REPORT"] = "1" if write_report else "0"
        if persist_session:
            merged["FAKE_PI_PERSIST_SESSION"] = "1"
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
        "GLINK_HARNESS": "pi",
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_PORT": "0",
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


def serve(instance: GlinkHarnessServer):
    """Run ``instance`` in a daemon thread; returns a closer that shuts it down."""
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()

    def close() -> None:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)

    return close


@pytest.fixture()
def server(tmp_path: Path):
    instance = create_server(make_config(tmp_path), client_factory=fake_factory())
    close = serve(instance)
    try:
        yield instance
    finally:
        close()


def request(
    server: GlinkHarnessServer, path: str, *, method: str = "GET", body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    port = server.server_address[1]
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=payload, method=method
    )
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def start_run(server: GlinkHarnessServer, body: dict) -> dict:
    status, _headers, raw = request(server, "/api/runs", method="POST", body=body)
    assert status == 201, raw
    return json.loads(raw)["run"]


def wait_for_terminal(server: GlinkHarnessServer, run_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    latest: dict = {}
    while time.monotonic() < deadline:
        status, _headers, raw = request(server, f"/api/runs/{run_id}")
        latest = json.loads(raw)["run"]
        if latest["status"] != "running":
            return latest
        time.sleep(0.05)
    return latest


def _run_dirs(server: GlinkHarnessServer) -> list[str]:
    runs = server.config.data_root / "runs"
    return sorted(path.name for path in runs.glob("*")) if runs.is_dir() else []


def read_stream(
    server: GlinkHarnessServer, run_id: str, *, query: str = "",
    headers: dict[str, str] | None = None,
) -> list[dict]:
    """Replay a finished run's stream and return the parsed frames."""
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        connection.request(
            "GET", f"/api/runs/{run_id}/events{query}", headers=headers or {}
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type").startswith("text/event-stream")
        body = response.read().decode("utf-8")
    finally:
        connection.close()
    frames = []
    for chunk in body.split("\n\n"):
        if not chunk.strip():
            continue
        frame: dict = {}
        for line in chunk.split("\n"):
            if line.startswith("id: "):
                frame["id"] = int(line[4:])
            elif line.startswith("event: "):
                frame["event"] = line[7:]
            elif line.startswith("data: "):
                frame["data"] = json.loads(line[6:])
        frames.append(frame)
    return frames


# -- 契约 1.1：禁用字段 ----------------------------------------------------


@pytest.mark.parametrize("field", sorted(FORBIDDEN_RUN_BODY_FIELDS))
def test_forbidden_run_body_fields_are_refused_400(
    server: GlinkHarnessServer, field: str
) -> None:
    """契约 1.1：模型/凭据类客户端字段一律 400 invalid_request，且先于 run 创建。"""
    before = _run_dirs(server)
    status, _headers, raw = request(
        server, "/api/runs", method="POST",
        body={"prompt": "合成题面", field: "sk-client-supplied"},
    )
    assert status == 400
    payload = json.loads(raw)
    assert payload["error"] == "invalid_request"
    assert payload["fields"] == [field]
    assert _run_dirs(server) == before
    assert server.manager.active_run_id is None


@pytest.mark.parametrize("field", ["Model", " APIKEY ", "openaiApiKey"])
def test_forbidden_field_matching_ignores_case_and_space(
    server: GlinkHarnessServer, field: str
) -> None:
    """契约 1.1：禁用字段的匹配对大小写与首尾空白不敏感。"""
    status, _headers, raw = request(
        server, "/api/runs", method="POST",
        body={"prompt": "合成题面", field: "sk-client-supplied"},
    )
    assert status == 400
    assert json.loads(raw)["error"] == "invalid_request"


def test_accepted_shape_ignored_extras_and_frozen_spec(server: GlinkHarnessServer) -> None:
    """契约 1.1/1.2：三字段受理为 201 + run 视图；未识别字段被忽略，RunSpec 来自冻结配置。"""
    run = start_run(server, {
        "prompt": "合成题面",
        "executor": "e2b",
        "harness": "codex",
        "model_choice": "gpt-whatever",
    })
    for key in (
        "runId", "status", "prompt", "parentRunId", "createdAt", "finishedAt",
        "durationSeconds", "sessionId", "hasReport", "deliverables", "stopReason",
        "error", "eventCount", "usage", "redacted", "blueprint", "blueprintStages",
    ):
        assert key in run, key
    assert "workspace" not in run and "reportPath" not in run
    record = server.store.get(run["runId"])
    assert (record.spec.family, record.spec.pack, record.spec.executor) == ("pi", "pi", "inproc")
    assert wait_for_terminal(server, run["runId"])["status"] == "finished"


@pytest.mark.parametrize("body", [
    {"prompt": 123},
    {"prompt": "   "},
    {"prompt": "合成", "parentRunId": "../escape"},
    {"prompt": "合成", "blueprint": 7},
])
def test_field_shape_violations_are_400(server: GlinkHarnessServer, body: dict) -> None:
    """契约 1.1：字段形状违规（类型、空白 prompt、parentRunId 格式）是 400 invalid_request。"""
    before = _run_dirs(server)
    status, _headers, raw = request(server, "/api/runs", method="POST", body=body)
    assert status == 400
    assert json.loads(raw)["error"] == "invalid_request"
    assert _run_dirs(server) == before


# -- 契约 1.3：追问拒绝映射 -------------------------------------------------


def test_followup_unknown_parent_is_404(server: GlinkHarnessServer) -> None:
    """契约 1.3：未知父 run 的追问是 404 unknown_run。"""
    status, _headers, raw = request(
        server, "/api/runs", method="POST",
        body={"prompt": "追问", "parentRunId": "nosuchparent1"},
    )
    assert status == 404
    assert json.loads(raw)["error"] == "unknown_run"


def test_followup_to_a_running_parent_is_409_run_busy(tmp_path: Path) -> None:
    """契约 1.3：父轮未终态的追问是 409 run_busy。"""
    instance = create_server(make_config(tmp_path), client_factory=fake_factory())
    close = serve(instance)
    try:
        parent = start_run(instance, {"prompt": "父轮保持运行 NOSETTLE"})
        status, _headers, raw = request(
            instance, "/api/runs", method="POST",
            body={"prompt": "追问未结束的父轮", "parentRunId": parent["runId"]},
        )
        assert status == 409
        assert json.loads(raw)["error"] == "run_busy"

        status, _headers, raw = request(
            instance, f"/api/runs/{parent['runId']}/stop", method="POST"
        )
        assert status == 200
        assert wait_for_terminal(instance, parent["runId"])["status"] == "cancelled"
    finally:
        close()


def test_followup_after_a_followup_is_409_stale_run(tmp_path: Path) -> None:
    """契约 1.3：已有后续轮次的父 run 再被追问是 409 stale_run。"""
    instance = create_server(
        make_config(tmp_path), client_factory=fake_factory(persist_session=True)
    )
    close = serve(instance)
    try:
        parent = start_run(instance, {"prompt": "第一轮"})
        wait_for_terminal(instance, parent["runId"])
        child = start_run(
            instance, {"prompt": "第二轮", "parentRunId": parent["runId"]}
        )
        assert wait_for_terminal(instance, child["runId"])["status"] == "finished"

        status, _headers, raw = request(
            instance, "/api/runs", method="POST",
            body={"prompt": "不能从旧轮分叉", "parentRunId": parent["runId"]},
        )
        assert status == 409
        assert json.loads(raw)["error"] == "stale_run"
    finally:
        close()


def test_followup_without_a_resumable_session_is_409(server: GlinkHarnessServer) -> None:
    """契约 1.3：父轮没有可续接会话（未持久化）时追问是 409 session_unavailable。"""
    parent = start_run(server, {"prompt": "无会话文件的父轮"})
    wait_for_terminal(server, parent["runId"])
    status, _headers, raw = request(
        server, "/api/runs", method="POST",
        body={"prompt": "追问", "parentRunId": parent["runId"]},
    )
    assert status == 409
    assert json.loads(raw)["error"] == "session_unavailable"


def test_followup_with_unreadable_seed_report_fails_closed(tmp_path: Path) -> None:
    """契约 1.3：追问的报告种子无法从父 run 安全读取时，run 受理后以 run_failed 收口。

    种子复制在 Prepare 阶段（受理后）执行，因此这不是受理同步拒绝：
    追问被受理、run 失败、error 载明 report_unavailable——fail-closed，
    不产生「已续接」的假象。
    """
    instance = create_server(
        make_config(tmp_path), client_factory=fake_factory(persist_session=True)
    )
    close = serve(instance)
    try:
        parent = start_run(instance, {"prompt": "第一轮产出报告"})
        assert wait_for_terminal(instance, parent["runId"])["hasReport"] is True

        # 合成故障：父轮已发布报告在追问受理前从 artifacts 消失。
        artifact = (
            tmp_path / "data" / "runs" / parent["runId"] / "artifacts" / "report.md"
        )
        assert artifact.is_file()
        artifact.unlink()

        child = start_run(
            instance, {"prompt": "追问", "parentRunId": parent["runId"]}
        )
        latest = wait_for_terminal(instance, child["runId"])
        assert latest["status"] == "failed"
        assert "report_unavailable" in (latest.get("error") or "")
        frames = read_stream(instance, child["runId"])
        assert frames[-1]["event"] == "run_failed"
    finally:
        close()


# -- 契约 1.4：幂等键 ------------------------------------------------------


def test_the_same_idempotency_key_replays_the_admitted_run(
    server: GlinkHarnessServer,
) -> None:
    """契约 1.4：同键同请求重放原受理（201 同 runId），不二次执行。

    重放响应是该 run 当前状态的视图，不是首次响应的快照：终态后的重放
    直接看到终态视图（status/eventCount 都已前进）。
    """
    body = {"prompt": "幂等创建的第一回"}
    status, _headers, raw = request(
        server, "/api/runs", method="POST", body=body, headers={"Idempotency-Key": "order-1"}
    )
    assert status == 201
    first = json.loads(raw)["run"]
    assert first["status"] == "running", "首次响应是运行中视图"
    assert wait_for_terminal(server, first["runId"])["status"] == "finished"
    after_first = _run_dirs(server)

    status, _headers, raw = request(
        server, "/api/runs", method="POST", body=body, headers={"Idempotency-Key": "order-1"}
    )
    assert status == 201
    replayed = json.loads(raw)["run"]
    assert replayed["runId"] == first["runId"]
    assert replayed["status"] == "finished", "重放反映重放时刻的状态，不是首次快照"
    assert replayed["eventCount"] > 0
    assert _run_dirs(server) == after_first, "重放不得创建第二个 run 目录"


def test_the_same_key_with_a_different_request_is_a_conflict(
    server: GlinkHarnessServer,
) -> None:
    """契约 1.4：同键异请求 409 idempotency_key_conflict，体不泄漏原 run 事实。"""
    status, _headers, raw = request(
        server, "/api/runs", method="POST",
        body={"prompt": "原始请求"}, headers={"Idempotency-Key": "order-2"},
    )
    assert status == 201
    run_id = json.loads(raw)["run"]["runId"]

    status, _headers, raw = request(
        server, "/api/runs", method="POST",
        body={"prompt": "另一个请求"}, headers={"Idempotency-Key": "order-2"},
    )
    assert status == 409
    text = raw.decode("utf-8")
    payload = json.loads(text)
    assert payload["error"] == "idempotency_key_conflict"
    assert run_id not in text and "原始请求" not in text, "冲突体不得携带占用者事实"

    assert wait_for_terminal(server, run_id)["status"] == "finished"


def test_a_replay_while_running_wins_over_run_busy(server: GlinkHarnessServer) -> None:
    """契约 1.4：运行中的同键重试是重放（201 同 run），不是 409 run_busy。"""
    body = {"prompt": "这一轮不要结束 NOSETTLE"}
    status, _headers, raw = request(
        server, "/api/runs", method="POST", body=body, headers={"Idempotency-Key": "busy-1"}
    )
    assert status == 201
    run_id = json.loads(raw)["run"]["runId"]

    status, _headers, raw = request(
        server, "/api/runs", method="POST", body=body, headers={"Idempotency-Key": "busy-1"}
    )
    assert status == 201
    assert json.loads(raw)["run"]["runId"] == run_id

    # 收尾：停掉挂起的 run，避免把执行线程留给后续用例。
    status, _headers, raw = request(server, f"/api/runs/{run_id}/stop", method="POST")
    assert status == 200
    assert wait_for_terminal(server, run_id)["status"] == "cancelled"


@pytest.mark.parametrize("key", ["", "a" * 129, "bad key", "order/1"])
def test_malformed_idempotency_keys_are_refused(
    server: GlinkHarnessServer, key: str
) -> None:
    """契约 1.4：空/畸形 Idempotency-Key 是 400 invalid_request，不静默忽略。"""
    before = _run_dirs(server)
    status, _headers, raw = request(
        server, "/api/runs", method="POST",
        body={"prompt": "合成题面"}, headers={"Idempotency-Key": key},
    )
    assert status == 400
    assert json.loads(raw)["error"] == "invalid_request"
    assert _run_dirs(server) == before


def test_a_replay_survives_a_host_restart(tmp_path: Path) -> None:
    """契约 1.4：键是持久事实——重启后同键同请求仍重放原 run。"""
    body = {"prompt": "重启前受理"}
    instance = create_server(make_config(tmp_path), client_factory=fake_factory())
    close = serve(instance)
    try:
        status, _headers, raw = request(
            instance, "/api/runs", method="POST", body=body,
            headers={"Idempotency-Key": "restart-1"},
        )
        assert status == 201
        first = json.loads(raw)["run"]
        assert wait_for_terminal(instance, first["runId"])["status"] == "finished"
    finally:
        close()

    relaunched = create_server(make_config(tmp_path), client_factory=fake_factory())
    close = serve(relaunched)
    try:
        status, _headers, raw = request(
            relaunched, "/api/runs", method="POST", body=body,
            headers={"Idempotency-Key": "restart-1"},
        )
        assert status == 201
        assert json.loads(raw)["run"]["runId"] == first["runId"]
    finally:
        close()


def test_concurrent_twin_requests_with_one_key_both_replay_the_winner(
    server: GlinkHarnessServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约 1.4：prepare 窗口内并发的同键孪生请求，败者也重放，不是 run_busy/500。

    prepare（蓝图解析、凭据、文件 IO）在准入锁外运行：两个同键请求都
    lookup 未命中后会合在 prepare 之后（Barrier 注入），再竞速 admit。
    本地模式下败者的 admit 抛 RunBusy——必须被收口成对胜者的重放。
    """
    barrier = threading.Barrier(2)
    original_prepare = server.manager._admission.prepare

    def rendezvous_prepare(*args, **kwargs):
        result = original_prepare(*args, **kwargs)
        barrier.wait(timeout=15)
        return result

    monkeypatch.setattr(server.manager._admission, "prepare", rendezvous_prepare)

    body = {"prompt": "并发孪生请求"}
    outcomes: list[tuple[int, str]] = []
    failures: list[BaseException] = []

    def post() -> None:
        try:
            status, _headers, raw = request(
                server, "/api/runs", method="POST", body=body,
                headers={"Idempotency-Key": "race-1"},
            )
            outcomes.append((status, json.loads(raw)["run"]["runId"]))
        except BaseException as exc:  # noqa: BLE001 - 线程内失败也要让用例变红
            failures.append(exc)

    threads = [threading.Thread(target=post) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert failures == []
    assert len(outcomes) == 2
    assert [status for status, _run in outcomes] == [201, 201], (
        "竞态败者必须拿到重放，而不是 409 run_busy 或 500"
    )
    winner = outcomes[0][1]
    assert outcomes[1][1] == winner
    run_dirs = [name for name in _run_dirs(server) if not name.startswith("runs.db")]
    assert run_dirs == [winner], "孪生请求只能落一个 run 目录"
    assert wait_for_terminal(server, winner)["status"] == "finished"


def test_the_command_form_and_the_field_form_are_distinct_requests(
    server: GlinkHarnessServer,
) -> None:
    """契约 1.4：`/蓝图名 题目` 命令形式与 blueprint+prompt 字段形式是不同请求。

    两种形式受理后语义等价（§1.1 的确定性重写），但 digest 盖重写前的
    原始字段——同键互换得 409 冲突（宁拒不双执行），不是重放。
    """
    status, _headers, raw = request(
        server, "/api/runs", method="POST",
        body={"prompt": "/deepresearch 合成题目"}, headers={"Idempotency-Key": "form-1"},
    )
    assert status == 201
    run_id = json.loads(raw)["run"]["runId"]

    status, _headers, raw = request(
        server, "/api/runs", method="POST",
        body={"prompt": "合成题目", "blueprint": "deepresearch"},
        headers={"Idempotency-Key": "form-1"},
    )
    assert status == 409
    assert json.loads(raw)["error"] == "idempotency_key_conflict"

    assert wait_for_terminal(server, run_id)["status"] == "finished"


# -- 契约 2：事件通道 -------------------------------------------------------


def test_after_resumes_precisely_and_last_event_id_wins(server: GlinkHarnessServer) -> None:
    """契约 2.2：?after 精确续传；Last-Event-ID 优先于 ?after；非法游标退回全量重放。"""
    run = start_run(server, {"prompt": "续传契约"})
    wait_for_terminal(server, run["runId"])
    frames = read_stream(server, run["runId"])
    seqs = [frame["id"] for frame in frames]
    assert seqs == sorted(seqs) and len(seqs) > 3

    watermark = seqs[1]
    tail = read_stream(server, run["runId"], query=f"?after={watermark}")
    assert [frame["id"] for frame in tail] == [seq for seq in seqs if seq > watermark]

    # 头优先于查询参数：只有 ?after=<最后 seq> 会是空尾，Last-Event-ID 把它顶回水位。
    chosen = read_stream(
        server, run["runId"], query=f"?after={seqs[-1]}",
        headers={"Last-Event-ID": str(watermark)},
    )
    assert [frame["id"] for frame in chosen] == [seq for seq in seqs if seq > watermark]

    full = read_stream(server, run["runId"], query="?after=not-a-seq")
    assert [frame["id"] for frame in full] == seqs


def test_terminal_frame_set_and_finality(server: GlinkHarnessServer) -> None:
    """契约 2.5：终帧集合恰为 run_finished/run_failed/run_cancelled；终帧唯一且收尾。"""
    assert events.TERMINAL_EVENT_TYPES == {"run_finished", "run_failed", "run_cancelled"}
    assert set(event_schema.TERMINAL_TYPES) == events.TERMINAL_EVENT_TYPES

    run = start_run(server, {"prompt": "终帧契约"})
    wait_for_terminal(server, run["runId"])
    frames = read_stream(server, run["runId"])
    terminal = [f for f in frames if f["event"] in events.TERMINAL_EVENT_TYPES]
    assert len(terminal) == 1 and terminal[0] is frames[-1]
    assert frames[-1]["event"] == "run_finished"
    assert frames[-1]["data"]["payload"]["status"] == "finished"


def test_failed_run_ends_with_run_failed(server: GlinkHarnessServer) -> None:
    """契约 2.5：模型错误重试后仍失败的 run 以 run_failed 收尾。"""
    run = start_run(server, {"prompt": "MODELERROR 合成失败"})
    assert wait_for_terminal(server, run["runId"])["status"] == "failed"
    frames = read_stream(server, run["runId"])
    assert frames[-1]["event"] == "run_failed"
    assert frames[-1]["data"]["payload"]["status"] == "failed"


def test_cancelled_run_ends_with_run_cancelled(server: GlinkHarnessServer) -> None:
    """契约 2.5/4：受理的取消必然以 run_cancelled 终帧收口（受理必 cancelled）。"""
    run = start_run(server, {"prompt": "请保持运行 NOSETTLE"})
    status, _headers, raw = request(
        server, f"/api/runs/{run['runId']}/stop", method="POST"
    )
    assert status == 200
    assert json.loads(raw)["requested"] is True
    assert wait_for_terminal(server, run["runId"])["status"] == "cancelled"
    frames = read_stream(server, run["runId"])
    assert frames[-1]["event"] == "run_cancelled"
    assert frames[-1]["data"]["payload"]["status"] == "cancelled"


def test_stream_limit_429_carries_a_readable_json_body(tmp_path: Path) -> None:
    """契约 2.3：429 stream_limit 在流响应头之前，JSON 体可读，附 Retry-After。"""
    instance = create_server(
        make_config(tmp_path, GLINK_MAX_GLOBAL_STREAMS="1"),
        client_factory=fake_factory(),
    )
    close = serve(instance)
    try:
        run = start_run(instance, {"prompt": "占住流额度 NOSETTLE"})
        held = HTTPConnection("127.0.0.1", instance.server_address[1], timeout=10)
        try:
            held.request("GET", f"/api/runs/{run['runId']}/events")
            response = held.getresponse()
            assert response.status == 200
            assert response.read1(256)

            status, headers, raw = request(
                instance, f"/api/runs/{run['runId']}/events"
            )
            assert status == 429
            assert headers.get("Content-Type", "").startswith("application/json")
            assert headers.get("Retry-After") == "5"
            payload = json.loads(raw)
            assert payload == {
                "error": "stream_limit",
                "scope": "global",
                "limit": 1,
                "message": "stream connection limit reached; retry later",
            }
            assert run["runId"] not in raw.decode("utf-8")
        finally:
            held.close()
        request(instance, f"/api/runs/{run['runId']}/stop", method="POST")
        wait_for_terminal(instance, run["runId"])
    finally:
        close()


def test_stop_reason_length_is_finished_with_incomplete_delivery(
    server: GlinkHarnessServer,
) -> None:
    """契约 2.5：stopReason=length 时 run 仍 finished，但明确标记交付不完整。"""
    run = start_run(server, {"prompt": "MODELLENGTH 输出被长度截断"})
    final = wait_for_terminal(server, run["runId"])
    assert final["status"] == "finished"
    assert final["stopReason"] == "length"
    frames = read_stream(server, run["runId"])
    assert frames[-1]["event"] == "run_finished"
    assert frames[-1]["data"]["payload"]["stopReason"] == "length"


# -- 契约 3：报告与产物端点 -------------------------------------------------


def test_report_endpoint_and_has_report(server: GlinkHarnessServer) -> None:
    """契约 3.1/3.3：report 200 + text/markdown + X-Redacted；hasReport 与端点同口径。"""
    run = start_run(server, {"prompt": "报告契约"})
    final = wait_for_terminal(server, run["runId"])
    assert final["hasReport"] is True

    status, headers, body = request(server, f"/api/runs/{run['runId']}/report")
    assert status == 200
    assert headers["Content-Type"].startswith("text/markdown")
    assert headers.get("X-Redacted") in ("true", "false")
    assert "fake pi" in body.decode("utf-8")


def test_missing_report_is_404_no_report(tmp_path: Path) -> None:
    """契约 3.1/3.3：无报告的 run 是 hasReport=false 与 404 no_report。"""
    instance = create_server(
        make_config(tmp_path), client_factory=fake_factory(write_report=False)
    )
    close = serve(instance)
    try:
        run = start_run(instance, {"prompt": "无报告"})
        assert wait_for_terminal(instance, run["runId"])["hasReport"] is False
        status, _headers, raw = request(instance, f"/api/runs/{run['runId']}/report")
        assert status == 404
        assert json.loads(raw)["error"] == "no_report"
    finally:
        close()


def test_run_view_deliverables_and_download(tmp_path: Path) -> None:
    """契约 3.2/3.3：run 视图 deliverables 清单带 available；下载只服务 status=ok 的副本。"""
    instance = create_server(make_config(tmp_path), client_factory=fake_factory())
    close = serve(instance)
    try:
        run_id = "contractdlv1"
        instance.store.create(
            "产物契约",
            run_id=run_id,
            workspace=instance.config.workspace_root / run_id,
            spec=RunSpec(family="pi", pack="pi", executor="inproc"),
        )
        artifacts = instance.store.run_dir(run_id) / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "notes.md").write_text("# 交付物\n\n合成内容。\n", encoding="utf-8")
        finish(instance.store, run_id, "finished", payload={"deliverables": [
            {"path": "notes.md", "status": "ok"},
            {"path": "missing.md", "status": "rejected", "reason": "文件读写失败"},
        ]})

        status, _headers, raw = request(instance, f"/api/runs/{run_id}")
        assert status == 200
        deliverables = json.loads(raw)["run"]["deliverables"]
        assert [item["path"] for item in deliverables] == ["notes.md", "missing.md"]
        assert deliverables[0]["available"] is True
        assert deliverables[1]["available"] is False

        status, headers, body = request(instance, f"/api/runs/{run_id}/deliverables/0")
        assert status == 200
        assert "合成内容" in body.decode("utf-8")
        assert headers.get("X-Redacted") in ("true", "false")

        status, _headers, raw = request(instance, f"/api/runs/{run_id}/deliverables/1")
        assert status == 404
        assert json.loads(raw)["error"] == "no_artifact"
        # 契约边界：index 只有 0-7，越界不成路由（404）。
        assert request(instance, f"/api/runs/{run_id}/deliverables/8")[0] == 404
    finally:
        close()


def test_followup_seed_report_does_not_masquerade_as_new_report(tmp_path: Path) -> None:
    """契约 3.3：追问轮报告与父轮种子 digest 相同 → 本轮 hasReport=false，report 404。"""
    launches = 0

    def factory(argv, *, cwd=None, env=None, on_event=None):
        nonlocal launches
        launches += 1
        return PiRpcClient(
            [sys.executable, str(FAKE_PI), *argv[1:]],
            cwd=cwd,
            env={
                **(env or {}),
                "FAKE_PI_PERSIST_SESSION": "1",
                # 只有第一轮产出报告；追问轮不产出，工作区里只剩复制进来的种子。
                "FAKE_PI_WRITE_REPORT": "1" if launches == 1 else "0",
            },
            on_event=on_event,
        )

    instance = create_server(make_config(tmp_path), client_factory=factory)
    close = serve(instance)
    try:
        parent = start_run(instance, {"prompt": "第一轮产出报告"})
        assert wait_for_terminal(instance, parent["runId"])["hasReport"] is True

        child = start_run(
            instance, {"prompt": "追问不产出新报告", "parentRunId": parent["runId"]}
        )
        assert wait_for_terminal(instance, child["runId"])["status"] == "finished"
        # 种子只是修订上下文，不是本轮产物：标志与端点同口径地拒绝。
        detail = json.loads(request(instance, f"/api/runs/{child['runId']}")[2])["run"]
        assert detail["hasReport"] is False
        status, _headers, raw = request(instance, f"/api/runs/{child['runId']}/report")
        assert status == 404
        assert json.loads(raw)["error"] == "no_report"
        # 父轮报告不受追问影响，消费方沿父链回溯即可拿到最近一份报告。
        assert request(instance, f"/api/runs/{parent['runId']}/report")[0] == 200
    finally:
        close()


# -- 契约 4：取消 ------------------------------------------------------------


def test_stop_mapping_terminal_and_unknown(server: GlinkHarnessServer) -> None:
    """契约 4：已提交终态 → 409 run_not_cancellable{status, reason=terminal}；未知 → 404。"""
    run = start_run(server, {"prompt": "已结束的 run"})
    assert wait_for_terminal(server, run["runId"])["status"] == "finished"

    status, _headers, raw = request(server, f"/api/runs/{run['runId']}/stop", method="POST")
    assert status == 409
    assert json.loads(raw) == {
        "error": "run_not_cancellable",
        "requested": False,
        "status": "finished",
        "reason": "terminal",
    }
    status, _headers, raw = request(server, "/api/runs/does-not-exist/stop", method="POST")
    assert status == 404
    assert json.loads(raw)["error"] == "unknown_run"


def test_stop_while_finalizing_is_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约 4：终帧尚未提交成功（finalizing）时取消是 409 reason=finalizing。"""
    instance = create_server(make_config(tmp_path), client_factory=fake_factory())
    close = serve(instance)
    original_commit = instance.store.commit_terminal

    def failing_terminal(run_id: str, event) -> None:
        raise OSError("simulated append failure on the terminal frame")

    monkeypatch.setattr(instance.store, "commit_terminal", failing_terminal)
    try:
        run = start_run(instance, {"prompt": "终帧写盘失败"})
        execution = instance.manager._executions[run["runId"]]  # noqa: SLF001 - 等收尾落地
        assert execution.finalized.wait(30), "the finalizer never finished"

        status, _headers, raw = request(
            instance, f"/api/runs/{run['runId']}/stop", method="POST"
        )
        assert status == 409
        payload = json.loads(raw)
        assert payload["error"] == "run_not_cancellable"
        assert payload["requested"] is False
        assert payload["reason"] == "finalizing"
        # 未提交的 run 在任何读面都不显示终态。
        assert json.loads(request(instance, f"/api/runs/{run['runId']}")[2])["run"]["status"] == "running"
    finally:
        monkeypatch.setattr(instance.store, "commit_terminal", original_commit)
        close()
