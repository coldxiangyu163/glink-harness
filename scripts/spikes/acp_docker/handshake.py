#!/usr/bin/env python3
"""ACP docker 面容器内握手探针（GLINK-ACP-05 / M4 起，GLINK-ACP-06 / M5 扩展，不消耗模型调用）。

用**生产装配路径**（``build_config`` → ``prepare_acp_runtime`` →
``docker_env.build_acp_plan`` → ``AcpExecutionAdapter``）在本地 ACP worker
镜像里跑 ``initialize`` + ``session/new``，不发 ``session/prompt``：

* 证明镜像里的 ACP agent 经 ``docker run -i`` 的容器 stdio 完成 ACP 握手；
* 记录 ``session/new`` 的 ``cwd``（容器路径 ``/workspace``）、能力矩阵与会话选项；
* ``--credential`` 把源文件只读复制进 run HOME（容器内以嵌套只读挂载呈现，
  M5 用 opencode 的 ``auth.json`` 验证这条通道）；``--session-config`` 实测
  ``PI_GLINK_ACP_SESSION_CONFIG``；
* 结束时按持久化容器名 force-remove，并确认无同名容器残留。

容器内没有宿主凭据时，**这条探针不发 prompt**，因此不需要模型凭据；
docker/e2b 面的真实模型 run 属预期阻塞，如实记录。不打印任何凭据值。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "host"))

from pi_glink.config import build_config  # noqa: E402
from pi_glink.kernel.acp_env import prepare_acp_runtime  # noqa: E402
from pi_glink.kernel.acp_execution import AcpExecutionAdapter  # noqa: E402
from pi_glink.kernel.docker_env import (  # noqa: E402
    CONTAINER_WORKSPACE,
    build_acp_plan,
    confirm_absent,
    stop_argv,
)
from pi_glink.kernel.execution import ExecutionSpec  # noqa: E402

DEFAULT_COMMAND = "claude-code-acp"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ACP docker 容器内握手（不发 prompt）")
    parser.add_argument("--command", default=DEFAULT_COMMAND)
    parser.add_argument("--image", default="pi-glink-acp-worker:latest")
    parser.add_argument("--env", action="append", default=[], help="NAME=value，可重复")
    parser.add_argument("--credential", action="append", default=[], help="source=dest，可重复")
    parser.add_argument(
        "--session-config",
        default="",
        help="PI_GLINK_ACP_SESSION_CONFIG（; 分隔的 id=value）",
    )
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args(argv)

    workdir = Path(tempfile.mkdtemp(prefix="glink-acp-docker-handshake-"))
    workspace = workdir / "workspace"
    workspace.mkdir()
    values = {
        "PI_GLINK_HARNESS": "acp",
        "PI_GLINK_ACP_COMMAND": args.command,
        "LOCAL_WEB_EXECUTOR": "docker",
        "LOCAL_WEB_DATA_ROOT": str(workdir / "data"),
        "LOCAL_WEB_ACP_WORKER_IMAGE": args.image,
        "LOCAL_WEB_TASK_TIMEOUT_SECONDS": str(args.timeout),
        "PI_GLINK_WEB_SEARCH_CONFIG": str(workdir / "absent-web-search.json"),
    }
    if args.env:
        values["PI_GLINK_ACP_ENV"] = ";".join(args.env)
    if args.credential:
        values["PI_GLINK_ACP_CREDENTIAL_FILES"] = ";".join(args.credential)
    if args.session_config:
        values["PI_GLINK_ACP_SESSION_CONFIG"] = args.session_config
    config = build_config(values, REPO_ROOT)

    run_id = "m4-handshake-" + args.command.split()[0].replace("/", "_")
    runtime = prepare_acp_runtime(
        config, runtime_dir=workdir / "data" / "acp-agent"
    )
    plan = build_acp_plan(config, runtime, run_id=run_id, workspace=workspace)

    print("ACP docker 容器内握手实测（不消耗模型调用）")
    print("=" * 44)
    print(f"日期: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"镜像: {plan.image}")
    print(f"容器名: {plan.container_name}")
    print(f"容器内命令: {args.command}")
    print(f"命令级 env 名: {', '.join(name for name, _ in config.acp_env) or '（无）'}")
    credential_dests = ", ".join(dest for _source, dest in config.acp_credential_files) or "（无）"
    print(f"凭据落点: HOME/{credential_dests}（源只读；容器内为嵌套只读挂载）")
    session_config = "; ".join(
        f"{name}={value}" for name, value in config.acp_session_config
    )
    print(f"会话配置: {session_config or '（无）'}")
    print(f"数据根: {workdir}")
    print(f"docker 二进制: {subprocess.run(['docker', '--version'], capture_output=True, text=True).stdout.strip()}")

    frames: list[dict] = []
    spec = ExecutionSpec(
        config=config,
        runtime=runtime,
        run_id=run_id,
        prompt="",
        system_prompt="",
        workspace=workspace,
        timeout_seconds=args.timeout,
    )
    adapter = AcpExecutionAdapter(
        spec,
        argv=plan.argv,
        env=plan.env,
        cwd=None,
        session_cwd=CONTAINER_WORKSPACE,
        container_name=plan.container_name,
        container_stop=stop_argv,
        on_frame=frames.append,
    )
    started = time.monotonic()
    exit_code = 0
    try:
        adapter.start()
    except Exception as exc:  # noqa: BLE001 - the failure is the evidence
        print(f"\n[FAIL] initialize/session/new: {type(exc).__name__}: {exc}")
        exit_code = 1
    else:
        session = adapter.session_info()
        payload = dict(session.payload) if session else {}
        print(f"\n握手成功（{time.monotonic() - started:.1f}s）")
        print(f"protocolVersion: {payload.get('protocolVersion')}")
        print(f"agentInfo: {json.dumps(payload.get('agentInfo'), ensure_ascii=False)}")
        print(f"agentCapabilities: {json.dumps(payload.get('agentCapabilities'), ensure_ascii=False)}")
        print(f"authMethods: {json.dumps(payload.get('authMethods'), ensure_ascii=False)}")
        print(f"sessionId: {payload.get('sessionId')}")
        print(f"cwd: {payload.get('cwd')}")
        print(f"modes: {json.dumps(payload.get('modes'), ensure_ascii=False)}")
        print(f"configOptions: {json.dumps(payload.get('configOptions'), ensure_ascii=False)}")
    finally:
        close = adapter.close()
        print(f"\nclient.close(): released={close.released} failures={list(close.failures)}")

    absent, detail = confirm_absent(plan.container_name)
    listing = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name={plan.container_name}", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    print(f"容器按名对账: confirmed_absent={absent} detail={detail} 同名容器列表={listing!r}")
    if not absent:
        exit_code = 1
    print(f"临时目录: {workdir}（按本脚本创建的路径清理；未做模式匹配删除）")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
