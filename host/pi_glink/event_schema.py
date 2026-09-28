"""The product event contract as one versioned, declarative schema.

This module is the **single source** of the event vocabulary shipped to the
browser: every event type, its payload fields and the payload version ``v``.
Three consumers read it and nothing else:

* :class:`pi_glink.events.EventBus` validates every frame before it is
  persisted or shown (:func:`check`), and stamps ``v`` on the envelope;
* the kernel projectors validate what they produce (:func:`check`), so a
  projection drift is reported at its source, not only at the bus;
* ``scripts/gen-event-schema.py`` renders :func:`schema_document` into
  ``web/modules/event-schema.gen.mjs`` — the front-end's event names, versions
  and field lists. ``--check`` (run by ``scripts/check.sh``) fails when the
  generated file is not what this module would produce.

**Versioning.** ``v`` is per event type and starts at 1. A payload change that
an older reader would misinterpret bumps that type's ``v``; the front-end
reducer applies only frames whose ``v`` equals the version it was generated
against and counts every other frame as rejected. Event logs written before the
schema existed carry no ``v`` and are therefore **not readable** by the current
front-end (docs/architecture.md §3.6「事件 schema 与版本」): there is no
migration.

**Violations.** In strict mode (``PI_GLINK_EVENT_SCHEMA=strict``, set by the
test suite) a violating frame raises :class:`EventSchemaError` and is never
committed. Otherwise the violation is logged and the frame is still committed:
a production run does not die on a contract bug, but the log names it. Every
violation is also recorded (:func:`violations`) so a test can fail on one that
a background thread swallowed.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

logger = logging.getLogger("pi_glink.event_schema")

#: JSON types a field may take. ``None`` as a field's type list means "any".
JSON_TYPES = ("string", "integer", "number", "boolean", "null", "object", "array")

STRICT_ENV = "PI_GLINK_EVENT_SCHEMA"


class EventSchemaError(ValueError):
    """A frame does not match the product event schema."""


@dataclass(frozen=True, slots=True)
class Field:
    name: str
    types: tuple[str, ...] | None
    required: bool = True
    enum: tuple[Any, ...] | None = None
    #: Element type of an ``array`` field (``None``: any element).
    items: str | None = None
    doc: str = ""


@dataclass(frozen=True, slots=True)
class EventSpec:
    type: str
    v: int
    fields: tuple[Field, ...]
    #: Open payloads accept keys the schema does not name (kernel-reported
    #: session facts); closed payloads reject them.
    open: bool = False
    terminal: bool = False
    doc: str = ""
    by_name: Mapping[str, Field] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_name", {f.name: f for f in self.fields})


def _f(name: str, *types: str, required: bool = True, **kw: Any) -> Field:
    return Field(name, tuple(types) if types else None, required, **kw)


def _opt(name: str, *types: str, **kw: Any) -> Field:
    return _f(name, *types, required=False, **kw)


#: ``run_stage.stage`` values (pi projection + ACP plan).
RUN_STAGES = (
    "agent_start",
    "agent_end",
    "compaction_start",
    "compaction_end",
    "auto_retry_start",
    "auto_retry_end",
    "plan",
)


def _terminal(event_type: str, status: str) -> EventSpec:
    return EventSpec(
        event_type,
        1,
        (
            _f("status", "string", enum=(status,)),
            _f("stopReason", "string", "null"),
            _f("error", "string", "null"),
            _f("reportPath", "string", "null"),
            _f("reportStatus", "string"),
            _f("cleanup", "object", doc="{released, failures, external}"),
            _f("children", "array", items="object", doc="子代理终态摘要（权威）"),
            _opt("modelStopReason", "string", "null"),
            _opt("sessionId", "string", "null"),
            _opt("sessionFile", "string", "null"),
            _opt("usage", "object", "null"),
            _opt("kernelUsage", "object", "null"),
            _opt("redacted", "boolean"),
            _opt("durationSeconds", "number"),
            _opt("artifactFailures", "array"),
            _opt("external", "object", doc="遗留 run 收口时的外部资源事实"),
        ),
        terminal=True,
        doc="权威终帧：status 与事件类型一一对应",
    )


SPECS: tuple[EventSpec, ...] = (
    EventSpec("run_started", 1, (_f("prompt", "string"),)),
    EventSpec(
        "run_stage",
        1,
        (
            _f("stage", "string", enum=RUN_STAGES),
            _opt("willRetry", "boolean"),
            _opt("reason", "string"),
            _opt("attempt", "integer", "null"),
            _opt("errorMessage", "string"),
            _opt("success", "boolean"),
            _opt("entries", "array", items="object", doc="ACP plan: {content, status, priority}"),
        ),
    ),
    _terminal("run_finished", "finished"),
    _terminal("run_failed", "failed"),
    _terminal("run_cancelled", "cancelled"),
    EventSpec(
        "session_ready",
        1,
        (
            _opt("sessionId", "string", "null"),
            _f("sessionFile", "string", "null"),
            _opt("model", "string", "null"),
            _opt("thinkingLevel", "string", "null"),
        ),
        open=True,
        doc="内核自报的会话事实，其余键按内核原样透传",
    ),
    EventSpec("turn_started", 1, ()),
    EventSpec("turn_finished", 1, ()),
    EventSpec("assistant_delta", 1, (_f("text", "string"),), doc="增量"),
    EventSpec("thinking_delta", 1, (_f("text", "string"),), doc="增量"),
    EventSpec(
        "assistant_message",
        1,
        (
            _f("text", "string"),
            _opt("truncated", "boolean"),
            _opt("stopReason", "string"),
            _opt("usage", "object"),
        ),
        doc="权威正文；truncated 时不覆盖更长的流式正文",
    ),
    EventSpec(
        "tool_started",
        1,
        (
            _f("toolCallId", "string"),
            _f("toolName", "string"),
            _f("args", "object"),
            _opt("kind", "string"),
        ),
    ),
    EventSpec(
        "tool_updated",
        1,
        (_f("toolCallId", "string"), _f("toolName", "string"), _f("output", "string")),
        doc="output 是累计快照，替换而非拼接",
    ),
    EventSpec(
        "tool_finished",
        1,
        (
            _f("toolCallId", "string"),
            _f("toolName", "string"),
            _f("isError", "boolean"),
            _f("result", "string"),
        ),
        doc="工具终帧（权威）",
    ),
    EventSpec(
        "subagent_started",
        1,
        (
            _f("n", "integer"),
            _f("toolCallId", "string"),
            _f("agent", "string"),
            _f("task", "string"),
        ),
    ),
    EventSpec(
        "subagent_finished",
        1,
        (
            _f("toolCallId", "string"),
            _f("ns", "array", items="integer"),
            _f("status", "string", enum=("finished", "failed")),
            _f("isError", "boolean"),
            _f("resultPreview", "string"),
        ),
    ),
    EventSpec(
        "subagent_updated",
        1,
        (
            _f("n", "integer"),
            _f("steps", "integer"),
            _f("lastToolName", "string", "null"),
            _f("lastToolTarget", "string", "null"),
            _f("bytes", "integer"),
        ),
        doc="运行中进度（累计快照，最新值为准）",
    ),
    EventSpec(
        "usage_reported",
        1,
        (_f("usage", "object"), _opt("requestUsage", "object")),
        doc="usage 为累计值",
    ),
    EventSpec(
        "kernel_status",
        1,
        (
            _f("level", "string", enum=("info", "warning")),
            _f("message", "string"),
            _f("detail", "string"),
            _opt("extensionPath", "string"),
            _opt("resumeMethod", "string"),
            _opt("sessionId", "string"),
            _opt("replayedUpdates", "integer"),
            _opt("decision", "string"),
            _opt("policy", "string"),
            _opt("toolCallId", "string"),
            _opt("toolTitle", "string"),
            _opt("toolKind", "string"),
            _opt("toolKindSource", "string"),
            _opt("optionId", "string"),
            _opt("optionKind", "string"),
            _opt("commands", "array", items="object"),
        ),
    ),
)

BY_TYPE: Mapping[str, EventSpec] = {spec.type: spec for spec in SPECS}
EVENT_VERSIONS: Mapping[str, int] = {spec.type: spec.v for spec in SPECS}
TERMINAL_TYPES: frozenset[str] = frozenset(spec.type for spec in SPECS if spec.terminal)


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, (list, tuple)):
        return "array"
    return type(value).__name__


def _matches(actual: str, allowed: Sequence[str]) -> bool:
    return actual in allowed or (actual == "integer" and "number" in allowed)


def problems(event_type: str, payload: Any) -> list[str]:
    """Every way ``payload`` departs from the schema of ``event_type``."""
    spec = BY_TYPE.get(event_type)
    if spec is None:
        return [f"未知事件类型 {event_type!r}"]
    if not isinstance(payload, Mapping):
        return [f"{event_type}: payload 必须是对象，实际 {_json_type(payload)}"]
    found: list[str] = []
    for item in spec.fields:
        if item.name not in payload:
            if item.required:
                found.append(f"{event_type}.{item.name}: 缺少必填字段")
            continue
        value = payload[item.name]
        actual = _json_type(value)
        if item.types is not None and not _matches(actual, item.types):
            found.append(
                f"{event_type}.{item.name}: 类型 {actual} 不在 {'|'.join(item.types)} 内"
            )
            continue
        if item.enum is not None and value not in item.enum:
            found.append(f"{event_type}.{item.name}: 值 {value!r} 不在 {list(item.enum)} 内")
        if item.items is not None and actual == "array":
            for index, element in enumerate(value):
                if not _matches(_json_type(element), (item.items,)):
                    found.append(
                        f"{event_type}.{item.name}[{index}]: 元素类型 {_json_type(element)} 不是 {item.items}"
                    )
                    break
    if not spec.open:
        extra = sorted(set(payload) - set(spec.by_name))
        if extra:
            found.append(f"{event_type}: 未声明的字段 {extra}")
    return found


def validate(event_type: str, payload: Any) -> None:
    """Raise :class:`EventSchemaError` when the payload violates the schema."""
    found = problems(event_type, payload)
    if found:
        raise EventSchemaError("；".join(found))


_violations: list[str] = []
_violations_lock = threading.Lock()


def strict() -> bool:
    return os.environ.get(STRICT_ENV, "").strip().lower() == "strict"


def check(event_type: str, payload: Any, *, source: str) -> None:
    """Validate at a publishing boundary (``source`` names it in the log).

    Strict mode raises; otherwise the violation is logged. Either way it is
    recorded for :func:`violations`.
    """
    found = problems(event_type, payload)
    if not found:
        return
    message = f"{source}: " + "；".join(found)
    with _violations_lock:
        _violations.append(message)
    if strict():
        raise EventSchemaError(message)
    logger.error("事件违反 schema（仍提交）%s", message)


def violations() -> list[str]:
    with _violations_lock:
        return list(_violations)


def reset_violations() -> None:
    with _violations_lock:
        _violations.clear()


def envelope_problems(frame: Any) -> list[str]:
    """Problems of one persisted/wire envelope ``{type, v, runId, seq, ts, payload}``."""
    if not isinstance(frame, Mapping):
        return ["帧必须是对象"]
    event_type = frame.get("type")
    if not isinstance(event_type, str):
        return ["帧缺少 type"]
    found: list[str] = []
    expected = EVENT_VERSIONS.get(event_type)
    if expected is not None and frame.get("v") != expected:
        found.append(f"{event_type}: v={frame.get('v')!r}，schema 版本为 {expected}")
    if not isinstance(frame.get("runId"), str):
        found.append(f"{event_type}: runId 必须是字符串")
    seq = frame.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        found.append(f"{event_type}: seq 必须是正整数")
    ts = frame.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        found.append(f"{event_type}: ts 必须是数值")
    found.extend(problems(event_type, frame.get("payload")))
    return found


def schema_document() -> dict[str, Any]:
    """The schema as plain JSON data (input of the front-end generator)."""
    return {
        "events": [
            {
                "type": spec.type,
                "v": spec.v,
                "open": spec.open,
                "terminal": spec.terminal,
                "doc": spec.doc,
                "fields": [
                    {
                        "name": item.name,
                        "types": list(item.types) if item.types is not None else None,
                        "required": item.required,
                        **({"enum": list(item.enum)} if item.enum is not None else {}),
                        **({"items": item.items} if item.items is not None else {}),
                        **({"doc": item.doc} if item.doc else {}),
                    }
                    for item in spec.fields
                ],
            }
            for spec in SPECS
        ]
    }
