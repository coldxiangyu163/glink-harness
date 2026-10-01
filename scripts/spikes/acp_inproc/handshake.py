#!/usr/bin/env python3
"""ACP inproc 握手探针（GLINK-ACP-05 / M4 起，GLINK-ACP-06 / M5 扩展，不消耗模型调用）。

用**生产装配路径**（``build_config`` → ``prepare_acp_runtime`` →
``AcpExecutionAdapter``）在本机跑 ``initialize`` + ``session/new``，不发
``session/prompt``：

* 记录被测 agent 的 ``initialize`` 能力矩阵与 ``session/new`` 响应原文
  （dsh / codex-acp / opencode 都用同一个探针）；
* 记录能力位来源（agentInfo / protocolVersion / loadSession /
  sessionCapabilities / promptCapabilities / authMethods / modes /
  configOptions）供集成笔记并排对照；
* 记录每 run HOME 的落点（路径、权限位、文件大小；不含内容）、凭据源文件
  sha256 前后一致、子进程命令行（证明钉版入口）；
* ``--session-config`` 可实测 session option（写入临时探针包清单；M5 的 opencode
  用 ``model=…`` 观测 option 变化与 ``effort`` 选项的联动）；
* 不打印任何凭据值；凭据源始终只读。

CLI 入口只做钉版调用（默认 ``dsh --profile acp``；opencode 用
``npx -y opencode-ai@1.18.32 acp``，本地已装 1.18.18 与它同族），
不做全局安装。``--env`` 可注入 ``npm_config_cache``（npx 缓存）等命令级环境；
``--watch`` 选择子进程采样关键字（dsh / codex-acp / opencode）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "host"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "spikes"))
from acp_pack import PROBE_PACK_NAME, write_probe_pack  # noqa: E402

from glink_harness import __version__ as HOST_VERSION  # noqa: E402
from glink_harness.config import build_config  # noqa: E402
from glink_harness.kernel.acp_env import prepare_acp_runtime  # noqa: E402
from glink_harness.kernel.execution import ExecutionSpec  # noqa: E402
from glink_harness.kernel.harness_acp import AcpHarnessFamily  # noqa: E402
from glink_harness.kernel.sandbox import InprocSandbox  # noqa: E402
from glink_harness.store import RunStore  # noqa: E402

DEFAULT_COMMAND = "dsh --profile acp"

#: Capability fields worth printing on their own line, in a stable order.
CAPABILITY_FIELDS = (
    "protocolVersion",
    "agentInfo",
    "agentCapabilities",
    "authMethods",
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resolve_source(source_text: str) -> Path:
    if source_text.startswith("~/"):
        return Path.home() / source_text[2:]
    return Path(source_text)


def watch_processes(
    dest: list[str], stop: threading.Event, *, pattern: str = "dsh", timeout: float = 90.0
) -> None:
    deadline = time.monotonic() + timeout
    while not stop.is_set() and time.monotonic() < deadline:
        try:
            output = subprocess.run(
                ["ps", "-o", "pid,ppid,command", "-ax"], capture_output=True, text=True
            ).stdout
        except OSError:
            return
        for line in output.splitlines():
            if pattern in line and "grep" not in line and line not in dest:
                dest.append(line.strip())
        time.sleep(0.4)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ACP inproc 握手（不发 prompt）")
    parser.add_argument("--command", default=DEFAULT_COMMAND)
    parser.add_argument("--credential", action="append", default=[], help="source=dest，可重复")
    parser.add_argument("--env", action="append", default=[], help="NAME=value，可重复")
    parser.add_argument("--npm-cache", default="", help="注入 npm_config_cache 的便捷参数")
    parser.add_argument(
        "--session-config",
        default="",
        help="session_config（; 分隔的 id=value，写入临时探针包清单，应用在 session/new 之后）",
    )
    parser.add_argument(
        "--watch",
        default="dsh",
        help="子进程命令行采样关键字（默认 dsh；opencode 用 opencode）",
    )
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args(argv)

    env_entries = list(args.env)
    if args.npm_cache:
        env_entries.append(f"npm_config_cache={args.npm_cache}")

    workdir = Path(tempfile.mkdtemp(prefix="glink-acp-inproc-handshake-"))
    workspace = workdir / "workspace"
    workspace.mkdir()
    credentials = list(args.credential)
    packs = write_probe_pack(
        workdir / "probe",
        args.command,
        session_config=args.session_config,
        permission_allow=args.permission_allow,
    )
    values = {
        "GLINK_HARNESS": PROBE_PACK_NAME,
        "GLINK_HARNESSES_DIR": str(packs),
        "LOCAL_WEB_DATA_ROOT": str(workdir / "data"),
        "LOCAL_WEB_TASK_TIMEOUT_SECONDS": str(args.timeout),
    }
    if credentials:
        values["GLINK_ACP_CREDENTIAL_FILES"] = ";".join(credentials)
    if env_entries:
        values["GLINK_ACP_ENV"] = ";".join(env_entries)

    config = build_config(values, REPO_ROOT)
    runtime = prepare_acp_runtime(config, runtime_dir=workdir / "data" / "acp-agent")

    print("ACP inproc 握手实测（不消耗模型调用）")
    print("=" * 44)
    print(f"日期: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"宿主: {sys.platform} / Python {sys.version.split()[0]} / host glink-harness {HOST_VERSION}")
    print(f"agent 命令: {args.command}")
    print(f"命令级 env 名: {', '.join(name for name, _ in config.acp_env) or '（无）'}")
    session_config = "; ".join(
        f"{name}={value}" for name, value in config.pack.capabilities.session_config
    )
    print(f"会话配置: {session_config or '（无）'}")
    print(f"运行期 HOME: {runtime.home}")
    print(f"redactor 并入值: {len(runtime.secrets)} 条（不打印值）")
    credential_rows: list[tuple[str, Path, str, str]] = []
    for source_text, dest in config.acp_credential_files:
        source = resolve_source(source_text)
        if not source.is_file():
            # prepare_acp_runtime would already have failed; keep the guard for clarity.
            print(f"凭据来源不可读: {source_text}", file=sys.stderr)
            return 2
        credential_rows.append((source_text, source, dest, sha256_file(source)))
        print(
            f"凭据: {source_text} -> HOME/{dest}"
            f"（源 sha256={sha256_file(source)[:16]}…，"
            f"mtime={time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(source.stat().st_mtime))}）"
        )

    frames: list[dict] = []
    processes: list[str] = []
    stop = threading.Event()
    watcher = threading.Thread(
        target=watch_processes, args=(processes, stop), kwargs={"pattern": args.watch}, daemon=True
    )
    watcher.start()

    spec = ExecutionSpec(
        config=config,
        runtime=runtime,
        run_id="handshake",
        prompt="",
        system_prompt="",
        workspace=workspace,
        timeout_seconds=args.timeout,
    )
    # 生产组合：harness.driver(sandbox.spawn(harness.launch(spec)))。
    family = AcpHarnessFamily(config=config, store=RunStore(workdir / "runs"))
    sandbox = InprocSandbox(config=config, run_id="handshake", workspace=workspace)
    adapter = family.build_adapter(spec, sandbox, on_frame=frames.append)
    started = time.monotonic()
    exit_code = 0
    try:
        adapter.start()
    except Exception as exc:  # noqa: BLE001 - the failure is the evidence
        stop.set()
        print(f"\n[FAIL] initialize/session/new: {type(exc).__name__}: {exc}")
        print(f"耗时: {time.monotonic() - started:.1f}s")
        exit_code = 1
    else:
        session = adapter.session_info()
        payload = dict(session.payload) if session else {}
        print(f"\n握手成功（{time.monotonic() - started:.1f}s）")
        print("能力矩阵（initialize / session/new 响应原文）")
        print("-" * 44)
        for field in CAPABILITY_FIELDS:
            print(f"{field}: {json.dumps(payload.get(field), ensure_ascii=False)}")
        print(f"sessionId: {payload.get('sessionId')}")
        print(f"cwd: {payload.get('cwd')}")
        print(f"modes: {json.dumps(payload.get('modes'), ensure_ascii=False)}")
        options = payload.get("configOptions") or []
        print(f"configOptions: {json.dumps(options, ensure_ascii=False)}")
        commands = [
            frame.get("params", {}).get("update", {}).get("availableCommands")
            for frame in frames
            if frame.get("method") == "session/update"
            and isinstance(frame.get("params"), dict)
            and isinstance(frame["params"].get("update"), dict)
            and frame["params"]["update"].get("sessionUpdate") == "available_commands_update"
        ]
        if commands:
            print(f"available_commands: {len(commands[0] or [])} 条（宿主门禁拒绝 / 开头 prompt）")
    finally:
        stop.set()
        close = adapter.close()
        print(f"\nclient.close(): released={close.released} failures={list(close.failures)}")

    print("\nagent 子进程命令行（采样，证明钉版入口）")
    print("-" * 44)
    print(f"  采样关键字: {args.watch}")
    for line in processes[:6]:
        print(f"  {line[:300]}")
    print("\n每 run HOME 落点（路径 + 权限位 + 大小；不含内容）")
    print("-" * 44)
    for path in sorted(runtime.home.rglob("*"))[:40]:
        try:
            mode = oct(path.stat().st_mode & 0o777)
            size = path.stat().st_size if path.is_file() else ""
        except OSError:
            continue
        print(f"  {path.relative_to(runtime.home)} {mode} {size}".rstrip())

    print("\n凭据源只读核对")
    print("-" * 44)
    for source_text, source, _dest, before in credential_rows:
        after = sha256_file(source)
        state = "unchanged" if after == before else "CHANGED"
        print(f"  {source_text}: sha256 {state}")
        if after != before:
            exit_code = 1

    print(f"\n临时目录: {workdir}（按本脚本创建的路径清理；未做模式匹配删除）")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
