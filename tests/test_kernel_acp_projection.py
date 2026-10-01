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

from glink_harness import events
from glink_harness.kernel import projection as pi_projection
from glink_harness.kernel.acp_projection import (
    ACP_STOP_REASONS,
    COMMAND_LIMIT,
    COMMAND_NAME_LIMIT,
    MESSAGE_CHUNK_UPDATES,
    PLAN_CONTENT_LIMIT,
    PLAN_ENTRY_LIMIT,
    TOOL_TEXT_LIMIT,
    AcpProjector,
)
from glink_harness.redaction import SecretRedactor


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


def test_state_channel_degraded_is_a_visible_warning() -> None:
    """归档通道降级（0018）：可见 warning，不静默。"""
    projector = AcpProjector(SecretRedactor())
    produced = projector.project(
        {
            "type": "acp_state_channel_degraded",
            "detail": "归档通道回收失败，本轮已降级为 files 逐文件回收（1 个文件，"
            "9 字节）；追问仍可续接",
        }
    )
    assert [kind for kind, _ in produced] == [events.KERNEL_STATUS]
    payload = produced[0][1]
    assert payload["level"] == "warning"
    assert payload["message"] == "acp_state_channel_degraded"
    assert "降级" in payload["detail"]


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


def test_present_declares_files_without_displacing_ordinary_tool_events() -> None:
    projector = AcpProjector(SecretRedactor(["secret-value"]))
    produced = _project(projector, {
        "sessionUpdate": "tool_call", "toolCallId": "present-1", "name": "present",
        "kind": "other", "status": "pending",
        "rawInput": {"files": [{"path": "memo.md", "description": "secret-value 摘要"}]},
    })
    assert [kind for kind, _ in produced] == [events.TOOL_STARTED, events.DELIVERABLES_DECLARED]
    assert produced[0][1]["presentation"] == "deliver"
    assert produced[1][1] == {"files": [{"path": "memo.md", "description": "[REDACTED] 摘要"}]}
    assert _project(projector, {"sessionUpdate": "tool_call", "toolCallId": "x",
                                 "name": "present", "rawInput": {"files": "bad"}})[0][0] == events.TOOL_STARTED


def test_present_absolute_paths_under_the_workspace_become_relative() -> None:
    """dsh 实测交付沙箱绝对路径（GLINK-DELIVER-ABS-20261001）：归一为相对路径。

    落在本 run 工作区根下的绝对路径按契约改写为工作区相对路径；根外的绝对
    路径原样保留，由 artifacts 按契约拒绝（不静默改写语义）。
    """
    projector = AcpProjector(SecretRedactor(), workspace_prefix="/home/user/workspace")
    produced = _project(projector, {
        "sessionUpdate": "tool_call", "toolCallId": "present-1", "name": "present",
        "kind": "other", "status": "pending",
        "rawInput": {"files": [
            {"path": "/home/user/workspace/notes.md", "description": "沙箱绝对路径"},
            {"path": "/home/user/workspace/sub/report.md"},
            {"path": "relative.md"},
            {"path": "/etc/passwd"},
            {"path": "/home/user/workspace2/sibling"},
        ]},
    })
    assert [kind for kind, _ in produced] == [events.TOOL_STARTED, events.DELIVERABLES_DECLARED]
    assert [f["path"] for f in produced[1][1]["files"]] == [
        "notes.md", "sub/report.md", "relative.md", "/etc/passwd", "/home/user/workspace2/sibling",
    ]


def test_present_paths_pass_through_without_a_workspace_prefix() -> None:
    """未绑定工作区前缀时行为不变（旧调用形态兼容）。"""
    projector = AcpProjector(SecretRedactor())
    produced = _project(projector, {
        "sessionUpdate": "tool_call", "toolCallId": "present-1", "name": "present",
        "kind": "other", "status": "pending",
        "rawInput": {"files": [{"path": "/abs/notes.md"}]},
    })
    assert produced[1][1]["files"][0]["path"] == "/abs/notes.md"




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
        "presentation": "execute",
    }
    assert produced[1][1] == {
        "toolCallId": "call_1",
        "toolName": "bash",
        "isError": False,
        "result": "done",
    }


def test_dsh_tool_names_override_other_kind_without_guessing_unknown_tools() -> None:
    projector = AcpProjector(SecretRedactor())
    hints = {
        "web_search": "search_web", "web_fetch": "fetch_page",
        "todo_write": "plan", "write": "write_file", "read": "read_file",
        "edit": "edit_file", "bash": "execute",
        "list_agents": "manage_children", "job_list": "manage_children",
        "send_message": "message_child", "interrupt_agent": "stop_child",
        "unknown_dsh_tool": "generic",
    }
    for index, (name, hint) in enumerate(hints.items()):
        started = _project(projector, {
            "sessionUpdate": "tool_call", "toolCallId": f"c{index}",
            "title": name, "kind": "other", "rawInput": {},
        })
        assert started[0][1]["presentation"] == hint
        assert started[0][1]["toolName"] == name
        assert started[0][1]["kind"] == "other"


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


# -- typed subagent projection (docs/architecture.md §3.5) ----------------------
#
# dsh exposes a tool named ``subagent`` whose dispatch args are
# ``{description, prompt}`` — one child per call (acp-subagent-spike.txt). The
# typed lifecycle mirrors the pi family: ``subagent_started`` replaces the
# generic card, progress updates are suppressed, and the terminal update closes
# the child with ``subagent_finished``. The ACP args carry no agent name, so
# the ``agent`` field stays empty rather than being invented (§3.5 取空规则).


def _subagent_call(
    tool_call_id: str = "sub-1",
    raw_input: Any = None,
    status: str = "pending",
    **update_fields: Any,
) -> dict[str, Any]:
    update: dict[str, Any] = {
        "sessionUpdate": "tool_call",
        "toolCallId": tool_call_id,
        "name": "subagent",
        "status": status,
        "rawInput": {"description": "法域 A 取证", "prompt": "完整子提示"}
        if raw_input is None
        else raw_input,
    }
    update.update(update_fields)
    return update


def test_subagent_dispatch_projects_a_typed_started_event() -> None:
    projector = AcpProjector(SecretRedactor())
    produced = _project(projector, _subagent_call())
    assert produced == [
        (
            events.SUBAGENT_STARTED,
            {"n": 0, "toolCallId": "sub-1", "agent": "", "task": "法域 A 取证"},
        )
    ]


def test_subagent_dispatch_recognised_by_title_when_name_is_absent() -> None:
    """dsh puts the actual tool name in ``title`` (kind is ``other`` for every
    tool): the typed rule keys on the same resolved label as the generic card."""
    projector = AcpProjector(SecretRedactor())
    call = _subagent_call()
    del call["name"]
    call["title"] = "subagent"
    call["kind"] = "other"
    produced = _project(projector, call)
    assert [kind for kind, _ in produced] == [events.SUBAGENT_STARTED]


def test_subagent_task_falls_back_to_the_prompt_without_a_description() -> None:
    projector = AcpProjector(SecretRedactor())
    produced = _project(
        projector, _subagent_call(raw_input={"prompt": "完整子提示"}),
    )
    assert produced[0][1]["task"] == "完整子提示"


def test_subagent_updates_are_suppressed_and_the_terminal_update_finishes() -> None:
    projector = AcpProjector(SecretRedactor())
    _project(projector, _subagent_call())
    running = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "sub-1",
            "status": "in_progress",
            "content": [{"type": "content", "content": {"type": "text", "text": "(running...)"}}],
        },
    )
    assert running == [], "子代理调用的进度占位一律抑制（§3.5）"
    finished = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "sub-1",
            "status": "completed",
            "content": [{"type": "content", "content": {"type": "text", "text": "子代理结论"}}],
        },
    )
    assert finished == [
        (
            events.SUBAGENT_FINISHED,
            {
                "toolCallId": "sub-1",
                "ns": [0],
                "status": "finished",
                "isError": False,
                "resultPreview": "子代理结论",
            },
        )
    ]


def test_async_pack_dispatch_acceptance_does_not_finish_the_child() -> None:
    """声明 ``subagent_dispatch="async"`` 的包（dsh）：completed 只是派发被接受。

    真实 dsh r1（GLINK-OBS-01）：工具终态与子代理会话创建相差 17ms，子代理会话随后
    继续运行约 5 分钟；若在这里发 ``subagent_finished``，产品事件就与会话事实不一致。
    原义务（OBS-01 初版）：宿主用正则识别 dsh 的 ``started subagent <id>`` 回复文本；
    新义务：由包清单声明派发语义，投影不读 agent 的措辞。
    """
    projector = AcpProjector(SecretRedactor(), subagent_dispatch="async")
    _project(projector, _subagent_call())
    ack = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "sub-1",
            "status": "completed",
            "content": [
                {
                    "type": "content",
                    "content": {
                        "type": "text",
                        "text": "started subagent 0c8309aa-9469-45f1-9d8f-6c47b58fe4b5",
                    },
                }
            ],
        },
    )
    assert ack == [], "派发确认不能冒充子代理完成"
    # 确认之后迟到的占位更新继续抑制，不得回退成通用工具卡片。
    late = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "sub-1",
            "status": "in_progress",
            "content": [
                {"type": "content", "content": {"type": "text", "text": "(running...)"}}
            ],
        },
    )
    assert late == []


def test_async_pack_dispatch_completed_on_arrival_emits_started_only() -> None:
    """同一次通知里就已完成的异步派发也只发 started。"""
    projector = AcpProjector(SecretRedactor(), subagent_dispatch="async")
    produced = _project(
        projector,
        _subagent_call(
            status="completed",
            content=[
                {
                    "type": "content",
                    "content": {
                        "type": "text",
                        "text": "Started subagent child-1",
                    },
                }
            ],
        ),
    )
    assert [kind for kind, _ in produced] == [events.SUBAGENT_STARTED]


def test_a_sync_pack_is_not_fooled_by_dispatch_wording() -> None:
    """未声明 async 的包：即使回复文本长得像 dsh 的派发确认，终态仍是完成。

    证明判定来自包声明而不是回复措辞（措辞变化不能改变语义，反之亦然）。
    """
    projector = AcpProjector(SecretRedactor())
    _project(projector, _subagent_call())
    produced = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "sub-1",
            "status": "completed",
            "content": [
                {"type": "content", "content": {"type": "text", "text": "started subagent x"}}
            ],
        },
    )
    assert [kind for kind, _ in produced] == [events.SUBAGENT_FINISHED]


def test_async_pack_failed_dispatch_still_ends_the_child_as_failed() -> None:
    """async 包的派发被拒（failed）不是「已接受」：child 以失败收口。"""
    projector = AcpProjector(SecretRedactor(), subagent_dispatch="async")
    _project(projector, _subagent_call())
    produced = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "sub-1",
            "status": "failed",
            "content": [
                {"type": "content", "content": {"type": "text", "text": "dispatch refused"}}
            ],
        },
    )
    assert [kind for kind, _ in produced] == [events.SUBAGENT_FINISHED]
    assert produced[0][1]["status"] == "failed"


def test_synchronous_acp_subagent_completion_still_finishes() -> None:
    """同步派发包（未声明 async，其他 ACP agent）的终态保持原义务：收口该 call。"""
    projector = AcpProjector(SecretRedactor())
    _project(projector, _subagent_call())
    produced = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "sub-1",
            "status": "completed",
            "content": [
                {"type": "content", "content": {"type": "text", "text": "子代理结论"}}
            ],
        },
    )
    assert [kind for kind, _ in produced] == [events.SUBAGENT_FINISHED]


def test_subagent_failed_status_marks_the_finished_event() -> None:
    projector = AcpProjector(SecretRedactor())
    _project(projector, _subagent_call())
    (_kind, payload), = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "sub-1",
            "status": "failed",
            "content": [{"type": "content", "content": {"type": "text", "text": "boom"}}],
        },
    )
    assert payload["status"] == "failed"
    assert payload["isError"] is True
    assert payload["resultPreview"] == "boom"


def test_subagent_terminal_on_arrival_emits_started_then_finished() -> None:
    projector = AcpProjector(SecretRedactor())
    produced = _project(
        projector,
        _subagent_call(
            status="completed",
            content=[{"type": "content", "content": {"type": "text", "text": "done"}}],
        ),
    )
    assert [kind for kind, _ in produced] == [events.SUBAGENT_STARTED, events.SUBAGENT_FINISHED]
    assert produced[0][1]["n"] == 0
    assert produced[1][1]["ns"] == [0]
    assert produced[1][1]["resultPreview"] == "done"


def test_subagent_n_increments_across_dispatches() -> None:
    projector = AcpProjector(SecretRedactor())
    first = _project(projector, _subagent_call(tool_call_id="sub-1"))
    second = _project(
        projector, _subagent_call(tool_call_id="sub-2", raw_input={"description": "法域 B 取证"})
    )
    assert first[0][1]["n"] == 0
    assert second[0][1]["n"] == 1, "n 是 run 内单调分配的序号，不按调用重置"
    finished = _project(
        projector,
        {"sessionUpdate": "tool_call_update", "toolCallId": "sub-2", "status": "completed"},
    )
    assert finished[0][1]["ns"] == [1]


@pytest.mark.parametrize(
    "raw_input",
    [
        None,
        "plain-string",
        {},  # neither description nor prompt
        {"description": ""},  # empty strings cannot label a child
        {"task": "pi 族字段名"},  # the pi family's shape, not dsh's
        {"description": 42},
    ],
)
def test_malformed_dispatch_args_fall_back_to_a_generic_card(raw_input: Any) -> None:
    """args 形状不符时回退普通工具卡片（防御，不伪造类型事件），且该 toolCallId
    不进入子代理登记表：后续 update 仍按通用卡片流动。"""
    projector = AcpProjector(SecretRedactor())
    update = _subagent_call(tool_call_id="sub-9")
    update["rawInput"] = raw_input
    produced = _project(projector, update)
    assert [kind for kind, _ in produced] == [events.TOOL_STARTED]
    assert produced[0][1]["toolName"] == "subagent"
    running = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "sub-9",
            "status": "in_progress",
            "content": [{"type": "content", "content": {"type": "text", "text": "x"}}],
        },
    )
    assert [kind for kind, _ in running] == [events.TOOL_UPDATED], "未登记的调用不抑制 update"


def test_subagent_payloads_pass_through_the_run_redactor() -> None:
    """类型事件载荷与现有投影同一道 redactor：task 与结果里的凭据值被脱敏。"""
    secret = "opaque-token-value-99"
    projector = AcpProjector(SecretRedactor([secret]))
    started = _project(
        projector, _subagent_call(raw_input={"description": f"用 {secret} 取证"})
    )
    assert started[0][1]["task"] == "用 [REDACTED] 取证"
    finished = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "sub-1",
            "status": "completed",
            "content": [
                {"type": "content", "content": {"type": "text", "text": f"回显 {secret}"}}
            ],
        },
    )
    assert finished[0][1]["resultPreview"] == "回显 [REDACTED]"


def test_non_subagent_calls_are_untouched_by_a_dispatch_in_the_same_run() -> None:
    """普通工具调用的投影逐点不变：与子代理派发交错时仍走通用卡片。"""
    projector = AcpProjector(SecretRedactor())
    _project(projector, _subagent_call())
    started = _project(
        projector,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "c1",
            "name": "bash",
            "status": "pending",
            "rawInput": {"command": "ls"},
        },
    )
    assert [kind for kind, _ in started] == [events.TOOL_STARTED]
    assert started[0][1]["toolName"] == "bash"
    finished = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "c1",
            "status": "completed",
            "content": [{"type": "content", "content": {"type": "text", "text": "done"}}],
        },
    )
    assert [kind for kind, _ in finished] == [events.TOOL_FINISHED]
