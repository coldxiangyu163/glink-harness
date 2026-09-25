#!/usr/bin/env python3
"""ACP E2B dev 模板的真实沙箱握手（GLINK-ACP-04 / M3b 起，M5 扩展，不消耗模型）。

用**生产装配路径**（``build_config`` → ``prepare_acp_runtime`` →
``E2BAcpSandboxClient`` → ``build_adapter`` → ``AcpSandboxExecutionAdapter``）在真实
沙箱里跑 ``initialize`` + ``session/new``，不发 ``session/prompt``：

* 证明 E2B 模板里的 agent 确实经 ``send_stdin`` / ``CommandHandle`` 完成 ACP 握手；
* 证明每 run HOME 与凭据文件在真实沙箱里可用（无凭据时 codex-acp 的
  ``session/new`` 会以 Authentication required 拒绝，是注入生效的观测点）；
* 记录 sandbox_id / 协议版本 / agentInfo / configOptions，供集成笔记与证据；
* ``opencode``（M5）在全新 HOME 下也不需要凭据就能 initialize/session/new，但带上它的
  ``auth.json`` 能顺带实测§6.3 的上传与沙箱内 0600 加固路径。

只创建并操作自己的沙箱，结束时按 sandbox id 终止；不打印任何凭据值。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "host"))

from pi_glink.config import build_config  # noqa: E402
from pi_glink.kernel.acp_env import prepare_acp_runtime  # noqa: E402
from pi_glink.kernel.e2b_env import E2BAcpSandboxClient  # noqa: E402
from pi_glink.kernel.execution import ExecutionSpec, build_adapter  # noqa: E402
from pi_glink.kernel.rpc import PiRpcClient  # noqa: E402

#: The agents and the credential location each one reads (M2/M4 measurement).
#: ``claude-code-acp`` runs from the SDK-bundled or image-local claude CLI and
#: needs no credential for initialize/session/new on a fresh HOME; a real model
#: run needs ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN supplied by the host (the
#: sandbox has no macOS Keychain).
AGENTS = {
    "dsh": {
        "command": "dsh --profile acp",
        "credential": "~/.dsh/.credentials.yaml=.dsh/.credentials.yaml",
    },
    "codex-acp": {
        "command": "codex-acp",
        "credential": "~/.codex/auth.json=.codex/auth.json",
    },
    "claude-code-acp": {
        "command": "claude-code-acp",
        "credential": "",
    },
    # opencode（M5）：凭据是单个 auth.json；initialize/session/new 不依赖它，
    # 带上是为了同时实测每 run HOME 在沙箱内的上传与 0600 加固。
    "opencode": {
        "command": "opencode acp",
        "credential": "~/.local/share/opencode/auth.json=.local/share/opencode/auth.json",
    },
}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_api_key(args: argparse.Namespace) -> str:
    if os.environ.get("E2B_API_KEY"):
        return os.environ["E2B_API_KEY"]
    if args.env_file:
        for line in Path(args.env_file).read_text(encoding="utf-8").splitlines():
            if line.startswith("E2B_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ACP E2B 真实沙箱握手（不发 prompt）")
    parser.add_argument("--agent", choices=sorted(AGENTS), required=True)
    parser.add_argument("--credential", default="", help="覆盖 source=dest（默认按 agent 选择）")
    parser.add_argument("--template", default="glink-acp-worker")
    parser.add_argument("--env-file", default="")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)

    api_key = resolve_api_key(args)
    if not api_key:
        print("缺少 E2B_API_KEY（进程环境或 --env-file）", file=sys.stderr)
        return 2

    agent = AGENTS[args.agent]
    credential = args.credential or agent["credential"]
    source: Path | None = None
    source_digest = ""
    source_text = ""
    if credential:
        source_text = credential.split("=", 1)[0]
        source = Path(source_text.replace("~/", str(Path.home()) + "/", 1))
        if not source.is_file():
            print(f"缺少凭据来源: {source}", file=sys.stderr)
            return 2
        source_digest = sha256_file(source)

    result: dict = {
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "agent": args.agent,
        "command": agent["command"],
        "template": args.template,
        "credential_source": source_text or None,
        "credential_dest": credential.split("=", 1)[1] if credential else None,
        "credential_sha256_before": source_digest or None,
        "source_mtime": (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(source.stat().st_mtime))
            if source is not None
            else None
        ),
    }

    workdir = Path(tempfile.mkdtemp(prefix="glink-acp-e2b-handshake-"))
    values = {
        "PI_GLINK_HARNESS": "acp",
        "PI_GLINK_ACP_COMMAND": agent["command"],
        "LOCAL_WEB_EXECUTOR": "e2b",
        "LOCAL_WEB_DATA_ROOT": str(workdir / "data"),
        "LOCAL_WEB_E2B_ACP_TEMPLATE": args.template,
        "LOCAL_WEB_TASK_TIMEOUT_SECONDS": "300",
        "E2B_API_KEY": api_key,
        "PI_GLINK_WEB_SEARCH_CONFIG": str(workdir / "absent-web-search.json"),
    }
    if credential:
        values["PI_GLINK_ACP_CREDENTIAL_FILES"] = credential
    config = build_config(values, REPO_ROOT)
    runtime = prepare_acp_runtime(config, runtime_dir=workdir / "acp-agent")
    result["runtime_home"] = str(runtime.home)
    result["credential_dests"] = list(runtime.credential_dests)
    result["secret_count"] = len(runtime.secrets)

    sandbox_client = E2BAcpSandboxClient(
        config,
        runtime,
        run_id=f"handshake-{args.agent.replace('-', '_')}",
        workspace=workdir / "workspace",
    )
    spec = ExecutionSpec(
        config=config,
        runtime=runtime,
        run_id="handshake",
        prompt="",
        system_prompt="",
        workspace=workdir / "workspace",
        timeout_seconds=args.timeout,
    )
    adapter = build_adapter(
        spec,
        harness="acp",
        executor="e2b",
        client_factory=PiRpcClient,
        on_frame=lambda frame: None,
        rpc_plan=lambda _spec: ([], {}, None),
        docker_plan=lambda _spec: ([], {}, "container"),
        docker_stop=lambda _name: [],
        acp_plan=lambda _spec: ([], {}, None),
        acp_e2b_plan=lambda _spec: sandbox_client,
    )
    try:
        adapter.start()
        result["sandbox_id"] = sandbox_client.sandbox_id
        result["sandbox_command"] = sandbox_client.command()
        result["harden_command"] = "chmod 0700 /home/user/acp/home …"
        result["upload_manifest"] = [
            {"path": relative, "sha256": digest}
            for relative, digest in sandbox_client.upload_manifest
        ]
        session = adapter.session_info()
        if session is None:
            result["initializeOk"] = False
            result["error"] = "没有 session_info"
        else:
            payload = session.payload
            result["initializeOk"] = True
            result["protocolVersion"] = payload.get("protocolVersion")
            result["agentInfo"] = payload.get("agentInfo")
            result["capabilities"] = sorted(
                (payload.get("agentCapabilities") or {}).keys()
            )
            result["authMethodIds"] = [
                method.get("id") for method in (payload.get("authMethods") or [])
                if isinstance(method, dict)
            ]
            result["sessionId"] = payload.get("sessionId")
            result["cwd"] = payload.get("cwd")
            result["configOptionIds"] = [
                option.get("id") for option in (payload.get("configOptions") or [])
            ]
    except Exception as exc:  # noqa: BLE001 - 失败本身是证据
        result["initializeOk"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["sandbox_id"] = sandbox_client.sandbox_id
    finally:
        cleanup = adapter.close()
        result["cleanup"] = {
            "released": cleanup.released,
            "failures": list(cleanup.failures),
            "external": dict(cleanup.external),
        }
        result["credential_sha256_after"] = (
            sha256_file(source) if source is not None else None
        )
        result["source_unchanged"] = (
            True if source is None else result["credential_sha256_after"] == source_digest
        )

    print(json.dumps(result, ensure_ascii=False, indent=2))
    ok = (
        result.get("initializeOk") is True
        and result["cleanup"]["released"] is True
        and result["source_unchanged"] is True
        and bool(result.get("sandbox_id"))
    )
    print(f"[{'PASS' if ok else 'FAIL'}] {args.agent} 真实沙箱 ACP 握手（initialize + session/new）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
