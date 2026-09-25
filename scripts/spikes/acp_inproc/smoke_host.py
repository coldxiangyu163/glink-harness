#!/usr/bin/env python3
"""ACP inproc 最小真实冒烟（GLINK-ACP-05 / M4）。

一次短题、有界超时、临时数据根、随机端口：起真实宿主进程（``python -m
pi_glink.app``）→ ``POST /api/runs`` → 同时订阅 SSE 看流式 → 轮询终态 →
``GET /api/runs/<id>/report`` 回收 ``report.md``。

清理只操作已知身份：宿主进程用本脚本持有的 PID（``terminate``）；冒烟期间
采样的 agent 后代进程按 PID 记录，结束后只对这些已知 PID 发 ``TERM`` 并核对
是否退出——没有 ``pkill -f``，没有名字或命令行通配删除。凭据值不打印、不落
证据：``--derive-settings-from`` 只把宿主 claude CLI 设置里的 ``env`` 块抽成
一份 0600 的运行期 settings 文件（放在临时数据根内），真实设置文件只读。

它**会消耗一次真实模型调用**，只在获得授权时使用。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "host"))

TERMINAL = {"finished", "failed", "cancelled"}

DEFAULT_PROMPT = (
    "请先在工作区写出 report.md：用不超过 200 字、2 条要点说明 ACP 这类"
    "客户端-代理协议对宿主接多 harness 的价值，并附 1 条可核验的来源链接"
    "（需要时可用 web_search/web_fetch）；随后用一句话总结。"
)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def derive_settings(source: Path, destination: Path) -> None:
    """Write a run-only settings file holding just the source's ``env`` block.

    The operator's real ``settings.json`` carries hooks, plugins and a status
    line that must not run inside a research run (they act on the operator's
    real state). The CLI only needs the ``env`` mapping to reach the configured
    provider, so that is the part copied into the run.
    """
    document = json.loads(source.read_text(encoding="utf-8"))
    env = document.get("env") if isinstance(document, dict) else None
    if not isinstance(env, dict) or not env:
        raise SystemExit(f"--derive-settings-from 的 {source} 没有可复制的 env 块")
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(destination.parent, 0o700)
    destination.write_text(json.dumps({"env": env}, indent=2), encoding="utf-8")
    os.chmod(destination, 0o600)


def process_table() -> dict[int, tuple[int, str]]:
    output = subprocess.run(
        ["ps", "-o", "pid,ppid,command", "-ax"], capture_output=True, text=True
    ).stdout
    table: dict[int, tuple[int, str]] = {}
    for line in output.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        ppid = int(parts[1]) if parts[1].isdigit() else 0
        command = parts[2] if len(parts) > 2 else ""
        table[pid] = (ppid, command)
    return table


def descendants(root_pid: int) -> dict[int, str]:
    """(pid -> command line) for every live descendant of ``root_pid``."""
    table = process_table()
    found: dict[int, str] = {}
    frontier = [root_pid]
    while frontier:
        parent = frontier.pop()
        for pid, (ppid, command) in table.items():
            if ppid == parent and pid not in found:
                found[pid] = command
                frontier.append(pid)
    return found


def watch_descendants(
    root_pid: int, seen: dict[int, str], stop: threading.Event
) -> None:
    while not stop.is_set():
        for pid, command in descendants(root_pid).items():
            seen.setdefault(pid, command)
        time.sleep(0.5)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sse_collector(base: str, run_id: str, frames: list[dict], stop: threading.Event) -> None:
    import httpx

    try:
        with httpx.stream("GET", f"{base}/api/runs/{run_id}/events", timeout=600) as response:
            event = ""
            for line in response.iter_lines():
                if stop.is_set():
                    break
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    raw = line.split(":", 1)[1].strip()
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    frames.append({"at": time.time(), "event": event, "payload": payload})
                    if payload.get("type") in {"run_finished", "run_failed", "run_cancelled"}:
                        break
    except Exception as exc:  # noqa: BLE001 - the evidence records a stream failure
        frames.append(
            {
                "at": time.time(),
                "event": "sse_error",
                "payload": {"error": f"{type(exc).__name__}: {exc}"},
            }
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ACP inproc 最小真实冒烟")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--agent", default="npx -y @zed-industries/claude-code-acp@0.16.2")
    parser.add_argument("--credential", action="append", default=[], help="source=dest，可重复")
    parser.add_argument("--env", action="append", default=[], help="NAME=value，可重复")
    parser.add_argument(
        "--derive-settings-from",
        default="",
        help="把该 claude settings.json 的 env 块抽成运行期 settings（0600，临时数据根内）",
    )
    parser.add_argument("--npm-cache", default="")
    parser.add_argument(
        "--permission-allow",
        default="",
        help="PI_GLINK_ACP_PERMISSION_ALLOW（默认留空 = fail-closed 原行为）",
    )
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--data-root", default="")
    parser.add_argument("--log", default="")
    args = parser.parse_args(argv)

    data_root = (
        Path(args.data_root) if args.data_root else Path(tempfile.mkdtemp(prefix="glink-acp-inproc-smoke-"))
    )
    data_root.mkdir(parents=True, exist_ok=True)
    os.chmod(data_root, 0o700)
    log_path = Path(args.log) if args.log else data_root / "host.log"
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    credentials = list(args.credential)
    env_entries = list(args.env)
    if args.npm_cache:
        env_entries.append(f"npm_config_cache={args.npm_cache}")
    source_settings = Path(args.derive_settings_from).expanduser() if args.derive_settings_from else None
    source_digest = ""
    if source_settings is not None:
        if not source_settings.is_file():
            print(f"--derive-settings-from 不存在: {source_settings}", file=sys.stderr)
            return 2
        source_digest = sha256_file(source_settings)
        derived = data_root / "run-settings" / "settings.json"
        derive_settings(source_settings, derived)
        credentials.append(f"{derived}=.claude/settings.json")

    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT / "host"),
            "PI_GLINK_HARNESS": "acp",
            "PI_GLINK_ACP_COMMAND": args.agent,
            "LOCAL_WEB_DATA_ROOT": str(data_root),
            "LOCAL_WEB_TASK_TIMEOUT_SECONDS": str(args.timeout),
            "PI_GLINK_RESOURCE_RECONCILE_SECONDS": "0",
        }
    )
    if credentials:
        env["PI_GLINK_ACP_CREDENTIAL_FILES"] = ";".join(credentials)
    if env_entries:
        env["PI_GLINK_ACP_ENV"] = ";".join(env_entries)
    if args.permission_allow:
        env["PI_GLINK_ACP_PERMISSION_ALLOW"] = args.permission_allow
    env.pop("PI_GLINK_WEB_SEARCH_CONFIG", None)

    print("ACP M4 最小真实冒烟（inproc）")
    print("=" * 44)
    print(f"日期: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"宿主命令: python -m pi_glink.app --port {port}（harness=acp, executor=inproc）")
    print(f"agent 命令: {args.agent}")
    print(f"命令级 env 名: {', '.join(name for name, _ in (e.split('=', 1) for e in env_entries)) or '（无）'}")
    print(f"权限白名单: {args.permission_allow or '（空 = fail-closed）'}")
    print(f"数据根: {data_root}")
    print(f"任务超时: {args.timeout}s")
    if source_settings is not None:
        print(f"运行期 settings 派生自: {source_settings}（源仅读；sha256={source_digest[:16]}…）")
    print()

    with log_path.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            [sys.executable, "-m", "pi_glink.app", "--port", str(port)],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        print(f"宿主 PID: {process.pid}（只按这个 PID 收尾）")
        descendants_seen: dict[int, str] = {}
        watcher_stop = threading.Event()
        watcher = threading.Thread(
            target=watch_descendants,
            args=(process.pid, descendants_seen, watcher_stop),
            daemon=True,
        )
        watcher.start()

        exit_code: int | None = None
        run_id = ""
        frames: list[dict] = []
        run_view: dict = {}
        report_status = 0
        report_body = b""
        try:
            import httpx

            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    response = httpx.get(f"{base}/api/health", timeout=2)
                    if response.status_code == 200:
                        break
                except Exception:  # noqa: BLE001 - host still starting
                    pass
                time.sleep(0.2)
            else:
                print("[FAIL] 宿主未在 60s 内就绪")
                return 1
            print(f"宿主健康检查: HTTP {response.status_code}")

            started = time.time()
            created = httpx.post(f"{base}/api/runs", json={"prompt": args.prompt}, timeout=30)
            print(f"POST /api/runs -> HTTP {created.status_code}")
            if created.status_code != 201:
                print(created.text[:800])
                return 1
            run_view = created.json()["run"]
            run_id = run_view["runId"]
            print(f"runId: {run_id}")

            stop = threading.Event()
            thread = threading.Thread(
                target=sse_collector, args=(base, run_id, frames, stop), daemon=True
            )
            thread.start()

            deadline = time.monotonic() + args.timeout + 180
            while time.monotonic() < deadline:
                current = httpx.get(f"{base}/api/runs/{run_id}", timeout=10).json()["run"]
                if current.get("status") in TERMINAL:
                    run_view = current
                    break
                time.sleep(2)
            stop.set()
            thread.join(timeout=10)
            print(
                f"终态: {run_view.get('status')} duration={run_view.get('durationSeconds')}s "
                f"events={run_view.get('eventCount')}"
            )

            report = httpx.get(f"{base}/api/runs/{run_id}/report", timeout=30)
            report_status = report.status_code
            report_body = report.content
            print(f"GET /api/runs/{run_id}/report -> HTTP {report_status}（{len(report_body)} bytes）")
        finally:
            process.terminate()
            try:
                exit_code = process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                exit_code = process.wait(timeout=10)
            watcher_stop.set()
            watcher.join(timeout=5)
            print(f"宿主按 PID {process.pid} 终止: exit={exit_code}")

    time.sleep(1.0)
    descendants_seen.pop(process.pid, None)
    leftovers = {pid: command for pid, command in descendants_seen.items() if alive(pid)}
    residual: list[int] = []
    print()
    print(f"run 期间采样的宿主后代进程: {len(descendants_seen)} 个（只按 PID 记录）")
    for pid, command in list(descendants_seen.items())[:12]:
        state = "leftover" if pid in leftovers else "exited"
        print(f"  {pid} [{state}] {command[:160]}")
    if leftovers:
        print("仍存活的后代 PID（按已知 PID 发 TERM，不用模式匹配）")
        for pid, command in leftovers.items():
            print(f"  TERM {pid} {command[:120]}")
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and any(alive(pid) for pid in leftovers):
            time.sleep(0.5)
        residual = [pid for pid in leftovers if alive(pid)]
        print(f"  清理后仍存活: {residual or '无'}")

    # -- facts from the run directory (read-only) -----------------------------
    run_dir = data_root / "runs" / run_id
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    first_at = next((frame["at"] for frame in frames if frame["event"] != "sse_error"), None)
    kinds: dict[str, int] = {}
    for event in events:
        kinds[event["type"]] = kinds.get(event["type"], 0) + 1

    print()
    print("SSE 流式（到达时间相对 run 创建）")
    started = started if "started" in locals() else time.time()
    for frame in frames[:40]:
        relative = (frame["at"] - started) if first_at else 0.0
        event_type = frame["payload"].get("type", frame["event"])
        print(f"  {relative:7.2f}s {event_type}")
        if frame["event"] == "sse_error":
            print(f"    {frame['payload']}")
    print(f"  SSE 帧数: {len(frames)}")

    print()
    print("事件类型统计（events.jsonl）")
    print("  " + " · ".join(f"{name} {count}" for name, count in sorted(kinds.items())))

    print()
    print("权限决定（kernel_status）")
    permission_events = [
        event["payload"]
        for event in events
        if event["type"] == "kernel_status"
        and str(event["payload"].get("message", "")).startswith("acp_permission_")
    ]
    if not permission_events:
        print("  （本次 run 没有权限请求）")
    for payload in permission_events[:20]:
        print(
            f"  {payload.get('message')} decision={payload.get('decision')} "
            f"policy={payload.get('policy')} kind={payload.get('toolKind')} "
            f"source={payload.get('toolKindSource')}"
        )

    print()
    print("事件时间线（seq/相对秒/type/摘要）")
    t0 = events[0]["ts"] if events else time.time()
    for event in events[:60]:
        payload = event.get("payload") or {}
        summary = ""
        for key in ("message", "detail", "toolName", "status", "stopReason", "summary"):
            if payload.get(key):
                summary = str(payload[key])[:90]
                break
        print(f"  {event.get('seq'):4} {event['ts'] - t0:6.2f}s {event['type']:18} {summary}")

    print()
    print("终态与清理事实（meta.json）")
    print(
        f"  status={meta.get('status')} modelStopReason={meta.get('model_stop_reason')} "
        f"reportStatus={meta.get('report_status')}"
    )
    print(f"  container_name={meta.get('container_name')!r} sandbox_id={meta.get('sandbox_id')!r}")
    print(f"  cleanup={json.dumps(meta.get('cleanup'), ensure_ascii=False)}")
    print(f"  report_path={meta.get('report_path')}")

    if source_settings is not None:
        source_after = sha256_file(source_settings)
        print()
        print(
            f"settings 源只读核对: {'unchanged' if source_after == source_digest else 'CHANGED'}"
        )

    print()
    print(f"report.md（HTTP {report_status}，{len(report_body)} bytes）")
    print("-" * 60)
    print(report_body.decode("utf-8", "replace"))
    print("-" * 60)

    print()
    print("宿主日志尾部")
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-12:]:
        print(f"  {line}")

    ok = (
        run_view.get("status") == "finished"
        and report_status == 200
        and bool(report_body)
        and bool(meta.get("cleanup", {}).get("released"))
        and not residual
        and exit_code is not None
        and (source_settings is None or sha256_file(source_settings) == source_digest)
    )
    print()
    print(f"[{'PASS' if ok else 'FAIL'}] 冒烟（流式/终态/report.md/清理只操作已知 PID）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
