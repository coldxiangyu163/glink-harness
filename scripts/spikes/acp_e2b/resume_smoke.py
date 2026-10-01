#!/usr/bin/env python3
"""ACP E2B 真实续接冒烟（GLINK-ACP-13 / M8）。

一次「首轮 + 追问」，两轮各一个真实 E2B 沙箱：起真实宿主进程
（``python -m glink_harness.app``，``executor=e2b``）经宿主 HTTP API 先跑首轮
（约定随机代号、写 report.md），再对同一个数据根跑追问（``parentRunId``）。
判据：

* 追问 run 的 ``sandboxId`` 与首轮不同（真的换了沙箱）、``sessionId`` 与首轮
  相同、``session_ready`` 带 ``resumed``/``resumeMethod``，事件流有
  ``acp_session_resumed``；
* 首轮 run 的状态根在沙箱销毁前被有界回收（``runs/<id>/acp-agent/recovered``
  存在且非空），追问 run 的同一目录存在、且两者有内容一致的会话状态文件
  （跨沙箱继承的证据，凭据目标已排除）；
* 追问 run 的助手正文与 report.md 都出现首轮约定的代号，而首轮 report.md 与
  追问题面都不含该代号（新会话不可能凭工作区或题面答出）；
* 清理只按已知身份：宿主进程用本脚本持有的 PID（``terminate``），两个沙箱用
  生产对账助手 ``confirm_sandbox_absent`` 按持久化 sandbox id 确认不存在；
  没有 ``pkill -f``、没有名字通配删除。

它**会消耗真实模型调用与 E2B 沙箱**，只在获得授权时使用。``E2B_API_KEY``
从 ``--env-file``（或进程环境）读取，不打印、不落证据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
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

from glink_harness.kernel.e2b_env import confirm_sandbox_absent  # noqa: E402

TERMINAL = {"finished", "failed", "cancelled"}
#: Ceiling for hashing one inherited state file (the mirror is bounded; the cap
#: keeps the evidence walk from reading a pathological file whole).
DIGEST_MAX_BYTES = 16 * 1024 * 1024


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
    import socket

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
    return text.replace(str(data_root), "$TMP/" + data_root.name).replace(home, "~")


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


def sse_collector(base: str, run_id: str, frames: list[dict], stop: threading.Event) -> None:
    """Read the run's SSE stream and record each frame with an arrival time."""
    import httpx

    try:
        with httpx.stream("GET", f"{base}/api/runs/{run_id}/events", timeout=900) as response:
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
        frames.append(
            {"at": time.time(), "event": "sse_error", "payload": {"error": f"{type(exc).__name__}: {exc}"}}
        )


def run_once(
    base: str, prompt: str, *, parent_run_id: str | None, timeout: int, label: str
) -> dict:
    """Create one run over the HTTP API and collect its stream + terminal facts."""
    import httpx

    frames: list[dict] = []
    body: dict = {"prompt": prompt}
    if parent_run_id is not None:
        body["parentRunId"] = parent_run_id
    created = httpx.post(f"{base}/api/runs", json=body, timeout=30)
    print(f"[{label}] POST /api/runs -> HTTP {created.status_code}")
    if created.status_code != 201:
        print(created.text[:800])
        return {"created": created.status_code, "error": created.text[:800]}
    run = created.json()["run"]
    run_id = run["runId"]
    print(f"[{label}] runId: {run_id}")

    stop = threading.Event()
    thread = threading.Thread(
        target=sse_collector, args=(base, run_id, frames, stop), daemon=True
    )
    thread.start()
    deadline = time.monotonic() + timeout + 300
    current = run
    while time.monotonic() < deadline:
        current = httpx.get(f"{base}/api/runs/{run_id}", timeout=10).json()["run"]
        if current.get("status") in TERMINAL:
            break
        time.sleep(2)
    stop.set()
    thread.join(timeout=10)
    print(
        f"[{label}] 终态: {current.get('status')} duration={current.get('durationSeconds')}s "
        f"events={current.get('eventCount')} sessionId={current.get('sessionId')} "
        f"error={current.get('error')!r}"
    )
    report = httpx.get(f"{base}/api/runs/{run_id}/report", timeout=60)
    print(
        f"[{label}] GET /api/runs/{run_id}/report -> HTTP {report.status_code}"
        f"（{len(report.content)} bytes）"
    )
    return {
        "created": 201,
        "run": current,
        "frames": frames,
        "reportStatus": report.status_code,
        "report": report.content,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ACP E2B 真实续接冒烟（首轮 + 追问）")
    parser.add_argument("--label", default="e2b")
    parser.add_argument("--agent", default="dsh --profile acp")
    parser.add_argument("--template", default="glink-acp-worker")
    parser.add_argument("--credential", default="~/.dsh/.credentials.yaml=.dsh/.credentials.yaml")
    parser.add_argument("--env", action="append", default=[], help="NAME=value，可重复")
    parser.add_argument("--env-file", default="", help="含 E2B_API_KEY 的冻结配置")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--data-root", default="")
    parser.add_argument("--log", default="")
    args = parser.parse_args(argv)

    api_key = resolve_api_key(args)
    if not api_key:
        print("缺少 E2B_API_KEY（进程环境或 --env-file）", file=sys.stderr)
        return 2

    data_root = (
        Path(args.data_root)
        if args.data_root
        else Path(tempfile.mkdtemp(prefix="glink-acp13-e2b-resume-"))
    )
    data_root.mkdir(parents=True, exist_ok=True)
    os.chmod(data_root, 0o700)
    log_path = Path(args.log) if args.log else data_root / "host.log"
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    credential_dests = {
        chunk.split("=", 1)[1].strip()
        for chunk in [args.credential]
        if "=" in chunk and chunk.split("=", 1)[1].strip()
    }
    packs = write_probe_pack(data_root / "probe", args.agent)
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT / "host"),
            "GLINK_HARNESS": PROBE_PACK_NAME,
            "GLINK_HARNESSES_DIR": str(packs),
            "LOCAL_WEB_EXECUTOR": "e2b",
            "LOCAL_WEB_E2B_ACP_TEMPLATE": args.template,
            "LOCAL_WEB_DATA_ROOT": str(data_root),
            "LOCAL_WEB_TASK_TIMEOUT_SECONDS": str(args.timeout),
            "GLINK_RESOURCE_RECONCILE_SECONDS": "0",
            "E2B_API_KEY": api_key,
        }
    )
    if args.credential:
        env["GLINK_ACP_CREDENTIAL_FILES"] = args.credential
    if args.env:
        env["GLINK_ACP_ENV"] = ";".join(args.env)

    code = codename()
    first = first_prompt(code)
    followup = followup_prompt()
    assert code in first and code not in followup

    print(f"ACP M8 真实续接冒烟（e2b / {args.label}）")
    print("=" * 52)
    print(f"日期: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"宿主命令: python -m glink_harness.app --port {port}（harness=acp, executor=e2b）")
    print(f"agent 命令: {args.agent}")
    print(f"模板: {args.template}")
    print(f"命令级 env 名: {', '.join(item.split('=', 1)[0] for item in args.env) or '（无）'}")
    print(f"凭据目标: {', '.join(sorted(credential_dests)) or '（无）'}（只读复制进每 run HOME）")
    print(f"数据根: $TMP/{data_root.name}")
    print(f"任务超时: {args.timeout}s")
    print("连续性判据: 首轮约定随机代号（不写入首轮 report.md），追问要求复述并写入新 report.md")
    print()

    results: dict[str, dict] = {}
    exit_code: int | None = None
    with log_path.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            [sys.executable, "-m", "glink_harness.app", "--port", str(port)],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        print(f"宿主 PID: {process.pid}（只按这个 PID 收尾）")
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

            results["parent"] = run_once(
                base, first, parent_run_id=None, timeout=args.timeout, label="首轮"
            )
            if results["parent"].get("created") != 201:
                return 1
            parent_id = results["parent"]["run"]["runId"]
            results["child"] = run_once(
                base, followup, parent_run_id=parent_id, timeout=args.timeout, label="追问"
            )
        finally:
            process.terminate()
            try:
                exit_code = process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                exit_code = process.wait(timeout=10)
            print(f"宿主按 PID {process.pid} 终止: exit={exit_code}")

    if "child" not in results or results["child"].get("created") != 201:
        print()
        print("[FAIL] 追问未被受理（见上方 POST 返回）")
        return 1

    parent = results["parent"]["run"]
    child = results["child"]["run"]
    parent_dir = data_root / "runs" / parent["runId"]
    child_dir = data_root / "runs" / child["runId"]
    parent_meta = _run_record(parent_dir)
    child_meta = _run_record(child_dir)

    def events_of(run_dir: Path) -> list[dict]:
        return _run_events(run_dir)

    parent_events = events_of(parent_dir)
    child_events = events_of(child_dir)

    def payloads(events: list[dict], event_type: str) -> list[dict]:
        return [event["payload"] for event in events if event["type"] == event_type]

    def assistant_text(events: list[dict]) -> str:
        return "".join(
            str(payload.get("text") or "")
            for payload in payloads(events, "assistant_delta")
        )

    def terminal_failures(events: list[dict]) -> list[str]:
        """The terminal frame's ``artifactFailures`` (the record of refusals)."""
        terminal = payloads(events, "run_finished") + payloads(events, "run_failed")
        if not terminal:
            return ["缺少终态帧"]
        failures = terminal[-1].get("artifactFailures")
        return [str(item) for item in failures] if isinstance(failures, list) else []

    print()
    print("SSE 流式（到达时间相对各自 run 创建）")
    for label in ("parent", "child"):
        frames = results[label]["frames"]
        started = frames[0]["at"] if frames else time.monotonic()
        for frame in frames[:20]:
            relative = frame["at"] - started
            print(f"  [{label}] {relative:7.2f}s {frame['payload'].get('type', frame['event'])}")
        print(f"  [{label}] SSE 帧数: {len(frames)}")

    print()
    print("跨沙箱续接事实")
    print(f"  parent sandboxId: {parent_meta.get('sandbox_id')}")
    print(f"  child  sandboxId: {child_meta.get('sandbox_id')}")
    print(f"  两个沙箱不同: {parent_meta.get('sandbox_id') != child_meta.get('sandbox_id')}")
    ready = payloads(child_events, "session_ready")
    # The first ``session_ready`` is the pre-turn record; the last one is
    # published after the state root travelled back to the host (M8), so the
    # recovery verdict lives there.
    child_ready = ready[0] if ready else {}
    child_ready_final = ready[-1] if ready else {}
    parent_ready = payloads(parent_events, "session_ready")
    parent_ready_final = parent_ready[-1] if parent_ready else {}
    print(
        "  child session_ready（首帧）: "
        + sanitize(
            json.dumps(
                {
                    key: child_ready.get(key)
                    for key in ("sessionId", "resumed", "resumeMethod", "cwd", "sandboxId")
                },
                ensure_ascii=False,
            ),
            data_root,
        )
    )
    print(
        "  child session_ready（末帧）: "
        + sanitize(
            json.dumps(
                {
                    key: child_ready_final.get(key)
                    for key in ("sessionId", "stateRootRecovered", "sandboxId")
                },
                ensure_ascii=False,
            ),
            data_root,
        )
    )
    parent_session = parent_ready[0].get("sessionId") if parent_ready else None
    print(f"  parent sessionId: {parent_session}")
    print(f"  child.sessionId == parent.sessionId: {child.get('sessionId') == parent.get('sessionId')}")
    resumed_notices = [
        payload
        for payload in payloads(child_events, "kernel_status")
        if payload.get("message") == "acp_session_resumed"
    ]
    print(f"  child acp_session_resumed: {json.dumps(resumed_notices, ensure_ascii=False)[:400]}")
    print(
        "  工作区: parent=%s · child=%s"
        % (
            sanitize(str(parent_meta.get("workspace")), data_root),
            sanitize(str(child_meta.get("workspace")), data_root),
        )
    )

    print()
    print("状态根回收（宿主 runs/<id>/acp-agent/recovered）")
    parent_state = parent_dir / "acp-agent" / "recovered"
    child_state = child_dir / "acp-agent" / "recovered"
    parent_digests = home_digests(parent_state, credential_dests) if parent_state.is_dir() else {}
    child_digests = home_digests(child_state, credential_dests) if child_state.is_dir() else {}
    parent_files = sorted(parent_digests)
    child_files = sorted(child_digests)
    shared = {
        relative: digest
        for relative, digest in parent_digests.items()
        if child_digests.get(relative) == digest
    }
    print(f"  parent recovered 存在: {parent_state.is_dir()}；文件数: {len(parent_files)}")
    print(f"  child  recovered 存在: {child_state.is_dir()}；文件数: {len(child_files)}")
    print(f"  内容一致的继承文件: {len(shared)} 个")
    for relative in list(sorted(shared))[:10]:
        print(f"    {relative}")
    print(f"  child recovered 文件（前 20）: {child_files[:20]}")
    session_marked = [
        relative
        for relative in child_files
        if parent_session and str(parent_session) in relative
    ]
    print(f"  含父 sessionId 的 child 状态文件: {len(session_marked)} 个（会话跨沙箱存续的直接观测）")
    for relative in session_marked[:5]:
        print(f"    {relative}")

    parent_report = results["parent"]["report"].decode("utf-8", "replace")
    child_report = results["child"]["report"].decode("utf-8", "replace")
    child_text = assistant_text(child_events)
    print()
    print("连续性判据")
    print(f"  代号出现在 child 助手正文: {code in child_text}")
    print(f"  代号出现在 child report: {code in child_report}")
    print(f"  代号出现在 parent report（应为 False）: {code in parent_report}")
    print(f"  代号出现在 child 题面（应为 False）: {code in str(child.get('prompt'))}")
    print(f"  child 助手正文尾部: {child_text[-400:]!r}")

    print()
    print("终态与清理事实（runs.db 记录）")
    for label, meta in (("parent", parent_meta), ("child", child_meta)):
        print(
            f"  [{label}] status={meta.get('status')} modelStopReason={meta.get('model_stop_reason')} "
            f"reportStatus={meta.get('report_status')} session_id={meta.get('session_id')}"
        )
        print(f"    cleanup={json.dumps(meta.get('cleanup'), ensure_ascii=False)}")
    print(f"  parent artifactFailures={json.dumps(terminal_failures(parent_events), ensure_ascii=False)}")
    print(f"  child  artifactFailures={json.dumps(terminal_failures(child_events), ensure_ascii=False)}")

    print()
    print("沙箱终止对账（只按持久化 sandbox_id，SDK 查/杀同一 id）")
    absent: dict[str, bool] = {}
    for label, meta in (("parent", parent_meta), ("child", child_meta)):
        sandbox_id = meta.get("sandbox_id") or ""
        gone, detail = confirm_sandbox_absent(sandbox_id, api_key=api_key)
        absent[label] = gone
        print(f"  [{label}] sandbox_id={sandbox_id} confirmed_absent={gone} detail={detail}")

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
        and bool((parent_meta.get("cleanup") or {}).get("released"))
        and parent_files
        and parent_ready_final.get("stateRootRecovered") is True
        and not terminal_failures(parent_events)
    )
    child_ok = (
        child.get("status") == "finished"
        and results["child"]["reportStatus"] == 200
        and bool(results["child"]["report"])
        and bool((child_meta.get("cleanup") or {}).get("released"))
        and child.get("sessionId") == parent.get("sessionId")
        and child_ready.get("resumed") is True
        and bool(resumed_notices)
        and child_ready_final.get("stateRootRecovered") is True
        and child_meta.get("sandbox_id") != parent_meta.get("sandbox_id")
        and bool(child_files)
        and bool(session_marked)
        and code in child_report
        and code in child_text
        and code not in parent_report
        and code not in str(child.get("prompt"))
        and absent.get("parent") is True
        and absent.get("child") is True
        and not terminal_failures(child_events)
        and exit_code is not None
    )
    ok = parent_ok and child_ok
    print()
    print(f"  parent 通过: {parent_ok}；child 通过: {child_ok}")
    print(
        f"[{'PASS' if ok else 'FAIL'}] ACP E2B 真实续接冒烟"
        "（新沙箱/同 sessionId/代号回环/状态回收/按 id 清理）"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
