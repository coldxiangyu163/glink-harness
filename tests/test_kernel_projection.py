"""kernel/projection.py 的直接单元测试：pi 帧 → 产品事件的投影契约。

既有覆盖走的是间接路径：tests/test_kernel_rpc.py 经真实管道（PiRpcClient +
fake_pi）驱动同一投影，tests/test_runner.py 经 RunManager 的事件回路。本文件不
经过进程、总线或 HTTP，直接构造被测对象（``project_event`` / ``project_frames``
/ ``Projector`` / ``project_args`` / ``extract_text``）钉住
docs/architecture.md §3 的事件契约。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from pi_glink import events
from pi_glink.kernel.projection import (
    ARG_ITEM_LIMIT,
    ARG_LIST_LIMIT,
    ASSISTANT_TEXT_LIMIT,
    DELTA_LIMIT,
    SUBAGENT_AGENT_LIMIT,
    SUBAGENT_TASK_LIMIT,
    SUBAGENT_TOOL_NAME,
    TOOL_TEXT_LIMIT,
    Projector,
    extract_text,
    parse_subagent_tasks,
    project_args,
    project_event,
    project_frames,
)
from pi_glink.redaction import REPLACEMENT, SecretRedactor


def _kinds(produced: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [kind for kind, _payload in produced]


def _text_delta(text: str) -> dict[str, Any]:
    return {
        "type": "message_update",
        "assistantMessageEvent": {"type": "text_delta", "delta": text},
    }


def _thinking_delta(text: str) -> dict[str, Any]:
    return {
        "type": "message_update",
        "assistantMessageEvent": {"type": "thinking_delta", "delta": text},
    }


# -- 事件类型映射表（docs/architecture.md §3 表格） ------------------------------


def test_every_pi_frame_type_maps_to_the_documented_product_events() -> None:
    cases: list[tuple[dict[str, Any], list[str]]] = [
        ({"type": "agent_start"}, [events.RUN_STAGE]),
        ({"type": "agent_end", "willRetry": True}, [events.RUN_STAGE]),
        ({"type": "turn_start"}, [events.TURN_STARTED]),
        ({"type": "turn_end"}, [events.TURN_FINISHED]),
        (_text_delta("正文"), [events.ASSISTANT_DELTA]),
        (_thinking_delta("思考"), [events.THINKING_DELTA]),
        (
            {"type": "message_end", "message": {"role": "assistant", "content": "回答"}},
            [events.ASSISTANT_MESSAGE],
        ),
        (
            {
                "type": "tool_execution_start",
                "toolCallId": "c1",
                "toolName": "bash",
                "args": {"command": "ls"},
            },
            [events.TOOL_STARTED],
        ),
        (
            {
                "type": "tool_execution_update",
                "toolCallId": "c1",
                "toolName": "bash",
                "partialResult": "out",
            },
            [events.TOOL_UPDATED],
        ),
        (
            {
                "type": "tool_execution_end",
                "toolCallId": "c1",
                "toolName": "bash",
                "result": "done",
                "isError": False,
            },
            [events.TOOL_FINISHED],
        ),
        ({"type": "compaction_start", "reason": "context"}, [events.RUN_STAGE]),
        ({"type": "compaction_end"}, [events.RUN_STAGE]),
        (
            {"type": "auto_retry_start", "attempt": 1, "errorMessage": "boom"},
            [events.RUN_STAGE],
        ),
        ({"type": "auto_retry_end", "success": False}, [events.RUN_STAGE]),
        (
            {"type": "extension_error", "error": "bad", "extensionPath": "/x"},
            [events.KERNEL_STATUS],
        ),
    ]
    seen: set[str] = set()
    for frame, expected in cases:
        produced = project_event(frame)
        assert _kinds(produced) == expected, frame["type"]
        seen.update(expected)
    # 宿主自有事件（run_started/session_ready/run_finished/…）不由投影层产生。
    host_owned = {
        events.RUN_STARTED,
        events.SESSION_READY,
        events.RUN_FINISHED,
        events.RUN_FAILED,
        events.RUN_CANCELLED,
    }
    assert seen.isdisjoint(host_owned)


def test_host_owned_and_runner_only_frames_produce_nothing() -> None:
    for frame in (
        {"type": "agent_settled"},
        {"type": "message_start"},
        {"type": "queue_update"},
        {"type": "some_future_event", "payload": 1},
        {},
    ):
        assert project_event(frame) == []


def test_run_stage_payloads_carry_the_stage_name_and_fields() -> None:
    (_kind, payload), = project_event({"type": "agent_start"})
    assert payload == {"stage": "agent_start"}

    (_kind, payload), = project_event({"type": "agent_end", "willRetry": 1})
    assert payload == {"stage": "agent_end", "willRetry": True}
    (_kind, payload), = project_event({"type": "agent_end"})
    assert payload == {"stage": "agent_end", "willRetry": False}

    (_kind, payload), = project_event({"type": "compaction_start", "reason": "context_limit"})
    assert payload == {"stage": "compaction_start", "reason": "context_limit"}
    (_kind, payload), = project_event({"type": "compaction_end"})
    assert payload == {"stage": "compaction_end"}

    (_kind, payload), = project_event(
        {"type": "auto_retry_start", "attempt": 2, "errorMessage": "e" * 900}
    )
    assert payload["stage"] == "auto_retry_start"
    assert payload["attempt"] == 2
    assert len(payload["errorMessage"]) == 500, "错误详情同样有界"

    (_kind, payload), = project_event({"type": "auto_retry_end", "success": 0})
    assert payload == {"stage": "auto_retry_end", "success": False}

    (_kind, payload), = project_event(
        {"type": "extension_error", "error": "x" * 900, "extensionPath": "/ext"}
    )
    assert payload["level"] == "warning"
    assert payload["message"] == "extension_error"
    assert payload["extensionPath"] == "/ext"
    assert len(payload["detail"]) == 500


# -- tool_updated.output 是累计快照，原样传递（§3「事件语义细节」） -----------------


def test_tool_updated_passes_each_cumulative_snapshot_through_verbatim() -> None:
    """pi 的 partialResult 是「截至目前累计输出」，投影逐帧原样交出快照——
    前端**替换**显示（tests/web/run-stream.test.mjs）；投影若做增量换算或拼接，
    替换语义就被破坏。"""
    snapshots = ["", "total 8\n", "total 8\nreport.md\n"]
    outputs = []
    for snapshot in snapshots:
        (kind, payload), = project_event(
            {
                "type": "tool_execution_update",
                "toolCallId": "c1",
                "toolName": "bash",
                "partialResult": {"content": [{"type": "text", "text": snapshot}]},
            }
        )
        assert kind == events.TOOL_UPDATED
        outputs.append(payload["output"])
    assert outputs == snapshots


def test_tool_updated_snapshot_is_bounded_at_the_tool_text_limit() -> None:
    big = "y" * (TOOL_TEXT_LIMIT + 100)
    (_kind, payload), = project_event(
        {
            "type": "tool_execution_update",
            "toolCallId": "c1",
            "toolName": "bash",
            "partialResult": big,
        }
    )
    assert payload["output"].startswith("y" * 100)
    assert payload["output"].endswith("…[truncated 100 chars]")


def test_tool_finished_flattens_result_blocks_and_coerces_fields() -> None:
    (_kind, payload), = project_event(
        {
            "type": "tool_execution_end",
            "toolCallId": "c1",
            "toolName": "read",
            "result": [{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}],
            "isError": 0,
        }
    )
    assert payload["result"] == "第一段\n第二段"
    assert payload["isError"] is False

    (_kind, payload), = project_event(
        {
            "type": "tool_execution_end",
            "toolCallId": "c2",
            "toolName": "read",
            "result": "ENOENT",
            "isError": 1,
        }
    )
    assert payload["isError"] is True


def test_tool_frames_coerce_missing_ids_and_names_to_empty_strings() -> None:
    (_kind, payload), = project_event({"type": "tool_execution_start"})
    assert payload == {"toolCallId": "", "toolName": "", "args": {}}
    (_kind, payload), = project_event({"type": "tool_execution_update"})
    assert payload == {"toolCallId": "", "toolName": "", "output": ""}


# -- assistant_message：截断声明与 stopReason 透传 ---------------------------------


@pytest.mark.parametrize("stop", ["length", "error", "aborted"])
def test_assistant_message_passes_stop_reason_through_verbatim(stop: str) -> None:
    """§4 的终态判定（length=交付不完整、error/aborted=failed）是 runner 的职责；
    投影层只做原样透传，不区分这三种形态。"""
    (_kind, payload), = project_event(
        {
            "type": "message_end",
            "message": {"role": "assistant", "content": "答", "stopReason": stop},
        }
    )
    assert payload["stopReason"] == stop
    assert "errorMessage" not in payload, "错误细节由 runner 从原始帧读取，不进投影载荷"


def test_assistant_message_accepts_snake_case_stop_reason() -> None:
    (_kind, payload), = project_event(
        {
            "type": "message_end",
            "message": {"role": "assistant", "content": "答", "stop_reason": "length"},
        }
    )
    assert payload["stopReason"] == "length"


def test_a_truncated_authoritative_message_keeps_its_stop_reason() -> None:
    text = "答" * (ASSISTANT_TEXT_LIMIT + 10)
    (_kind, payload), = project_event(
        {
            "type": "message_end",
            "message": {"role": "assistant", "content": text, "stopReason": "length"},
        }
    )
    assert payload["truncated"] is True
    assert payload["stopReason"] == "length"
    assert payload["text"].endswith("…[truncated 10 chars]")


def test_message_end_for_non_assistant_roles_projects_to_nothing() -> None:
    assert project_event({"type": "message_end", "message": {"role": "user", "content": "问"}}) == []
    assert project_event({"type": "message_end", "message": "not-a-mapping"}) == []
    assert project_event({"type": "message_end"}) == []


def test_assistant_message_flattens_content_blocks() -> None:
    (_kind, payload), = project_event(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "上半"}, {"type": "text", "text": "下半"}],
            },
        }
    )
    assert payload["text"] == "上半\n下半"
    assert "truncated" not in payload


# -- 工具参数投影：字符串保持字符串、数组保持有界字符串数组、对象元素紧凑 JSON --------


def test_project_args_keeps_scalars_and_rejects_non_mapping_args() -> None:
    assert project_args(None) == {}
    assert project_args("ls") == {}
    assert project_args(["a"]) == {}
    projected = project_args({"s": "x", "i": 3, "f": 1.5, "b": True, "n": None})
    assert projected == {"s": "x", "i": 3, "f": 1.5, "b": True, "n": None}


def test_project_args_bounds_the_key_count() -> None:
    projected = project_args({f"k{i}": i for i in range(30)})
    assert len(projected) == 20, "参数表的键数同样有界"


def test_project_args_clips_long_strings_and_stays_json_safe() -> None:
    projected = project_args({"path": "report.md", "content": "y" * 5000})
    assert projected["path"] == "report.md"
    assert projected["content"].endswith("…[truncated 3000 chars]")
    json.dumps(projected, ensure_ascii=False)


def test_project_args_arrays_stay_bounded_arrays_of_strings() -> None:
    projected = project_args({"queries": ["q" * (ARG_ITEM_LIMIT + 300)] * (ARG_LIST_LIMIT + 10)})
    queries = projected["queries"]
    assert len(queries) == ARG_LIST_LIMIT
    assert all(isinstance(item, str) for item in queries)
    assert all(item.endswith(f"…[truncated {300} chars]") for item in queries)


def test_project_args_object_items_and_object_values_become_compact_json() -> None:
    projected = project_args(
        {
            "tasks": [{"agent": "researcher", "task": "取证"}, ["nested", 1]],
            "filters": {"site": "example.com", "n": 2},
        }
    )
    assert projected["tasks"][0] == '{"agent":"researcher","task":"取证"}'
    assert projected["tasks"][1] == '["nested",1]'
    assert json.loads(projected["filters"]) == {"site": "example.com", "n": 2}


def test_project_args_non_text_scalar_array_items_degrade_to_empty_strings() -> None:
    """extract_text 的未知形状退化契约延伸到数组元素：不抛错、不捏造文本。"""
    assert project_args({"ids": [1, None, True]}) == {"ids": ["", "", ""]}


# -- usage 语义（§3.4） -------------------------------------------------------------
#
# 分工：投影层把 turn_end.message.usage 的**本次请求原始快照**原样交出（只求和
# 白名单字段，不预先累计、不造 requestUsage）；跨请求求和与同 message 去重是
# runs.channel.accumulate_usage 的职责，下方不经 HTTP/RunManager 实例直接钉住它。
# 端到端形态另由 tests/test_runner.py 的 usage 回归钉住。


def test_turn_end_emits_the_raw_per_request_snapshot_unchanged() -> None:
    usage = {
        "input": 100,
        "output": 5,
        "cacheRead": 3,
        "cacheWrite": 2,
        "totalTokens": 105,
        "cost": {"total": 0.01},
    }
    produced = project_event(
        {
            "type": "turn_end",
            "message": {"role": "assistant", "timestamp": 7, "usage": usage},
        }
    )
    assert _kinds(produced) == [events.TURN_FINISHED, events.USAGE_REPORTED]
    assert produced[1][1] == {"usage": usage}, (
        "投影不交累计值、不补 requestUsage：那是 runner 求和后的形态"
    )


def test_usage_projection_keeps_only_the_documented_fields() -> None:
    produced = project_event(
        {
            "type": "turn_end",
            "message": {
                "role": "assistant",
                "usage": {"input": 1, "totalTokens": 2, "requests": 9, "internalDebug": "x"},
            },
        }
    )
    # requests 由 runner 自己计数，pi 快照里的同名字段不透传，避免两份事实。
    assert produced[1][1]["usage"] == {"input": 1, "totalTokens": 2}


def test_turn_end_without_usable_usage_emits_no_usage_event() -> None:
    assert _kinds(project_event({"type": "turn_end"})) == [events.TURN_FINISHED]
    assert _kinds(project_event({"type": "turn_end", "message": {"role": "assistant"}})) == [
        events.TURN_FINISHED
    ]
    for bad_usage in ({}, "n/a", 5):
        produced = project_event(
            {"type": "turn_end", "message": {"role": "assistant", "usage": bad_usage}}
        )
        assert _kinds(produced) == [events.TURN_FINISHED]


def test_assistant_message_embeds_usage_when_present() -> None:
    (_kind, payload), = project_event(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": "答",
                "usage": {"input": 1, "output": 2, "totalTokens": 3},
            },
        }
    )
    assert payload["usage"] == {"input": 1, "output": 2, "totalTokens": 3}


def _accumulate(runner: Any, execution: Any, frame: dict[str, Any]) -> dict[str, Any] | None:
    """Drive one turn_end frame through projection then the run's accumulator."""
    from pi_glink.runs.channel import accumulate_usage  # noqa: PLC0415

    payload = project_event(frame)[1][1]
    return accumulate_usage(runner._family_of(execution), execution.execution_facts, frame, payload)


def _turn(timestamp: Any, tokens: int, **usage_extra: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "usage": {"input": tokens, "output": 0, "totalTokens": tokens, **usage_extra},
    }
    if timestamp is not None:
        message["timestamp"] = timestamp
    return {"type": "turn_end", "message": message}


def _bare_runner() -> Any:
    """Just the run's family — no bus, no store, no RunManager instance. 求和
    算法已搬进 pi 族（config/store 在这条路径上不被读取），
    ``accumulate_usage`` 只剩按 run 的族委托。"""
    from pi_glink.kernel.harness_pi import PiHarnessFamily  # noqa: PLC0415

    family = PiHarnessFamily(config=None, store=None)  # type: ignore[arg-type]
    return SimpleNamespace(_family_of=lambda _execution: family)


def test_usage_is_summed_across_requests_and_deduped_by_message_identity() -> None:
    runner = _bare_runner()
    execution = SimpleNamespace(execution_facts=SimpleNamespace(usage_total={}, usage_keys=set()))

    out1 = _accumulate(runner, execution, _turn(1, 100))
    assert out1 is not None
    assert out1["usage"]["totalTokens"] == 100
    assert out1["usage"]["requests"] == 1
    assert out1["requestUsage"] == {"input": 100, "output": 0, "totalTokens": 100}

    out2 = _accumulate(runner, execution, _turn(2, 200))
    assert out2 is not None
    assert out2["usage"]["totalTokens"] == 300, "run 用量是跨请求求和"
    assert out2["usage"]["requests"] == 2

    # 同一 message 身份（timestamp/id）重复到达只计一次：返回 None，不产生第二帧。
    assert _accumulate(runner, execution, _turn(2, 200)) is None
    assert execution.execution_facts.usage_total["requests"] == 2
    # 没有身份的帧无从去重，每次都计。
    out3 = _accumulate(runner, execution, _turn(None, 50))
    assert out3 is not None and out3["usage"]["totalTokens"] == 350
    # 宿主不内置价格表：pi 没报 cost 时总计里也不伪造。
    assert "cost" not in out3["usage"]


def test_usage_cost_is_summed_only_when_reported() -> None:
    runner = _bare_runner()
    execution = SimpleNamespace(execution_facts=SimpleNamespace(usage_total={}, usage_keys=set()))
    _accumulate(runner, execution, _turn(1, 10, cost={"total": 0.5}))
    out = _accumulate(runner, execution, _turn(2, 10, cost={"total": 0.5}))
    assert out is not None
    assert out["usage"]["cost"] == {"total": 1.0}


# -- 脱敏边界：payload 投影必经 SecretRedactor --------------------------------------


class _SpyRedactor(SecretRedactor):
    """Counts top-level payload redactions (the recursion counts depth > 0)."""

    def __init__(self, values: Any = ()) -> None:
        super().__init__(values)
        self.calls = 0
        self._depth = 0

    def redact_payload(self, payload: Any) -> Any:
        if self._depth == 0:
            self.calls += 1
        self._depth += 1
        try:
            return super().redact_payload(payload)
        finally:
            self._depth -= 1


def test_every_projected_payload_passes_through_the_redactor() -> None:
    secret = "sk-proj-0123456789abcdef"
    spy = _SpyRedactor([secret])
    produced = project_event(
        {
            "type": "tool_execution_end",
            "toolCallId": "c1",
            "toolName": "bash",
            "result": f"读到 {secret} 完毕",
            "isError": False,
        },
        spy,
    )
    assert spy.calls == 1
    rendered = json.dumps(produced[0][1], ensure_ascii=False)
    assert secret not in rendered
    assert REPLACEMENT in rendered


def test_each_produced_event_of_a_frame_is_redacted_independently() -> None:
    spy = _SpyRedactor()
    project_event(
        {"type": "turn_end", "message": {"role": "assistant", "usage": {"input": 1}}},
        spy,
    )
    assert spy.calls == 2, "turn_finished 与 usage_reported 各自过一遍脱敏"


def test_missing_redactor_still_masks_credential_shapes() -> None:
    """fail-closed 默认：调用方忘传 redactor 时形状检测仍生效（完成审计正是用
    tool_execution_end 里的 OPENAI_API_KEY=… 复现了这条路径）。"""
    (_kind, payload), = project_event(
        {
            "type": "tool_execution_end",
            "toolCallId": "c1",
            "toolName": "env",
            "result": "OPENAI_API_KEY=sk-abcdefghijklmnop",
            "isError": False,
        }
    )
    rendered = json.dumps(payload, ensure_ascii=False)
    assert "sk-abcdefghijklmnop" not in rendered
    assert REPLACEMENT in rendered


# -- Projector：每 run 的有状态投影边界（§3.3） -------------------------------------


def test_projector_masks_a_credential_split_across_deltas() -> None:
    secret = "sk-proj-0123456789abcdef"
    projector = Projector(SecretRedactor([secret]))
    released: list[str] = []
    for delta in ("答案是 " + secret[:10], secret[10:] + "，已记录"):
        for kind, payload in projector.project(_text_delta(delta)):
            assert kind == events.ASSISTANT_DELTA
            released.append(payload["text"])
    # 整块在第二个 delta 处即可判定并释放，边界帧不再有暂存尾。
    assert projector.project({"type": "turn_end"}) == [(events.TURN_FINISHED, {})]
    assert "".join(released) == "答案是 " + REPLACEMENT + "，已记录"


def test_stateless_projection_cannot_see_a_split_credential() -> None:
    """半个凭据各自不过检测——钉住这个边界是为了说明 runner 必须用 Projector
    而不是逐帧 project_event（§3.3），不是目标形态本身。"""
    secret = "sk-proj-0123456789abcdef"
    redactor = SecretRedactor([secret])
    halves = ["答案是 " + secret[:10], secret[10:] + "，已记录"]
    text = "".join(
        payload["text"]
        for frame in halves
        for _kind, payload in project_event(_text_delta(frame), redactor)
    )
    assert secret in text


def test_projector_releases_the_withheld_tail_before_the_boundary_events() -> None:
    """暂存尾在边界帧自己的产品事件之前释放，使其仍归属原内容块。"""
    projector = Projector(SecretRedactor())
    assert projector.project(_text_delta("文本 sk")) == [
        (events.ASSISTANT_DELTA, {"text": "文本 "})
    ]
    boundary = projector.project({"type": "turn_end"})
    assert _kinds(boundary) == [events.ASSISTANT_DELTA, events.TURN_FINISHED]
    assert boundary[0][1] == {"text": "sk"}
    # agent_settled 没有产品事件，但同样先释放暂存尾。
    assert projector.project(_text_delta("又一段 sk")) == [
        (events.ASSISTANT_DELTA, {"text": "又一段 "})
    ]
    assert projector.project({"type": "agent_settled"}) == [
        (events.ASSISTANT_DELTA, {"text": "sk"})
    ]


def test_projector_flush_releases_both_delta_kinds_in_stream_order() -> None:
    projector = Projector(SecretRedactor())
    projector.project(_thinking_delta("思考 sk"))
    projector.project(_text_delta("正文 sk"))
    assert projector.flush() == [
        (events.THINKING_DELTA, {"text": "sk"}),
        (events.ASSISTANT_DELTA, {"text": "sk"}),
    ]
    assert projector.flush() == [], "已释放的暂存不会重复吐出"


def test_projector_redacts_non_delta_payloads_with_the_run_redactor() -> None:
    secret = "sk-proj-0123456789abcdef"
    projector = Projector(SecretRedactor([secret]))
    (_kind, payload), = projector.project(
        {
            "type": "tool_execution_end",
            "toolCallId": "c1",
            "toolName": "bash",
            "result": secret,
            "isError": False,
        }
    )
    assert payload["result"] == REPLACEMENT


# -- message_update 与 extract_text 的边角 ------------------------------------------


def test_message_update_drops_unknown_empty_and_non_string_deltas() -> None:
    assert project_event({"type": "message_update"}) == []
    assert project_event({"type": "message_update", "assistantMessageEvent": "bad"}) == []
    assert (
        project_event(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "toolcall_delta", "delta": "x"},
            }
        )
        == []
    )
    assert project_event(_text_delta("")) == []
    assert (
        project_event(
            {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": 5}}
        )
        == []
    )


def test_a_single_delta_is_bounded_at_the_delta_limit() -> None:
    (_kind, payload), = project_event(_text_delta("d" * (DELTA_LIMIT + 10)))
    assert payload["text"].endswith("…[truncated 10 chars]")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ("纯文本", "纯文本"),
        ({"text": "a"}, "a"),
        ({"output": "b"}, "b"),
        ({"result": {"text": "deep"}}, "deep"),
        ({"content": [{"type": "text", "text": "x"}, {"type": "text", "text": "y"}]}, "x\ny"),
        ([{"type": "text", "text": "x"}, "y", None], "x\ny"),
        ({"content": []}, ""),
        ({"unknown": 1}, ""),
        (5, ""),
    ],
)
def test_extract_text_flattens_pi_content_shapes(value: Any, expected: str) -> None:
    """未知形状退化为空串而不是抛错：渲染面不能因为协议新增而崩溃。"""
    assert extract_text(value) == expected


def test_project_frames_is_sequential_project_event() -> None:
    frames = [{"type": "turn_start"}, {"type": "agent_settled"}, {"type": "turn_end"}]
    assert project_frames(frames) == [
        (events.TURN_STARTED, {}),
        (events.TURN_FINISHED, {}),
    ]


# -- 子代理类型化投影（docs/architecture.md §3.5，Projector 的每 run 状态） ------------
#
# 识别依据：工具名等于 SUBAGENT_TOOL_NAME（pi-subagents 的派发工具名，常量钉住）。
# 类型化是每 run 契约：Projector 记住哪些 toolCallId 派发了子代理，因此只有
# 有状态投影路径产生 subagent_started/subagent_finished；无状态 project_event
# 保持通用卡片（旧格式日志与单帧检查仍能按原形状投影）。


def _dispatch(
    tool_call_id: str = "sub-1",
    tasks: Any = None,
    **extra_args: Any,
) -> dict[str, Any]:
    args: dict[str, Any] = dict(extra_args)
    if tasks is not None:
        args["tasks"] = tasks
    return {
        "type": "tool_execution_start",
        "toolCallId": tool_call_id,
        "toolName": SUBAGENT_TOOL_NAME,
        "args": args,
    }


_TWO_TASKS = [
    {"agent": "researcher", "task": "法域 A 取证"},
    {"agent": "researcher", "task": "法域 B 取证"},
]


def test_tool_name_constant_matches_the_dispatch_tool() -> None:
    """识别依据钉住：pi-subagents 的派发工具就叫 ``subagent``（§3.5）。"""
    assert SUBAGENT_TOOL_NAME == "subagent"


def test_dispatch_emits_one_started_per_task_with_increasing_n() -> None:
    projector = Projector(SecretRedactor())
    produced = projector.project(_dispatch(tasks=list(_TWO_TASKS)))
    assert _kinds(produced) == [events.SUBAGENT_STARTED, events.SUBAGENT_STARTED]
    first, second = (payload for _kind, payload in produced)
    assert first == {
        "n": 0,
        "toolCallId": "sub-1",
        "agent": "researcher",
        "task": "法域 A 取证",
    }
    assert second["n"] == 1
    assert second["task"] == "法域 B 取证"
    # n 是 run 内单调分配的序号：下一次派发从 2 继续，不按调用重置。
    followup = projector.project(
        _dispatch(tool_call_id="sub-2", tasks=[{"task": "法域 C"}])
    )
    assert _kinds(followup) == [events.SUBAGENT_STARTED]
    assert followup[0][1]["n"] == 2
    assert followup[0][1]["agent"] == "", "缺失的 agent 取空，不伪造"


def test_dispatch_updates_are_suppressed_and_end_closes_every_child() -> None:
    projector = Projector(SecretRedactor())
    projector.project(_dispatch(tasks=list(_TWO_TASKS)))
    # "(running...)" 占位没有信息：该 toolCallId 的 update 一律抑制。
    assert (
        projector.project(
            {
                "type": "tool_execution_update",
                "toolCallId": "sub-1",
                "toolName": SUBAGENT_TOOL_NAME,
                "partialResult": "(running...)",
            }
        )
        == []
    )
    produced = projector.project(
        {
            "type": "tool_execution_end",
            "toolCallId": "sub-1",
            "toolName": SUBAGENT_TOOL_NAME,
            "result": {"content": [{"type": "text", "text": "两个子代理都完成"}]},
            "isError": False,
        }
    )
    assert _kinds(produced) == [events.SUBAGENT_FINISHED]
    payload = produced[0][1]
    assert payload == {
        "toolCallId": "sub-1",
        "ns": [0, 1],
        "status": "finished",
        "isError": False,
        "resultPreview": "两个子代理都完成",
    }


def test_dispatch_end_reports_failed_status_on_error() -> None:
    projector = Projector(SecretRedactor())
    projector.project(_dispatch(tasks=[{"task": "取证"}]))
    (_kind, payload), = projector.project(
        {
            "type": "tool_execution_end",
            "toolCallId": "sub-1",
            "toolName": SUBAGENT_TOOL_NAME,
            "result": "子代理异常",
            "isError": True,
        }
    )
    assert payload["status"] == "failed"
    assert payload["isError"] is True
    assert payload["ns"] == [0]


@pytest.mark.parametrize(
    "args",
    [
        {},  # 没有 tasks
        {"tasks": []},  # 空数组：没有可指认的子代理
        {"tasks": "法域 A"},  # 不是数组
        {"tasks": [{"agent": "researcher"}, "裸字符串"]},  # 数组元素不全是对象
        {"tasks": [None]},  # 同上
    ],
)
def test_malformed_dispatch_args_fall_back_to_a_generic_card(args: dict[str, Any]) -> None:
    """args 形状不符时回退普通工具卡片（防御，不伪造类型事件），且该 toolCallId
    不进入子代理登记表：后续 update/end 仍按通用卡片流动。"""
    projector = Projector(SecretRedactor())
    (kind, payload), = projector.project(
        {
            "type": "tool_execution_start",
            "toolCallId": "sub-9",
            "toolName": SUBAGENT_TOOL_NAME,
            "args": args,
        }
    )
    assert kind == events.TOOL_STARTED
    assert payload["toolName"] == SUBAGENT_TOOL_NAME
    updated = projector.project(
        {
            "type": "tool_execution_update",
            "toolCallId": "sub-9",
            "toolName": SUBAGENT_TOOL_NAME,
            "partialResult": "out",
        }
    )
    assert _kinds(updated) == [events.TOOL_UPDATED], "未登记的调用不抑制 update"
    finished = projector.project(
        {
            "type": "tool_execution_end",
            "toolCallId": "sub-9",
            "toolName": SUBAGENT_TOOL_NAME,
            "result": "done",
            "isError": False,
        }
    )
    assert _kinds(finished) == [events.TOOL_FINISHED], "未登记的调用不发 subagent_finished"


def test_parse_subagent_tasks_shape_rules() -> None:
    assert parse_subagent_tasks({"tasks": [{"task": "x"}]}) == [{"task": "x"}]
    for bad in (None, "tasks", {"tasks": None}, {"tasks": {}}, {"tasks": [[1]]}):
        assert parse_subagent_tasks(bad) is None


def test_subagent_payloads_pass_through_the_run_redactor() -> None:
    """类型事件载荷与现有投影同一道 redactor：task 与结果里的凭据形状值被脱敏。"""
    secret = "sk-subagent0123456789abcdef"
    projector = Projector(SecretRedactor([secret]))
    (kind, payload), = projector.project(
        _dispatch(tasks=[{"agent": "researcher", "task": f"用 {secret} 取证"}])
    )
    assert kind == events.SUBAGENT_STARTED
    assert payload["task"] == f"用 {REPLACEMENT} 取证"
    (_kind, finished), = projector.project(
        {
            "type": "tool_execution_end",
            "toolCallId": "sub-1",
            "toolName": SUBAGENT_TOOL_NAME,
            "result": f"回显 {secret}",
            "isError": False,
        }
    )
    assert finished["resultPreview"] == f"回显 {REPLACEMENT}"
    rendered = json.dumps(finished, ensure_ascii=False)
    assert secret not in rendered


def test_subagent_fields_are_bounded() -> None:
    projector = Projector(SecretRedactor())
    (kind, payload), = projector.project(
        _dispatch(
            tasks=[
                {
                    "agent": "a" * (SUBAGENT_AGENT_LIMIT + 50),
                    "task": "t" * (SUBAGENT_TASK_LIMIT + 50),
                }
            ]
        )
    )
    assert kind == events.SUBAGENT_STARTED
    assert payload["agent"].endswith("…[truncated 50 chars]")
    assert payload["task"].endswith("…[truncated 50 chars]")
    (_kind, finished), = projector.project(
        {
            "type": "tool_execution_end",
            "toolCallId": "sub-1",
            "toolName": SUBAGENT_TOOL_NAME,
            "result": "r" * (TOOL_TEXT_LIMIT + 10),
            "isError": False,
        }
    )
    assert finished["resultPreview"].endswith("…[truncated 10 chars]")


def test_non_subagent_calls_are_untouched_by_a_dispatch_in_the_same_run() -> None:
    """普通工具调用的投影逐点不变：与子代理派发交错时仍走通用卡片。"""
    projector = Projector(SecretRedactor())
    projector.project(_dispatch(tasks=list(_TWO_TASKS)))
    (kind, payload), = projector.project(
        {
            "type": "tool_execution_start",
            "toolCallId": "c1",
            "toolName": "bash",
            "args": {"command": "ls"},
        }
    )
    assert kind == events.TOOL_STARTED
    assert payload == {"toolCallId": "c1", "toolName": "bash", "args": {"command": "ls"}}
    updated = projector.project(
        {
            "type": "tool_execution_update",
            "toolCallId": "c1",
            "toolName": "bash",
            "partialResult": "out",
        }
    )
    assert _kinds(updated) == [events.TOOL_UPDATED]
    finished = projector.project(
        {
            "type": "tool_execution_end",
            "toolCallId": "c1",
            "toolName": "bash",
            "result": "done",
            "isError": False,
        }
    )
    assert _kinds(finished) == [events.TOOL_FINISHED]


def test_stateless_projection_keeps_subagent_calls_as_generic_cards() -> None:
    """钉住分工：类型化需要记住 toolCallId，只有 Projector 能做到；无状态
    project_event 保持通用卡片，旧格式日志与单帧检查因此仍按原形状投影。"""
    produced = project_event(_dispatch(tasks=list(_TWO_TASKS)))
    assert _kinds(produced) == [events.TOOL_STARTED]
    end = project_event(
        {
            "type": "tool_execution_end",
            "toolCallId": "sub-1",
            "toolName": SUBAGENT_TOOL_NAME,
            "result": "done",
            "isError": False,
        }
    )
    assert _kinds(end) == [events.TOOL_FINISHED]
