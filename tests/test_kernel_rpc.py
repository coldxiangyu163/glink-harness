"""Regressions for the pi headless RPC client.

These run against ``tests/fixtures/fake_pi.py`` — a protocol stand-in, not a
mock object — so framing, correlation, error frames and process death are
exercised over real pipes.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Mapping

import pytest

from pi_glink.kernel import PiRpcClient, PiRpcTimeout, project_event

FAKE_PI = Path(__file__).resolve().parent / "fixtures" / "fake_pi.py"


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


def test_runner_only_frames_do_not_leak_to_the_product_stream() -> None:
    for frame_type in ("agent_settled", "message_start", "queue_update"):
        assert project_event({"type": frame_type}) == []
