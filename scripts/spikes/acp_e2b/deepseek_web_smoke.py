#!/usr/bin/env python3
"""真实 dsh Search/Fetch 迁移验收：候选模板配置、HTTP/SSE 两轮与取消。

显式 --template；只操作本脚本创建的临时宿主、数据根与沙箱。读取项目 .env 的
模型/DeepSeek/E2B 凭据，不读取 ~/.dsh。会产生真实模型、搜索与沙箱费用，需授权。
不发布模板，不改 .env，不重启其他服务。输出目录必须为空且位于仓库外。
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import secrets
import shlex
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "host"))
from glink_harness.app import create_server  # noqa: E402
from glink_harness.config import build_config  # noqa: E402
from glink_harness.dotenv import parse_env_file  # noqa: E402
from glink_harness.kernel.acp_env import prepare_acp_runtime  # noqa: E402
from glink_harness.kernel.acp_e2b_env import AcpSandboxTransfer  # noqa: E402
from glink_harness.kernel.e2b_env import confirm_sandbox_absent  # noqa: E402

TERMINALS = {"run_finished", "run_failed", "run_cancelled"}
PAGE = "https://api-docs.deepseek.com/guides/search/"


class ProbeFailure(RuntimeError):
    """A secret-free acceptance assertion, unlike arbitrary SDK exception text."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeFailure(message)


def request(port: int, method: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def preflight(config, out: Path) -> None:
    """Native profile parsing in its real template, with no model/credential calls."""
    from e2b import Sandbox

    # Render the same pack without credentials for the config-only sandbox.
    bare = build_config({"GLINK_HARNESS": "dsh", "OPENAI_MODEL": config.openai_model,
                         "OPENAI_REASONING_EFFORT": config.openai_reasoning_effort}, ROOT)
    runtime = prepare_acp_runtime(bare, runtime_dir=out / "preflight-runtime")
    sandbox = Sandbox.create(template=config.acp_e2b_template, api_key=config.e2b_api_key,
                             timeout=300, request_timeout=60)
    facts = {"sandboxId": sandbox.sandbox_id, "template": config.acp_e2b_template}
    (out / "preflight.json").write_text(json.dumps(facts))
    try:
        AcpSandboxTransfer(sandbox, runtime, workspace=out / "preflight-workspace").upload_home()
        version = sandbox.commands.run("dsh --version && acp-self-check", timeout=60)
        facts["versions"] = version.stdout
        require(version.exit_code == 0 and "0.2.1-rc.2" in version.stdout.splitlines(), "candidate version mismatch")
        home = "/home/user/acp/home"
        command = shlex.join([*config.pack.command, "--patch", f"{home}/.dsh/patches/llm-pi-ai.yml", "--dump-config"])
        result = sandbox.commands.run(f"HOME={home} {command}", timeout=90)
        facts["dumpExit"] = result.exit_code
        require(result.exit_code == 0, "native profile composition failed")
        require("searchProvider: deepseek-search-api" in result.stdout, "native search provider not selected")
        require("fetchProvider: deepseek-fetch-api" in result.stdout, "native fetch provider not selected")
        require("sendOriginalQuestion: false" in result.stdout, "unexpected original-question disclosure")
        require("jina" not in result.stdout.lower(), "legacy provider still in composition")
        (out / "profile.yaml").write_text(result.stdout)
    finally:
        absent, _ = confirm_sandbox_absent(sandbox.sandbox_id, api_key=config.e2b_api_key)
        facts["confirmedAbsent"] = absent
        (out / "preflight.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2))
    require(facts["confirmedAbsent"] is True, "preflight cleanup not confirmed")
    print("[PASS] candidate version and native profile composition", flush=True)


def round_trip(server, out: Path, prompt: str, *, parent: str | None = None, cancel: bool = False) -> dict:
    port = server.server_address[1]
    body = {"prompt": prompt}
    if parent:
        body["parentRunId"] = parent
    status, payload = request(port, "POST", "/api/runs", body)
    require(status == 201, f"create returned HTTP {status}")
    run_id = json.loads(payload)["run"]["runId"]
    print(f"run={run_id} cancel={cancel}", flush=True)
    events = []
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=server.config.task_timeout_seconds + 180)
    deadline = time.monotonic() + server.config.task_timeout_seconds + 180
    try:
        connection.request("GET", f"/api/runs/{run_id}/events")
        response = connection.getresponse()
        require(response.status == 200, "SSE admission failed")
        for line in response:
            require(time.monotonic() < deadline, "SSE total deadline exceeded")
            if not line.startswith(b"data:"):
                continue
            event = json.loads(line[5:])
            events.append(event)
            kind = event.get("type")
            print(f"{run_id} {kind}", flush=True)
            if cancel and kind == "session_ready":
                stop_status, _ = request(port, "POST", f"/api/runs/{run_id}/stop")
                require(stop_status == 200, f"cancel returned HTTP {stop_status}")
            if kind in TERMINALS:
                break
    finally:
        connection.close()
        (out / f"{run_id}-events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2))
    require(bool(events) and events[-1].get("type") in TERMINALS, "missing terminal event")
    record = server.store.get(run_id)
    require(record is not None, "missing durable run")
    facts = {"runId": run_id, "status": record.status, "sessionId": record.session_id,
             "sandboxId": record.sandbox_id, "cleanup": record.cleanup, "events": len(events)}
    (out / f"{run_id}.json").write_text(json.dumps(facts, ensure_ascii=False, indent=2))
    require(record.status == ("cancelled" if cancel else "finished"), f"run status={record.status}")
    require(bool(record.cleanup and record.cleanup.get("released") is True), "run did not release its sandbox")
    require(not record.cleanup.get("failures"), "run cleanup has failures")
    if not cancel:
        status, report = request(port, "GET", f"/api/runs/{run_id}/report")
        require(status == 200 and bool(report), "missing downloadable report")
        (out / f"{run_id}-report.md").write_bytes(report)
        facts["report"] = report.decode("utf-8")
        facts["reportSha256"] = hashlib.sha256(report).hexdigest()
    facts["frames"] = events
    return facts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    out = (args.out or Path(tempfile.mkdtemp(prefix="glink-dsweb-live-"))).resolve()
    require(not out.is_relative_to(ROOT), "evidence must be outside repository")
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(not any(out.iterdir()), "evidence directory must be empty")
    require(60 <= args.timeout <= 900, "timeout must be between 60 and 900 seconds")
    env = parse_env_file(args.env_file)
    # No inherited service identity, account DB, data root, Jina key or proxy credentials.
    values = {key: env.get(key, "") for key in (
        "E2B_API_KEY", "OPENAI_MODEL", "OPENAI_REASONING_EFFORT", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY",
    )}
    values.update({
        "GLINK_HARNESS": "dsh", "LOCAL_WEB_EXECUTOR": "e2b", "LOCAL_WEB_PORT": "0",
        "LOCAL_WEB_DATA_ROOT": str(out / "data"), "LOCAL_WEB_TASK_TIMEOUT_SECONDS": str(args.timeout),
        "LOCAL_WEB_E2B_ACP_TEMPLATE": args.template, "GLINK_CONVERSATION_LEASE_TTL_SECONDS": "0",
        "GLINK_ACP_CREDENTIAL_ENV": "OPENROUTER_API_KEY,DEEPSEEK_API_KEY",
        "GLINK_RESOURCE_RECONCILE_SECONDS": "0",
    })
    config = build_config(values, ROOT)
    summary = {"template": args.template, "passed": False, "cleanup": []}
    server = None
    thread = None
    sandbox_ids: list[str] = []
    try:
        preflight(config, out)
        server = create_server(config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        code = "CHECK-" + secrets.token_hex(5)
        first = round_trip(server, out,
            f"这是有界集成验收。记住代号 {code}，不要将代号写入文件。"
            "请调用一次 web_search 搜索 DeepSeek Search Fetch API 官方文档，"
            f"然后用 web_fetch 打开 {PAGE}，基于实际正文写 150 字以内的 report.md，"
            "注明两个 API 路径和来源链接。不要委派子代理，不要使用 bash/curl 绕过 web 工具。")
        tools = [e["payload"] for e in first["frames"] if e.get("type") == "tool_finished"]
        for name in ("web_search", "web_fetch"):
            require(any(t.get("toolName") == name and t.get("isError") is False and t.get("result") for t in tools),
                    f"missing successful {name}")
        require(PAGE.rstrip("/") in first["report"] and code not in first["report"], "first report contract failed")
        second = round_trip(server, out,
            "请从会话记忆复述上一轮代号，将 `续接验收：<代号>` 追加到 report.md，并在回复中复述。"
            "只读写工作区 report.md，不要检索或读取任何会话/配置文件。", parent=first["runId"])
        require(first["sessionId"] == second["sessionId"] and bool(first["sessionId"]), "session identity changed")
        require(first["sandboxId"] != second["sandboxId"], "cold resume did not use another sandbox")
        require(code in second["report"], "continuation did not recover session memory")
        require(any(e.get("type") == "session_ready" and e["payload"].get("resumed") for e in second["frames"]),
                "missing resumed protocol fact")
        status, original = request(server.server_address[1], "GET", f"/api/runs/{first['runId']}/report")
        require(status == 200 and hashlib.sha256(original).hexdigest() == first["reportSha256"], "old report changed")
        cancelled = round_trip(server, out, "请调用 web_fetch 打开官方 DeepSeek 文档，然后写简短报告。", cancel=True)
        summary["runs"] = [r["runId"] for r in (first, second, cancelled)]
        summary["passed"] = True
    except Exception as exc:
        # API/config exceptions may carry credentials; expose the class, not arbitrary messages.
        summary["errorType"] = type(exc).__name__
        if isinstance(exc, ProbeFailure):
            summary["error"] = str(exc)
    finally:
        if server is not None:
            try:
                # Stop in-flight allocation before taking the exact identity list.
                server.manager.shutdown()
            except Exception as exc:
                summary["shutdownErrorType"] = type(exc).__name__
                summary["passed"] = False
            try:
                sandbox_ids = [r.sandbox_id for r in server.store.list(limit=None) if r.sandbox_id]
            finally:
                server.shutdown()
                try:
                    server.server_close()
                except Exception as exc:
                    summary["closeErrorType"] = type(exc).__name__
                    summary["passed"] = False
        if thread is not None:
            thread.join(timeout=10)
        for sandbox_id in sandbox_ids:
            absent, _ = confirm_sandbox_absent(sandbox_id, api_key=config.e2b_api_key)
            summary["cleanup"].append({"sandboxId": sandbox_id, "confirmedAbsent": absent})
            if not absent:
                summary["passed"] = False
        (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    print(f"evidence={out}", flush=True)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
