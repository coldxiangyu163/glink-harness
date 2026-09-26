"""Direct unit tests for :class:`kernel.acp_projection.AcpProjector`.

``tests/test_kernel_acp.py`` already pins the happy-path §4.1 mapping (chunk
deltas, tool lifecycle, plan, usage conversion, content/notice dedup) through
the projector. This file covers the rest of the projector's contract without
going through HTTP or ``RunManager``:

* frame routing: malformed frames and foreign methods project nothing;
* synthetic frames: ``acp_prompt_result``/``acp_turn_finished`` release the
  withheld delta tail *before* their own events; ``acp_session_resumed``
  records the resume method and the dropped replay count;
* the fail-closed permission notice: defaults, the allowlist variant, tool
  kind provenance, bounding and redaction;
* tool content shapes (diff / terminal / rawOutput fallback), the
  whole-replacement semantics of ``tool_call_update``, the remembered tool
  name, and every payload bound;
* usage input validation (booleans, negatives, incomplete cost) and its
  once-per-run notices;
* the emitted names stay inside the pi family's product vocabulary.
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from pi_glink import events
from pi_glink.kernel import projection as pi_projection
from pi_glink.kernel.acp_projection import (
    ACP_STOP_REASONS,
    COMMAND_LIMIT,
    COMMAND_NAME_LIMIT,
    MESSAGE_CHUNK_UPDATES,
    PLAN_CONTENT_LIMIT,
    PLAN_ENTRY_LIMIT,
    TOOL_TEXT_LIMIT,
    AcpProjector,
)
from pi_glink.redaction import SecretRedactor


def _notification(update: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": "test-session", "update": dict(update)},
    }


def _project(
    projector: AcpProjector, update: Mapping[str, Any]
) -> list[tuple[str, dict[str, Any]]]:
    return projector.project(_notification(update))


def _chunk(text: str, kind: str = "agent_message_chunk") -> dict[str, Any]:
    return {"sessionUpdate": kind, "content": {"type": "text", "text": text}}


# -- frame routing --------------------------------------------------------------


@pytest.mark.parametrize(
    "frame",
    [
        # A synthetic type this module does not translate.
        {"type": "acp_unlisted_synthetic"},
        # A JSON-RPC notification that is not session/update.
        {"method": "session/other", "params": {"update": {"sessionUpdate": "plan"}}},
        # session/update with missing or malformed params/update wrappers.
        {"method": "session/update"},
        {"method": "session/update", "params": "not-a-mapping"},
        {"method": "session/update", "params": {"update": ["not", "a", "mapping"]}},
        # An update without a usable sessionUpdate kind.
        {"method": "session/update", "params": {"update": {}}},
        {"method": "session/update", "params": {"update": {"sessionUpdate": ""}}},
        {"method": "session/update", "params": {"update": {"sessionUpdate": 42}}},
        {},
    ],
)
def test_frames_without_a_projectable_update_yield_nothing(
    frame: Mapping[str, Any],
) -> None:
    projector = AcpProjector(SecretRedactor())
    assert projector.project(frame) == []


def test_each_unmapped_update_kind_gets_exactly_one_notice() -> None:
    projector = AcpProjector(SecretRedactor())
    first = _project(projector, {"sessionUpdate": "current_mode_update", "currentModeId": "code"})
    assert [kind for kind, _ in first] == [events.KERNEL_STATUS]
    assert first[0][1]["message"] == "acp_update_not_projected"
    assert "current_mode_update" in first[0][1]["detail"]
    # Same kind again: no flood.
    assert _project(projector, {"sessionUpdate": "current_mode_update", "currentModeId": "plan"}) == []
    # A different unmapped kind is a different fact and gets its own notice.
    other = _project(projector, {"sessionUpdate": "session_info_update", "title": "会话"})
    assert [kind for kind, _ in other] == [events.KERNEL_STATUS]
    assert other[0][1]["message"] == "acp_update_not_projected"
    assert "session_info_update" in other[0][1]["detail"]


# -- chunk content edge shapes ----------------------------------------------------


@pytest.mark.parametrize(
    "update",
    [
        # The content wrapper is not a mapping at all.
        {"sessionUpdate": "agent_message_chunk", "content": "plain"},
        # A text block whose text is not a string.
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": 123}},
        # An empty text block emits no delta.
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": ""}},
    ],
)
def test_chunk_updates_without_text_emit_nothing(update: Mapping[str, Any]) -> None:
    projector = AcpProjector(SecretRedactor())
    assert _project(projector, update) == []


def test_the_content_notice_is_once_per_block_type() -> None:
    projector = AcpProjector(SecretRedactor())
    image = _project(
        projector,
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "image", "data": "aGVsbG8="}},
    )
    assert image[0][1]["message"] == "acp_content_not_projected"
    assert "type=image" in image[0][1]["detail"]
    # A different unsupported block type is a new fact.
    resource = _project(
        projector,
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "resource", "resource": {}}},
    )
    assert resource[0][1]["message"] == "acp_content_not_projected"
    assert "type=resource" in resource[0][1]["detail"]
    # A content block without any type is reported as unknown, still once.
    untyped = _project(
        projector, {"sessionUpdate": "agent_message_chunk", "content": {"data": "aGVsbG8="}}
    )
    assert untyped[0][1]["message"] == "acp_content_not_projected"
    assert "type=unknown" in untyped[0][1]["detail"]
    assert _project(projector, {"sessionUpdate": "agent_message_chunk", "content": {"data": "eA=="}}) == []


# -- stream-boundary flush ordering -----------------------------------------------


def test_turn_finished_releases_both_withheld_tails_before_closing() -> None:
    """A credential-shaped tail withheld by the stream redactor belongs to the
    block the turn boundary closes, so it is released first."""
    projector = AcpProjector(SecretRedactor())
    # "sk-" at the end is an open credential shape: withheld, not emitted yet.
    assert _project(projector, _chunk("答 sk-")) == [(events.ASSISTANT_DELTA, {"text": "答 "})]
    assert _project(projector, _chunk("想 sk-", "agent_thought_chunk")) == [
        (events.THINKING_DELTA, {"text": "想 "})
    ]
    produced = projector.project({"type": "acp_turn_finished"})
    assert produced == [
        (events.THINKING_DELTA, {"text": "sk-"}),
        (events.ASSISTANT_DELTA, {"text": "sk-"}),
        (events.TURN_FINISHED, {}),
    ]


def test_prompt_result_releases_the_tail_without_a_turn_event() -> None:
    """``acp_prompt_result`` is a turn boundary, not a render boundary: it
    flushes the withheld text and nothing else (the runner owns the verdict)."""
    projector = AcpProjector(SecretRedactor())
    assert _project(projector, _chunk("答 sk-")) == [(events.ASSISTANT_DELTA, {"text": "答 "})]
    produced = projector.project({"type": "acp_prompt_result", "stopReason": "end_turn"})
    assert produced == [(events.ASSISTANT_DELTA, {"text": "sk-"})]


def test_non_chunk_updates_release_the_withheld_tail_first() -> None:
    """Both the mapped-handler path and the unmapped-notice path flush before
    emitting, so withheld text lands ahead of the frame that supersedes it."""
    projector = AcpProjector(SecretRedactor())
    assert _project(projector, _chunk("答 sk-")) == [(events.ASSISTANT_DELTA, {"text": "答 "})]
    mapped = _project(
        projector,
        {"sessionUpdate": "tool_call", "toolCallId": "c1", "name": "bash", "status": "pending"},
    )
    assert mapped[0] == (events.ASSISTANT_DELTA, {"text": "sk-"})
    assert mapped[1][0] == events.TOOL_STARTED

    assert _project(projector, _chunk("又 sk-")) == [(events.ASSISTANT_DELTA, {"text": "又 "})]
    unmapped = _project(projector, {"sessionUpdate": "current_mode_update", "currentModeId": "code"})
    assert unmapped[0] == (events.ASSISTANT_DELTA, {"text": "sk-"})
    assert unmapped[1] == (
        events.KERNEL_STATUS,
        unmapped[1][1],
    )
    assert unmapped[1][1]["message"] == "acp_update_not_projected"


# -- the synthetic resume frame ---------------------------------------------------


def test_session_resumed_records_the_method_and_the_dropped_replay() -> None:
    projector = AcpProjector(SecretRedactor())
    produced = projector.project(
        {
            "type": "acp_session_resumed",
            "method": "session/load",
            "sessionId": "parent-session",
            "replayedUpdates": 5,
        }
    )
    assert [kind for kind, _ in produced] == [events.KERNEL_STATUS]
    payload = produced[0][1]
    assert payload["level"] == "info"
    assert payload["message"] == "acp_session_resumed"
    assert payload["resumeMethod"] == "session/load"
    assert payload["sessionId"] == "parent-session"
    assert payload["replayedUpdates"] == 5
    # The dropped replay is the parent run's already-recorded history: the
    # notice says how much was not projected a second time.
    assert "session/load" in payload["detail"]
    assert "5" in payload["detail"]


def test_session_resumed_without_a_replay_says_so() -> None:
    projector = AcpProjector(SecretRedactor())
    payload = projector.project(
        {
            "type": "acp_session_resumed",
            "method": "session/resume",
            "sessionId": "parent-session",
            "replayedUpdates": 0,
        }
    )[0][1]
    assert payload["resumeMethod"] == "session/resume"
    assert payload["replayedUpdates"] == 0
    assert "回放" not in payload["detail"]


def test_session_resumed_tolerates_missing_or_typed_out_fields() -> None:
    projector = AcpProjector(SecretRedactor())
    payload = projector.project({"type": "acp_session_resumed", "replayedUpdates": "5"})[0][1]
    # No method: the notice admits it instead of guessing; a replay count that
    # is not an int is not projected as one.
    assert payload["resumeMethod"] == ""
    assert "unknown" in payload["detail"]
    assert "sessionId" not in payload
    assert "replayedUpdates" not in payload


def test_session_resumed_payload_passes_through_the_run_redactor() -> None:
    # Regression: this branch used to return its payload without _redact_all,
    # unlike every other path out of project(). sessionId/method are
    # agent-controlled identifiers and can echo a run credential.
    secret = "opaque-token-value-99"
    projector = AcpProjector(SecretRedactor([secret]))
    payload = projector.project(
        {
            "type": "acp_session_resumed",
            "method": "session/load",
            "sessionId": f"parent-{secret}",
            "replayedUpdates": 2,
        }
    )[0][1]
    assert secret not in payload["sessionId"]
    assert "[REDACTED]" in payload["sessionId"]
    assert secret not in payload["detail"]
    # Adjacent legal input is untouched: a clean sessionId survives verbatim.
    clean = projector.project(
        {"type": "acp_session_resumed", "method": "session/resume", "sessionId": "s-1"}
    )[0][1]
    assert clean["sessionId"] == "s-1"
    assert clean["resumeMethod"] == "session/resume"


# -- the permission notice ---------------------------------------------------------


def test_permission_notice_defaults_to_the_fail_closed_shape() -> None:
    projector = AcpProjector(SecretRedactor())
    produced = projector.project({"type": "acp_permission"})
    assert [kind for kind, _ in produced] == [events.KERNEL_STATUS]
    payload = produced[0][1]
    assert payload == {
        "level": "warning",
        "message": "acp_permission_denied",
        "detail": "ACP 权限请求按 fail-closed 应答：decision=rejected",
        "decision": "rejected",
        "policy": "fail_closed",
        "toolCallId": "",
    }


def test_permission_allowed_is_recorded_as_an_audit_fact() -> None:
    projector = AcpProjector(SecretRedactor())
    payload = projector.project(
        {
            "type": "acp_permission",
            "decision": "allowed",
            "policy": "allowlist",
            "optionId": "allow-once",
            "optionKind": "allow_once",
            "toolCall": {"toolCallId": "call_1", "title": "Fetch https://example.invalid"},
        }
    )[0][1]
    assert payload["message"] == "acp_permission_allowed"
    assert payload["level"] == "warning"
    assert payload["decision"] == "allowed"
    assert payload["policy"] == "allowlist"
    assert payload["toolCallId"] == "call_1"
    assert payload["toolTitle"] == "Fetch https://example.invalid"
    assert payload["optionId"] == "allow-once"
    assert payload["optionKind"] == "allow_once"
    assert payload["detail"] == (
        "ACP 权限请求按显式白名单应答：decision=allowed"
        "；tool=Fetch https://example.invalid；option=allow-once(allow_once)"
    )


def test_permission_notice_records_tool_kind_provenance() -> None:
    projector = AcpProjector(SecretRedactor())
    # The kind the agent published in the tool_call notification for the same id.
    resolved = projector.project(
        {
            "type": "acp_permission",
            "decision": "rejected",
            "toolKind": "read",
            "toolKindSource": "tool_call",
            "toolCall": {"toolCallId": "c1"},
        }
    )[0][1]
    assert resolved["toolKind"] == "read"
    assert resolved["toolKindSource"] == "tool_call"
    assert "kind=read（来自同一 toolCallId 的 tool_call 通知）" in resolved["detail"]

    # The kind carried by the request itself is the "request" source.
    from_request = projector.project(
        {
            "type": "acp_permission",
            "decision": "rejected",
            "toolCall": {"toolCallId": "c2", "kind": "edit"},
        }
    )[0][1]
    assert from_request["toolKind"] == "edit"
    assert from_request["toolKindSource"] == "request"
    assert "（来自" not in from_request["detail"]

    # A frame-level kind without an explicit source is the request's own.
    explicit = projector.project(
        {
            "type": "acp_permission",
            "decision": "rejected",
            "toolKind": "execute",
            "toolCall": {"toolCallId": "c3"},
        }
    )[0][1]
    assert explicit["toolKind"] == "execute"
    assert explicit["toolKindSource"] == "request"


def test_permission_title_is_bounded() -> None:
    projector = AcpProjector(SecretRedactor())
    title = "t" * (COMMAND_NAME_LIMIT + 80)
    payload = projector.project(
        {"type": "acp_permission", "decision": "rejected", "toolCall": {"toolCallId": "c", "title": title}}
    )[0][1]
    assert payload["toolTitle"] == f"{'t' * COMMAND_NAME_LIMIT}\n…[truncated 80 chars]"


def test_permission_frame_releases_the_withheld_tail_first() -> None:
    projector = AcpProjector(SecretRedactor())
    assert _project(projector, _chunk("答 sk-")) == [(events.ASSISTANT_DELTA, {"text": "答 "})]
    produced = projector.project({"type": "acp_permission", "decision": "rejected"})
    assert produced[0] == (events.ASSISTANT_DELTA, {"text": "sk-"})
    assert produced[1][0] == events.KERNEL_STATUS
    assert produced[1][1]["message"] == "acp_permission_denied"


def test_notice_payloads_pass_through_the_run_redactor() -> None:
    secret = "opaque-token-value-99"
    projector = AcpProjector(SecretRedactor([secret]))
    commands = _project(
        projector,
        {
            "sessionUpdate": "available_commands_update",
            "availableCommands": [{"name": "plan", "description": f"读取 {secret} 配置"}],
        },
    )
    assert commands[0][1]["commands"] == [
        {"name": "plan", "description": "读取 [REDACTED] 配置"}
    ]
    notice = projector.project(
        {
            "type": "acp_permission",
            "decision": "rejected",
            "toolCall": {"toolCallId": "c1", "title": f"运行 {secret}"},
        }
    )
    assert notice[0][1]["toolTitle"] == "运行 [REDACTED]"
    assert secret not in notice[0][1]["detail"]


# -- tool call projection ----------------------------------------------------------


def test_a_tool_call_arriving_terminal_emits_started_and_finished() -> None:
    projector = AcpProjector(SecretRedactor())
    produced = _project(
        projector,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "call_1",
            "name": "bash",
            "status": "completed",
            "content": [{"type": "content", "content": {"type": "text", "text": "done"}}],
        },
    )
    assert [kind for kind, _ in produced] == [events.TOOL_STARTED, events.TOOL_FINISHED]
    assert produced[0][1] == {
        "toolCallId": "call_1",
        "toolName": "bash",
        "args": {},
    }
    assert produced[1][1] == {
        "toolCallId": "call_1",
        "toolName": "bash",
        "isError": False,
        "result": "done",
    }


def test_the_tool_name_is_remembered_for_later_updates() -> None:
    """An update that carries no name of its own still shows the name the
    agent published in the ``tool_call`` notification for the same id."""
    projector = AcpProjector(SecretRedactor())
    _project(
        projector,
        {"sessionUpdate": "tool_call", "toolCallId": "call_1", "name": "bash", "status": "pending"},
    )
    running = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_1",
            "status": "in_progress",
            "content": [{"type": "content", "content": {"type": "text", "text": "x"}}],
        },
    )
    assert running == [
        (events.TOOL_UPDATED, {"toolCallId": "call_1", "toolName": "bash", "output": "x"})
    ]
    finished = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_1",
            "status": "completed",
            "content": [{"type": "content", "content": {"type": "text", "text": "x"}}],
        },
    )
    assert finished[0][1]["toolName"] == "bash"
    # An id the agent never named stays honestly empty rather than invented.
    anonymous = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_9",
            "status": "failed",
            "content": [{"type": "content", "content": {"type": "text", "text": "boom"}}],
        },
    )
    assert anonymous[0][1]["toolName"] == ""
    assert anonymous[0][1]["isError"] is True


def test_tool_update_content_replaces_instead_of_accumulating() -> None:
    """``tool_call_update`` carries the whole content collection each time; the
    projection shows the latest collection, not a sum of every update."""
    projector = AcpProjector(SecretRedactor())
    first = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_1",
            "status": "in_progress",
            "content": [{"type": "content", "content": {"type": "text", "text": "第一段"}}],
        },
    )
    second = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_1",
            "status": "in_progress",
            "content": [{"type": "content", "content": {"type": "text", "text": "第二段"}}],
        },
    )
    assert first[0][1]["output"] == "第一段"
    assert second[0][1]["output"] == "第二段"


def test_tool_content_renders_diff_terminal_and_plain_items() -> None:
    projector = AcpProjector(SecretRedactor())
    produced = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_1",
            "status": "in_progress",
            "content": [
                {"type": "diff", "path": "a.py", "newText": "+行"},
                {"type": "diff", "newText": "+无路径"},
                {"type": "terminal", "terminalId": "t1"},
                "纯文本",
                {"type": "unknown_block", "data": " ignored "},
            ],
        },
    )
    assert produced[0][1]["output"] == (
        "diff a.py\n+行\ndiff\n+无路径\n[terminal t1]\n纯文本"
    )


def test_tool_result_falls_back_to_compact_raw_output() -> None:
    projector = AcpProjector(SecretRedactor())
    structured = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "c1",
            "status": "completed",
            "content": [],
            "rawOutput": {"exitCode": 0},
        },
    )
    assert structured[0][1]["result"] == '{"exitCode":0}'
    plain = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "c2",
            "status": "completed",
            "rawOutput": "raw 文本",
        },
    )
    assert plain[0][1]["result"] == "raw 文本"
    empty = _project(
        projector,
        {"sessionUpdate": "tool_call_update", "toolCallId": "c3", "status": "completed"},
    )
    assert empty[0][1]["result"] == ""


def test_tool_output_and_result_are_bounded() -> None:
    projector = AcpProjector(SecretRedactor())
    oversized = "y" * (TOOL_TEXT_LIMIT + 100)
    running = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "c1",
            "status": "in_progress",
            "content": [{"type": "content", "content": {"type": "text", "text": oversized}}],
        },
    )
    assert running[0][1]["output"] == f"{'y' * TOOL_TEXT_LIMIT}\n…[truncated 100 chars]"
    finished = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "c1",
            "status": "completed",
            "content": [{"type": "content", "content": {"type": "text", "text": oversized}}],
        },
    )
    assert finished[0][1]["result"] == f"{'y' * TOOL_TEXT_LIMIT}\n…[truncated 100 chars]"


# -- plan and available commands ---------------------------------------------------


def test_plan_entries_are_bounded_and_non_entries_are_skipped() -> None:
    projector = AcpProjector(SecretRedactor())
    entries: list[Any] = [
        {"content": f"条目{i}", "status": "pending", "priority": "low"}
        for i in range(PLAN_ENTRY_LIMIT + 5)
    ]
    produced = _project(projector, {"sessionUpdate": "plan", "entries": entries})
    payload = produced[0][1]
    assert payload["stage"] == "plan"
    assert len(payload["entries"]) == PLAN_ENTRY_LIMIT
    assert payload["entries"][0] == {"content": "条目0", "status": "pending", "priority": "low"}
    assert payload["entries"][-1]["content"] == f"条目{PLAN_ENTRY_LIMIT - 1}"

    # The bound applies to the raw list window: non-mapping items inside it are
    # skipped and do not free their slot for later entries.
    with_junk = _project(
        projector,
        {"sessionUpdate": "plan", "entries": ["not-a-mapping", 7, {"content": "真条目"}]},
    )
    assert [entry["content"] for entry in with_junk[0][1]["entries"]] == ["真条目"]

    long_entry = _project(
        projector,
        {
            "sessionUpdate": "plan",
            "entries": [{"content": "x" * (PLAN_CONTENT_LIMIT + 50), "status": "pending"}],
        },
    )
    item = long_entry[0][1]["entries"][0]
    assert item["content"] == f"{'x' * PLAN_CONTENT_LIMIT}\n…[truncated 50 chars]"
    assert item["priority"] == ""

    malformed = _project(projector, {"sessionUpdate": "plan", "entries": "待办"})
    assert malformed[0][1]["entries"] == []


def test_available_commands_are_bounded_and_the_total_is_reported() -> None:
    projector = AcpProjector(SecretRedactor())
    source: list[Any] = [{"name": f"cmd-{i}", "description": "d"} for i in range(COMMAND_LIMIT + 3)]
    payload = _project(
        projector, {"sessionUpdate": "available_commands_update", "availableCommands": source}
    )[0][1]
    assert payload["message"] == "acp_available_commands"
    assert len(payload["commands"]) == COMMAND_LIMIT
    # The notice reports the declared total, not just the projected window.
    assert str(COMMAND_LIMIT + 3) in payload["detail"]

    oversized = _project(
        projector,
        {
            "sessionUpdate": "available_commands_update",
            "availableCommands": [
                {"name": "n" * (COMMAND_NAME_LIMIT + 10), "description": "d" * 250}
            ],
        },
    )[0][1]
    command = oversized["commands"][0]
    assert command["name"] == f"{'n' * COMMAND_NAME_LIMIT}\n…[truncated 10 chars]"
    assert command["description"] == f"{'d' * 200}\n…[truncated 50 chars]"

    malformed = _project(
        projector, {"sessionUpdate": "available_commands_update", "availableCommands": "none"}
    )[0][1]
    assert malformed["commands"] == []
    assert " 0 " in malformed["detail"]


# -- usage -------------------------------------------------------------------------


def test_usage_without_cost_keeps_the_acp_basis_without_inventing_one() -> None:
    projector = AcpProjector(SecretRedactor())
    produced = _project(projector, {"sessionUpdate": "usage_update", "used": 100, "size": 200})
    assert produced == [
        (
            events.USAGE_REPORTED,
            {
                "usage": {"totalTokens": 100, "contextSize": 200, "basis": "acp_usage_update"},
                "requestUsage": {"used": 100, "size": 200},
            },
        )
    ]


@pytest.mark.parametrize(
    "update",
    [
        {"sessionUpdate": "usage_update", "used": True, "size": 200},
        {"sessionUpdate": "usage_update", "used": -1, "size": 200},
        {"sessionUpdate": "usage_update", "used": "100", "size": 200},
        {"sessionUpdate": "usage_update", "used": 100},
        {"sessionUpdate": "usage_update", "used": 100, "size": 1.5},
    ],
)
def test_usage_counters_must_be_non_negative_ints(update: Mapping[str, Any]) -> None:
    projector = AcpProjector(SecretRedactor())
    produced = _project(projector, update)
    assert [kind for kind, _ in produced] == [events.KERNEL_STATUS]
    assert produced[0][1]["message"] == "acp_usage_invalid"
    assert produced[0][1]["level"] == "warning"


def test_invalid_usage_is_noticed_once_per_run() -> None:
    projector = AcpProjector(SecretRedactor())
    first = _project(projector, {"sessionUpdate": "usage_update", "used": None, "size": 1})
    assert first[0][1]["message"] == "acp_usage_invalid"
    assert _project(projector, {"sessionUpdate": "usage_update", "used": -5, "size": 1}) == []


@pytest.mark.parametrize(
    "cost",
    [
        {"amount": 0.1},  # currency missing
        {"amount": 0.1, "currency": ""},  # currency empty
        {"amount": "0.1", "currency": "USD"},  # amount not a number
        {"amount": True, "currency": "USD"},  # a bool is not an amount
        "not-a-mapping",
    ],
)
def test_usage_cost_must_be_a_complete_amount_currency_pair(cost: Any) -> None:
    projector = AcpProjector(SecretRedactor())
    produced = _project(
        projector, {"sessionUpdate": "usage_update", "used": 1, "size": 2, "cost": cost}
    )
    assert [kind for kind, _ in produced] == [events.KERNEL_STATUS, events.USAGE_REPORTED]
    assert produced[0][1]["message"] == "acp_usage_cost_invalid"
    # The counters are still projected; the invalid cost is dropped from both views.
    assert "cost" not in produced[1][1]["usage"]
    assert "cost" not in produced[1][1]["requestUsage"]


def test_invalid_cost_is_noticed_once_but_usage_keeps_flowing() -> None:
    projector = AcpProjector(SecretRedactor())
    _project(
        projector,
        {"sessionUpdate": "usage_update", "used": 1, "size": 2, "cost": {"amount": "x"}},
    )
    second = _project(
        projector,
        {"sessionUpdate": "usage_update", "used": 2, "size": 2, "cost": {"amount": "y"}},
    )
    assert [kind for kind, _ in second] == [events.USAGE_REPORTED]


# -- vocabulary parity with the pi family -------------------------------------------


def test_stop_reason_vocabulary_is_the_protocol_v1_set() -> None:
    assert ACP_STOP_REASONS == frozenset(
        {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}
    )


def test_every_emitted_name_is_the_shared_product_vocabulary() -> None:
    """The browser keeps one event contract across harnesses: nothing the ACP
    projector emits may be a name the pi family does not also own."""
    projector = AcpProjector(SecretRedactor())
    frames: list[Mapping[str, Any]] = [
        {"type": "acp_turn_started"},
        _notification(_chunk("答")),
        _notification(_chunk("想", "agent_thought_chunk")),
        _notification({"sessionUpdate": "plan", "entries": []}),
        _notification(
            {"sessionUpdate": "tool_call", "toolCallId": "c", "name": "bash", "status": "pending"}
        ),
        _notification(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "c",
                "status": "completed",
                "content": [{"type": "content", "content": {"type": "text", "text": "ok"}}],
            }
        ),
        _notification({"sessionUpdate": "usage_update", "used": 1, "size": 2}),
        _notification({"sessionUpdate": "available_commands_update", "availableCommands": []}),
        _notification({"sessionUpdate": "current_mode_update", "currentModeId": "code"}),
        _notification(
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "image", "data": "eA=="}}
        ),
        {"type": "acp_permission", "decision": "rejected"},
        {"type": "acp_session_resumed", "method": "session/resume"},
        {"type": "acp_prompt_result", "stopReason": "end_turn"},
        {"type": "acp_turn_finished"},
    ]
    emitted: list[str] = []
    for frame in frames:
        emitted.extend(kind for kind, _ in projector.project(frame))
    vocabulary = {
        value for name, value in vars(events).items() if name.isupper() and isinstance(value, str)
    }
    assert emitted  # the drive actually produced events
    assert set(emitted) <= vocabulary
    # The chunk mapping targets the pi family's delta events, not ACP-only names.
    assert MESSAGE_CHUNK_UPDATES == {
        "agent_message_chunk": events.ASSISTANT_DELTA,
        "agent_thought_chunk": events.THINKING_DELTA,
    }


def test_tool_payloads_keep_the_pi_family_shape() -> None:
    """Same card, same keys: the ACP tool payload is the pi payload's shape
    (plus the ACP-only ``kind`` card content on start)."""
    acp = AcpProjector(SecretRedactor())
    raw_input = {"command": "ls -la"}
    acp_started = _project(
        acp,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "c",
            "name": "bash",
            "kind": "execute",
            "status": "pending",
            "rawInput": raw_input,
        },
    )[0][1]
    pi_started = pi_projection.project_event(
        {
            "type": "tool_execution_start",
            "toolCallId": "c",
            "toolName": "bash",
            "args": raw_input,
        }
    )[0][1]
    assert set(pi_started) <= set(acp_started)
    assert set(acp_started) - set(pi_started) == {"kind"}
    assert acp_started["args"] == pi_started["args"]

    acp_updated = _project(
        acp,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "c",
            "status": "in_progress",
            "content": [{"type": "content", "content": {"type": "text", "text": "o"}}],
        },
    )[0][1]
    pi_updated = pi_projection.project_event(
        {
            "type": "tool_execution_update",
            "toolCallId": "c",
            "toolName": "bash",
            "partialResult": "o",
        }
    )[0][1]
    assert set(acp_updated) == set(pi_updated) == {"toolCallId", "toolName", "output"}

    acp_finished = _project(
        acp,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "c",
            "status": "failed",
            "content": [{"type": "content", "content": {"type": "text", "text": "boom"}}],
        },
    )[0][1]
    pi_finished = pi_projection.project_event(
        {
            "type": "tool_execution_end",
            "toolCallId": "c",
            "toolName": "bash",
            "isError": True,
            "result": "boom",
        }
    )[0][1]
    assert set(acp_finished) == set(pi_finished) == {
        "toolCallId",
        "toolName",
        "isError",
        "result",
    }
    assert acp_finished["isError"] == pi_finished["isError"] is True
