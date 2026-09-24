#!/usr/bin/env python3
"""Minimal ACP agent stand-in used by the host-side ACP regressions.

Implements just enough of the Agent Client Protocol v1 to exercise the client,
the projector and the run lifecycle end to end: ``initialize`` with capability
negotiation, ``session/new``, ``session/update`` notifications (streamed content,
tool calls, a plan, usage, available commands), ``session/request_permission``
(agent -> client), ``session/cancel`` and ``session/prompt``'s ``stopReason``.

Transport is the documented stdio shape: UTF-8, one JSON-RPC 2.0 message per
line, LF-delimited. ``chunked`` writes the first three notifications in a single
raw write with CRLF endings, so the client's LF-only framing is exercised the
way the pi face exercises it.

Settings come from an optional ``--config <file.json>`` argument (the host
passes a filtered environment, so the harness has no other channel for them) and
are overridden by ``FAKE_ACP_*`` environment variables when present. Facts worth
asserting are appended to the ``log`` path as JSON lines: ``initialize`` (what
the client asked for), ``session_new`` (the ``cwd``), ``home``, ``prompt`` (the
user text), ``permission_outcome`` (the client's answer), ``cancel_received``
and ``prompt_done`` (the stop reason).

Prompt markers select the terminal result:

=====================  ======================================================
``MAXTOKENS``          ``stopReason=max_tokens``
``REFUSAL``            ``stopReason=refusal``
``MAXTURN``            ``stopReason=max_turn_requests``
``CANCEL``             ``stopReason=cancelled`` without a client cancel
``NOSETTLE``           never answers until ``session/cancel`` arrives
``CRASH``              exits (code 3) after accepting the prompt, no answer
``FAILPROMPT``         answers with a JSON-RPC error instead of a stop reason
``UNKNOWNSTOP``        answers with a stop reason outside the v1 set
``USAGE_TWICE``        two ``usage_update`` notifications (replace semantics)
``SECRET``             streams ``secret`` in small chunks
``NONUPDATE``          one unmapped update type and one non-text content chunk
=====================  ======================================================
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_SECRET = "sk-live-abcdefghijklmnopqrstuvwxyz"
DEFAULT_PERMISSION_OPTIONS = [
    {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
]
DEFAULT_CAPABILITIES: dict[str, Any] = {
    "loadSession": False,
    "promptCapabilities": {"image": False, "audio": False, "embeddedContext": False},
    "sessionCapabilities": {"resume": {}},
}

_send_lock = threading.Lock()
_log_lock = threading.Lock()
_pending_lock = threading.Lock()
_pending: dict[int, list[Any]] = {}
_request_counter = 0
_cancel = threading.Event()
CONFIG: dict[str, Any] = {}


# -- settings -----------------------------------------------------------------


def setting(name: str, default: Any = None) -> Any:
    """One knob: the environment wins over the ``--config`` file."""
    if name in os.environ:
        return os.environ[name]
    return CONFIG.get(name, default)


def flag(name: str) -> bool:
    raw = setting(name)
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    return str(raw) == "1" or str(raw).lower() in {"true", "yes", "on"}


def structured(name: str, default: Any) -> Any:
    raw = setting(name)
    if raw is None:
        return default
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default
    return raw


def session_id() -> str:
    return str(setting("FAKE_ACP_SESSION_ID", "fake-acp-session"))


def log(event: str, **fields: Any) -> None:
    path = setting("FAKE_ACP_LOG")
    if not path:
        return
    line = json.dumps({"event": event, **fields}, ensure_ascii=False)
    with _log_lock:
        with open(str(path), "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def send(frame: dict) -> None:
    with _send_lock:
        sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def send_chunked(frames: list[dict]) -> None:
    """Write several frames in one raw chunk with CRLF endings (framing test)."""
    blob = "".join(json.dumps(frame, ensure_ascii=False) + "\r\n" for frame in frames)
    with _send_lock:
        sys.stdout.write(blob)
        sys.stdout.flush()


def respond(request: dict, *, result: Any = None, error: dict | None = None) -> None:
    frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request.get("id")}
    if error is not None:
        frame["error"] = error
    else:
        frame["result"] = result if result is not None else {}
    send(frame)


def notify(method: str, params: dict) -> None:
    send({"jsonrpc": "2.0", "method": method, "params": params})


def send_request(method: str, params: dict, *, timeout: float = 15.0) -> dict:
    """Send one request to the client and wait for its response frame."""
    global _request_counter
    with _pending_lock:
        _request_counter += 1
        request_id = _request_counter
        entry: list[Any] = [threading.Event(), None]
        _pending[request_id] = entry
    send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    entry[0].wait(timeout)
    with _pending_lock:
        _pending.pop(request_id, None)
    frame = entry[1]
    return frame if isinstance(frame, dict) else {"error": {"code": -32001, "message": "timeout"}}


def capabilities() -> dict:
    parsed = structured("FAKE_ACP_CAPABILITIES", DEFAULT_CAPABILITIES)
    return dict(parsed) if isinstance(parsed, dict) else dict(DEFAULT_CAPABILITIES)


def prompt_text(params: dict) -> str:
    blocks = params.get("prompt")
    if not isinstance(blocks, list) or not blocks:
        log("prompt_invalid", prompt=blocks)
        return ""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        else:
            log("prompt_invalid", prompt=block)
    return "\n".join(parts)


def write_report(text: str) -> None:
    body = f"# 研究报告（fake acp）\n\n原始问题：{text}\n\n结论：这是 ACP 测试替身产出的报告。\n"
    Path("report.md").write_text(body, encoding="utf-8")


def _text_chunk(kind: str, text: str) -> dict:
    return {"sessionUpdate": kind, "content": {"type": "text", "text": text}}


def prompt_updates(text: str) -> list[dict]:
    thought = "先看目录结构"
    answer = "我来看一下工作区。"
    if flag("FAKE_ACP_SEPARATORS"):
        # U+2028/U+2029 are content inside a JSON string, never frame boundaries.
        thought = "line\u2028separator"
        answer = "para\u2029separator"
    updates = [
        _text_chunk("agent_thought_chunk", thought),
        _text_chunk("agent_message_chunk", answer),
        {
            "sessionUpdate": "plan",
            "entries": [
                {"content": "检查工作区", "priority": "high", "status": "in_progress"},
                {"content": "写出报告", "priority": "medium", "status": "pending"},
            ],
        },
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "call_1",
            "title": "运行 ls -la",
            "name": "bash",
            "kind": "execute",
            "status": "pending",
            "rawInput": {"command": "ls -la"},
        },
        {
            "sessionUpdate": "usage_update",
            "used": 1200,
            "size": 400000,
            "cost": {"amount": 0.012, "currency": "USD"},
        },
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_1",
            "status": "in_progress",
            "content": [
                {"type": "content", "content": {"type": "text", "text": "total 8\nreport.md"}}
            ],
        },
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_1",
            "status": "completed",
            "content": [
                {"type": "content", "content": {"type": "text", "text": "total 8\nreport.md"}}
            ],
            "rawOutput": {"exitCode": 0},
        },
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "call_2",
            "title": "读取 missing.md",
            "name": "read",
            "kind": "read",
            "status": "pending",
        },
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_2",
            "status": "failed",
            "content": [
                {"type": "content", "content": {"type": "text", "text": "ENOENT: no such file"}}
            ],
        },
    ]
    if "USAGE_TWICE" in text:
        updates.append(
            {
                "sessionUpdate": "usage_update",
                "used": 2400,
                "size": 400000,
                "cost": {"amount": 0.03, "currency": "USD"},
            }
        )
    if "SECRET" in text:
        secret = str(setting("FAKE_ACP_SECRET") or DEFAULT_SECRET)
        for index in range(0, len(secret), 4):
            updates.append(_text_chunk("agent_message_chunk", secret[index : index + 4]))
    if "NONUPDATE" in text:
        updates.append(
            {
                "sessionUpdate": "session_info_update",
                "title": "测试会话",
                "updatedAt": "2026-09-24T00:00:00Z",
            }
        )
        updates.append(
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
            }
        )
    updates.append(_text_chunk("agent_message_chunk", "工作区里有一份报告草稿。"))
    return updates


def stop_reason(text: str) -> str:
    if _cancel.is_set():
        return "cancelled"
    for marker, reason in (
        ("MAXTOKENS", "max_tokens"),
        ("REFUSAL", "refusal"),
        ("MAXTURN", "max_turn_requests"),
        ("CANCEL", "cancelled"),
        ("UNKNOWNSTOP", "made_up_reason"),
    ):
        if marker in text:
            return reason
    return str(setting("FAKE_ACP_STOP_REASON") or "end_turn")


def ask_permission() -> None:
    options = structured("FAKE_ACP_PERMISSION_OPTIONS", DEFAULT_PERMISSION_OPTIONS)
    frame = send_request(
        "session/request_permission",
        {
            "sessionId": session_id(),
            "toolCall": {"toolCallId": "call_1", "title": "运行 ls -la"},
            "options": options,
        },
    )
    result = frame.get("result")
    outcome = result.get("outcome") if isinstance(result, dict) else None
    log("permission_outcome", outcome=outcome, error=frame.get("error"))


def run_prompt(request: dict, params: dict) -> None:
    text = prompt_text(params)
    log("prompt", text=text, sessionId=params.get("sessionId"))
    if "CRASH" in text:
        sys.stdout.flush()
        os._exit(3)
    updates = prompt_updates(text)
    permission_index = next(
        (
            index
            for index, update in enumerate(updates)
            if update.get("sessionUpdate") == "tool_call"
            and update.get("toolCallId") == "call_1"
        ),
        None,
    )
    permission_enabled = flag("FAKE_ACP_PERMISSION")
    chunked = flag("FAKE_ACP_CHUNKED")
    buffered: list[dict] = []

    def flush_buffered() -> None:
        if not buffered:
            return
        send_chunked(
            [
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {"sessionId": session_id(), "update": update},
                }
                for update in buffered
            ]
        )
        buffered.clear()

    for index, update in enumerate(updates):
        if _cancel.is_set():
            break
        if index == permission_index and permission_enabled:
            flush_buffered()
            ask_permission()
        if chunked and len(buffered) < 3:
            buffered.append(update)
            continue
        flush_buffered()
        notify("session/update", {"sessionId": session_id(), "update": update})
        time.sleep(0.002)
    flush_buffered()
    if flag("FAKE_ACP_WRITE_REPORT"):
        write_report(text)
    if "NOSETTLE" in text:
        _cancel.wait(timeout=30.0)
    if "FAILPROMPT" in text:
        respond(request, error={"code": -32000, "message": "prompt rejected by fake agent"})
        log("prompt_done", error="rejected")
        return
    reason = stop_reason(text)
    if flag("FAKE_ACP_NO_STOP_REASON"):
        respond(request, result={})
    else:
        respond(request, result={"stopReason": reason})
    log("prompt_done", stopReason=reason)


def handle_initialize(request: dict) -> None:
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    log(
        "initialize",
        protocolVersion=params.get("protocolVersion"),
        clientCapabilities=params.get("clientCapabilities"),
        clientInfo=params.get("clientInfo"),
    )
    if flag("FAKE_ACP_INIT_ERROR"):
        respond(request, error={"code": -32000, "message": "authentication required"})
        return
    requested = params.get("protocolVersion")
    advertised = int(setting("FAKE_ACP_PROTOCOL_VERSION", 1))
    respond(
        request,
        result={
            "protocolVersion": advertised
            if advertised != 1
            else (requested if isinstance(requested, int) else 1),
            "agentInfo": {"name": "fake-acp", "version": "0.1.0"},
            "agentCapabilities": capabilities(),
            "authMethods": [],
        },
    )


def handle_new_session(request: dict) -> None:
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    log("session_new", cwd=params.get("cwd"), mcpServers=params.get("mcpServers"))
    home = os.environ.get("HOME", "")
    log("home", home=home)
    if home:
        try:
            Path(home, "fake-acp-state.json").write_text(
                json.dumps({"sessionId": session_id()}), encoding="utf-8"
            )
        except OSError as exc:
            log("home_write_failed", error=str(exc))
    respond(request, result={"sessionId": session_id()})
    if not flag("FAKE_ACP_NO_COMMANDS"):
        notify(
            "session/update",
            {
                "sessionId": session_id(),
                "update": {
                    "sessionUpdate": "available_commands_update",
                    "availableCommands": [
                        {"name": "deepresearch", "description": "做一轮完整检索"},
                        {"name": "plan", "description": "只输出计划"},
                    ],
                },
            },
        )
    if flag("FAKE_ACP_CURRENT_MODE"):
        notify(
            "session/update",
            {
                "sessionId": session_id(),
                "update": {"sessionUpdate": "current_mode_update", "currentModeId": "code"},
            },
        )


def deliver_response(frame: dict) -> None:
    with _pending_lock:
        entry = _pending.get(frame.get("id"))
    if entry is None:
        return
    entry[1] = frame
    entry[0].set()


def main() -> int:
    argv = sys.argv[1:]
    if "--config" in argv:
        index = argv.index("--config")
        if index + 1 < len(argv):
            path = Path(argv[index + 1])
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    CONFIG.update(parsed)
            except (OSError, json.JSONDecodeError) as exc:
                print(f"fake_acp: cannot read config {path}: {exc}", file=sys.stderr)
                return 2
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(frame, dict):
            continue
        method = frame.get("method")
        if method is None and "id" in frame:
            deliver_response(frame)
            continue
        if method == "initialize":
            handle_initialize(frame)
        elif method == "session/new":
            handle_new_session(frame)
        elif method == "session/prompt":
            threading.Thread(
                target=run_prompt, args=(frame, frame.get("params") or {}), daemon=True
            ).start()
        elif method == "session/cancel":
            _cancel.set()
            log("cancel_received", sessionId=(frame.get("params") or {}).get("sessionId"))
        elif method == "session/resume":
            respond(frame, error={"code": -32601, "message": "Method not found: session/resume"})
        else:
            respond(frame, error={"code": -32601, "message": f"Method not found: {method}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
