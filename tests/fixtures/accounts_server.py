#!/usr/bin/env python3
"""Isolated host for the accounts browser acceptance.

Starts a real ``pi_glink.app`` host in accounts mode on an OS-assigned loopback
port with:

* a temporary data root and a temporary SQLite account database (never the
  operator's ``~/.pi-glink``);
* synthetic tenant credentials from a 0600 file in the temporary directory;
* the real Runner/RunStore/EventBus/HTTP/SSE stack and the production front-end,
  with only the execution face replaced by :class:`FakeAdapter` — no pi process,
  no container, no sandbox, no model call;
* two tenants with one admin each, created directly through the account store
  (the operator-onboarding path, not an HTTP endpoint).

It prints one JSON line to stdout once ready::

    READY {"port": 43123, "baseUrl": "http://127.0.0.1:43123", ...}

and serves until SIGTERM/SIGINT.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOST_ROOT = REPO_ROOT / "host"
if str(HOST_ROOT) not in sys.path:
    sys.path.insert(0, str(HOST_ROOT))

from pi_glink.accounts import AccountsStore  # noqa: E402
import pi_glink.kernel.harness_pi as pi_family  # noqa: E402
from pi_glink.app import HostRequestHandler, create_server  # noqa: E402
from pi_glink.config import build_config  # noqa: E402
from pi_glink.kernel.execution import (  # noqa: E402
    ArtifactRecovery,
    CleanupReport,
    ExecutionAdapter,
    ExecutionOutcome,
)

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


class FakeAdapter(ExecutionAdapter):
    """A synthetic execution face: emits a short synthetic answer, then settles."""

    def __init__(self, spec, *, on_frame):
        super().__init__(spec, on_frame=on_frame)

    def start(self) -> None:
        self.spec.workspace.mkdir(parents=True, exist_ok=True)
        (self.spec.workspace / "report.md").write_text(REPORT, encoding="utf-8")
        for frame in FRAMES:
            self._on_frame(frame)

    def deliver_input(self) -> None:
        pass

    def session_info(self):
        return None

    def identity(self):
        return {}

    def wait(self, *, cancel, deadline):
        return ExecutionOutcome(settled=True)

    def cancel(self) -> None:
        pass

    def recover_artifacts(self, destination):
        return ArtifactRecovery()

    def close(self):
        return CleanupReport(released=True)


FRAMES = (
    {"type": "agent_start"},
    {
        "type": "message_update",
        "assistantMessageEvent": {"type": "thinking_delta", "delta": "先规划，再回答。"},
    },
    {
        "type": "message_update",
        "assistantMessageEvent": {
            "type": "text_delta",
            "delta": "这是一份浏览器验收用的合成回答，不包含真实模型输出。",
        },
    },
    {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "这是一份浏览器验收用的合成回答，不包含真实模型输出。"}
            ],
            "stopReason": "stop",
        },
    },
    {"type": "agent_settled"},
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


def main() -> int:
    parser = argparse.ArgumentParser(description="isolated accounts host for browser acceptance")
    parser.add_argument("--state-dir", required=True, help="temporary directory for all runtime state")
    parser.add_argument(
        "--request-log",
        help=(
            "optional JSONL file that records every raw request line and Referer "
            "header — acceptance evidence that a token never reached an HTTP URL"
        ),
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

    config = build_config(
        {
            "OPENAI_MODEL": "synthetic/browser-model",
            # The runner freezes the upstream endpoint before building the
            # execution face; a synthetic placeholder keeps that contract intact
            # without any network call (the adapter is replaced below).
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
            "PI_GLINK_WEB_SEARCH_CONFIG": str(state_dir / "absent-web-search.json"),
            "LOCAL_WEB_DATA_ROOT": str(state_dir / "data"),
            "LOCAL_WEB_PORT": "0",
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
            "LOCAL_WEB_TASK_TIMEOUT_SECONDS": "120",
        },
        REPO_ROOT,
    )
    # Replace only the execution face, exactly like the Python regressions do
    # (``monkeypatch.setattr("pi_glink.kernel.harness_pi.compose_adapter", …)``):
    # the real Runner, store, event bus, HTTP/SSE and front-end all stay in the
    # loop, but no pi binary, Docker daemon or sandbox is contacted.
    pi_family.compose_adapter = build_fake
    server = create_server(config)
    bootstrap = bootstrap_accounts(server.accounts)
    state = {
        "port": server.server_address[1],
        "baseUrl": f"http://127.0.0.1:{server.server_address[1]}",
        "stateDir": str(state_dir),
        "dataRoot": str(config.data_root),
        "accountsDb": str(config.accounts_db),
        "requestLog": str(request_log) if request_log is not None else None,
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
