#!/usr/bin/env python3
"""Minimal ACP agent stand-in used by the host-side ACP regressions.

Implements just enough of the Agent Client Protocol v1 to exercise the client,
the projector and the run lifecycle end to end: ``initialize`` with capability
negotiation, ``session/new``, ``session/resume`` / ``session/load`` (state-root
backed), ``session/update`` notifications (streamed content, tool calls, a plan,
usage, available commands), ``session/request_permission`` (agent -> client),
``session/cancel`` and ``session/prompt``'s ``stopReason``.

Transport is the documented stdio shape: UTF-8, one JSON-RPC 2.0 message per
line, LF-delimited. ``chunked`` writes the first three notifications in a single
raw write with CRLF endings, so the client's LF-only framing is exercised the
way the pi face exercises it.

Settings come from an optional ``--config <file.json>`` argument (the host
passes a filtered environment, so the harness has no other channel for them) and
are overridden by ``FAKE_ACP_*`` environment variables when present. Facts worth
asserting are appended to the ``log`` path as JSON lines: ``initialize`` (what
the client asked for), ``session_new`` (the ``cwd``), ``resume`` / ``load`` (the
resume target and the state found for it), ``home``, ``env_probe``
(``FAKE_ACP_ENV_PROBE`` names one variable to report), ``config_option``
(``session/set_config_option`` calls), ``prompt`` (the user text),
``permission_outcome`` (the client's answer), ``cancel_received`` and
``prompt_done`` (the stop reason).

Session state lives in ``$HOME/fake-acp-state.json`` — the stand-in for a real
agent's session store. A ``session/resume`` only succeeds when that file exists
and records the session id being resumed, so state inheritance is observable:
the same file must have been copied into the child run's HOME. The first prompt
remembers a marker (``REMEMBER:<word>`` or the live probe's ``代号：<word>``) in
the state file, and a later prompt containing ``RECALL`` appends the remembered
word to the answer and to ``report.md`` (``UNKNOWN`` when nothing was
remembered/read) — the synthetic continuity check. ``write_report`` strips the
marker from the deliverable, exactly as the probe instructs the real agent.

Prompt markers select the terminal result:

=====================  ======================================================
``MAXTOKENS``          ``stopReason=max_tokens``
``REFUSAL``            ``stopReason=refusal``
``MAXTURN``            ``stopReason=max_turn_requests``
``CANCEL``             ``stopReason=cancelled`` without a client cancel
``NOSETTLE``           never answers until ``session/cancel`` arrives;
                       ``FAKE_ACP_CANCEL_HANG`` makes it ignore the cancel and
                       keep running, so only the host's teardown ends it
``CRASH``              exits (code 3) after accepting the prompt, no answer;
                       ``FAKE_ACP_CRASH_EXIT_DELAY`` closes stdout first and
                       then waits that long, widening the exit window;
                       ``FAKE_ACP_STDERR`` writes one line to stderr before the
                       crash (the host's kernel_exit report must carry it)
``SUBAGENT``           dispatches one ``subagent`` tool call and receives dsh's
                       asynchronous acknowledgement (``started subagent
                       <childId>``); the child runs on afterwards
``FAILPROMPT``         answers with a JSON-RPC error instead of a stop reason
``UNKNOWNSTOP``        answers with a stop reason outside the v1 set
``USAGE_TWICE``        two ``usage_update`` notifications (replace semantics)
``SECRET``             streams ``secret`` in small chunks
``NONUPDATE``          one unmapped update type and one non-text content chunk
=====================  ======================================================

The permission-request order is selectable because real agents differ:
``FAKE_ACP_PERMISSION`` asks before the ``tool_call`` notification for
``call_1`` (the M1 order), while ``FAKE_ACP_PERMISSION_AFTER_TOOL_CALL`` asks
after it -- the order an ACP agent used in the M4 measurement, where the
request omits ``toolCall.kind`` but the notification already published it.
``FAKE_ACP_TOOL_CALL_KIND`` overrides the kind of that notification (default
``execute``).
"""

from __future__ import annotations

import json
import os
import re
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
_session_config_options: Any = None


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


def state_path() -> Path | None:
    home = os.environ.get("HOME", "")
    if not home:
        return None
    return Path(home, "fake-acp-state.json")


def read_state() -> dict:
    path = state_path()
    if path is None or not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def write_state(state: dict) -> None:
    path = state_path()
    if path is None:
        return
    try:
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError as exc:
        log("home_write_failed", error=str(exc))


def remember(text: str) -> None:
    """Record the prompt's codename marker in the state file.

    Two spellings mean the same session fact: the synthetic fixtures use
    ``REMEMBER:<word>`` and the live probe asks in Chinese (``代号：<word>``).
    """
    marker = "REMEMBER:"
    index = text.find(marker)
    if index >= 0:
        tail = text[index + len(marker) :]
        word = re.split(r"[\s,，。.;；、：:）)(（\"']", tail, maxsplit=1)[0].strip()
    else:
        match = re.search(r"代号[：:]\s*([A-Za-z0-9][A-Za-z0-9-]*)", text)
        word = match.group(1) if match else ""
    if not word:
        return
    state = read_state()
    state["sessionId"] = session_id()
    state["remembered"] = word
    write_state(state)
    log("remembered", word=word)


def recalled_word() -> str:
    state = read_state()
    value = state.get("remembered")
    return value if isinstance(value, str) and value else "UNKNOWN"


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


def asks_to_recall(text: str) -> bool:
    """Does this prompt ask the agent to state the remembered codename?

    The synthetic fixtures say ``RECALL``; the live probe asks in Chinese
    (``…代号是什么？``). Both are the same request to the stand-in agent.
    """
    return "RECALL" in text or "复述" in text or "代号是什么" in text


def write_report(text: str) -> None:
    """Write the run's deliverable, obeying the codename instructions.

    A ``REMEMBER:<word>`` / ``代号：<word>`` marker is a session fact the probe
    asks the agent **not** to persist, so it is stripped from the report body;
    a ``RECALL`` prompt appends the remembered word, which is what the real
    continuity check asks the agent to do.
    """
    body_text = re.sub(r"(?:REMEMBER:|代号[：:])\s*\S+", "（代号已按指令隐去）", text)
    body = f"# 研究报告（fake acp）\n\n原始问题：{body_text}\n\n结论：这是 ACP 测试替身产出的报告。\n"
    if asks_to_recall(text):
        body += f"\n代号 = {recalled_word()}\n"
    Path("report.md").write_text(body, encoding="utf-8")


def _text_chunk(kind: str, text: str) -> dict:
    return {"sessionUpdate": kind, "content": {"type": "text", "text": text}}


def _subagent_ack_updates() -> list[dict]:
    """dsh 的异步 ``subagent`` 派发：工具调用 + ``started subagent <id>`` 确认。

    形状取自 GLINK-OBS-01 的真实 dsh 会话（工具终态文本就是
    ``started subagent <childId>``），子代理在确认之后才继续运行。
    """
    child_id = str(
        setting("FAKE_ACP_SUBAGENT_CHILD_ID") or "0c8309aa-9469-45f1-9d8f-6c47b58fe4b5"
    )
    return [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "call_subagent",
            "title": "subagent",
            "name": "subagent",
            "kind": "other",
            "status": "pending",
            "rawInput": {
                "description": "核实中国香港跨境架构",
                "prompt": "独立研究该法域的个人税收居民与股权转让口径。",
            },
        },
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_subagent",
            "status": "completed",
            "content": [
                {
                    "type": "content",
                    "content": {"type": "text", "text": f"started subagent {child_id}"},
                }
            ],
        },
    ]


def prompt_updates(text: str) -> list[dict]:
    """The notification sequence one prompt produces.

    ``FAKE_ACP_TOOL_CALL_KIND`` sets the ACP kind announced for ``call_1``
    (default ``execute``), so a test can pair the published kind with a
    permission request that omits its own ``kind``.
    """
    tool_call_kind = str(setting("FAKE_ACP_TOOL_CALL_KIND", "execute") or "")
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
            **(({"kind": tool_call_kind}) if tool_call_kind else {}),
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
    declared = structured("FAKE_ACP_PRESENT_FILES", [])
    if declared:
        for item in declared:
            if "text" in item:
                target = Path(item["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(item["text"], encoding="utf-8")
            if "link" in item:
                target = Path(item["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(item["link"])
        for offset in range(0, len(declared), 8):
            call_id = f"deliver_{offset}"
            updates.append({
                "sessionUpdate": "tool_call", "toolCallId": call_id, "name": "present",
                "kind": "other", "status": "pending",
                "rawInput": {"files": [
                    {"path": item["path"], "description": item.get("description", "")}
                    for item in declared[offset:offset + 8]
                ]},
            })
            updates.append({"sessionUpdate": "tool_call_update", "toolCallId": call_id,
                            "status": "completed", "content": []})
    if "SUBAGENT" in text:
        updates[0:0] = _subagent_ack_updates()
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
    if asks_to_recall(text):
        updates.append(
            _text_chunk("agent_message_chunk", f"REMEMBERED:{recalled_word()}")
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
    tool_call: dict = {"toolCallId": "call_1", "title": "运行 ls -la"}
    tool_kind = setting("FAKE_ACP_PERMISSION_TOOL_KIND")
    if tool_kind:
        tool_call["kind"] = str(tool_kind)
    frame = send_request(
        "session/request_permission",
        {
            "sessionId": session_id(),
            "toolCall": tool_call,
            "options": options,
        },
    )
    result = frame.get("result")
    outcome = result.get("outcome") if isinstance(result, dict) else None
    log("permission_outcome", outcome=outcome, toolKind=tool_call.get("kind"), error=frame.get("error"))


def run_prompt(request: dict, params: dict) -> None:
    text = prompt_text(params)
    log("prompt", text=text, sessionId=params.get("sessionId"))
    remember(text)
    if "CRASH" in text:
        sys.stdout.flush()
        if "SUBAGENT" in text:
            # 先派发、后崩溃：宿主已经看到 subagent_started，子代理仍在
            # running（真实 dsh r2 就是这个顺序）。
            for update in _subagent_ack_updates():
                notify("session/update", {"sessionId": session_id(), "update": update})
        crash_stderr = str(setting("FAKE_ACP_STDERR") or "")
        if crash_stderr:
            # The agent's own explanation of the crash: ACP speaks JSON-RPC on
            # stdout, so stderr is the only channel that carries it.
            sys.stderr.write(crash_stderr + "\n")
            sys.stderr.flush()
        delay = float(setting("FAKE_ACP_CRASH_EXIT_DELAY", 0) or 0)
        if delay > 0:
            # The crash's event order, widened: the protocol stream ends (the
            # client reads EOF) before this process exits and is reapable --
            # exactly what ``os._exit`` does, where the two are microseconds
            # apart. The holder thread is deliberately *not* a daemon: when the
            # host closes stdin, the main loop would otherwise exit 0 and end
            # the process before the delayed ``os._exit(3)``, which would test
            # the wrong death.
            threading.Thread(target=time.sleep, args=(delay * 4,), daemon=False).start()
            os.close(1)
            time.sleep(delay)
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
    permission_after_tool_call = flag("FAKE_ACP_PERMISSION_AFTER_TOOL_CALL")
    # The published ``tool_call`` must reach the client before a permission
    # request that relies on it, so that one update is never buffered.
    ask_after_here = permission_enabled and permission_after_tool_call
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
        if index == permission_index and permission_enabled and not permission_after_tool_call:
            flush_buffered()
            ask_permission()
        if chunked and len(buffered) < 3:
            if index == permission_index and ask_after_here:
                flush_buffered()
            else:
                buffered.append(update)
                continue
        flush_buffered()
        notify("session/update", {"sessionId": session_id(), "update": update})
        if index == permission_index and permission_enabled and permission_after_tool_call:
            ask_permission()
        time.sleep(0.002)
    flush_buffered()
    if flag("FAKE_ACP_WRITE_REPORT"):
        write_report(text)
    if "NOSETTLE" in text:
        _cancel.wait(timeout=30.0)
        if flag("FAKE_ACP_CANCEL_HANG"):
            # A hung agent: the cancel arrived and the agent never answers, so
            # the driver's ``session/prompt`` request only unblocks when the
            # host tears the pipe down -- after the host has already decided
            # the deadline verdict. That is the ordering the late-write
            # regression needs, held open instead of raced.
            return
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
            "agentInfo": {
                "name": setting("FAKE_ACP_AGENT_NAME", "fake-acp"),
                "version": "0.1.0",
            },
            "agentCapabilities": capabilities(),
            "authMethods": [],
        },
    )


def handle_new_session(request: dict) -> None:
    global _session_config_options
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    log("session_new", cwd=params.get("cwd"), mcpServers=params.get("mcpServers"))
    home = os.environ.get("HOME", "")
    log("home", home=home)
    if home:
        state = read_state()
        state["sessionId"] = session_id()
        write_state(state)
        legacy_rows = structured("FAKE_ACP_DSH_WRITE_V3_ROWS", None)
        if isinstance(legacy_rows, list):
            import zstandard

            path = Path(home) / ".dsh/sessions/--workspace--" / session_id() / "session.v3.jsonl.zstd"
            path.parent.mkdir(parents=True, exist_ok=True)
            records = [{"type": "session", "version": 3, "id": session_id()}, *legacy_rows]
            path.write_bytes(b"".join(zstandard.ZstdCompressor(write_checksum=True).compress(
                json.dumps(row).encode() + b"\n"
            ) for row in records))
            (path.parent / "session.lock").write_bytes(b"")
    result: dict = {"sessionId": session_id()}
    probe = os.environ.get("FAKE_ACP_ENV_PROBE")
    if probe:
        log("env_probe", name=probe, value=os.environ.get(probe))
    _session_config_options = structured("FAKE_ACP_CONFIG_OPTIONS", None)
    if _session_config_options is not None:
        result["configOptions"] = _session_config_options
    modes = structured("FAKE_ACP_MODES", None)
    if modes is not None:
        result["modes"] = modes
    respond(request, result=result)
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


def handle_resume(request: dict, method: str) -> None:
    """``session/resume`` (no replay) and ``session/load`` (replays) stand-in.

    Both require the session to exist in this process's HOME state file: a run
    whose parent state was not inherited therefore fails to resume, exactly like
    a real agent that cannot find the session. ``FAKE_ACP_RESUME_ERROR`` forces
    the failure for the fail-closed regression; ``FAKE_ACP_RESUME_ALLOW_MISSING``
    lets a test exercise the protocol path without a state root.
    """
    global _session_config_options
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    target = params.get("sessionId")
    state = read_state()
    log(
        method.replace("/", "_"),
        sessionId=target,
        cwd=params.get("cwd"),
        mcpServers=params.get("mcpServers"),
        stateSessionId=state.get("sessionId"),
        remembered=state.get("remembered"),
    )
    if flag("FAKE_ACP_DSH_V3_LOG"):
        # Substitute only the external dsh parser: consume the actual inherited
        # log, with the released frozen-vocabulary refusal, before accepting
        # resume or letting the host apply model configuration/send a prompt.
        import zstandard

        known = set(structured("FAKE_ACP_DSH_KNOWN_EVENTS", ["tool/result"]))
        candidates = list(Path(os.environ["HOME"]).glob(
            f".dsh/sessions/*/{target}/session.v3.jsonl.zstd"
        ))
        if len(candidates) != 1:
            respond(request, error={"code": -32602, "message": "session log not found"})
            return
        with zstandard.ZstdDecompressor().stream_reader(candidates[0].read_bytes()) as stream:
            rows = [json.loads(line) for line in stream.read().splitlines()]
        for event in rows[1:]:
            if event["type"] not in known and event.get("ignorable") is not True:
                respond(request, error={
                    "code": -32603, "message": "Internal error",
                    "data": {"details": "unknown to this harness and not marked ignorable"},
                })
                return
    if flag("FAKE_ACP_RESUME_ERROR"):
        respond(request, error={"code": -32000, "message": "resume refused by fake agent"})
        return
    resume_details = setting("FAKE_ACP_RESUME_ERROR_DETAILS")
    if isinstance(resume_details, str):
        # dsh's shape: a generic -32603 whose real cause is only in data.details.
        respond(
            request,
            error={
                "code": -32603,
                "message": "Internal error",
                "data": {"details": resume_details},
            },
        )
        return
    if not flag("FAKE_ACP_RESUME_ALLOW_MISSING") and (
        not state or state.get("sessionId") != target
    ):
        respond(
            request,
            error={"code": -32602, "message": f"session not found: {target}"},
        )
        return
    if method == "session/load":
        # The replaying method sends the parent's conversation as ordinary
        # updates before answering; the host must not project them twice.
        count = int(setting("FAKE_ACP_LOAD_REPLAY_UPDATES", 2) or 0)
        for index in range(count):
            notify(
                "session/update",
                {
                    "sessionId": target,
                    "update": _text_chunk(
                        "agent_message_chunk", f"REPLAYED-HISTORY-{index}"
                    ),
                },
            )
        log("load_replayed", count=count)
    _session_config_options = structured("FAKE_ACP_CONFIG_OPTIONS", None)
    result = {} if _session_config_options is None else {"configOptions": _session_config_options}
    respond(request, result=result)


def handle_set_config_option(request: dict) -> None:
    global _session_config_options
    params = request.get("params") if isinstance(request.get("params"), dict) else {}
    log("config_option", configId=params.get("configId"), value=params.get("value"))
    if flag("FAKE_ACP_CONFIG_ERROR"):
        respond(request, error={"code": -32000, "message": "config option refused by fake agent"})
        return
    responses = structured("FAKE_ACP_CONFIG_RESPONSES", {})
    if isinstance(responses, dict) and params.get("configId") in responses:
        result = responses[params["configId"]]
        _session_config_options = result.get("configOptions") if isinstance(result, dict) else None
        respond(request, result=result)
        return
    if isinstance(_session_config_options, list):
        updated = []
        for option in _session_config_options:
            if isinstance(option, dict) and option.get("id") == params.get("configId"):
                if not flag("FAKE_ACP_CONFIG_NO_CHANGE"):
                    option = {**option, "currentValue": params.get("value")}
            updated.append(option)
        _session_config_options = updated
        respond(request, result={"configOptions": updated})
        return
    respond(request, result={})


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
        elif method == "session/set_config_option":
            handle_set_config_option(frame)
        elif method == "session/prompt":
            threading.Thread(
                target=run_prompt, args=(frame, frame.get("params") or {}), daemon=True
            ).start()
        elif method == "session/cancel":
            _cancel.set()
            log("cancel_received", sessionId=(frame.get("params") or {}).get("sessionId"))
        elif method in ("session/resume", "session/load"):
            handle_resume(frame, method)
        else:
            respond(frame, error={"code": -32601, "message": f"Method not found: {method}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
