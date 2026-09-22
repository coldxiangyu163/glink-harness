"""Translate pi session events into the product event stream.

This is the only place that knows pi's wire vocabulary. Everything downstream
(the run bus, the SSE surface, the browser) speaks the product names defined in
:mod:`pi_glink.events`, so a pi protocol addition shows up as a change here and
nowhere else.

Design rules:

* **Never forward raw frames.** A payload is a small, explicit projection; the
  browser must not be able to depend on pi internals by accident.
* **Bound the payload.** Tool results and assistant text can be megabytes; they
  are clipped with an explicit ``truncated`` flag instead of blowing up the
  event stream.
* **One pi event may yield several product events; it may also yield none.**
  Lifecycle frames that the host owns (``run_started`` …) are not produced here.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .. import events

#: Characters kept from a tool result / partial output before clipping.
TOOL_TEXT_LIMIT = 4000
#: Characters kept from a single streaming delta (a delta is already small).
DELTA_LIMIT = 20000

# pi assistant-stream delta types -> product event type
_DELTA_EVENTS = {
    "text_delta": events.ASSISTANT_DELTA,
    "thinking_delta": events.THINKING_DELTA,
}


def project_event(
    frame: Mapping[str, Any], redactor: Any = None
) -> list[tuple[str, dict[str, Any]]]:
    """Return the product events for one pi frame (possibly empty).

    ``redactor`` is applied to every payload before it leaves this module, so a
    credential echoed by a tool (``env``, a config-file read, the model quoting
    one back) is masked in the event stream, the event log and the browser.
    """
    frame_type = frame.get("type")
    handler = _HANDLERS.get(frame_type)
    if handler is None:
        return []
    produced = handler(frame)
    if redactor is None or not produced:
        return produced
    return [
        (kind, redactor.redact_payload(payload) or payload) for kind, payload in produced
    ]


def _agent_start(_frame: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(events.RUN_STAGE, {"stage": "agent_start"})]


def _agent_end(frame: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            events.RUN_STAGE,
            {"stage": "agent_end", "willRetry": bool(frame.get("willRetry"))},
        )
    ]


def _turn_start(_frame: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(events.TURN_STARTED, {})]


def _turn_end(frame: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    produced: list[tuple[str, dict[str, Any]]] = [(events.TURN_FINISHED, {})]
    usage = _usage_from_message(frame.get("message"))
    if usage is not None:
        produced.append((events.USAGE_REPORTED, {"usage": usage}))
    return produced


def _message_update(frame: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    delta = frame.get("assistantMessageEvent")
    if not isinstance(delta, Mapping):
        return []
    event_type = _DELTA_EVENTS.get(str(delta.get("type")))
    if event_type is None:
        return []
    text = delta.get("delta")
    if not isinstance(text, str) or not text:
        return []
    return [(event_type, {"text": _clip_text(text, DELTA_LIMIT)})]


def _message_end(frame: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    message = frame.get("message")
    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        return []
    text = extract_text(message.get("content"))
    payload: dict[str, Any] = {"text": _clip_text(text, TOOL_TEXT_LIMIT * 4)}
    stop_reason = message.get("stopReason") or message.get("stop_reason")
    if isinstance(stop_reason, str):
        payload["stopReason"] = stop_reason
    usage = _usage_from_message(message)
    if usage is not None:
        payload["usage"] = usage
    return [(events.ASSISTANT_MESSAGE, payload)]


def _tool_execution_start(
    frame: Mapping[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            events.TOOL_STARTED,
            {
                "toolCallId": str(frame.get("toolCallId") or ""),
                "toolName": str(frame.get("toolName") or ""),
                "args": _project_args(frame.get("args")),
            },
        )
    ]


def _tool_execution_update(
    frame: Mapping[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            events.TOOL_UPDATED,
            {
                "toolCallId": str(frame.get("toolCallId") or ""),
                "toolName": str(frame.get("toolName") or ""),
                "output": _clip_text(extract_text(frame.get("partialResult")), TOOL_TEXT_LIMIT),
            },
        )
    ]


def _tool_execution_end(frame: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    result_text = extract_text(frame.get("result"))
    return [
        (
            events.TOOL_FINISHED,
            {
                "toolCallId": str(frame.get("toolCallId") or ""),
                "toolName": str(frame.get("toolName") or ""),
                "isError": bool(frame.get("isError")),
                "result": _clip_text(result_text, TOOL_TEXT_LIMIT),
            },
        )
    ]


def _compaction_start(frame: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            events.RUN_STAGE,
            {"stage": "compaction_start", "reason": str(frame.get("reason") or "")},
        )
    ]


def _compaction_end(frame: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [(events.RUN_STAGE, {"stage": "compaction_end"})]


def _auto_retry_start(frame: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            events.RUN_STAGE,
            {
                "stage": "auto_retry_start",
                "attempt": frame.get("attempt"),
                "errorMessage": str(frame.get("errorMessage") or "")[:500],
            },
        )
    ]


def _auto_retry_end(frame: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            events.RUN_STAGE,
            {"stage": "auto_retry_end", "success": bool(frame.get("success"))},
        )
    ]


def _extension_error(frame: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            events.KERNEL_STATUS,
            {
                "level": "warning",
                "message": "extension_error",
                "detail": str(frame.get("error") or "")[:500],
                "extensionPath": str(frame.get("extensionPath") or ""),
            },
        )
    ]


_HANDLERS = {
    "agent_start": _agent_start,
    "agent_end": _agent_end,
    "turn_start": _turn_start,
    "turn_end": _turn_end,
    "message_update": _message_update,
    "message_end": _message_end,
    "tool_execution_start": _tool_execution_start,
    "tool_execution_update": _tool_execution_update,
    "tool_execution_end": _tool_execution_end,
    "compaction_start": _compaction_start,
    "compaction_end": _compaction_end,
    "auto_retry_start": _auto_retry_start,
    "auto_retry_end": _auto_retry_end,
    "extension_error": _extension_error,
}

#: pi frames that carry meaning for the runner but need no product event.
RUNNER_ONLY_EVENTS = frozenset({"agent_settled", "message_start", "queue_update"})


def extract_text(value: Any) -> str:
    """Flatten a pi message content / tool result into display text.

    pi content blocks are ``{"type": "text", "text": …}`` style objects; tool
    results may be a plain string, a list of blocks, or a mapping carrying
    ``content``/``text``/``output``. Unknown shapes degrade to an empty string
    rather than raising: a renderer must not crash on a protocol addition.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("text", "output", "content", "result", "message"):
            if key in value:
                text = extract_text(value[key])
                if text:
                    return text
        return ""
    if isinstance(value, (list, tuple)):
        parts: list[str] = []
        for item in value:
            text = extract_text(item)
            if text:
                parts.append(text)
        return "\n".join(parts)
    return ""


def _usage_from_message(message: Any) -> dict[str, Any] | None:
    if not isinstance(message, Mapping):
        return None
    usage = message.get("usage")
    if not isinstance(usage, Mapping):
        return None
    projected = {
        key: usage[key]
        for key in ("input", "output", "cacheRead", "cacheWrite", "totalTokens", "cost")
        if key in usage
    }
    return projected or None


def _project_args(args: Any) -> dict[str, Any]:
    """Keep tool arguments small and JSON-safe."""
    if not isinstance(args, Mapping):
        return {}
    projected: dict[str, Any] = {}
    for key, value in list(args.items())[:20]:
        if isinstance(value, str):
            projected[str(key)] = _clip_text(value, 2000)
        elif isinstance(value, (int, float, bool)) or value is None:
            projected[str(key)] = value
        else:
            projected[str(key)] = _clip_text(extract_text(value), 500)
    return projected


def _clip_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit]
    return f"{head}\n…[truncated {len(text) - limit} chars]"


def project_frames(
    frames: Iterable[Mapping[str, Any]], redactor: Any = None
) -> list[tuple[str, dict[str, Any]]]:
    """Convenience for tests and offline replay."""
    produced: list[tuple[str, dict[str, Any]]] = []
    for frame in frames:
        produced.extend(project_event(frame, redactor))
    return produced
