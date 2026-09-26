"""Client and projection regressions for the ACP harness family.

The client is exercised against real child processes (a synthetic frame emitter
and the shared ``tests/fixtures/fake_acp.py`` agent): framing, request/response
correlation, error answers and process death are protocol facts, not call
shapes, so they are verified over the wire. The projector is a pure function of
its frames and is exercised directly.
"""

from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import pytest

from pi_glink.events import (
    ASSISTANT_DELTA,
    KERNEL_STATUS,
    RUN_STAGE,
    THINKING_DELTA,
    TOOL_FINISHED,
    TOOL_STARTED,
    TOOL_UPDATED,
    TURN_FINISHED,
    TURN_STARTED,
    USAGE_REPORTED,
)
from pi_glink.kernel.acp_execution import CLIENT_CAPABILITIES
from pi_glink.kernel.acp_projection import AcpProjector
from pi_glink.kernel.acp_rpc import (
    AcpClosed,
    AcpRequestError,
    AcpRpcClient,
    AcpTimeout,
)
from pi_glink.kernel.projection import project_args
from pi_glink.redaction import SecretRedactor

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_ACP = Path(__file__).resolve().parent / "fixtures" / "fake_acp.py"
FRAMING_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "lf-framing.json"


def _wait_for(predicate, *, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _script(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


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


# -- client framing -----------------------------------------------------------


def test_acp_client_frames_records_on_lf_only(tmp_path: Path) -> None:
    """U+2028/U+2029 stay inside a frame; CRLF writers are tolerated.

    ``tests/fixtures/lf-framing.json`` is the shared corpus the pi RPC and E2B
    faces frame with; here each record is one ACP ``session/update`` payload
    written with alternating LF/CRLF endings in a single chunk.
    """
    corpus = json.loads(FRAMING_FIXTURE.read_text(encoding="utf-8"))
    notifications = [_notification(frame) for frame in corpus["frames"]]
    wire = "".join(
        json.dumps(notification, ensure_ascii=False) + ("\r\n" if index % 2 else "\n")
        for index, notification in enumerate(notifications)
    )
    script = _script(
        tmp_path,
        "emitter.py",
        "import sys\nsys.stdout.buffer.write(" + repr(wire.encode("utf-8")) + ")\n",
    )
    frames: list[Mapping[str, Any]] = []
    client = AcpRpcClient([sys.executable, str(script)], on_notification=frames.append)
    client.start()
    try:
        assert _wait_for(lambda: len(frames) == len(notifications))
        time.sleep(0.1)
    finally:
        client.close()
    assert [frame["params"]["update"] for frame in frames] == corpus["frames"]
    assert client.notification_count == len(notifications)
    assert client.invalid_frame_count == 0


def test_acp_client_chunks_a_real_agent_write(tmp_path: Path) -> None:
    """The fake agent writes its first three notifications in one raw CRLF chunk."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"FAKE_ACP_CHUNKED": True}), encoding="utf-8")
    frames: list[Mapping[str, Any]] = []
    client = AcpRpcClient(
        [
            sys.executable,
            str(FAKE_ACP),
            "--config",
            str(settings),
        ],
        on_notification=frames.append,
    )
    client.start()
    try:
        assert client.request("initialize", {"protocolVersion": 1}, timeout=10)["protocolVersion"] == 1
        client.request("session/new", {"cwd": str(tmp_path), "mcpServers": []}, timeout=10)
        client.request(
            "session/prompt",
            {"sessionId": "fake-acp-session", "prompt": [{"type": "text", "text": "分帧"}]},
            timeout=10,
        )
    finally:
        client.close()
    updates = [frame["params"]["update"]["sessionUpdate"] for frame in frames]
    assert "agent_thought_chunk" in updates and "agent_message_chunk" in updates
    assert client.invalid_frame_count == 0


# -- client request/response --------------------------------------------------


def test_acp_client_refuses_an_unknown_client_method(tmp_path: Path) -> None:
    """An agent request the client did not advertise is answered, never ignored."""
    out = tmp_path / "answer.json"
    script = _script(
        tmp_path,
        "requester.py",
        "\n".join(
            [
                "import json, sys",
                "request = {'jsonrpc': '2.0', 'id': 7, 'method': 'fs/read_text_file', 'params': {'path': '/etc/passwd'}}",
                "sys.stdout.write(json.dumps(request) + '\\n')",
                "sys.stdout.flush()",
                "line = sys.stdin.readline()",
                "open(sys.argv[1], 'w', encoding='utf-8').write(line)",
            ]
        ),
    )
    client = AcpRpcClient([sys.executable, str(script), str(out)])
    client.start()
    try:
        assert _wait_for(out.is_file)
    finally:
        client.close()
    answer = json.loads(out.read_text(encoding="utf-8"))
    assert answer["id"] == 7
    assert answer["error"]["code"] == -32601
    assert "fs/read_text_file" in answer["error"]["message"]


def test_acp_client_surfaces_a_jsonrpc_error_response(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "rejector.py",
        "\n".join(
            [
                "import json, sys",
                "line = sys.stdin.readline()",
                "request = json.loads(line)",
                "answer = {'jsonrpc': '2.0', 'id': request.get('id'), 'error': {'code': -32000, 'message': 'nope'}}",
                "sys.stdout.write(json.dumps(answer) + '\\n')",
                "sys.stdout.flush()",
            ]
        ),
    )
    client = AcpRpcClient([sys.executable, str(script)])
    client.start()
    try:
        with pytest.raises(AcpRequestError) as excinfo:
            client.request("session/prompt", {"sessionId": "s"}, timeout=10)
    finally:
        client.close()
    assert excinfo.value.code == -32000
    assert excinfo.value.method == "session/prompt"


def test_acp_client_times_out_and_reports_process_death(tmp_path: Path) -> None:
    silent = _script(
        tmp_path,
        "silent.py",
        "import sys, time\nfor line in sys.stdin:\n    time.sleep(0.05)\n",
    )
    client = AcpRpcClient([sys.executable, str(silent)])
    client.start()
    try:
        with pytest.raises(AcpTimeout):
            client.request("initialize", {"protocolVersion": 1}, timeout=0.2)
    finally:
        client.close()

    dying = _script(tmp_path, "dying.py", "import sys\nsys.exit(0)\n")
    client = AcpRpcClient([sys.executable, str(dying)])
    client.start()
    try:
        with pytest.raises(AcpClosed):
            client.request("initialize", {"protocolVersion": 1}, timeout=5)
    finally:
        client.close()


def test_acp_client_close_terminates_a_stubborn_agent(tmp_path: Path) -> None:
    stubborn = _script(
        tmp_path,
        "stubborn.py",
        "import sys, time\nwhile True:\n    time.sleep(0.2)\n",
    )
    client = AcpRpcClient([sys.executable, str(stubborn)])
    client.start()
    try:
        assert client.running is True
        client.close(timeout=0.5)
    finally:
        client.close()
    assert client.running is False


# -- projection: content ------------------------------------------------------


# -- projection: turn boundaries and content ---------------------------------


def test_turn_frames_close_the_streamed_blocks() -> None:
    """ACP sends no turn updates; the adapter's synthetic frames supply them."""
    projector = AcpProjector(SecretRedactor())
    assert projector.project({"type": "acp_turn_started"}) == [(TURN_STARTED, {})]
    assert _project(
        projector,
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "回答"}},
    ) == [(ASSISTANT_DELTA, {"text": "回答"})]
    assert projector.project({"type": "acp_turn_finished"}) == [(TURN_FINISHED, {})]


def test_message_and_thought_chunks_project_to_deltas() -> None:
    projector = AcpProjector(SecretRedactor())
    assert _project(
        projector,
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "我来看一下"}},
    ) == [(ASSISTANT_DELTA, {"text": "我来看一下"})]
    assert _project(
        projector,
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "先看目录"}},
    ) == [(THINKING_DELTA, {"text": "先看目录"})]


def test_a_non_text_content_chunk_is_reported_not_invented() -> None:
    projector = AcpProjector(SecretRedactor())
    produced = _project(
        projector,
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
        },
    )
    assert [event_type for event_type, _payload in produced] == [KERNEL_STATUS]
    assert produced[0][1]["message"] == "acp_content_not_projected"
    # The notice is emitted once per content type, not once per chunk.
    again = _project(
        projector,
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
        },
    )
    assert again == []


def test_split_credentials_are_withheld_across_chunks() -> None:
    secret = "abcdefghijklmnopqrstuvwxyz012345"
    redactor = SecretRedactor([secret])
    projector = AcpProjector(redactor)
    released = ""
    for index in range(0, len(secret), 4):
        for event_type, payload in _project(
            projector,
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": secret[index : index + 4]},
            },
        ):
            assert event_type == ASSISTANT_DELTA
            released += payload["text"]
    for _event_type, payload in projector.flush():
        released += payload["text"]
    assert secret not in released
    assert "[REDACTED]" in released


# -- projection: tools and plan ----------------------------------------------


def test_tool_call_lifecycle_projects_to_started_updated_finished() -> None:
    projector = AcpProjector(SecretRedactor())
    started = _project(
        projector,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "call_1",
            "title": "运行 ls -la",
            "name": "bash",
            "kind": "execute",
            "status": "pending",
            "rawInput": {"command": "ls -la"},
        },
    )
    assert started == [
        (
            TOOL_STARTED,
            {
                "toolCallId": "call_1",
                "toolName": "bash",
                "args": {"command": "ls -la"},
                "kind": "execute",
            },
        )
    ]
    running = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_1",
            "status": "in_progress",
            "content": [{"type": "content", "content": {"type": "text", "text": "total 8\n"}}],
        },
    )
    assert running == [
        (TOOL_UPDATED, {"toolCallId": "call_1", "toolName": "bash", "output": "total 8\n"})
    ]
    finished = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_1",
            "status": "completed",
            "content": [{"type": "content", "content": {"type": "text", "text": "total 8\nreport.md"}}],
            "rawOutput": {"exitCode": 0},
        },
    )
    assert finished == [
        (
            TOOL_FINISHED,
            {
                "toolCallId": "call_1",
                "toolName": "bash",
                "isError": False,
                "result": "total 8\nreport.md",
            },
        )
    ]
    failed = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_2",
            "status": "failed",
            "content": [{"type": "content", "content": {"type": "text", "text": "ENOENT"}}],
        },
    )
    assert failed[0][0] == TOOL_FINISHED and failed[0][1]["isError"] is True


def test_tool_arguments_keep_the_shared_product_contract() -> None:
    """ACP ``rawInput`` is projected by the same bounded function pi uses."""
    raw_input = {"queries": ["a", "b"], "nested": {"x": 1}, "count": 2}
    projector = AcpProjector(SecretRedactor())
    produced = _project(
        projector,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "call_9",
            "name": "search",
            "kind": "search",
            "status": "in_progress",
            "rawInput": raw_input,
        },
    )
    assert produced[0][1]["args"] == project_args(raw_input)


def test_a_tool_call_without_a_programmatic_name_uses_the_title() -> None:
    """dsh sends ``kind: "other"`` and puts the tool name in ``title``."""
    projector = AcpProjector(SecretRedactor())
    produced = _project(
        projector,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "call_5",
            "title": "read_file",
            "kind": "other",
            "status": "pending",
            "rawInput": {"path": "notes.md"},
        },
    )
    assert produced[0][1]["toolName"] == "read_file"
    assert produced[0][1]["kind"] == "other"


def test_plan_projects_to_a_run_stage() -> None:
    projector = AcpProjector(SecretRedactor())
    produced = _project(
        projector,
        {
            "sessionUpdate": "plan",
            "entries": [
                {"content": "检查工作区", "priority": "high", "status": "in_progress"},
                {"content": "写出报告", "priority": "medium", "status": "pending"},
            ],
        },
    )
    assert produced == [
        (
            RUN_STAGE,
            {
                "stage": "plan",
                "entries": [
                    {"content": "检查工作区", "status": "in_progress", "priority": "high"},
                    {"content": "写出报告", "status": "pending", "priority": "medium"},
                ],
            },
        )
    ]


# -- projection: usage and notices -------------------------------------------


def test_usage_update_is_converted_and_labelled_not_summed() -> None:
    projector = AcpProjector(SecretRedactor())
    first = _project(
        projector,
        {"sessionUpdate": "usage_update", "used": 1200, "size": 400000, "cost": {"amount": 0.012, "currency": "USD"}},
    )
    assert first == [
        (
            USAGE_REPORTED,
            {
                "usage": {
                    "totalTokens": 1200,
                    "contextSize": 400000,
                    "basis": "acp_usage_update",
                    "cost": {"amount": 0.012, "currency": "USD"},
                },
                "requestUsage": {
                    "used": 1200,
                    "size": 400000,
                    "cost": {"amount": 0.012, "currency": "USD"},
                },
            },
        )
    ]
    second = _project(
        projector,
        {"sessionUpdate": "usage_update", "used": 2400, "size": 400000, "cost": {"amount": 0.03, "currency": "USD"}},
    )
    assert second[0][1]["usage"]["totalTokens"] == 2400
    assert second[0][1]["usage"]["basis"] == "acp_usage_update"


def test_a_malformed_usage_update_is_reported_not_guessed() -> None:
    projector = AcpProjector(SecretRedactor())
    produced = _project(projector, {"sessionUpdate": "usage_update", "used": None, "size": 400000})
    assert produced[0][0] == KERNEL_STATUS
    assert produced[0][1]["message"] == "acp_usage_invalid"
    bad_cost = _project(
        projector,
        {"sessionUpdate": "usage_update", "used": 1, "size": 2, "cost": {"amount": "free"}},
    )
    assert bad_cost[0][0] == KERNEL_STATUS
    assert bad_cost[0][1]["message"] == "acp_usage_cost_invalid"
    assert bad_cost[1][0] == USAGE_REPORTED
    assert "cost" not in bad_cost[1][1]["usage"]


def test_available_commands_and_unmapped_updates_are_notices() -> None:
    projector = AcpProjector(SecretRedactor())
    commands = _project(
        projector,
        {
            "sessionUpdate": "available_commands_update",
            "availableCommands": [{"name": "plan", "description": "只输出计划"}],
        },
    )
    assert commands[0][0] == KERNEL_STATUS
    assert commands[0][1]["level"] == "warning"
    assert commands[0][1]["message"] == "acp_available_commands"
    assert commands[0][1]["commands"] == [{"name": "plan", "description": "只输出计划"}]

    mode = _project(projector, {"sessionUpdate": "current_mode_update", "currentModeId": "code"})
    assert mode[0][0] == KERNEL_STATUS
    assert mode[0][1]["message"] == "acp_update_not_projected"
    assert _project(projector, {"sessionUpdate": "current_mode_update", "currentModeId": "code"}) == []


def test_permission_and_tool_results_are_redacted() -> None:
    secret = "abcdefghijklmnopqrstuvwxyz012345"
    projector = AcpProjector(SecretRedactor([secret]))
    notice = projector.project(
        {
            "type": "acp_permission",
            "decision": "rejected",
            "optionId": "reject-once",
            "optionKind": "reject_once",
            "toolCall": {"toolCallId": "call_1", "title": "运行 env"},
        }
    )
    assert notice[0][0] == KERNEL_STATUS
    assert notice[0][1]["message"] == "acp_permission_denied"
    assert notice[0][1]["decision"] == "rejected"
    finished = _project(
        projector,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_1",
            "status": "completed",
            "content": [
                {"type": "content", "content": {"type": "text", "text": f"KEY={secret}"}}
            ],
        },
    )
    assert secret not in finished[0][1]["result"]
    assert "[REDACTED]" in finished[0][1]["result"]


# -- the capability negotiation contract -------------------------------------


def test_the_client_advertises_no_filesystem_or_terminal_capability() -> None:
    """The agent owns its filesystem; the host offers no client-side methods."""
    assert CLIENT_CAPABILITIES["fs"] == {"readTextFile": False, "writeTextFile": False}
    assert CLIENT_CAPABILITIES["terminal"] is False
    assert CLIENT_CAPABILITIES["auth"] == {"terminal": False}


def test_fake_agent_command_helper_is_available_to_the_runner_tests() -> None:
    """The command template used by the lifecycle tests is shlex-splittable."""
    settings = REPO_ROOT / "tests" / "fixtures" / "fake_acp.py"
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(settings))}"
    assert shlex.split(command) == [sys.executable, str(settings)]
