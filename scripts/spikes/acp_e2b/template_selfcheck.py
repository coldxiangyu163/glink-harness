#!/usr/bin/env python3
"""ACP E2B dev 模板的真实沙箱自检（GLINK-ACP-04 / M3b）。

构建完 ``docker/e2b/Dockerfile.acp-e2b`` 后，必须在真实沙箱里确认模板装出来的
东西确实可用（只看构建成功不够——历史事故是构建成功但镜像残缺）：

* 沙箱默认用户/家目录/工作目录与宿主假设一致；
* ``node -v``、``dsh --version``、``codex-acp --version``、``opencode --version``
  与 ARG 钉版一致；
* 每 run 的 agent 状态根（``/home/user/acp/home``）可创建、可写；
* 镜像内自检脚本 ``acp-self-check`` 能跑。

脚本只创建并操作自己的沙箱，结束时按 sandbox id 终止（不做模式匹配/批量清理），
不打印任何凭据。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tomllib
from pathlib import Path

PROBE = r"""
set -u
echo "id=$(id -un) uid=$(id -u) gid=$(id -g)"
echo "home=$HOME pwd=$(pwd)"
echo "node=$(node -v)"
echo "dsh=$(dsh --version 2>&1)"
echo "codex_acp=$(codex-acp --version 2>&1)"
echo "opencode=$(opencode --version 2>&1)"
echo "acp_dir_writable=$(test -w /home/user/acp && echo yes || echo no)"
echo "workspace_writable=$(test -w /home/user/workspace && echo yes || echo no)"
self_check=$(acp-self-check 2>&1)
echo "self_check_exit=$?"
echo "self_check=$self_check"
echo "deepseek_web_api=$(test -f /usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-web-deepseek-api/lib/index.js && echo present || echo missing)"
mkdir -p /home/user/acp/home && echo "per_run_home_mkdir=ok" || echo "per_run_home_mkdir=fail"
printf 'credential-probe' > /home/user/acp/home/probe.txt \
  && echo "per_run_home_write=ok" || echo "per_run_home_write=fail"
echo "probe_read=$(cat /home/user/acp/home/probe.txt)"
"""


def expected_versions() -> dict[str, str]:
    """Read the same pins that generate the images; never maintain another version list."""
    root = Path(__file__).resolve().parents[3] / "harnesses"
    return {
        pack: tomllib.loads((root / pack / "harness.toml").read_text())["image"]["packages"][0]["version"]
        for pack in ("dsh", "codex", "opencode")
    }


def resolve_api_key(args: argparse.Namespace) -> str:
    key = os.environ.get("E2B_API_KEY", "")
    if key:
        return key
    if args.env_file:
        for line in Path(args.env_file).read_text(encoding="utf-8").splitlines():
            if line.startswith("E2B_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ACP E2B dev 模板的真实沙箱自检")
    parser.add_argument("--template", default="glink-acp-worker")
    parser.add_argument("--sandbox-timeout", type=int, default=600)
    parser.add_argument("--env-file", default="")
    args = parser.parse_args(argv)

    api_key = resolve_api_key(args)
    if not api_key:
        print("缺少 E2B_API_KEY（进程环境或 --env-file）", file=sys.stderr)
        return 2

    from e2b import Sandbox

    out: dict = {
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "template": args.template,
    }
    versions = expected_versions()
    sandbox = None
    try:
        sandbox = Sandbox.create(template=args.template, timeout=args.sandbox_timeout, api_key=api_key, request_timeout=60)
        out["sandbox_id"] = sandbox.sandbox_id
        out["envd_version"] = str(getattr(sandbox, "_envd_version", ""))
        result = sandbox.commands.run(PROBE, timeout=120)
        out["exit_code"] = result.exit_code
        out["stdout"] = result.stdout
        out["stderr"] = result.stderr
    finally:
        if sandbox is not None:
            try:
                out["killed"] = sandbox.kill()
            except Exception as exc:  # noqa: BLE001 - 终止失败必须出现在证据里
                out["killed"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(out, ensure_ascii=False, indent=2))
    ok = (
        out.get("exit_code") == 0
        and out.get("killed") is True
        and "per_run_home_write=ok" in out.get("stdout", "")
        and f"dsh={versions['dsh']}" in out.get("stdout", "").splitlines()
        and f"codex_acp=@agentclientprotocol/codex-acp {versions['codex']}" in out.get("stdout", "").splitlines()
        and f"opencode={versions['opencode']}" in out.get("stdout", "").splitlines()
        and "self_check_exit=0" in out.get("stdout", "").splitlines()
        and "deepseek_web_api=present" in out.get("stdout", "").splitlines()
    )
    print(f"[{'PASS' if ok else 'FAIL'}] 模板自检")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
