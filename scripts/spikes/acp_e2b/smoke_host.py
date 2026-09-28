#!/usr/bin/env python3
"""ACP E2B 最小真实冒烟（GLINK-ACP-04 / M3b）。

一次短题、有界超时、临时数据根、随机端口：起真实宿主进程（``python -m
pi_glink.app``）→ ``POST /api/runs`` → 同时订阅 SSE 看流式 → 轮询终态 →
``GET /api/runs/<id>/report`` 回收 ``report.md`` → 用生产对账助手
``confirm_sandbox_absent`` 按持久化 sandbox id 确认沙箱已不存在。

清理只操作已知身份：宿主进程用本脚本持有的 PID（``terminate``），沙箱按
sandbox id；没有 ``pkill -f``，没有名字通配删除。不打印任何凭据值。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
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

from pi_glink.kernel.e2b_env import confirm_sandbox_absent  # noqa: E402

TERMINAL = {"finished", "failed", "cancelled"}

DEFAULT_PROMPT = (
    "请先在工作区写出 report.md：用不超过 300 字、2 条要点说明云沙箱隔离对 AI 编程 "
    "agent 运行安全的价值，并附 1 条可核验的来源链接（可用 web_search）；随后用一句话总结。"
)


def _run_record(run_dir: Path) -> dict:
    """run 记录：存于 ``<runs>/runs.db``（ADR 0009），只读打开；不存在时返回空对象。"""
    import sqlite3
    from contextlib import closing

    database = run_dir.parent / "runs.db"
    if not database.is_file():
        return {}
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
        row = connection.execute(
            "SELECT record FROM runs WHERE run_id = ?", (run_dir.name,)
        ).fetchone()
    return json.loads(row[0]) if row else {}


def _run_events(run_dir: Path) -> list[dict]:
    """run 的已提交事件（``events`` 表按 seq 顺序），只读打开。"""
    import sqlite3
    from contextlib import closing

    database = run_dir.parent / "runs.db"
    if not database.is_file():
        return []
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as connection:
        rows = connection.execute(
            "SELECT body FROM events WHERE run_id = ? ORDER BY seq", (run_dir.name,)
        ).fetchall()
    return [json.loads(body) for (body,) in rows]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def resolve_api_key(args: argparse.Namespace) -> str:
    if os.environ.get("E2B_API_KEY"):
        return os.environ["E2B_API_KEY"]
    if args.env_file:
        for line in Path(args.env_file).read_text(encoding="utf-8").splitlines():
            if line.startswith("E2B_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def sse_collector(base: str, run_id: str, frames: list[dict], stop: threading.Event) -> None:
    """Read the run's SSE stream and record each frame with an arrival time."""
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
    except Exception as exc:  # noqa: BLE001 - the evidence records the stream failure
        frames.append({"at": time.time(), "event": "sse_error", "payload": {"error": f"{type(exc).__name__}: {exc}"}})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ACP E2B 最小真实冒烟")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--agent", default="dsh --profile acp")
    parser.add_argument("--template", default="glink-acp-worker")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--credential", default="~/.dsh/.credentials.yaml=.dsh/.credentials.yaml")
    parser.add_argument("--env-file", default="")
    parser.add_argument("--data-root", default="")
    parser.add_argument("--log", default="")
    args = parser.parse_args(argv)

    api_key = resolve_api_key(args)
    if not api_key:
        print("缺少 E2B_API_KEY（进程环境或 --env-file）", file=sys.stderr)
        return 2

    data_root = Path(args.data_root) if args.data_root else Path(tempfile.mkdtemp(prefix="glink-acp-e2b-smoke-"))
    data_root.mkdir(parents=True, exist_ok=True)
    log_path = Path(args.log) if args.log else data_root / "host.log"
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    packs = write_probe_pack(data_root / "probe", args.agent)
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT / "host"),
            "PI_GLINK_HARNESS": PROBE_PACK_NAME,
            "PI_GLINK_HARNESSES_DIR": str(packs),
            "PI_GLINK_ACP_CREDENTIAL_FILES": args.credential,
            "LOCAL_WEB_EXECUTOR": "e2b",
            "LOCAL_WEB_E2B_ACP_TEMPLATE": args.template,
            "LOCAL_WEB_DATA_ROOT": str(data_root),
            "LOCAL_WEB_TASK_TIMEOUT_SECONDS": str(args.timeout),
            "PI_GLINK_RESOURCE_RECONCILE_SECONDS": "0",
            "E2B_API_KEY": api_key,
        }
    )
    print("ACP M3b 最小真实冒烟")
    print(f"日期: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"宿主命令: python -m pi_glink.app --port {port}（harness=acp, executor=e2b）")
    print(f"agent 命令: {args.agent}")
    print(f"模板: {args.template}")
    print(f"数据根: {data_root}")
    print(f"任务超时: {args.timeout}s")
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
            print(f"终态: {run_view.get('status')} duration={run_view.get('durationSeconds')}s events={run_view.get('eventCount')}")

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
            print(f"宿主按 PID {process.pid} 终止: exit={exit_code}")

    # -- facts from the run directory (read-only) -----------------------------
    run_dir = data_root / "runs" / run_id
    meta = _run_record(run_dir)
    events = _run_events(run_dir)
    first_at = next((frame["at"] for frame in frames if frame["event"] != "sse_error"), None)
    kinds: dict[str, int] = {}
    for event in events:
        kinds[event["type"]] = kinds.get(event["type"], 0) + 1

    print()
    print("SSE 流式（到达时间相对 run 创建）")
    for frame in frames[:40]:
        relative = (frame["at"] - started) if first_at else 0.0
        event_type = frame["payload"].get("type", frame["event"])
        print(f"  {relative:7.2f}s {event_type}")
        if frame["event"] == "sse_error":
            print(f"    {frame['payload']}")
    print(f"  SSE 帧数: {len(frames)}")

    print()
    print("事件类型统计（runs.db events）")
    print("  " + " · ".join(f"{name} {count}" for name, count in sorted(kinds.items())))
    print()
    print("事件时间线（seq/相对秒/type/摘要）")
    t0 = events[0]["ts"] if events else time.time()
    for event in events[:40]:
        payload = event.get("payload") or {}
        summary = ""
        for key in ("message", "detail", "toolName", "status", "stopReason", "summary"):
            if payload.get(key):
                summary = str(payload[key])[:80]
                break
        print(f"  {event.get('seq'):4} {event['ts'] - t0:6.2f}s {event['type']:18} {summary}")

    print()
    print("终态与清理事实（runs.db 记录）")
    print(f"  status={meta.get('status')} modelStopReason={meta.get('model_stop_reason')} "
          f"reportStatus={meta.get('report_status')}")
    print(f"  sandbox_id={meta.get('sandbox_id')} container_name={meta.get('container_name')!r}")
    print(f"  cleanup={json.dumps(meta.get('cleanup'), ensure_ascii=False)}")
    print(f"  report_path={meta.get('report_path')}")

    sandbox_id = meta.get("sandbox_id") or ""
    absent, detail = confirm_sandbox_absent(sandbox_id, api_key=api_key)
    print()
    print("沙箱终止对账（只按持久化 sandbox_id，SDK 查/杀同一 id）")
    print(f"  sandbox_id={sandbox_id} confirmed_absent={absent} detail={detail}")

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
        and absent is True
        and bool(sandbox_id)
        and exit_code is not None
    )
    print()
    print(f"[{'PASS' if ok else 'FAIL'}] 冒烟（流式/终态/report.md/沙箱按 id 无残留）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
