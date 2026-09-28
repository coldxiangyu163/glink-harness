#!/usr/bin/env python3
"""Isolated host for the browser acceptance suite (accounts / local modes).

Starts a real ``pi_glink.app`` host on an OS-assigned loopback port with:

* a temporary data root and (accounts mode) a temporary SQLite account database
  — never the operator's ``~/.pi-glink``;
* synthetic tenant credentials from a 0600 file in the temporary directory;
* the real Runner/RunStore/EventBus/HTTP/SSE stack and the production front-end,
  with only the execution face replaced by :class:`FakeAdapter` — no pi process,
  no container, no sandbox, no model call;
* accounts mode: two tenants with one admin each, created directly through the
  account store (the operator-onboarding path, not an HTTP endpoint);
* local mode: the single-user surface (``/api/me`` answers ``mode=local``);
* gateway mode: the multi-tenant deployment surface *without* in-app accounts —
  ``/api/me`` answers 401 with ``mode=gateway`` and the front-end shows the
  "reach it through the organisation gateway" face (no login form).

The fake execution face is scripted by a marker in the prompt, so one fixture
serves every browser scenario the suite needs:

=======================  =========================================================
``[scenario:tools]``     tool cards (two successes, one failure) + usage
``[scenario:subagent]``  one dispatch of two children with running progress
                         (transcripts), recovered artifacts and a settled close
``[scenario:slow]``      stays in the running phase until cancelled
``[scenario:fail]``      model error close (``stopReason=error``)
``[scenario:length]``    truncated delivery (``stopReason=length``)
no marker                thinking + answer + ``report.md`` (the original frames)
=======================  =========================================================

It prints one JSON line to stdout once ready::

    READY {"port": 43123, "baseUrl": "http://127.0.0.1:43123", "mode": "accounts", ...}

and serves until SIGTERM/SIGINT. Terminate by the recorded PID (never a broad
``pkill``): the process owns its temporary data root and nothing else.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import signal
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOST_ROOT = REPO_ROOT / "host"
if str(HOST_ROOT) not in sys.path:
    sys.path.insert(0, str(HOST_ROOT))

from pi_glink.accounts import AccountsStore  # noqa: E402
import pi_glink.kernel.harness_pi as pi_family  # noqa: E402
from pi_glink.app import create_server  # noqa: E402
from pi_glink.config import build_config  # noqa: E402
from pi_glink.kernel.child_progress import PiChildProgressSource  # noqa: E402
from pi_glink.kernel.e2b_env import SUBAGENT_ARTIFACTS_DIRNAME  # noqa: E402
from pi_glink.kernel.execution import (  # noqa: E402
    ArtifactRecovery,
    CleanupReport,
    ExecutionAdapter,
    ExecutionOutcome,
    SessionInfo,
)

logger = logging.getLogger("browser-fixture")

PASSWORD = "correct horse battery"
TENANTS = {
    "tenant-alpha": {"name": "组织甲", "modelApiKey": "sk-alpha-browser-0000000000"},
    "tenant-beta": {"name": "组织乙", "modelApiKey": "sk-beta-browser-00000000000"},
}
ADMINS = {
    "admin-alpha@example.test": {"tenant": "tenant-alpha", "name": "管理员甲", "role": "admin"},
    "admin-beta@example.test": {"tenant": "tenant-beta", "name": "管理员乙", "role": "admin"},
}

REPORT = "# 合成报告\n\n这是浏览器验收生成的合成报告，不来自真实模型。\n"
SESSION_ID = "synthetic-browser-session"
SCENARIOS = ("default", "tools", "subagent", "slow", "fail", "length")
_SCENARIO_RE = re.compile(r"\[scenario:([a-z]+)\]")


def scenario_of(prompt: str) -> str:
    match = _SCENARIO_RE.search(prompt or "")
    name = match.group(1) if match else "default"
    return name if name in SCENARIOS else "default"


def _text_frame(text: str) -> dict:
    return {
        "type": "message_update",
        "assistantMessageEvent": {"type": "text_delta", "delta": text},
    }


def _thinking_frame(text: str) -> dict:
    return {
        "type": "message_update",
        "assistantMessageEvent": {"type": "thinking_delta", "delta": text},
    }


def _message_end(text: str, *, stop_reason: str = "stop", error: str | None = None,
                 usage: dict | None = None) -> dict:
    message: dict = {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "stopReason": stop_reason,
    }
    if error is not None:
        message["errorMessage"] = error
    if usage is not None:
        message["usage"] = usage
    return {"type": "message_end", "message": message}


DEFAULT_FRAMES = (
    {"type": "agent_start"},
    {"type": "turn_start"},
    _thinking_frame("先规划，再回答。"),
    _text_frame("这是一份浏览器验收用的合成回答，不包含真实模型输出。"),
    _message_end(
        "这是一份浏览器验收用的合成回答，不包含真实模型输出。",
        usage={"input": 120, "output": 40, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 160},
    ),
    {"type": "turn_end", "message": {"role": "assistant", "usage": {"input": 120, "output": 40, "totalTokens": 160}}, "toolResults": []},
    {"type": "agent_settled"},
)

TOOLS_FRAMES = (
    {"type": "agent_start"},
    {"type": "turn_start"},
    _thinking_frame("先看看工作区里有什么，再检索。"),
    {"type": "tool_execution_start", "toolCallId": "call-ls", "toolName": "bash", "args": {"command": "ls -la"}},
    {"type": "tool_execution_update", "toolCallId": "call-ls", "toolName": "bash", "args": {"command": "ls -la"}, "partialResult": "total 8\n"},
    {"type": "tool_execution_end", "toolCallId": "call-ls", "toolName": "bash",
     "result": {"content": [{"type": "text", "text": "total 8\nreport.md\nnotes.md"}]}, "isError": False},
    {"type": "tool_execution_start", "toolCallId": "call-read", "toolName": "read", "args": {"path": "missing.md"}},
    {"type": "tool_execution_end", "toolCallId": "call-read", "toolName": "read",
     "result": {"content": [{"type": "text", "text": "ENOENT: no such file or directory"}]}, "isError": True},
    {"type": "tool_execution_start", "toolCallId": "call-search", "toolName": "web_search",
     "args": {"query": "pi-glink 浏览器验收"}},
    {"type": "tool_execution_end", "toolCallId": "call-search", "toolName": "web_search",
     "result": {"content": [{"type": "text", "text": "1. 研究工作台\n   https://example.test/a\n2. 浏览器验收\n   https://example.test/b"}]}, "isError": False},
    _text_frame("工作区里有一份报告；有一条读取失败了，检索返回了两条结果。"),
    _message_end(
        "工作区里有一份报告；有一条读取失败了，检索返回了两条结果。",
        usage={"input": 300, "output": 90, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 390},
    ),
    {"type": "turn_end", "message": {"role": "assistant", "usage": {"input": 300, "output": 90, "totalTokens": 390}}, "toolResults": []},
    {"type": "agent_settled"},
)


class FakeAdapter(ExecutionAdapter):
    """A synthetic execution face: scripted frames, no kernel, no model call.

    ``start()`` returns immediately and a worker thread plays the scenario, so
    the runner's own phases (open while frames arrive, finalize after ``wait``)
    are exercised exactly as with a real face. ``wait`` joins the worker; the
    ``slow`` scenario waits for cancellation instead.
    """

    #: Safety net for the ``slow`` scenario: never outlive a sensible browser
    #: interaction window even if the test never presses stop.
    SLOW_MAX_SECONDS = 90.0

    def __init__(self, spec, *, on_frame, **kwargs):
        super().__init__(spec, on_frame=on_frame)
        self._scenario = scenario_of(spec.prompt)
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._session_dir = Path(spec.runtime.session_dir)
        self._artifacts_dir = self._session_dir / SUBAGENT_ARTIFACTS_DIRNAME

    # -- ExecutionAdapter surface ---------------------------------------------

    def start(self) -> None:
        self.spec.workspace.mkdir(parents=True, exist_ok=True)
        self._worker = threading.Thread(
            target=self._play, name=f"fixture-{self._scenario}", daemon=True
        )
        self._worker.start()

    def deliver_input(self) -> None:
        pass

    def session_info(self):
        """Publish a synthetic session so follow-up runs pass the resume gate."""
        try:
            self._session_dir.mkdir(parents=True, exist_ok=True)
            path = self._session_dir / "session.jsonl"
            if not path.exists():
                path.write_text(
                    json.dumps({"synthetic": True, "prompt": self.spec.prompt}, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
        except OSError:
            return None
        return SessionInfo(
            payload={"sessionId": SESSION_ID},
            session_id=SESSION_ID,
            session_file=str(path),
        )

    def wait(self, *, cancel, deadline):
        if self._scenario == "slow":
            while not cancel() and time.monotonic() < deadline and not self._stop.is_set():
                time.sleep(0.05)
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=10)
        return ExecutionOutcome(settled=True)

    def cancel(self) -> None:
        self._stop.set()

    def recover_artifacts(self, destination):
        return ArtifactRecovery()

    def close(self):
        return CleanupReport(released=True)

    def child_progress_source(self):
        """R3a seam: the same directory layout the in-process pi face polls."""
        return PiChildProgressSource(self._artifacts_dir)

    # -- scenario scripts ------------------------------------------------------

    def _play(self) -> None:
        try:
            getattr(self, f"_scenario_{self._scenario}")()
        except Exception:  # noqa: BLE001 - a fixture bug must not wedge the host
            logger.exception("browser fixture scenario crashed: %s", self._scenario)

    def _emit(self, frame) -> None:
        self._on_frame(frame)

    def _write_report(self) -> None:
        (self.spec.workspace / "report.md").write_text(
            REPORT + f"\n题目：{self.spec.prompt}\n", encoding="utf-8"
        )

    def _scenario_default(self) -> None:
        self._write_report()
        for frame in DEFAULT_FRAMES:
            self._emit(frame)

    def _scenario_tools(self) -> None:
        self._write_report()
        for frame in TOOLS_FRAMES:
            self._emit(frame)

    def _scenario_length(self) -> None:
        self._write_report()
        for frame in (
            {"type": "agent_start"},
            {"type": "turn_start"},
            _thinking_frame("先展开要点，再写下去。"),
            _text_frame("这份回答在输出上限处被截断，"),
            _text_frame("后半段没有交付。"),
            _message_end("这份回答在输出上限处被截断，后半段没有交付。", stop_reason="length"),
            {"type": "agent_settled"},
        ):
            self._emit(frame)

    def _scenario_fail(self) -> None:
        for frame in (
            {"type": "agent_start"},
            {"type": "turn_start"},
            _thinking_frame("先规划一下。"),
            _text_frame("我已经开始了这次研究，"),
            _message_end(
                "我已经开始了这次研究，",
                stop_reason="error",
                error="upstream 500: synthetic model failure",
            ),
            {"type": "agent_settled"},
        ):
            self._emit(frame)

    def _scenario_slow(self) -> None:
        self._emit({"type": "agent_start"})
        self._emit({"type": "turn_start"})
        self._emit(_thinking_frame("这次研究需要很久，正在准备。"))
        self._emit(_text_frame("研究已经开始了，仍在进行中。"))
        self._stop.wait(timeout=self.SLOW_MAX_SECONDS)
        self._emit({"type": "agent_settled"})

    # -- subagent scenario -----------------------------------------------------

    def _scenario_subagent(self) -> None:
        self._write_report()
        self._emit({"type": "agent_start"})
        self._emit({"type": "turn_start"})
        self._emit(_thinking_frame("先把两个子问题派出去核，再汇总。"))
        # One dispatch with two tasks: the projector turns this into two
        # subagent_started events and two child cards/rows.
        self._emit(
            {
                "type": "tool_execution_start",
                "toolCallId": "call-dispatch",
                "toolName": "subagent",
                "args": {
                    "tasks": [
                        {"agent": "researcher", "task": "核查跨境资金监管的两个口径"},
                        {"agent": "researcher", "task": "比对两地来源的时间线"},
                    ]
                },
            }
        )
        # Baseline transcripts: the progress source reads the first snapshot as
        # a baseline and only reports later changes.
        self._append_transcript(0, [self._message_record(0, "核查跨境资金监管的两个口径")])
        self._append_transcript(1, [self._message_record(1, "比对两地来源的时间线")])
        time.sleep(1.4)
        self._append_transcript(0, [self._tool_start_record(0, "web_search", "跨境资金监管 口径")])
        time.sleep(1.2)
        self._append_transcript(1, [self._tool_start_record(1, "read", "sources.md")])
        time.sleep(2.2)  # clears the 2s per-child throttle before the last update
        self._append_transcript(0, [self._tool_start_record(0, "bash", "grep -n 监管 notes.md")])
        time.sleep(0.8)
        self._write_subagent_artifacts()
        self._emit(
            {
                "type": "tool_execution_end",
                "toolCallId": "call-dispatch",
                "toolName": "subagent",
                "result": {
                    "content": [
                        {"type": "text", "text": "两个子代理都完成了核查：口径一致，时间线两处差异。"}
                    ]
                },
                "isError": False,
            }
        )
        self._emit(_text_frame("两个子代理都完成了核查，结论见报告。"))
        self._emit(
            _message_end(
                "两个子代理都完成了核查，结论见报告。",
                usage={"input": 800, "output": 200, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 1000},
            )
        )
        self._emit({"type": "turn_end", "message": {"role": "assistant", "usage": {"input": 800, "output": 200, "totalTokens": 1000}}, "toolResults": []})
        self._emit({"type": "agent_settled"})

    def _message_record(self, index: int, text: str) -> dict:
        return {
            "version": 1,
            "recordType": "message",
            "source": "foreground",
            "runId": self.spec.run_id,
            "agent": "researcher",
            "childIndex": index,
            "cwd": "/workspace",
            "ts": 1,
            "timestamp": "2026-09-26T00:00:00.000Z",
            "sourceEventType": "initial_prompt",
            "role": "user",
            "text": text,
        }

    def _tool_start_record(self, index: int, name: str, preview: str) -> dict:
        return {
            "version": 1,
            "recordType": "tool_start",
            "source": "foreground",
            "runId": self.spec.run_id,
            "agent": "researcher",
            "childIndex": index,
            "cwd": "/workspace",
            "ts": 2,
            "timestamp": "2026-09-26T00:00:01.000Z",
            "sourceEventType": "tool_execution_start",
            "toolName": name,
            "argsPreview": preview,
        }

    def _append_transcript(self, index: int, records: list[dict]) -> None:
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = self._artifacts_dir / f"{self.spec.run_id}_researcher_{index}_transcript.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _write_subagent_artifacts(self) -> None:
        """The pi-subagents artifact layout the R2 enrichment pairs by ordinal."""
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        run_id = self.spec.run_id
        for index in range(2):
            stem = self._artifacts_dir / f"{run_id}_researcher_{index}"
            stem.with_name(stem.name + "_input.md").write_text(
                f"# 子代理 {index} 输入\n\n核查任务 {index}\n", encoding="utf-8"
            )
            stem.with_name(stem.name + "_output.md").write_text(
                f"# 子代理 {index} 结论\n\n子代理 {index} 的合成结论。\n", encoding="utf-8"
            )
            stem.with_name(stem.name + "_meta.json").write_text(
                json.dumps(
                    {
                        "exitCode": 0,
                        "durationMs": 4200,
                        "toolCount": 3,
                        "model": "synthetic/browser-model",
                        "usage": {"input": 100, "output": 20, "turns": 2},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )


def build_fake(spec, **kwargs):
    return FakeAdapter(spec, on_frame=kwargs["on_frame"])


def bootstrap_accounts(store: AccountsStore) -> dict:
    """Operator onboarding, idempotent so a restarted fixture reuses its data root."""
    for tenant_id, entry in TENANTS.items():
        store.upsert_tenant(tenant_id, entry["name"])
    for email, entry in ADMINS.items():
        if store.find_user_by_email(email) is not None:
            continue
        store.create_user(
            tenant_id=entry["tenant"],
            email=email,
            name=entry["name"],
            role=entry["role"],
            password=PASSWORD,
        )
    return {
        "password": PASSWORD,
        "users": {
            "alphaAdmin": {"email": "admin-alpha@example.test", "tenant": "tenant-alpha", "name": "管理员甲"},
            "betaAdmin": {"email": "admin-beta@example.test", "tenant": "tenant-beta", "name": "管理员乙"},
        },
    }


def install_request_recorder(path: Path | None) -> None:
    """Record raw request lines and Referer headers for acceptance evidence.

    The recording happens *around* the production ``log_message``: the raw line
    is what a proxy or access log would see, while the production method still
    sanitizes it. A missing path disables the recorder.
    """
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    from pi_glink.app import HostRequestHandler

    original = HostRequestHandler.log_message

    def recording(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        entry = {
            "requestline": str(self.requestline),
            "referer": self.headers.get("Referer"),
        }
        with path.open("a", encoding="utf-8") as log:
            log.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return original(self, format, *args)

    HostRequestHandler.log_message = recording  # type: ignore[method-assign]


def config_values(mode: str, state_dir: Path, tenants_file: Path, gateway_secret: Path) -> dict:
    values = {
        "OPENAI_MODEL": "synthetic/browser-model",
        # The runner freezes the upstream endpoint before building the
        # execution face; a synthetic placeholder keeps that contract intact
        # without any network call (the adapter is replaced below).
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "PI_GLINK_WEB_SEARCH_CONFIG": str(state_dir / "absent-web-search.json"),
        "LOCAL_WEB_DATA_ROOT": str(state_dir / "data"),
        "LOCAL_WEB_PORT": "0",
        "LOCAL_WEB_TASK_TIMEOUT_SECONDS": "120",
    }
    if mode == "accounts":
        values.update(
            {
                "LOCAL_WEB_EXECUTOR": "docker",
                "PI_GLINK_TENANCY": "multitenant",
                "PI_GLINK_AUTH_MODE": "accounts",
                "PI_GLINK_TENANT_CONFIG_FILE": str(tenants_file),
                "PI_GLINK_ACCOUNTS_DB": str(state_dir / "data" / "accounts" / "accounts.db"),
                "PI_GLINK_COOKIE_SECURE": "0",  # loopback HTTP acceptance run
                "PI_GLINK_SESSION_ABSOLUTE_SECONDS": "3600",
                "PI_GLINK_SESSION_IDLE_SECONDS": "1800",
                "PI_GLINK_INVITE_TTL_SECONDS": "3600",
                "PI_GLINK_INVITE_CHECK_MAX_ATTEMPTS": "500",
            }
        )
    elif mode == "gateway":
        values.update(
            {
                "LOCAL_WEB_EXECUTOR": "docker",
                "PI_GLINK_TENANCY": "multitenant",
                "PI_GLINK_AUTH_MODE": "gateway",
                "PI_GLINK_TENANT_CONFIG_FILE": str(tenants_file),
                "PI_GLINK_GATEWAY_SECRET_FILE": str(gateway_secret),
                "PI_GLINK_TENANCY_AUDIENCE": "pi-glink-browser-acceptance",
            }
        )
    else:  # local
        values.update(
            {
                # Synthetic credential: the fake adapter never contacts a model.
                "OPENAI_API_KEY": "sk-local-browser-0000000000000000",
                "LOCAL_WEB_EXECUTOR": "inproc",
                "PI_GLINK_TENANCY": "local",
                "PI_GLINK_AUTH_MODE": "gateway",
            }
        )
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="isolated host for browser acceptance")
    parser.add_argument("--state-dir", required=True, help="temporary directory for all runtime state")
    parser.add_argument(
        "--request-log",
        help=(
            "optional JSONL file that records every raw request line and Referer "
            "header — acceptance evidence that a token never reached an HTTP URL"
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("accounts", "local", "gateway"),
        default="accounts",
        help="identity surface to serve (default: accounts)",
    )
    args = parser.parse_args()
    state_dir = Path(args.state_dir).resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    # DEBUG so every request line really lands in the host log: the acceptance
    # run then proves a token never shows up there either.
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    request_log = Path(args.request_log).resolve() if args.request_log else None
    install_request_recorder(request_log)

    tenants_file = state_dir / "tenants.json"
    gateway_secret = state_dir / "gateway-secret"
    if args.mode == "gateway":
        # A synthetic HMAC secret (never a real gateway key): the browser face
        # under test never carries an assertion, so only the unauthenticated
        # surface is exercised.
        gateway_secret.write_bytes(b"browser-acceptance-synthetic-gateway-secret")
        gateway_secret.chmod(0o600)
    if args.mode in ("accounts", "gateway"):
        tenants_file.write_text(
            json.dumps(
                {
                    "tenants": {
                        tenant_id: {"modelApiKey": entry["modelApiKey"]}
                        for tenant_id, entry in TENANTS.items()
                    }
                }
            ),
            encoding="utf-8",
        )
        tenants_file.chmod(0o600)

    config = build_config(config_values(args.mode, state_dir, tenants_file, gateway_secret), REPO_ROOT)
    # Replace only the execution face, exactly like the Python regressions do
    # (``monkeypatch.setattr("pi_glink.kernel.harness_pi.PiExecutionAdapter", …)``):
    # the real Runner, store, event bus, HTTP/SSE and front-end all stay in the
    # loop, but no pi binary, Docker daemon or sandbox is contacted.
    pi_family.PiExecutionAdapter = build_fake
    server = create_server(config)
    bootstrap = bootstrap_accounts(server.accounts) if args.mode == "accounts" else {"password": None, "users": {}}
    state = {
        "mode": args.mode,
        "port": server.server_address[1],
        "baseUrl": f"http://127.0.0.1:{server.server_address[1]}",
        "stateDir": str(state_dir),
        "dataRoot": str(config.data_root),
        "accountsDb": str(config.accounts_db) if config.accounts_db is not None else None,
        "requestLog": str(request_log) if request_log is not None else None,
        "scenarios": list(SCENARIOS),
        **bootstrap,
    }
    print("READY " + json.dumps(state), flush=True)

    stopping = threading.Event()

    def _stop(_signum, _frame):  # noqa: ANN001 - signal handler
        stopping.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    while not stopping.wait(0.2):
        pass
    server.manager.shutdown(timeout=5.0)
    server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
