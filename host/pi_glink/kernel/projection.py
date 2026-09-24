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

import json
from typing import Any, Iterable, Mapping

from .. import events
from ..redaction import SecretRedactor, StreamRedactor

#: Fail-closed default. Shape-based masking runs even when a caller forgets to
#: pass the host redactor, so a credential-shaped value can never leave this
#: module verbatim — the completion audit reproduced exactly that path with a
#: ``tool_execution_end`` frame carrying ``OPENAI_API_KEY=…``.
_FALLBACK_REDACTOR = SecretRedactor()

#: Characters kept from a tool result / partial output before clipping.
TOOL_TEXT_LIMIT = 4000
#: Characters kept from a single streaming delta (a delta is already small).
DELTA_LIMIT = 20000
#: Bound on an array argument (urls/queries/...): item count and item length.
ARG_LIST_LIMIT = 20
ARG_ITEM_LIMIT = 500

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

    When no redactor is supplied the module falls back to a shape-only one:
    forgetting the host redactor degrades detection (known values are missed) but
    never turns into a leak.
    """
    frame_type = frame.get("type")
    handler = _HANDLERS.get(frame_type)
    if handler is None:
        return []
    produced = handler(frame)
    if not produced:
        return produced
    active = redactor if redactor is not None else _FALLBACK_REDACTOR
    return [
        (kind, active.redact_payload(payload) or payload) for kind, payload in produced
    ]


#: Delta events whose split-credential protection needs stream state.
STREAM_EVENTS = (events.THINKING_DELTA, events.ASSISTANT_DELTA)
#: pi frames that end a streaming block; the withheld tail is released before
#: the frame's own product events so it still belongs to the block it came from.
STREAM_BOUNDARY_FRAMES = frozenset({"turn_end", "message_end", "agent_end", "agent_settled"})


class Projector:
    """Stateful projection of one run's frames: the run's redaction boundary.

    :func:`project_event` redacts every payload on its own, which cannot see a
    credential split across two deltas — and a per-delta redaction would also
    hide the raw text a stream needs to recognise an overlapping repeat. Here
    deltas bypass per-frame redaction and go through one
    :class:`~pi_glink.redaction.StreamRedactor` as raw text; every other payload
    is redacted exactly as in :func:`project_event`.
    """

    def __init__(self, redactor: SecretRedactor) -> None:
        self._redactor = redactor
        self._stream = StreamRedactor(redactor)

    def project(self, frame: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        produced: list[tuple[str, dict[str, Any]]] = []
        if frame.get("type") in STREAM_BOUNDARY_FRAMES:
            produced.extend(self.flush())
        handler = _HANDLERS.get(frame.get("type"))
        if handler is None:
            return produced
        for kind, payload in handler(frame):
            if kind in STREAM_EVENTS:
                text = self._stream.push(kind, payload["text"])
                if text:
                    produced.append((kind, {**payload, "text": text}))
            else:
                produced.append((kind, self._redactor.redact_payload(payload)))
        return produced

    def flush(self) -> list[tuple[str, dict[str, Any]]]:
        """Release every withheld delta tail (stream boundary or run end)."""
        released: list[tuple[str, dict[str, Any]]] = []
        for kind in STREAM_EVENTS:
            text = self._stream.flush(kind)
            if text:
                released.append((kind, {"text": text}))
        return released


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


#: How much of an assistant message's authoritative text the event log keeps.
#: The streamed deltas already carried the whole answer to the browser; this
#: bound exists so one frame cannot blow up ``events.jsonl`` and the replay ring.
#: When it clips, the payload says so (``truncated: true``) and the front-end
#: keeps the longer streamed body instead of replacing it with less.
ASSISTANT_TEXT_LIMIT = TOOL_TEXT_LIMIT * 4


def _message_end(frame: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    message = frame.get("message")
    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        return []
    text = extract_text(message.get("content"))
    clipped, was_truncated = _clip_text_flagged(text, ASSISTANT_TEXT_LIMIT)
    payload: dict[str, Any] = {"text": clipped}
    if was_truncated:
        # The authoritative copy is incomplete: a client that already holds a
        # longer streamed body must not treat this as the whole answer.
        payload["truncated"] = True
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
    """Keep tool arguments small, JSON-safe and type-stable.

    A tool that searches several queries (``queries``) or reads several pages
    (``urls``) passes an **array**; flattening it to one string here destroys
    the multi-value payload before the front-end can consume it. The payload
    contract is therefore: strings stay strings, primitive scalars stay
    scalars, an array stays a bounded array of clipped strings, and anything
    else degrades to one clipped string. Bounded means at most
    :data:`ARG_LIST_LIMIT` items of :data:`ARG_ITEM_LIMIT` characters.

    An array item that is itself an object (``subagent`` receives
    ``tasks: [{agent, task}, …]``) was previously flattened through
    :func:`extract_text`, which knows only message/content shapes and returned
    an empty string: the delegated work disappeared from the card. Such an item
    now renders as its compact JSON text — still one bounded string, so every
    array consumer keeps its contract, and the batch stays auditable.
    """
    if not isinstance(args, Mapping):
        return {}
    projected: dict[str, Any] = {}
    for key, value in list(args.items())[:20]:
        projected[str(key)] = _project_arg_value(value)
    return projected


def _project_arg_value(value: Any) -> Any:
    if isinstance(value, str):
        return _clip_text(value, 2000)
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_arg_item_text(item) for item in list(value)[:ARG_LIST_LIMIT]]
    if isinstance(value, Mapping):
        # A single object argument (a filter, an options block) stays readable
        # text: JSON rather than the empty string extract_text would give.
        return _clip_text(_compact_json(value), 500)
    return _clip_text(extract_text(value), 500)


def _arg_item_text(item: Any) -> str:
    """One array item as bounded text: strings as-is, objects as compact JSON."""
    if isinstance(item, str):
        return _clip_text(item, ARG_ITEM_LIMIT)
    if isinstance(item, (Mapping, list, tuple)):
        return _clip_text(_compact_json(item), ARG_ITEM_LIMIT)
    return _clip_text(extract_text(item), ARG_ITEM_LIMIT)


def _compact_json(value: Any) -> str:
    """Compact JSON for a bounded payload; unencodable values degrade to text."""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return extract_text(value)


def _clip_text(text: str, limit: int) -> str:
    return _clip_text_flagged(text, limit)[0]


def _clip_text_flagged(text: str, limit: int) -> tuple[str, bool]:
    """``(clipped, was_truncated)`` — callers that must not lie about
    completeness use the flag instead of guessing from the marker text."""
    if len(text) <= limit:
        return text, False
    head = text[:limit]
    return f"{head}\n…[truncated {len(text) - limit} chars]", True


def project_frames(
    frames: Iterable[Mapping[str, Any]], redactor: Any = None
) -> list[tuple[str, dict[str, Any]]]:
    """Convenience for tests and offline replay."""
    produced: list[tuple[str, dict[str, Any]]] = []
    for frame in frames:
        produced.extend(project_event(frame, redactor))
    return produced
