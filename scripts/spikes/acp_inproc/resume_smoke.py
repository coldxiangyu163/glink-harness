#!/usr/bin/env python3
"""ACP inproc 真实续接冒烟（GLINK-ACP-09 / M7）。

一次「首轮 + 追问」：起真实宿主进程（``python -m pi_glink.app``），经宿主
HTTP API 先跑首轮（约定一个随机代号、写 report.md），再对同一个 agent 数据根
跑追问（``parentRunId`` → 内核走 ``session/resume``）。判据：

* 追问 run 的 ``sessionId`` 与首轮相同、``session_ready`` 带 ``resumed`` 与
  ``resumeMethod``、事件流有 ``acp_session_resumed``；
* 追问 run 的助手正文与 report.md 都出现首轮约定的代号，而首轮 report.md 与
  追问题面都不含该代号（新会话不可能凭工作区或题面答出）；
* 追问 run 的每 run HOME 里存在与父 run HOME 内容一致的状态文件（继承证据，
  排除凭据目标）；
* 两次 run 的流式/终态/report 回收/清理（只按已知 PID，无 ``pkill``/通配）。

清理只操作已知身份：宿主进程用本脚本持有的 PID（``terminate``）；运行期采样
的 agent 后代按 PID 记录，结束后只对这些已知 PID 发 ``TERM`` 并核对退出。

它**会消耗真实模型调用**，只在获得授权时使用。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import signal
import string
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

import smoke_host as base  # noqa: E402 - same-directory helper, no side effects

TERMINAL = base.TERMINAL
#: Ceiling for hashing one inherited state file (the state root is bounded; the
#: cap keeps the evidence walk from reading a pathological file whole).
DIGEST_MAX_BYTES = 16 * 1024 * 1024


def codename() -> str:
    rng = random.SystemRandom()
    word = "".join(rng.choice(string.ascii_uppercase) for _ in range(5))
    return f"{word}-{rng.randint(1000, 9999)}"


def first_prompt(code: str) -> str:
    return (
        "这是一次会话连续性检查，请严格按顺序完成：\n"
        f"1) 记住一个代号：{code}。不要把它写进任何文件，也不要使用任何工具来完成这一步。\n"
        "2) 然后做一次简短研究：用不超过 200 字、2 条要点说明 ACP 这类客户端-代理协议"
        "对宿主接入多种 agent 的价值，并附 1 条可核验的来源链接（需要时可用 web_search/"
        "web_fetch）；结果写入工作区 report.md，回复里一句话总结即可。\n"
        "注意：代号只留在会话里，report.md 里不要出现它。"
    )


def followup_prompt() -> str:
    return (
        "连续性追问。不要使用任何检索或外部工具，也不要读取除工作区 report.md 以外的文件；只做两件事：\n"
        "1) 用一行回复：上一个会话里我要求你记住的那个代号是什么？\n"
        "2) 把那个代号追加写入工作区 report.md 的末尾，单独一行，格式为 `代号 = <代号>`。\n"
        "不要重写整篇报告，也不要再检索。"
    )


def sanitize(text: str, data_root: Path) -> str:
    """Replace the operator home and the probe's data root with placeholders."""
    home = str(Path.home())
    return (
        text.replace(str(data_root), "$TMP/" + data_root.name)
        .replace(home, "~")
    )


def home_digests(home: Path, skip: set[str]) -> dict[str, str]:
    """``relative path -> sha256`` for one state root, links and dests skipped."""
    digests: dict[str, str] = {}
    if not home.is_dir():
        return digests
    for child in sorted(home.rglob("*")):
        if child.is_symlink() or not child.is_file():
            continue
        relative = child.relative_to(home).as_posix()
        if relative in skip:
            continue
        try:
            if child.stat().st_size > DIGEST_MAX_BYTES:
                continue
            digests[relative] = hashlib.sha256(child.read_bytes()).hexdigest()
        except OSError:
            continue
    return digests


def run_once(
    base_url: str,
    prompt: str,
    *,
    parent_run_id: str | None,
    timeout: int,
    label: str,
) -> dict:
    """Create one run over the HTTP API and collect its stream + terminal facts."""
    import httpx

    frames: list[dict] = []
    body: dict = {"prompt": prompt}
    if parent_run_id is not None:
        body["parentRunId"] = parent_run_id
    created = httpx.post(f"{base_url}/api/runs", json=body, timeout=30)
    print(f"[{label}] POST /api/runs -> HTTP {created.status_code}")
    if created.status_code != 201:
        print(created.text[:800])
        return {"created": created.status_code, "error": created.text[:800]}
    run = created.json()["run"]
    run_id = run["runId"]
    print(f"[{label}] runId: {run_id}")

    stop = threading.Event()
    thread = threading.Thread(
        target=base.sse_collector, args=(base_url, run_id, frames, stop), daemon=True
    )
    thread.start()
    deadline = time.monotonic() + timeout + 180
    current = run
    while time.monotonic() < deadline:
        current = httpx.get(f"{base_url}/api/runs/{run_id}", timeout=10).json()["run"]
        if current.get("status") in TERMINAL:
            break
        time.sleep(1)
    stop.set()
    thread.join(timeout=10)
    print(
        f"[{label}] 终态: {current.get('status')} duration={current.get('durationSeconds')}s "
        f"events={current.get('eventCount')} sessionId={current.get('sessionId')} "
        f"error={current.get('error')!r}"
    )
    report = httpx.get(f"{base_url}/api/runs/{run_id}/report", timeout=30)
    print(f"[{label}] GET /api/runs/{run_id}/report -> HTTP {report.status_code}（{len(report.content)} bytes）")
    return {
        "created": 201,
        "run": current,
        "frames": frames,
        "reportStatus": report.status_code,
        "report": report.content,
    }


def state_facts(data_root: Path, run_id: str, skip: set[str]) -> tuple[list[str], dict[str, str]]:
    home = data_root / "runs" / run_id / "acp-agent" / "home"
    digests = home_digests(home, skip)
    return sorted(digests), digests


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ACP inproc 真实续接冒烟")
    parser.add_argument("--label", default="acp")
    parser.add_argument("--agent", default="dsh --profile acp")
    parser.add_argument("--credential", action="append", default=[], help="source=dest，可重复")
    parser.add_argument("--env", action="append", default=[], help="NAME=value，可重复")
    parser.add_argument("--npm-cache", default="")
    parser.add_argument("--permission-allow", default="")
    parser.add_argument("--session-config", default="")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--data-root", default="")
    parser.add_argument("--log", default="")
    parser.add_argument(
        "--parent-run-id",
        default="",
        help="只跑追问：复用已在同一数据根完成的首轮 run（需同时给 --codename）",
    )
    parser.add_argument(
        "--codename",
        default="",
        help="--parent-run-id 模式下的首轮代号（不重新跑首轮时由调用方提供）",
    )
    args = parser.parse_args(argv)

    data_root = (
        Path(args.data_root)
        if args.data_root
        else Path(tempfile.mkdtemp(prefix="glink-acp-m7-resume-"))
    )
    data_root.mkdir(parents=True, exist_ok=True)
    os.chmod(data_root, 0o700)
    log_path = Path(args.log) if args.log else data_root / "host.log"
    port = base.free_port()
    base_url = f"http://127.0.0.1:{port}"

    credentials = list(args.credential)
    env_entries = list(args.env)
    if args.npm_cache:
        env_entries.append(f"npm_config_cache={args.npm_cache}")
    # Every configured credential destination is excluded from the inheritance
    # comparison: the child re-renders those files fresh, so they prove nothing
    # about state inheritance (and their values must not enter the evidence).
    credential_dests = {
        chunk.split("=", 1)[1].strip()
        for chunk in credentials
        if "=" in chunk and chunk.split("=", 1)[1].strip()
    }

    packs = write_probe_pack(
        data_root / "probe",
        args.agent,
        session_config=args.session_config,
        permission_allow=args.permission_allow,
    )
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT / "host"),
            "PI_GLINK_HARNESS": PROBE_PACK_NAME,
            "PI_GLINK_HARNESSES_DIR": str(packs),
            "LOCAL_WEB_DATA_ROOT": str(data_root),
            "LOCAL_WEB_TASK_TIMEOUT_SECONDS": str(args.timeout),
            "PI_GLINK_RESOURCE_RECONCILE_SECONDS": "0",
        }
    )
    if credentials:
        env["PI_GLINK_ACP_CREDENTIAL_FILES"] = ";".join(credentials)
    if env_entries:
        env["PI_GLINK_ACP_ENV"] = ";".join(env_entries)
    env.pop("PI_GLINK_WEB_SEARCH_CONFIG", None)

    code = args.codename or codename()
    first = first_prompt(code)
    followup = followup_prompt()
    assert code in first and code not in followup
    resume_only = bool(args.parent_run_id)
    if resume_only and not args.codename:
        print("--parent-run-id 模式需要 --codename（沿用首轮的约定代号）", file=sys.stderr)
        return 2

    print(f"ACP M7 真实续接冒烟（inproc / {args.label}）")
    print("=" * 52)
    print(f"日期: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"宿主命令: python -m pi_glink.app --port {port}（harness=acp, executor=inproc）")
    print(f"agent 命令: {args.agent}")
    print(f"命令级 env 名: {', '.join(name for name, _ in (e.split('=', 1) for e in env_entries)) or '（无）'}")
    print(f"权限白名单: {args.permission_allow or '（空 = fail-closed）'}")
    print(f"会话配置: {args.session_config or '（无）'}")
    print(f"数据根: $TMP/{data_root.name}")
    print(f"任务超时: {args.timeout}s")
    print("连续性判据: 首轮约定随机代号（不写入首轮 report.md），追问要求复述并写入新 report.md")
    if resume_only:
        print(f"模式: 只跑追问（复用首轮 run {args.parent_run_id}；代号由 --codename 提供）")
    print()

    exit_code: int | None = None
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
            target=base.watch_descendants,
            args=(process.pid, descendants_seen, watcher_stop),
            daemon=True,
        )
        watcher.start()
        results: dict[str, dict] = {}
        try:
            import httpx

            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    response = httpx.get(f"{base_url}/api/health", timeout=2)
                    if response.status_code == 200:
                        break
                except Exception:  # noqa: BLE001 - host still starting
                    pass
                time.sleep(0.2)
            else:
                print("[FAIL] 宿主未在 60s 内就绪")
                return 1
            print(f"宿主健康检查: HTTP {response.status_code}")

            results["parent"] = (
                {
                    "created": 201,
                    "run": httpx.get(
                        f"{base_url}/api/runs/{args.parent_run_id}", timeout=10
                    ).json()["run"],
                    "frames": [],
                    "reportStatus": 0,
                    "report": b"",
                }
                if resume_only
                else run_once(
                    base_url, first, parent_run_id=None, timeout=args.timeout, label="首轮"
                )
            )
            parent_id = args.parent_run_id or results["parent"]["run"]["runId"]
            if resume_only:
                report = httpx.get(
                    f"{base_url}/api/runs/{parent_id}/report", timeout=30
                )
                results["parent"]["reportStatus"] = report.status_code
                results["parent"]["report"] = report.content
                print(
                    f"[首轮] 复用已有 run {parent_id}: status={results['parent']['run'].get('status')} "
                    f"sessionId={results['parent']['run'].get('sessionId')} "
                    f"report HTTP {report.status_code}（{len(report.content)} bytes）"
                )
            elif results["parent"].get("created") != 201:
                return 1
            results["child"] = run_once(
                base_url, followup, parent_run_id=parent_id, timeout=args.timeout, label="追问"
            )
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
    leftovers = {pid: command for pid, command in descendants_seen.items() if base.alive(pid)}
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
        while time.monotonic() < deadline and any(base.alive(pid) for pid in leftovers):
            time.sleep(0.5)
        residual = [pid for pid in leftovers if base.alive(pid)]
        print(f"  清理后仍存活: {residual or '无'}")

    verdicts: list[str] = []
    if "child" not in results or results["child"].get("created") != 201:
        print()
        print("[FAIL] 追问未被受理（见上方 POST 返回）")
        return 1

    parent = results["parent"]["run"]
    child = results["child"]["run"]
    parent_dir = data_root / "runs" / parent["runId"]
    child_dir = data_root / "runs" / child["runId"]

    def events_of(run_dir: Path) -> list[dict]:
        path = run_dir / "events.jsonl"
        if not path.is_file():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    parent_events = events_of(parent_dir)
    child_events = events_of(child_dir)

    def payloads(events: list[dict], event_type: str) -> list[dict]:
        return [event["payload"] for event in events if event["type"] == event_type]

    def assistant_text(events: list[dict]) -> str:
        return "".join(
            str(payload.get("text") or "")
            for payload in payloads(events, "assistant_delta")
        )

    print()
    print("SSE 流式（到达时间相对各自 run 创建）")
    for label in ("parent", "child"):
        frames = results[label]["frames"]
        started = frames[0]["at"] if frames else time.monotonic()
        types: list[str] = []
        for frame in frames:
            types.append(str(frame["payload"].get("type", frame["event"])))
        print(f"  [{label}] SSE 帧数: {len(frames)}")
        for frame in frames[:30]:
            relative = frame["at"] - started
            print(f"    {relative:7.2f}s {frame['payload'].get('type', frame['event'])}")
        if len(frames) > 30:
            print(f"    … 其余 {len(frames) - 30} 帧；类型序列尾部: {types[-6:]}")

    print()
    print("续接事实（events.jsonl）")
    ready = payloads(child_events, "session_ready")
    child_ready = ready[0] if ready else {}
    print(f"  child session_ready: {sanitize(json.dumps({k: child_ready.get(k) for k in ('sessionId', 'resumed', 'resumeMethod', 'cwd')}, ensure_ascii=False), data_root)}")
    parent_ready = payloads(parent_events, "session_ready")
    parent_session = parent_ready[0].get("sessionId") if parent_ready else None
    print(f"  parent sessionId: {parent_session}")
    parent_meta = json.loads((parent_dir / "meta.json").read_text(encoding="utf-8"))
    child_meta = json.loads((child_dir / "meta.json").read_text(encoding="utf-8"))
    print(
        "  工作区: parent=%s · child=%s（ACP 会话 cwd 跨轮稳定，应相同）"
        % (sanitize(str(parent_meta.get("workspace")), data_root), sanitize(str(child_meta.get("workspace")), data_root))
    )
    print(f"  session_ready.cwd == parent workspace: {child_ready.get('cwd') == parent_meta.get('workspace')}")
    resumed_notices = [
        payload
        for payload in payloads(child_events, "kernel_status")
        if payload.get("message") == "acp_session_resumed"
    ]
    print(f"  child acp_session_resumed: {json.dumps(resumed_notices, ensure_ascii=False)[:400]}")

    parent_report = results["parent"]["report"].decode("utf-8", "replace")
    child_report = results["child"]["report"].decode("utf-8", "replace")
    child_text = assistant_text(child_events)
    print()
    print("连续性判据")
    print(f"  代号出现在 child 助手正文: {code in child_text}")
    print(f"  代号出现在 child report: {code in child_report}")
    print(f"  代号出现在 parent report（应为 False）: {code in parent_report}")
    print(f"  代号出现在 child 题面（应为 False）: {code in str(child.get('prompt'))}")
    print(f"  child.sessionId == parent.sessionId: {child.get('sessionId') == parent.get('sessionId')}")
    child_text_tail = child_text[-600:]
    print(f"  child 助手正文尾部: {child_text_tail!r}")

    parent_files, parent_digests = state_facts(data_root, parent["runId"], credential_dests)
    child_files, child_digests = state_facts(data_root, child["runId"], credential_dests)
    shared = {
        relative: digest
        for relative, digest in parent_digests.items()
        if child_digests.get(relative) == digest
    }
    print()
    print("状态继承（父/子 run 各自 HOME 的同一文件内容一致，凭据目标已排除）")
    print(f"  parent HOME 文件数: {len(parent_files)}；child HOME 文件数: {len(child_files)}")
    print(f"  内容一致的继承文件: {len(shared)} 个")
    for relative in sorted(shared)[:12]:
        print(f"    {relative}")
    print(f"  child HOME 文件（前 20）: {child_files[:20]}")

    print()
    print("终态与清理事实（meta.json）")
    for label, run_dir in (("parent", parent_dir), ("child", child_dir)):
        meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
        print(
            f"  [{label}] status={meta.get('status')} modelStopReason={meta.get('model_stop_reason')} "
            f"reportStatus={meta.get('report_status')} session_id={meta.get('session_id')}"
        )
        print(f"    cleanup={json.dumps(meta.get('cleanup'), ensure_ascii=False)}")
    print(f"  parent report_status / child report_status: {results['parent']['reportStatus']} / {results['child']['reportStatus']}")

    print()
    print("child report.md（HTTP %s，%s bytes）" % (results["child"]["reportStatus"], len(results["child"]["report"])))
    print("-" * 60)
    print(sanitize(child_report, data_root))
    print("-" * 60)

    print()
    print("宿主日志尾部")
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-10:]:
        print(f"  {sanitize(line, data_root)}")

    parent_ok = (
        parent.get("status") == "finished"
        and results["parent"]["reportStatus"] == 200
        and bool(results["parent"]["report"])
        and bool((json.loads((parent_dir / "meta.json").read_text(encoding="utf-8")).get("cleanup") or {}).get("released"))
    )
    child_ok = (
        child.get("status") == "finished"
        and results["child"]["reportStatus"] == 200
        and bool(results["child"]["report"])
        and bool((json.loads((child_dir / "meta.json").read_text(encoding="utf-8")).get("cleanup") or {}).get("released"))
        and child.get("sessionId") == parent.get("sessionId")
        and child_ready.get("resumed") is True
        and bool(resumed_notices)
        and child_ready.get("cwd") == parent_meta.get("workspace")
        and child_meta.get("workspace") == parent_meta.get("workspace")
        and code in child_report
        and code in child_text
        and code not in parent_report
        and code not in str(child.get("prompt"))
        and bool(shared)
        and not residual
    )
    ok = parent_ok and child_ok and exit_code is not None
    print()
    print(f"  parent 通过: {parent_ok}；child 通过: {child_ok}")
    print(f"[{'PASS' if ok else 'FAIL'}] ACP 真实续接冒烟（首轮+追问，流式/终态/report/清理只按已知 PID）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
