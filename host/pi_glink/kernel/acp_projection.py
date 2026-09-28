"""Translate ACP notifications into the product event stream.

The mapping is the table fixed in ``docs/multi-harness-plan.md`` §4.1; this
module is its only implementation. Two rules from that document shape the code:

* **Nothing is approximated.** An ``usage_update`` is a context-window snapshot
  (``used``/``size``) plus a cumulative session cost, which is *not* pi's
  per-response token accounting. The conversion keeps the ACP semantics and
  labels them with a ``basis`` marker instead of inventing per-request token
  numbers; a variant the table does not map becomes a bounded
  ``kernel_status`` notice rather than a guessed product event.
* **The product vocabulary does not change.** The projector emits exactly the
  names in :mod:`pi_glink.events`, so the browser keeps one event contract
  across harnesses.

Streamed text (``agent_message_chunk`` / ``agent_thought_chunk``) goes through
the same :class:`~pi_glink.redaction.StreamRedactor` the pi projector uses, so a
credential split across two ACP chunks is withheld exactly like one split
across two pi deltas.

The projector is stateful for the length of one run. The adapter delivers four
synthetic frames this module translates: ``acp_turn_started`` /
``acp_turn_finished`` (the prompt turn's boundaries — ACP has no update for
them, but the product's stream needs them to close and render the streamed
blocks), ``acp_prompt_result`` (``session/prompt``'s answer) and
``acp_permission`` (the fail-closed permission answer). Everything else is a
real ``session/update`` notification. Keeping the translation here lets the
runner's event path stay harness-blind.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from .. import events
from ..redaction import SecretRedactor, StreamRedactor
from .projection import (
    checked,
    SUBAGENT_TOOL_NAME,
    extract_text,
    project_args,
    subagent_started_payload,
)

#: ACP ``StopReason`` values (protocol v1 schema). The runner translates these
#: into the single product verdict table; an unknown value fails closed there.
ACP_STOP_REASONS = frozenset(
    {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}
)

#: Chunk updates whose text streams into a product delta event.
MESSAGE_CHUNK_UPDATES = {
    "agent_message_chunk": events.ASSISTANT_DELTA,
    "agent_thought_chunk": events.THINKING_DELTA,
}
#: Product delta events whose split-secret protection needs stream state.
_STREAM_EVENTS = (events.THINKING_DELTA, events.ASSISTANT_DELTA)
#: ``tool_call``/``tool_call_update`` statuses that end a tool call.
_TERMINAL_TOOL_STATUS = frozenset({"completed", "failed"})

TOOL_TEXT_LIMIT = 4000
PLAN_ENTRY_LIMIT = 20
PLAN_CONTENT_LIMIT = 300
COMMAND_LIMIT = 20
COMMAND_NAME_LIMIT = 120
COMMAND_DESCRIPTION_LIMIT = 200
DETAIL_LIMIT = 500

# ACP's protocol categories describe *its* actions. Do not infer pi tool-name
# semantics from ACP titles (dsh reports kind=other even for named tools).
_ACP_KIND_PRESENTATION = {
    "read": "read", "edit": "edit_file", "delete": "delete",
    "move": "move", "search": "find", "execute": "execute",
    "think": "think", "fetch": "fetch_page",
    "switch_mode": "switch_mode", "other": "generic",
}


class AcpProjector:
    """Stateful projection of one ACP run's frames into product events.

    ``redactor`` is the run's own redaction boundary (host secrets plus the
    credentials that run may render), the same object the pi projector gets.
    """

    def __init__(self, redactor: SecretRedactor, *, subagent_dispatch: str = "sync") -> None:
        self._redactor = redactor
        #: The pack's declared ``subagent`` dispatch mode (manifest
        #: ``subagent_dispatch``). ``async`` (dsh): a ``completed`` dispatch
        #: call is only the acceptance — the child keeps running and is closed
        #: by the run's terminal frame (§3.4/§3.5); a ``failed`` dispatch call
        #: still ends the child as failed. ``sync``: the call's terminal status
        #: is the child's outcome. Declared by the pack, never inferred from
        #: an agent's reply wording.
        self._async_dispatch = subagent_dispatch == "async"
        self._stream = StreamRedactor(redactor)
        #: Tool id -> human name, so a later update without a name still shows one.
        self._tool_names: dict[str, str] = {}
        #: Notice keys already emitted (one notice per kind, never a flood).
        self._noted: set[str] = set()
        #: toolCallId -> the child ``n`` values that call dispatched (typed
        #: subagent lifecycle, docs/architecture.md §3.5). An ACP dispatch is
        #: one child per call (dsh's ``{description, prompt}`` args), so every
        #: list here holds exactly one ``n``; the entry is released when the
        #: call ends, and the map dies with the projector at run end.
        self._subagent_calls: dict[str, list[int]] = {}
        #: Next child sequence number (``n``) within this run, from 0 up.
        self._next_child_n = 0

    # -- the projection -------------------------------------------------------

    def project(self, frame: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        return checked(self._project(frame), "acp projection")

    def _project(self, frame: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        produced: list[tuple[str, dict[str, Any]]] = []
        frame_type = frame.get("type")
        if frame_type == "acp_turn_started":
            # The adapter's synthetic turn boundary: ACP has no update for it,
            # but the product's stream model needs it to close and render the
            # streamed blocks (front end stays harness-blind).
            return [(events.TURN_STARTED, {})]
        if frame_type == "acp_turn_finished":
            produced.extend(self.flush())
            produced.append((events.TURN_FINISHED, {}))
            return produced
        if frame_type == "acp_prompt_result":
            # A turn boundary: release the withheld delta tail before the
            # terminal judgement is recorded.
            produced.extend(self.flush())
            return produced
        if frame_type == "acp_session_resumed":
            # The adapter reached an existing session instead of creating one
            # (the follow-up path). Record the fact and, for the replaying
            # method, how much of the parent's already-recorded history was
            # dropped instead of projected a second time.
            method = str(frame.get("method") or "")
            replayed = frame.get("replayedUpdates")
            detail = f"ACP 追问续接到已有会话（method={method or 'unknown'}）"
            if isinstance(replayed, int) and replayed > 0:
                detail += (
                    f"；session/load 回放了 {replayed} 条历史更新，"
                    "它们属于父 run 已记录的历史，不重复投影"
                )
            payload: dict[str, Any] = {
                "level": "info",
                "message": "acp_session_resumed",
                "detail": detail,
                "resumeMethod": method,
            }
            if isinstance(frame.get("sessionId"), str):
                payload["sessionId"] = frame["sessionId"]
            if isinstance(replayed, int):
                payload["replayedUpdates"] = replayed
            # sessionId/method are agent-controlled identifiers: the payload
            # leaves through the same redaction boundary as every other path.
            return self._redact_all([(events.KERNEL_STATUS, payload)])
        if frame_type == "acp_permission":
            produced.extend(self.flush())
            produced.append((events.KERNEL_STATUS, self._permission_notice(frame)))
            return self._redact_all(produced)
        if frame.get("method") != "session/update":
            return produced
        params = frame.get("params")
        update = params.get("update") if isinstance(params, Mapping) else None
        if not isinstance(update, Mapping):
            return produced
        kind = update.get("sessionUpdate")
        if not isinstance(kind, str) or not kind:
            return produced
        stream_event = MESSAGE_CHUNK_UPDATES.get(kind)
        if stream_event is not None:
            text, unsupported = _chunk_text(update)
            if unsupported:
                produced.extend(
                    self._note_once(
                        f"content:{unsupported}",
                        "warning",
                        "acp_content_not_projected",
                        f"{kind} 携带 type={unsupported} 的内容块；产品事件只表示文本",
                    )
                )
            released = self._stream.push(stream_event, text) if text else ""
            if released:
                produced.append((stream_event, {"text": released}))
            return produced
        handler = _UPDATE_HANDLERS.get(kind)
        produced.extend(self.flush())
        if handler is None:
            produced.extend(
                self._note_once(
                    kind,
                    "info",
                    "acp_update_not_projected",
                    f"session/update 类型 {kind} 没有对应的产品事件（§4.1 未定义），如实不投影",
                )
            )
            return self._redact_all(produced)
        produced.extend(handler(update, self))
        return self._redact_all(produced)

    def flush(self) -> list[tuple[str, dict[str, Any]]]:
        """Release every withheld delta tail (stream boundary or run end)."""
        released: list[tuple[str, dict[str, Any]]] = []
        for kind in _STREAM_EVENTS:
            text = self._stream.flush(kind)
            if text:
                released.append((kind, {"text": text}))
        return released

    # -- helpers --------------------------------------------------------------

    def _redact_all(
        self, produced: list[tuple[str, dict[str, Any]]]
    ) -> list[tuple[str, dict[str, Any]]]:
        return [
            (event_type, self._redactor.redact_payload(payload) or payload)
            for event_type, payload in produced
        ]

    def _note_once(
        self, key: str, level: str, message: str, detail: str
    ) -> list[tuple[str, dict[str, Any]]]:
        if key in self._noted:
            return []
        self._noted.add(key)
        return [
            (
                events.KERNEL_STATUS,
                {"level": level, "message": message, "detail": _clip(detail, DETAIL_LIMIT)},
            )
        ]

    def _permission_notice(self, frame: Mapping[str, Any]) -> dict[str, Any]:
        tool = frame.get("toolCall")
        tool = tool if isinstance(tool, Mapping) else {}
        tool_call_id = str(tool.get("toolCallId") or "")
        title = _clip(str(tool.get("title") or ""), COMMAND_NAME_LIMIT)
        decision = str(frame.get("decision") or "rejected")
        policy = str(frame.get("policy") or "fail_closed")
        option_id = str(frame.get("optionId") or "")
        option_kind = str(frame.get("optionKind") or "")
        tool_kind = str(frame.get("toolKind") or tool.get("kind") or "")
        # Where the kind came from: the request itself, or the ``tool_call``
        # notification the agent sent for the same ``toolCallId``. The
        # distinction is recorded because it is the fact that makes a
        # kind-based allowlist usable for agents that omit ``kind`` in the
        # permission request (measured in M4).
        tool_kind_source = str(
            frame.get("toolKindSource") or ("request" if tool_kind else "")
        )
        if decision == "allowed":
            # The run was explicitly given an allowlist; the answer is still
            # recorded (an allowed tool is a fact the operator must be able to
            # audit), just not as a refusal.
            message = "acp_permission_allowed"
            detail = f"ACP 权限请求按显式白名单应答：decision={decision}"
        else:
            message = "acp_permission_denied"
            detail = f"ACP 权限请求按 fail-closed 应答：decision={decision}"
        if title or tool_call_id:
            detail += f"；tool={title or tool_call_id}"
        if tool_kind:
            detail += f"；kind={tool_kind}"
            if tool_kind_source == "tool_call":
                detail += "（来自同一 toolCallId 的 tool_call 通知）"
        if option_id or option_kind:
            detail += f"；option={option_id}({option_kind})"
        payload: dict[str, Any] = {
            "level": "warning",
            "message": message,
            "detail": _clip(detail, DETAIL_LIMIT),
            "decision": decision,
            "policy": policy,
            "toolCallId": tool_call_id,
        }
        if title:
            payload["toolTitle"] = title
        if tool_kind:
            payload["toolKind"] = tool_kind
        if tool_kind_source:
            payload["toolKindSource"] = tool_kind_source
        if option_id:
            payload["optionId"] = option_id
        if option_kind:
            payload["optionKind"] = option_kind
        return payload


# -- update handlers ----------------------------------------------------------


def _agent_tool_call(
    update: Mapping[str, Any], projector: AcpProjector
) -> list[tuple[str, dict[str, Any]]]:
    tool_call_id = str(update.get("toolCallId") or "")
    name = _tool_name(update)
    if name:
        projector._tool_names[tool_call_id] = name
    if name == SUBAGENT_TOOL_NAME:
        task = _acp_subagent_task(update.get("rawInput"))
        if task is not None:
            n = projector._next_child_n
            projector._next_child_n += 1
            projector._subagent_calls[tool_call_id] = [n]
            # §3.5: an ACP dispatch carries no agent name in its args, so the
            # field stays honestly empty (取空，不伪造) — the pi family's
            # ``agent`` only exists when the dispatch parameters give one.
            started = subagent_started_payload(n, tool_call_id, {"task": task})
            produced = [(events.SUBAGENT_STARTED, started)]
            if update.get("status") in _TERMINAL_TOOL_STATUS and not _dispatch_accepted_only(
                update, projector
            ):
                produced.append(
                    (events.SUBAGENT_FINISHED, _subagent_finished_payload(update, projector))
                )
            return produced
        # A subagent-named call whose args do not match the dispatch shape
        # falls back to a generic card: no typed event is fabricated (§3.5).
    payload: dict[str, Any] = {
        "toolCallId": tool_call_id,
        "toolName": name,
        "args": project_args(update.get("rawInput")),
        "presentation": _ACP_KIND_PRESENTATION.get(update.get("kind"), "generic")
        if isinstance(update.get("kind"), str) else "generic",
    }
    kind = update.get("kind")
    if isinstance(kind, str) and kind:
        # The ACP tool category is card content (plan §4.1): keep it in the
        # record without forwarding the raw frame.
        payload["kind"] = kind
    produced = [(events.TOOL_STARTED, payload)]
    if update.get("status") in _TERMINAL_TOOL_STATUS:
        produced.append((events.TOOL_FINISHED, _tool_finished_payload(update, projector)))
    return produced


def _dispatch_accepted_only(update: Mapping[str, Any], projector: AcpProjector) -> bool:
    """An async pack's successful dispatch call: accepted, not finished."""
    return projector._async_dispatch and update.get("status") == "completed"


def _agent_tool_update(
    update: Mapping[str, Any], projector: AcpProjector
) -> list[tuple[str, dict[str, Any]]]:
    tool_call_id = str(update.get("toolCallId") or "")
    if tool_call_id in projector._subagent_calls:
        if update.get("status") in _TERMINAL_TOOL_STATUS:
            if _dispatch_accepted_only(update, projector):
                # 异步派发被接受：不是完成，也不释放 call 状态（后续占位更新
                # 继续抑制）；child 由 run 终帧收口。
                return []
            return [
                (events.SUBAGENT_FINISHED, _subagent_finished_payload(update, projector))
            ]
        # A subagent call only ever reports placeholder progress; the typed
        # lifecycle replaces the generic card's updates (§3.5).
        return []
    name = _tool_name(update) or projector._tool_names.get(tool_call_id, "")
    if name:
        projector._tool_names[tool_call_id] = name
    if update.get("status") in _TERMINAL_TOOL_STATUS:
        return [(events.TOOL_FINISHED, _tool_finished_payload(update, projector))]
    return [
        (
            events.TOOL_UPDATED,
            {
                "toolCallId": tool_call_id,
                "toolName": name,
                "output": _clip(_tool_text(update), TOOL_TEXT_LIMIT),
            },
        )
    ]


def _agent_plan(
    update: Mapping[str, Any], _projector: AcpProjector
) -> list[tuple[str, dict[str, Any]]]:
    entries: list[dict[str, str]] = []
    source = update.get("entries")
    if isinstance(source, (list, tuple)):
        for item in list(source)[:PLAN_ENTRY_LIMIT]:
            if not isinstance(item, Mapping):
                continue
            entries.append(
                {
                    "content": _clip(str(item.get("content") or ""), PLAN_CONTENT_LIMIT),
                    "status": str(item.get("status") or ""),
                    "priority": str(item.get("priority") or ""),
                }
            )
    return [(events.RUN_STAGE, {"stage": "plan", "entries": entries})]


def _available_commands(
    update: Mapping[str, Any], _projector: AcpProjector
) -> list[tuple[str, dict[str, Any]]]:
    source = update.get("availableCommands")
    commands: list[dict[str, str]] = []
    total = 0
    if isinstance(source, (list, tuple)):
        total = len(source)
        for item in list(source)[:COMMAND_LIMIT]:
            if not isinstance(item, Mapping):
                continue
            commands.append(
                {
                    "name": _clip(str(item.get("name") or ""), COMMAND_NAME_LIMIT),
                    "description": _clip(
                        str(item.get("description") or ""), COMMAND_DESCRIPTION_LIMIT
                    ),
                }
            )
    return [
        (
            events.KERNEL_STATUS,
            {
                "level": "warning",
                "message": "acp_available_commands",
                "detail": (
                    f"ACP agent 声明了 {total} 个斜杠命令；本宿主对 ACP 族一律拒绝以 / 开头的 prompt"
                ),
                "commands": commands,
            },
        )
    ]


def _usage_update(
    update: Mapping[str, Any], projector: AcpProjector
) -> list[tuple[str, dict[str, Any]]]:
    used = update.get("used")
    size = update.get("size")
    if not _is_count(used) or not _is_count(size):
        return projector._note_once(
            "usage:invalid",
            "warning",
            "acp_usage_invalid",
            "usage_update 缺少合法的 used/size 计数，本次用量未投影",
        )
    # ACP reports the tokens *currently in context* and a cumulative session
    # cost. The product's pi-shaped usage is a per-request sum, so the ACP view
    # keeps its own fields and says which basis it is.
    usage: dict[str, Any] = {
        "totalTokens": used,
        "contextSize": size,
        "basis": "acp_usage_update",
    }
    request_usage: dict[str, Any] = {"used": used, "size": size}
    produced: list[tuple[str, dict[str, Any]]] = []
    cost = update.get("cost")
    if cost is not None:
        if (
            isinstance(cost, Mapping)
            and _is_amount(cost.get("amount"))
            and isinstance(cost.get("currency"), str)
            and cost.get("currency")
        ):
            reported = {"amount": cost["amount"], "currency": cost["currency"]}
            usage["cost"] = reported
            request_usage["cost"] = dict(reported)
        else:
            produced.extend(
                projector._note_once(
                    "usage:cost",
                    "warning",
                    "acp_usage_cost_invalid",
                    "usage_update 的 cost 不是 {amount, currency}，本次成本未投影",
                )
            )
    produced.append(
        (events.USAGE_REPORTED, {"usage": usage, "requestUsage": request_usage})
    )
    return produced


def _tool_name(update: Mapping[str, Any]) -> str:
    """The best available tool label: programmatic name, title, then kind.

    ACP's ``title`` is the human-readable description and is always present;
    ``name`` is the unstable programmatic field; ``kind`` is only a category
    (dsh sends ``kind: "other"`` for every tool and puts the actual tool name
    in ``title``). The product card keys on a name-like string, so the most
    programmatic value wins, the title is the practical fallback, and the
    category is the last resort that at least says what sort of tool ran.
    """
    for key in ("name", "title", "kind"):
        value = update.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _acp_subagent_task(raw_input: Any) -> str | None:
    """The task text of an ACP subagent dispatch, or ``None`` to fall back.

    dsh's measured dispatch args are ``{description, prompt}`` — one child per
    call (``docs/evidence/acp-subagent-spike.txt``): ``description`` is the
    short task label, ``prompt`` the full sub-prompt. The description wins; the
    prompt is the fallback when no description was given. Anything else means
    the shape was not a dispatch we can describe honestly, so the caller
    renders a generic tool card instead of fabricating a typed event (§3.5).
    """
    if not isinstance(raw_input, Mapping):
        return None
    for key in ("description", "prompt"):
        value = raw_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _subagent_finished_payload(
    update: Mapping[str, Any], projector: AcpProjector
) -> dict[str, Any]:
    """The ``subagent_finished`` payload (§3.5) closing one ACP dispatch.

    One terminal update closes every child the call dispatched (exactly one on
    this family); the call id is never reused, so its per-run state is released
    here. ``resultPreview`` is the bounded whole-call summary — it is not split
    per child.
    """
    tool_call_id = str(update.get("toolCallId") or "")
    ns = projector._subagent_calls.pop(tool_call_id, [])
    is_error = update.get("status") == "failed"
    return {
        "toolCallId": tool_call_id,
        "ns": list(ns),
        "status": "failed" if is_error else "finished",
        "isError": is_error,
        "resultPreview": _clip(_tool_text(update), TOOL_TEXT_LIMIT),
    }


def _tool_finished_payload(
    update: Mapping[str, Any], projector: AcpProjector
) -> dict[str, Any]:
    tool_call_id = str(update.get("toolCallId") or "")
    name = _tool_name(update) or projector._tool_names.get(tool_call_id, "")
    return {
        "toolCallId": tool_call_id,
        "toolName": name,
        "isError": update.get("status") == "failed",
        "result": _clip(_tool_text(update), TOOL_TEXT_LIMIT),
    }


def _tool_text(update: Mapping[str, Any]) -> str:
    """The tool call's result as display text.

    ``tool_call_update`` replaces the whole ``content`` collection, so the
    joined text is the accumulated output the product card expects. A terminal
    update with an empty collection falls back to ``rawOutput``, which is
    arbitrary JSON and therefore rendered compactly rather than dropped.
    """
    content = update.get("content")
    parts: list[str] = []
    if isinstance(content, (list, tuple)):
        for item in content:
            text = _tool_content_text(item)
            if text:
                parts.append(text)
    if parts:
        return "\n".join(parts)
    return _raw_text(update.get("rawOutput"))


def _tool_content_text(item: Any) -> str:
    if not isinstance(item, Mapping):
        return extract_text(item)
    kind = item.get("type")
    if kind == "content":
        return extract_text(item.get("content"))
    if kind == "diff":
        path = item.get("path")
        head = f"diff {path}" if isinstance(path, str) and path else "diff"
        new_text = item.get("newText")
        body = new_text if isinstance(new_text, str) else ""
        return f"{head}\n{body}".strip()
    if kind == "terminal":
        return f"[terminal {item.get('terminalId')}]"
    return ""


def _raw_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return extract_text(value)


def _chunk_text(update: Mapping[str, Any]) -> tuple[str, str]:
    """``(text, unsupported_content_type)`` for one content-chunk update.

    A text block yields its ``text``; any other content block yields no text
    plus the block's type, so the caller can record that the product event
    cannot represent it (images, resources …) instead of dropping it silently.
    """
    content = update.get("content")
    if not isinstance(content, Mapping):
        return "", ""
    block_type = content.get("type")
    if block_type == "text":
        text = content.get("text")
        return (text if isinstance(text, str) else ""), ""
    return "", str(block_type or "unknown")


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_amount(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n…[truncated {len(text) - limit} chars]"


_HANDLER = Callable[
    [Mapping[str, Any], AcpProjector], list[tuple[str, dict[str, Any]]]
]
_UPDATE_HANDLERS: dict[str, _HANDLER] = {
    "tool_call": _agent_tool_call,
    "tool_call_update": _agent_tool_update,
    "plan": _agent_plan,
    "available_commands_update": _available_commands,
    "usage_update": _usage_update,
}
