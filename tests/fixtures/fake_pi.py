#!/usr/bin/env python3
"""Minimal stand-in for ``pi --mode rpc`` used by host-side regressions.

It implements just enough of the documented protocol to exercise framing,
request/response correlation, error responses, process death and an event
sequence that mirrors a real research turn (thinking, text, a tool call with
streaming output, a second tool call that fails).

Behaviour is selected by the ``type`` of each incoming command:

===========================  ===========================================
``get_state``                responds with a tiny state object
``prompt``                   responds, then emits a scripted event sequence;
                             writes ``report.md`` into cwd when
                             ``FAKE_PI_WRITE_REPORT=1``; never settles when the
                             message contains ``NOSETTLE``
``abort``                    responds, then emits ``agent_settled``
``proto_frames``             responds, then writes raw frames in one chunk
``silent``                   never responds (client-side timeout test)
``exit``                     terminates the process without responding
anything else                error response (``Unknown command: …``)
===========================  ===========================================
"""

from __future__ import annotations

import json
import os
import sys

COMMANDS = {"get_state", "prompt", "abort", "proto_frames", "silent", "exit"}

PROMPT_EVENTS = [
    {"type": "agent_start"},
    {"type": "turn_start"},
    {
        "type": "message_update",
        "usage": {"input": 10, "output": 2, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 12},
        "assistantMessageEvent": {"type": "thinking_start", "contentIndex": 0},
    },
    {
        "type": "message_update",
        "usage": {},
        "assistantMessageEvent": {"type": "thinking_delta", "contentIndex": 0, "delta": "先看目录结构"},
    },
    {
        "type": "message_update",
        "usage": {},
        "assistantMessageEvent": {"type": "thinking_end", "contentIndex": 0, "thinking": "先看目录结构"},
    },
    {
        "type": "message_update",
        "usage": {},
        "assistantMessageEvent": {"type": "text_delta", "contentIndex": 1, "delta": "我来看一下工作区。"},
    },
    {
        "type": "message_update",
        "usage": {},
        "assistantMessageEvent": {
            "type": "toolcall_start",
            "contentIndex": 2,
            "id": "call_1",
            "toolName": "bash",
        },
    },
    {"type": "tool_execution_start", "toolCallId": "call_1", "toolName": "bash", "args": {"command": "ls -la"}},
    {
        "type": "tool_execution_update",
        "toolCallId": "call_1",
        "toolName": "bash",
        "args": {"command": "ls -la"},
        "partialResult": "total 8\n",
    },
    {
        "type": "tool_execution_end",
        "toolCallId": "call_1",
        "toolName": "bash",
        "result": {"content": [{"type": "text", "text": "total 8\nreport.md"}]},
        "isError": False,
    },
    {"type": "tool_execution_start", "toolCallId": "call_2", "toolName": "read", "args": {"path": "missing.md"}},
    {
        "type": "tool_execution_end",
        "toolCallId": "call_2",
        "toolName": "read",
        "result": {"content": [{"type": "text", "text": "ENOENT: no such file"}]},
        "isError": True,
    },
    {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "先看目录结构"},
                {"type": "text", "text": "我来看一下工作区。"},
            ],
            "stopReason": "stop",
            "usage": {"input": 120, "output": 40, "totalTokens": 160},
        },
    },
    {
        "type": "turn_end",
        "message": {
            "role": "assistant",
            "usage": {"input": 120, "output": 40, "totalTokens": 160},
        },
        "toolResults": [],
    },
    {"type": "agent_end", "messages": [], "willRetry": False},
    {"type": "agent_settled"},
]


def emit(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def respond(request: dict, *, success: bool = True, data: object = None, error: str | None = None) -> None:
    frame: dict[str, object] = {
        "type": "response",
        "command": request.get("type"),
        "success": success,
    }
    if request.get("id") is not None:
        frame["id"] = request["id"]
    if data is not None:
        frame["data"] = data
    if error is not None:
        frame["error"] = error
    emit(frame)


def raw_chunk() -> None:
    """Write several frames plus one oversized frame in a single write.

    ``\\r\\n`` line endings and a U+2028 inside a JSON string both appear here:
    the client must split on LF only and keep U+2028 inside the payload.
    """
    frames = [
        {"type": "turn_start"},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "line\u2028separator"}},
        {"type": "turn_end", "message": {"role": "assistant"}, "toolResults": []},
    ]
    blob = "".join(json.dumps(frame, ensure_ascii=False) + "\r\n" for frame in frames)
    sys.stdout.write(blob)
    sys.stdout.flush()


def write_report(prompt: str) -> None:
    """Stand in for the artifact the research contract asks pi to produce."""
    from pathlib import Path

    body = f"# 研究报告（fake pi）\n\n原始问题：{prompt}\n\n结论：这是测试替身产出的报告。\n"
    Path("report.md").write_text(body, encoding="utf-8")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        command = request.get("type")
        if command == "get_state":
            respond(
                request,
                data={
                    "model": {"id": "fake-model", "provider": "fake"},
                    "thinkingLevel": "medium",
                    "isStreaming": False,
                    "isCompacting": False,
                    "sessionId": "fake-session",
                    "sessionFile": "/tmp/fake-session.jsonl",
                    "messageCount": 0,
                },
            )
        elif command == "prompt":
            respond(request)
            message = str(request.get("message") or "")
            if os.environ.get("FAKE_PI_WRITE_REPORT") == "1":
                write_report(message)
            for event in PROMPT_EVENTS:
                # ``NOSETTLE`` keeps the run alive: the agent never settles, so
                # the host's timeout/cancel paths can be exercised.
                if "NOSETTLE" in message and event.get("type") == "agent_settled":
                    continue
                emit(event)
        elif command == "abort":
            respond(request)
            emit({"type": "agent_settled"})
        elif command == "proto_frames":
            respond(request)
            raw_chunk()
        elif command == "silent":
            continue
        elif command == "exit":
            return 0
        else:
            respond(request, success=False, error=f"Unknown command: {command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
