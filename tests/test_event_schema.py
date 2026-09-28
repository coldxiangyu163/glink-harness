"""The versioned product event schema and the boundaries that enforce it.

``host/pi_glink/event_schema.py`` is the single declaration; the bus, both
kernel projections and the resource ledger validate against it, and
``scripts/gen-event-schema.py`` renders it for the front-end. The suite runs in
strict mode (``tests/conftest.py``), so every other test already fails on a
violating frame; this file pins the schema's own behaviour.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from pi_glink import event_schema, events
from pi_glink.events import EventBus
from pi_glink.kernel import projection as pi_projection
from pi_glink.kernel.acp_projection import AcpProjector
from pi_glink.kernel.projection import Projector
from pi_glink.redaction import SecretRedactor

REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "scripts" / "gen-event-schema.py"
GENERATED = REPO_ROOT / "web" / "modules" / "event-schema.gen.mjs"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "events"

_SAMPLE = {
    "string": "x",
    "integer": 1,
    "number": 1.5,
    "boolean": True,
    "null": None,
    "object": {},
    "array": [],
}


def _minimal(spec: event_schema.EventSpec) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for item in spec.fields:
        if not item.required:
            continue
        if item.enum is not None:
            payload[item.name] = item.enum[0]
        elif item.types is None:
            payload[item.name] = "x"
        else:
            payload[item.name] = _SAMPLE[item.types[0]]
    return payload


@pytest.fixture
def expect_violation():
    """A test that provokes a violation on purpose clears the record after."""
    yield
    event_schema.reset_violations()


# -- the schema itself ---------------------------------------------------------


def test_the_schema_is_the_bus_vocabulary() -> None:
    assert events.EVENT_TYPES == frozenset(event_schema.BY_TYPE)
    assert events.TERMINAL_EVENT_TYPES == event_schema.TERMINAL_TYPES
    assert event_schema.TERMINAL_TYPES == {events.RUN_FINISHED, events.RUN_FAILED, events.RUN_CANCELLED}
    assert all(isinstance(spec.v, int) and spec.v >= 1 for spec in event_schema.SPECS)
    assert len({spec.type for spec in event_schema.SPECS}) == len(event_schema.SPECS)


@pytest.mark.parametrize("spec", event_schema.SPECS, ids=lambda spec: spec.type)
def test_every_spec_accepts_its_minimal_payload(spec: event_schema.EventSpec) -> None:
    assert event_schema.problems(spec.type, _minimal(spec)) == []


@pytest.mark.parametrize(
    ("event_type", "payload", "needle"),
    [
        ("run_started", {}, "run_started.prompt: 缺少必填字段"),
        ("run_started", {"prompt": 3}, "类型 integer"),
        ("run_started", {"prompt": "p", "index": 1}, "未声明的字段 ['index']"),
        ("run_stage", {"stage": "warming_up"}, "不在"),
        ("run_stage", {"stage": "auto_retry_start", "attempt": True}, "类型 boolean"),
        ("subagent_finished", {"toolCallId": "c", "ns": ["0"], "status": "finished",
                               "isError": False, "resultPreview": ""}, "ns[0]"),
        ("run_failed", {**_minimal(event_schema.BY_TYPE["run_failed"]), "status": "finished"}, "不在"),
        ("tool_started", [], "payload 必须是对象"),
        ("future_event", {}, "未知事件类型"),
    ],
)
def test_violations_are_named(event_type: str, payload: Any, needle: str) -> None:
    found = event_schema.problems(event_type, payload)
    assert found and any(needle in problem for problem in found), found


def test_tool_presentation_is_additive_and_old_tool_frames_remain_valid() -> None:
    old = {"toolCallId": "c1", "toolName": "bash", "args": {"command": "ls"}}
    assert event_schema.EVENT_VERSIONS[events.TOOL_STARTED] == 1
    assert event_schema.problems(events.TOOL_STARTED, old) == []
    assert event_schema.problems(events.TOOL_STARTED, {**old, "presentation": "execute"}) == []
    assert any("presentation" in p for p in event_schema.problems(
        events.TOOL_STARTED, {**old, "presentation": {"action": "execute"}}
    ))


def test_an_integer_satisfies_a_number_field_and_open_payloads_pass_extra_keys() -> None:
    terminal = {**_minimal(event_schema.BY_TYPE["run_finished"]), "durationSeconds": 3}
    assert event_schema.problems("run_finished", terminal) == []
    ready = {"sessionFile": None, "kernelReported": {"cwd": "/w"}}
    assert event_schema.problems("session_ready", ready) == []


# -- the bus -------------------------------------------------------------------


def test_the_bus_stamps_v_on_the_envelope_and_the_wire() -> None:
    bus = EventBus()
    event = bus.commit("r1", events.RUN_STARTED, {"prompt": "p"})
    assert event.v == event_schema.EVENT_VERSIONS[events.RUN_STARTED]
    envelope = event.as_dict()
    assert list(envelope) == ["type", "v", "runId", "seq", "ts", "payload"]
    assert event_schema.envelope_problems(envelope) == []
    sse = event.to_sse()
    data = json.loads(sse.split("data: ", 1)[1])
    assert data["v"] == 1 and data == envelope


def test_strict_mode_refuses_a_violating_frame_without_burning_a_seq(expect_violation) -> None:
    assert event_schema.strict(), "tests run in strict mode (tests/conftest.py)"
    bus = EventBus()
    persisted: list[events.RunEvent] = []
    bus.commit("r1", events.RUN_STARTED, {"prompt": "p"}, persist=persisted.append)
    with pytest.raises(event_schema.EventSchemaError, match="bus run=r1"):
        bus.commit("r1", events.TOOL_FINISHED, {"toolCallId": "c"}, persist=persisted.append)
    assert [event.seq for event in persisted] == [1], "违反 schema 的帧不得落盘"
    assert bus.last_seq("r1") == 1
    assert bus.commit("r1", events.TURN_STARTED, {}).seq == 2, "seq 未被消耗"
    assert len(event_schema.violations()) == 1


def test_lenient_mode_logs_and_still_commits(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, expect_violation
) -> None:
    monkeypatch.setenv(event_schema.STRICT_ENV, "")
    bus = EventBus()
    with caplog.at_level(logging.ERROR, logger="pi_glink.event_schema"):
        event = bus.commit("r1", events.RUN_STARTED, {"index": 1})
    assert event.seq == 1 and event.payload == {"index": 1}
    assert "事件违反 schema" in caplog.text and "run_started.prompt" in caplog.text
    assert event_schema.violations() and "bus run=r1" in event_schema.violations()[0]


# -- the projections -----------------------------------------------------------


def test_both_projections_validate_what_they_produce(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    real = event_schema.check

    def spy(event_type: str, payload: Any, *, source: str) -> None:
        calls.append((event_type, source))
        real(event_type, payload, source=source)

    monkeypatch.setattr(event_schema, "check", spy)
    Projector(SecretRedactor()).project({"type": "turn_start"})
    AcpProjector(SecretRedactor()).project(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "s",
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}},
            },
        }
    )
    sources = {source for _, source in calls}
    assert "pi projection" in sources and "acp projection" in sources, calls


def test_a_projection_drift_is_reported_at_its_source(expect_violation) -> None:
    with pytest.raises(event_schema.EventSchemaError, match="^pi projection: tool_started"):
        pi_projection.checked([(events.TOOL_STARTED, {"toolCallId": "c", "toolName": "bash", "args": "ls"})], "pi projection")


# -- fixtures and the generated module ----------------------------------------


def _frames(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_the_front_end_event_fixtures_are_valid_envelopes() -> None:
    paths = sorted(FIXTURES.glob("*.jsonl"))
    assert len(paths) >= 5
    for path in paths:
        for frame in _frames(path):
            found = event_schema.envelope_problems(frame)
            if path.stem == "legacy-unversioned":
                assert any("v=None" in problem for problem in found), (path.name, found)
            else:
                assert found == [], (path.name, frame["seq"], found)
    for frame in _frames(REPO_ROOT / "tests" / "fixtures" / "tool-update-frames.jsonl"):
        assert event_schema.envelope_problems(frame) == []


def _run_generator(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GENERATOR), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )


def test_the_generated_module_is_current() -> None:
    result = _run_generator("--check")
    assert result.returncode == 0, result.stderr


def test_the_generator_check_detects_a_stale_output(tmp_path: Path) -> None:
    target = tmp_path / "event-schema.gen.mjs"
    assert _run_generator("--check", "--output", str(target)).returncode == 1, "缺失的生成物不是最新的"
    written = _run_generator("--output", str(target))
    assert written.returncode == 0, written.stderr
    assert target.read_text(encoding="utf-8") == GENERATED.read_text(encoding="utf-8")
    assert _run_generator("--check", "--output", str(target)).returncode == 0
    target.write_text(target.read_text(encoding="utf-8").replace('"run_started": 1', '"run_started": 2'), encoding="utf-8")
    stale = _run_generator("--check", "--output", str(target))
    assert stale.returncode == 1
    assert "gen-event-schema.py" in stale.stderr


def test_the_generated_module_carries_every_type_and_version() -> None:
    text = GENERATED.read_text(encoding="utf-8")
    versions = json.loads(text.split("export const EVENT_VERSIONS = Object.freeze(", 1)[1].split(");", 1)[0])
    assert versions == dict(event_schema.EVENT_VERSIONS)
