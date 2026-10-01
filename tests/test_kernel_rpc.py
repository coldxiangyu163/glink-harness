"""Regressions for the pi headless RPC client.

These run against ``tests/fixtures/fake_pi.py`` — a protocol stand-in, not a
mock object — so framing, correlation, error frames and process death are
exercised over real pipes.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import pytest

from glink_harness.kernel import PiRpcClient, PiRpcTimeout, project_event

FAKE_PI = Path(__file__).resolve().parent / "fixtures" / "fake_pi.py"
FRAMING_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "lf-framing.json"


@pytest.fixture()
def session():
    frames: list[Mapping[str, Any]] = []
    client = PiRpcClient([sys.executable, str(FAKE_PI)], on_event=frames.append)
    client.start()
    try:
        yield client, frames
    finally:
        client.close()


def _wait_for(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# -- protocol -----------------------------------------------------------------


def test_state_round_trip_is_correlated_by_id(session) -> None:
    client, _frames = session
    response = client.request({"type": "get_state"})
    assert response.success is True
    assert response.command == "get_state"
    assert response.data["model"]["id"] == "fake-model"
    assert response.id == "req-1"
    # The next request must not reuse the id.
    second = client.request({"type": "get_state"})
    assert second.id == "req-2"


def test_unknown_command_is_an_error_response_not_a_crash(session) -> None:
    client, _frames = session
    response = client.request({"type": "get_available_commands"})
    assert response.success is False
    assert response.error is not None and "Unknown command" in response.error


def test_silent_command_times_out(session) -> None:
    client, _frames = session
    with pytest.raises(PiRpcTimeout):
        client.request({"type": "silent"}, timeout=0.4)


def test_process_exit_surfaces_as_failed_response(session) -> None:
    client, _frames = session
    response = client.request({"type": "exit"}, timeout=3.0)
    assert response.success is False
    assert response.error is not None and "closed" in response.error.lower()


def test_prompt_streams_a_full_turn(session) -> None:
    client, frames = session
    accepted = client.request({"type": "prompt", "message": "看一下工作区"})
    assert accepted.success is True
    assert _wait_for(lambda: any(f.get("type") == "agent_settled" for f in frames))

    projected = [event for frame in frames for event in project_event(frame)]
    kinds = [kind for kind, _payload in projected]

    # The stream must carry the pi semantics the front-end renders.
    assert "thinking_delta" in kinds
    assert "assistant_delta" in kinds
    assert kinds.count("tool_started") == 2
    assert kinds.count("tool_finished") == 2
    assert "turn_started" in kinds
    assert "turn_finished" in kinds
    assert "assistant_message" in kinds

    thinking = next(payload for kind, payload in projected if kind == "thinking_delta")
    assert thinking["text"] == "先看目录结构"

    failed_tool = [
        payload for kind, payload in projected if kind == "tool_finished" and payload["isError"]
    ]
    assert len(failed_tool) == 1
    assert failed_tool[0]["toolName"] == "read"
    assert "ENOENT" in failed_tool[0]["result"]


def test_frames_split_on_lf_only_and_keep_u2028(session) -> None:
    client, frames = session
    client.request({"type": "proto_frames"})
    assert _wait_for(lambda: len(frames) >= 3)
    deltas = [
        frame["assistantMessageEvent"]["delta"]
        for frame in frames
        if frame.get("type") == "message_update"
    ]
    assert deltas == ["line\u2028separator"]
    assert [frame["type"] for frame in frames] == ["turn_start", "message_update", "turn_end"]


def test_lf_framing_corpus_is_shared_with_the_e2b_face(tmp_path: Path) -> None:
    """Both faces frame over the same legal corpus (U+2028/U+2029 are content).

    ``tests/fixtures/lf-framing.json`` is rendered to a byte stream here and to
    a decoded sandbox file in the E2B regression; both must yield the identical
    frame list.
    """
    corpus = json.loads(FRAMING_FIXTURE.read_text(encoding="utf-8"))
    wire = "".join(
        json.dumps(frame, ensure_ascii=False) + ("\r\n" if index % 2 else "\n")
        for index, frame in enumerate(corpus["frames"])
    )
    script = tmp_path / "emitter.py"
    script.write_text(
        "import sys\nsys.stdout.buffer.write(" + repr(wire.encode("utf-8")) + ")\n",
        encoding="utf-8",
    )
    frames: list[Mapping[str, Any]] = []
    client = PiRpcClient([sys.executable, str(script)], on_event=frames.append)
    client.start()
    try:
        assert _wait_for(lambda: len(frames) == len(corpus["frames"]))
        time.sleep(0.1)
    finally:
        client.close()
    assert frames == corpus["frames"]


def test_client_reports_pid_and_running_state(session) -> None:
    client, _frames = session
    assert client.pid is not None and client.pid > 0
    assert client.running is True


# -- projection ---------------------------------------------------------------


def test_unknown_frame_types_project_to_nothing() -> None:
    assert project_event({"type": "some_future_event", "payload": 1}) == []
    assert project_event({}) == []


def test_tool_output_is_clipped_with_an_explicit_flag() -> None:
    big = "x" * 10_000
    (kind, payload), = project_event(
        {
            "type": "tool_execution_end",
            "toolCallId": "c1",
            "toolName": "bash",
            "result": {"content": [{"type": "text", "text": big}]},
            "isError": False,
        }
    )
    assert kind == "tool_finished"
    assert payload["result"].startswith("x" * 100)
    assert "truncated" in payload["result"]
    assert len(payload["result"]) < len(big)


def test_tool_args_are_bounded_and_json_safe() -> None:
    (kind, payload), = project_event(
        {
            "type": "tool_execution_start",
            "toolCallId": "c1",
            "toolName": "write",
            "args": {"path": "report.md", "content": "y" * 5000, "count": 3},
        }
    )
    assert kind == "tool_started"
    assert payload["args"]["count"] == 3
    assert payload["args"]["path"] == "report.md"
    assert "truncated" in payload["args"]["content"]


def test_tool_args_keep_arrays_for_multi_query_tools() -> None:
    """A search tool's ``queries``/``urls`` arrays must survive projection.

    The front-end's ``collectSources`` consumes arrays; flattening them to one
    string here silently collapsed multi-query runs to a single entry.
    """
    (kind, payload), = project_event(
        {
            "type": "tool_execution_start",
            "toolCallId": "c1",
            "toolName": "web_search",
            "args": {
                "queries": ["第一个查询", "第二个查询"],
                "urls": ["https://a.example/x", "https://b.example/y"],
                "count": 2,
                "filters": {"site": "example.com"},
            },
        }
    )
    assert kind == "tool_started"
    assert payload["args"]["queries"] == ["第一个查询", "第二个查询"]
    assert payload["args"]["urls"] == ["https://a.example/x", "https://b.example/y"]
    assert payload["args"]["count"] == 2
    assert isinstance(payload["args"]["filters"], str)

    (kind, payload), = project_event(
        {
            "type": "tool_execution_start",
            "toolCallId": "c2",
            "toolName": "web_search",
            "args": {"queries": ["q" * 800 for _ in range(50)]},
        }
    )
    queries = payload["args"]["queries"]
    assert len(queries) == 20, "the array must stay bounded"
    assert all(len(item) < 800 and "truncated" in item for item in queries)


def test_subagent_tool_card_projects_bounded_args_and_redacts_output() -> None:
    """GLINK-SUB-02：子代理派发在主 run 事件流里就是一张普通工具卡片。

    投影层不为 ``subagent`` 做特例：它走同一套事件映射（tool_started/updated/
    finished）、同一套参数有界投影（数组保持有界数组，不压成单串）与同一个脱敏边界。
    这里钉住的是「至少作为工具卡片出现」：卡片本身、参数数组形态，以及子代理回显的
    凭据不能从工具卡片泄露。

    原义务（GLINK-SUB-02）：数组里的对象元素经 ``extract_text`` 退化，`tasks` 在卡片上
    是空白字符串。新义务（2026-09-24 真机 A/B/C 对比发现）：对象元素按**紧凑 JSON 文本**
    投影，仍是「有界字符串数组」契约下的一项，前端据此显示派了哪些 agent、各自做什么。
    """
    from glink_harness.redaction import SecretRedactor

    secret = "sk-subagent0123456789abcdef"
    redactor = SecretRedactor([secret])
    (kind, payload), = project_event(
        {
            "type": "tool_execution_start",
            "toolCallId": "sub-1",
            "toolName": "subagent",
            "args": {
                "tasks": [
                    {"agent": "researcher", "task": "法域 A 取证"},
                    {"agent": "researcher", "task": "法域 B 取证"},
                ],
                "async": False,
                "concurrency": 2,
            },
        },
        redactor,
    )
    assert kind == "tool_started"
    assert payload["toolName"] == "subagent"
    assert payload["args"]["concurrency"] == 2
    assert payload["args"]["async"] is False
    assert isinstance(payload["args"]["tasks"], list), "数组形态保留给前端"
    assert len(payload["args"]["tasks"]) == 2, "两个并行子任务不被压成一项"
    assert all(isinstance(item, str) for item in payload["args"]["tasks"])
    # 对象元素不再退化成空串：卡片要能显示 agent 与任务（前端会解析这段 JSON）。
    first = json.loads(payload["args"]["tasks"][0])
    assert first == {"agent": "researcher", "task": "法域 A 取证"}
    assert payload["args"]["tasks"][1] == '{"agent":"researcher","task":"法域 B 取证"}'

    (kind, payload), = project_event(
        {
            "type": "tool_execution_end",
            "toolCallId": "sub-1",
            "toolName": "subagent",
            "result": {"content": [{"type": "text", "text": f"researcher 越权读到了 {secret}"}]},
            "isError": False,
        },
        redactor,
    )
    assert kind == "tool_finished"
    rendered = json.dumps(payload, ensure_ascii=False)
    assert secret not in rendered, "子代理回显同样走出站脱敏"
    assert "[REDACTED]" in rendered


def test_object_array_items_stay_bounded_json_text() -> None:
    """对象数组元素既要说得出内容，也不能破坏「有界字符串数组」的共享契约。"""
    (kind, payload), = project_event(
        {
            "type": "tool_execution_start",
            "toolCallId": "c9",
            "toolName": "subagent",
            "args": {
                "tasks": [
                    {"agent": "researcher", "task": "长" * 900},
                    "原样字符串",
                ],
                "filters": {"site": "example.com"},
            },
        }
    )
    assert kind == "tool_started"
    tasks = payload["args"]["tasks"]
    assert len(tasks) == 2 and all(isinstance(item, str) for item in tasks)
    assert tasks[1] == "原样字符串"
    assert len(tasks[0]) < 900 and "truncated" in tasks[0]
    # 超长对象被截断后不再是合法 JSON：前端按「解析失败就显示原文」处理。
    assert tasks[0].startswith('{"agent":"researcher"'), "截断只发生在尾部，前缀仍是可读 JSON"
    assert json.loads(payload["args"]["filters"]) == {"site": "example.com"}, "单对象参数同样可读"


def test_runner_only_frames_do_not_leak_to_the_product_stream() -> None:
    for frame_type in ("agent_settled", "message_start", "queue_update"):
        assert project_event({"type": frame_type}) == []


# -- cross-layer payload contract ---------------------------------------------


def test_projection_matches_the_shared_payload_corpus() -> None:
    """The Python half of ``tests/fixtures/tool-args-projection.json``.

    Node asserts the same corpus from the consumer side (``collectSources``);
    pinning it here means the projection cannot change the shape without
    breaking the front-end contract in both runtimes at once.
    """
    corpus = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "tool-args-projection.json")
        .read_text(encoding="utf-8")
    )
    produced: list[tuple[str, Mapping[str, Any]]] = []
    for frame in corpus["frames"]:
        produced.extend(project_event(frame))
    assert [(kind, dict(payload)) for kind, payload in produced] == [
        (event["type"], event["payload"]) for event in corpus["projected"]
    ]
    # The multi-value semantics the front-end depends on are explicit here.
    search_args = corpus["projected"][0]["payload"]["args"]
    assert search_args["queries"] == corpus["expected_sources"]["queries"]
    fetch_args = corpus["projected"][1]["payload"]["args"]
    assert fetch_args["urls"] == [page["href"] for page in corpus["expected_sources"]["pages"]]


# -- the authoritative answer frame may be clipped, and must say so ------------
#
# GLINK-INTEG-01: the streamed deltas carry the whole answer to the browser while
# the terminal ``assistant_message`` keeps only ``ASSISTANT_TEXT_LIMIT`` of it
# (one frame must not blow up the event log). A client that already holds the
# longer streamed body needs to know the authoritative copy is incomplete
# instead of silently replacing more with less.


def test_a_clipped_authoritative_message_is_marked_truncated() -> None:
    text = "答" * 20000
    produced = project_event(
        {"type": "message_end", "message": {"role": "assistant", "content": text, "stopReason": "stop"}}
    )
    ((kind, payload),) = produced
    assert kind == "assistant_message"
    assert payload["truncated"] is True
    assert payload["text"].startswith("答" * 16000)
    assert "…[truncated 4000 chars]" in payload["text"]
    assert payload["stopReason"] == "stop"


def test_a_short_authoritative_message_carries_no_truncated_flag() -> None:
    produced = project_event(
        {"type": "message_end", "message": {"role": "assistant", "content": "简短回答"}}
    )
    ((_kind, payload),) = produced
    assert payload["text"] == "简短回答"
    assert "truncated" not in payload


# -- framing bound ------------------------------------------------------------

_FLOOD = (
    "import sys, time\n"
    "sys.stdout.buffer.write(b'{\"type\":\"response\",\"id\":\"a\",\"command\":\"x\",\"success\":true}\\n')\n"
    "sys.stdout.buffer.flush()\n"
    "chunk = b'x' * 65536\n"
    "while True:\n"
    "    sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush()\n"
)


@pytest.mark.parametrize("client_kind", ["pi", "acp"])
def test_an_unterminated_flood_is_bounded_and_kills_the_agent(
    client_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stream that never sends LF must not grow host memory without bound.

    原义务：读取循环对未结束的尾巴无上限（``buffer += chunk``）。
    新义务：未结束记录超过 ``MAX_RECORD_BYTES`` 即中止读取、杀掉进程并记录
    ``protocol_error``；完整的合法帧照常处理。
    """
    from glink_harness.kernel import acp_rpc, framing, rpc

    monkeypatch.setattr(framing, "MAX_RECORD_BYTES", 256 * 1024)
    frames: list = []
    argv = [sys.executable, "-c", _FLOOD]
    if client_kind == "pi":
        client: Any = rpc.PiRpcClient(argv, on_event=frames.append)
    else:
        client = acp_rpc.AcpRpcClient(argv)
    client.start()
    try:
        assert _wait_for(lambda: client.protocol_error is not None, timeout=10)
        assert "LF" in client.protocol_error
        assert _wait_for(lambda: not client.running, timeout=10)
    finally:
        client.close(timeout=2)


def test_complete_records_never_trip_the_bound() -> None:
    from glink_harness.kernel.framing import check_pending_record, split_lf_records

    records, tail = split_lf_records(b"a" * 100 + b"\n" + b"b" * 10)
    check_pending_record(tail, limit=10)  # the complete 100-byte record left the tail
    assert records == [b"a" * 100]
