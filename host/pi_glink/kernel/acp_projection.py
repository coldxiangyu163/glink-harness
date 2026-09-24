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

The projector is stateful for the length of one run. ``session/prompt``'s
answer is delivered by the adapter as an ``acp_prompt_result`` frame;
``session/request_permission``'s fail-closed answer as an ``acp_permission``
frame. Both are translated here so the runner's event path stays harness-blind.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Mapping

from .. import events
from ..redaction import SecretRedactor, StreamRedactor
from .projection import _project_args, extract_text

logger = logging.getLogger("pi_glink.kernel.acp_projection")

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


class AcpProjector:
    """Stateful projection of one ACP run's frames into product events.

    ``redactor`` is the run's own redaction boundary (host secrets plus the
    credentials that run may render), the same object the pi projector gets.
    """

    def __init__(self, redactor: SecretRedactor) -> None:
        self._redactor = redactor
        self._stream = StreamRedactor(redactor)
        #: Tool id -> human name, so a later update without a name still shows one.
        self._tool_names: dict[str, str] = {}
        #: Notice keys already emitted (one notice per kind, never a flood).
        self._noted: set[str] = set()

    # -- the projection -------------------------------------------------------

    def project(self, frame: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        produced: list[tuple[str, dict[str, Any]]] = []
        frame_type = frame.get("type")
        if frame_type == "acp_prompt_result":
            # A turn boundary: release the withheld delta tail before the
            # terminal judgement is recorded.
            produced.extend(self.flush())
            return produced
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
        option_id = str(frame.get("optionId") or "")
        option_kind = str(frame.get("optionKind") or "")
        detail = f"ACP 权限请求按 fail-closed 应答：decision={decision}"
        if title or tool_call_id:
            detail += f"；tool={title or tool_call_id}"
        if option_id or option_kind:
            detail += f"；option={option_id}({option_kind})"
        payload: dict[str, Any] = {
            "level": "warning",
            "message": "acp_permission_denied",
            "detail": _clip(detail, DETAIL_LIMIT),
            "decision": decision,
            "toolCallId": tool_call_id,
        }
        if title:
            payload["toolTitle"] = title
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
    produced = [
        (
            events.TOOL_STARTED,
            {
                "toolCallId": tool_call_id,
                "toolName": name,
                "args": _project_args(update.get("rawInput")),
            },
        )
    ]
    if update.get("status") in _TERMINAL_TOOL_STATUS:
        produced.append((events.TOOL_FINISHED, _tool_finished_payload(update, projector)))
    return produced


def _agent_tool_update(
    update: Mapping[str, Any], projector: AcpProjector
) -> list[tuple[str, dict[str, Any]]]:
    tool_call_id = str(update.get("toolCallId") or "")
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
    """The best available tool label: programmatic name, kind, then title.

    ACP's ``title`` is human-readable and always present; ``name`` is the
    unstable programmatic field and ``kind`` the category. The product tool
    card keys on a stable name-like string, so the most programmatic value
    wins and the title is the fallback (not the other way round).
    """
    for key in ("name", "kind", "title"):
        value = update.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


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
