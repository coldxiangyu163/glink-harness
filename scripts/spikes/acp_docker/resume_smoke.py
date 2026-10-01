#!/usr/bin/env python3
"""ACP docker 面真实续接冒烟（GLINK-ACP-15 / M9）。

M7 的 live 续接只到 inproc（§10.5），M8 补了 e2b 面（§14.6）；docker 面一直
只有合成/无模型握手证据（§5、§10.7）。本探针在 docker 面跑一次真实「首轮 +
追问」：同一 agent 数据根、每 run 一个容器，追问 ``parentRunId`` → 内核走
``session/resume``。判据：

* 追问是**新容器**（容器名不同、容器 Id 不同），且两轮容器都按持久化名字对账
  ``confirm_absent``，``docker ps -a`` 无同名残留；
* 追问 run 的 ``sessionId`` 与首轮相同、``session_ready`` 带 ``resumed`` 与
  ``resumeMethod=session/resume``、事件流有 ``acp_session_resumed``；
* 追问容器运行期用 ``docker inspect`` 记录它的挂载：``acp-agent/home`` 挂到
  ``/home/acp``（rw）、凭据文件嵌套只读、``HOME=/home/acp``；同时记录该挂载
  源目录此刻已有父会话状态文件（继承状态根确实经挂载进了新容器）；
* 追问 run 的助手正文与 report.md 都出现首轮约定的代号，而首轮 report.md 与
  追问题面都不含该代号（新会话不可能凭工作区或题面答出）；
* 追问 HOME 与父 HOME 有内容一致的状态文件，其中含父 ``sessionId`` 的会话
  文件（container → 宿主挂载目录 → 下一容器这条链路的直接观测）；
* 清理只按已知身份：宿主进程用本脚本持有的 PID（``terminate``），运行期采样的
  后代 PID 只按 PID 记录并只对已知 PID 发 ``TERM``；容器只按**精确名字**
  （``docker inspect <name>`` / ``docker rm -f <name>``）对账删除——没有
  ``pkill``，没有名字前缀/通配删除。

它**会消耗真实模型调用**（首轮 + 追问），只在获得授权时使用。凭据源只读复制；
本脚本不打印任何凭据值。

Colima 只把 home 树共享给 VM，所以数据根必须在 ``$HOME`` 下（默认
``$HOME/glink-acp15-docker-resume-*``）；``--data-root`` 指到别处时脚本只告警。
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

from glink_harness.kernel.docker_env import (  # noqa: E402
    CONTAINER_HOME,
    CONTAINER_WORKSPACE,
    confirm_absent,
    container_name_for,
)

TERMINAL = {"finished", "failed", "cancelled"}
#: Ceiling for hashing one inherited state file (the state root is bounded; the
#: cap keeps the evidence walk from reading a pathological file whole).
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


def process_table() -> dict[int, tuple[int, str]]:
    output = subprocess.run(
        ["ps", "-o", "pid,ppid,command", "-ax"], capture_output=True, text=True
    ).stdout
    table: dict[int, tuple[int, str]] = {}
    for line in output.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        table[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) > 2 else "")
    return table


def watch_descendants(root_pid: int, seen: dict[int, str], stop: threading.Event) -> None:
    """Sample the host's descendants by PID (never by pattern)."""
    while not stop.is_set():
        table = process_table()
        for pid, (ppid, command) in table.items():
            if pid in seen:
                continue
            ancestor = ppid
            depth = 0
            while ancestor > 1 and depth < 12:
                if ancestor == root_pid:
                    seen[pid] = command
                    break
                ancestor = table.get(ancestor, (1, ""))[0]
                depth += 1
        stop.wait(0.5)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def docker_inspect(container_name: str) -> tuple[dict | None, str]:
    """One container's live facts by its exact name (no pattern, no listing).

    Returns ``(facts, failure)``. The environment is recorded by *name only*: the
    host passes ``GLINK_ACP_ENV`` by name with the values on the docker CLI's
    own environment, and this probe must never copy a value into evidence. The
    names are read by a separate template that emits one name per line — a Go
    template array built with ``range`` leaves a trailing comma and is then not
    valid JSON (the first live attempt of this probe lost every snapshot to
    exactly that, see the evidence file's addendum).
    """
    template = (
        '{"id":{{json .Id}},"image":{{json .Image}},'
        '"status":{{json .State.Status}},"cwd":{{json .Config.WorkingDir}},'
        '"mounts":{{json .Mounts}}}'
    )
    try:
        result = subprocess.run(
            ["docker", "inspect", "--type", "container", "--format", template, container_name],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if result.returncode != 0:
        return None, f"docker inspect exit {result.returncode}: {(result.stderr or '').strip()[-120:]}"
    try:
        facts = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        return None, f"docker inspect 输出不是有效 JSON: {exc}"
    try:
        names = subprocess.run(
            [
                "docker", "inspect", "--type", "container",
                "--format", '{{range .Config.Env}}{{println (index (split . "=") 0)}}{{end}}',
                container_name,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        facts["envNames"] = [name for name in names.stdout.splitlines() if name]
    except (OSError, subprocess.TimeoutExpired):
        facts["envNames"] = []
    return facts, ""


def container_absent(container_name: str) -> tuple[bool, str, str]:
    """Confirm one exact container name is gone; returns the CLI's own listing."""
    gone, detail = confirm_absent(container_name)
    listing = subprocess.run(
        [
            "docker", "ps", "-a",
            "--filter", f"name=^{container_name}$",
            "--format", "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout.strip()
    return gone, detail, listing


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
            {
                "at": time.time(),
                "event": "sse_error",
                "payload": {"error": f"{type(exc).__name__}: {exc}"},
            }
        )


def run_once(
    base: str,
    prompt: str,
    *,
    parent_run_id: str | None,
    timeout: int,
    label: str,
    data_root: Path,
) -> dict:
    """Create one run over the HTTP API; sample its container while it lives."""
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
    container_name = container_name_for(run_id)
    home = data_root / "runs" / run_id / "acp-agent" / "home"
    print(f"[{label}] runId: {run_id}  容器名（按 runId 预测）: {container_name}")

    stop = threading.Event()
    thread = threading.Thread(
        target=sse_collector, args=(base, run_id, frames, stop), daemon=True
    )
    thread.start()

    inspector_stop = threading.Event()
    inspect_snapshot: dict = {}
    inspect_failures: list[str] = []

    def inspect_loop() -> None:
        while not inspector_stop.is_set():
            if not inspect_snapshot:
                facts, failure = docker_inspect(container_name)
                if failure:
                    if failure not in inspect_failures:
                        inspect_failures.append(failure)
                if facts:
                    # Mount sources are host paths inside the data root; they are
                    # sanitized when printed, never written raw.
                    facts["home_file_count"] = (
                        sum(1 for item in home.rglob("*") if item.is_file())
                        if home.is_dir()
                        else 0
                    )
                    inspect_snapshot.update(facts)
            inspector_stop.wait(0.5)

    inspector = threading.Thread(target=inspect_loop, daemon=True)
    inspector.start()

    deadline = time.monotonic() + timeout + 180
    current = run
    while time.monotonic() < deadline:
        current = httpx.get(f"{base}/api/runs/{run_id}", timeout=10).json()["run"]
        if current.get("status") in TERMINAL:
            break
        time.sleep(1)
    stop.set()
    thread.join(timeout=10)
    inspector_stop.set()
    inspector.join(timeout=5)
    print(
        f"[{label}] 终态: {current.get('status')} duration={current.get('durationSeconds')}s "
        f"events={current.get('eventCount')} sessionId={current.get('sessionId')} "
        f"error={current.get('error')!r}"
    )
    if not inspect_snapshot and inspect_failures:
        print(f"[{label}] docker inspect 未采到快照，首次原因: {inspect_failures[0]}")
    if inspect_snapshot:
        mounts = inspect_snapshot.get("mounts") or []
        print(f"[{label}] 容器运行期 docker inspect（名字精确匹配）:")
        print(f"    Id={str(inspect_snapshot.get('id'))[:16]}… image={str(inspect_snapshot.get('image'))[:21]}…")
        print(f"    State.Status={inspect_snapshot.get('status')} Config.WorkingDir={inspect_snapshot.get('cwd')}")
        print(f"    env 名（只记名字，不记值）: {inspect_snapshot.get('envNames')}")
        for mount in mounts:
            print(
                "    mount: {src} -> {dst} mode={mode!r} rw={rw}".format(
                    src=sanitize(str(mount.get("Source")), data_root),
                    dst=mount.get("Destination"),
                    mode=mount.get("Mode"),
                    rw=mount.get("RW"),
                )
            )
        print(
            f"    HOME 挂载源此刻的文件数: {inspect_snapshot.get('home_file_count')}"
            f"（HOME={CONTAINER_HOME}）"
        )
    else:
        print(f"[{label}] 容器运行期未采到 docker inspect 快照（容器未观察到或极短命）")
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
        "containerName": container_name,
        "containerInspect": inspect_snapshot,
        "inspectFailures": inspect_failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ACP docker 面真实续接冒烟（首轮 + 追问）")
    parser.add_argument("--label", default="docker-dsh")
    parser.add_argument("--agent", default="dsh --profile acp")
    parser.add_argument("--image", default="glink-harness-acp-worker:latest")
    parser.add_argument(
        "--credential",
        action="append",
        default=[],
        help="source=dest，可重复；默认 ~/.dsh/.credentials.yaml=.dsh/.credentials.yaml",
    )
    parser.add_argument("--env", action="append", default=[], help="NAME=value，可重复")
    parser.add_argument("--session-config", default="")
    parser.add_argument("--permission-allow", default="")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--data-root", default="")
    parser.add_argument("--log", default="")
    args = parser.parse_args(argv)

    credentials = list(args.credential) or ["~/.dsh/.credentials.yaml=.dsh/.credentials.yaml"]
    credential_dests = {
        chunk.split("=", 1)[1].strip() for chunk in credentials if "=" in chunk
    }
    data_root = (
        Path(args.data_root).expanduser()
        if args.data_root
        else Path(tempfile.mkdtemp(prefix="glink-acp15-docker-resume-", dir=str(Path.home())))
    )
    data_root.mkdir(parents=True, exist_ok=True)
    os.chmod(data_root, 0o700)
    if Path.home() not in data_root.parents and data_root != Path.home():
        print(
            f"[warn] docker 面数据根 {data_root} 不在 $HOME 下：Colima 只共享 home 树，"
            "挂载可能静默变空（见集成笔记 §5.4）。"
        )
    log_path = Path(args.log) if args.log else data_root / "host.log"
    port = free_port()
    base = f"http://127.0.0.1:{port}"

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
            "GLINK_HARNESS": PROBE_PACK_NAME,
            "GLINK_HARNESSES_DIR": str(packs),
            "LOCAL_WEB_EXECUTOR": "docker",
            "LOCAL_WEB_ACP_WORKER_IMAGE": args.image,
            "LOCAL_WEB_DATA_ROOT": str(data_root),
            "LOCAL_WEB_TASK_TIMEOUT_SECONDS": str(args.timeout),
            "GLINK_RESOURCE_RECONCILE_SECONDS": "0",
            "GLINK_ACP_CREDENTIAL_FILES": ";".join(credentials),
        }
    )
    if args.env:
        env["GLINK_ACP_ENV"] = ";".join(args.env)

    code = codename()
    first = first_prompt(code)
    followup = followup_prompt()
    assert code in first and code not in followup

    image_id = subprocess.run(
        ["docker", "image", "inspect", args.image, "--format", "{{.Id}}"],
        capture_output=True,
        text=True,
    ).stdout.strip()

    print(f"ACP M9 真实续接冒烟（docker / {args.label}）")
    print("=" * 52)
    print(f"日期: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"宿主命令: python -m glink_harness.app --port {port}（harness=acp, executor=docker）")
    print(f"agent 命令: {args.agent}")
    print(f"镜像: {args.image}（{image_id[:19]}…）")
    print(f"命令级 env: {', '.join(item.split('=', 1)[0] for item in args.env) or '（无）'}")
    print(f"凭据目标: {', '.join(sorted(credential_dests))}（只读复制进每 run HOME，容器内 :ro 嵌套挂载）")
    print(f"会话配置: {args.session_config or '（无）'}")
    print(f"权限白名单: {args.permission_allow or '（空 = fail-closed）'}")
    print(f"数据根: {sanitize(str(data_root), data_root)}")
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
        descendants_seen: dict[int, str] = {}
        watcher_stop = threading.Event()
        watcher = threading.Thread(
            target=watch_descendants,
            args=(process.pid, descendants_seen, watcher_stop),
            daemon=True,
        )
        watcher.start()
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
                base, first, parent_run_id=None, timeout=args.timeout, label="首轮", data_root=data_root
            )
            if results["parent"].get("created") != 201:
                return 1
            results["child"] = run_once(
                base,
                followup,
                parent_run_id=results["parent"]["run"]["runId"],
                timeout=args.timeout,
                label="追问",
                data_root=data_root,
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
    leftovers = {pid: command for pid, command in descendants_seen.items() if alive(pid)}
    residual: list[int] = []
    print()
    print(f"run 期间采样的宿主后代进程: {len(descendants_seen)} 个（只按 PID 记录）")
    for pid, command in list(descendants_seen.items())[:10]:
        state = "leftover" if pid in leftovers else "exited"
        print(f"  {pid} [{state}] {command[:150]}")
    if leftovers:
        print("仍存活的后代 PID（按已知 PID 发 TERM，不用模式匹配）")
        for pid, command in leftovers.items():
            print(f"  TERM {pid} {command[:110]}")
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and any(alive(pid) for pid in leftovers):
            time.sleep(0.5)
        residual = [pid for pid in leftovers if alive(pid)]
        print(f"  清理后仍存活: {residual or '无'}")

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
    print("跨容器续接事实")
    print(f"  parent 容器名: {results['parent']['containerName']}")
    print(f"  child  容器名: {results['child']['containerName']}")
    print(f"  容器名不同（即新容器）: {results['parent']['containerName'] != results['child']['containerName']}")
    parent_id = (results["parent"].get("containerInspect") or {}).get("id")
    child_id = (results["child"].get("containerInspect") or {}).get("id")
    print(f"  容器 Id 不同: {bool(parent_id) and bool(child_id) and parent_id != child_id}")
    ready = payloads(child_events, "session_ready")
    child_ready = ready[0] if ready else {}
    parent_ready = payloads(parent_events, "session_ready")
    parent_session = parent_ready[0].get("sessionId") if parent_ready else None
    print(
        "  child session_ready: "
        + sanitize(
            json.dumps(
                {
                    key: child_ready.get(key)
                    for key in ("sessionId", "resumed", "resumeMethod", "cwd", "harness")
                },
                ensure_ascii=False,
            ),
            data_root,
        )
    )
    print(f"  parent sessionId: {parent_session}")
    print(f"  child.sessionId == parent.sessionId: {child.get('sessionId') == parent.get('sessionId')}")
    resumed_notices = [
        payload
        for payload in payloads(child_events, "kernel_status")
        if payload.get("message") == "acp_session_resumed"
    ]
    print(f"  child acp_session_resumed: {json.dumps(resumed_notices, ensure_ascii=False)[:400]}")
    print(
        "  工作区: parent=%s · child=%s（ACP 会话 cwd 跨轮稳定，应相同）"
        % (
            sanitize(str(parent_meta.get("workspace")), data_root),
            sanitize(str(child_meta.get("workspace")), data_root),
        )
    )
    print(f"  session_ready.cwd == 容器工作区 {CONTAINER_WORKSPACE}: {child_ready.get('cwd') == CONTAINER_WORKSPACE}")

    print()
    print("状态继承（父/子 run 各自宿主可见 HOME；凭据目标已排除）")
    parent_digests = home_digests(parent_dir / "acp-agent" / "home", credential_dests)
    child_digests = home_digests(child_dir / "acp-agent" / "home", credential_dests)
    shared = {
        relative: digest
        for relative, digest in parent_digests.items()
        if child_digests.get(relative) == digest
    }
    session_marked = [
        relative for relative in sorted(child_digests) if parent_session and parent_session in relative
    ]
    print(f"  parent HOME 文件数: {len(parent_digests)}；child HOME 文件数: {len(child_digests)}")
    print(f"  内容一致的继承文件: {len(shared)} 个")
    for relative in sorted(shared)[:10]:
        print(f"    {relative}")
    print(f"  含父 sessionId 的 child 状态文件: {len(session_marked)} 个")
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
    print(f"  代号出现在 child 题面: {code in str(child.get('prompt'))}（应为 False）")
    print(f"  child 助手正文尾部: {child_text[-400:]!r}")

    print()
    print("终态与清理事实（runs.db 记录）")
    for label, meta in (("parent", parent_meta), ("child", child_meta)):
        print(
            f"  [{label}] status={meta.get('status')} modelStopReason={meta.get('model_stop_reason')} "
            f"reportStatus={meta.get('report_status')} session_id={meta.get('session_id')}"
        )
        print(f"    cleanup={json.dumps(meta.get('cleanup'), ensure_ascii=False)}")
    print(f"  parent report_status / child report_status: {results['parent']['reportStatus']} / {results['child']['reportStatus']}")

    print()
    print("容器按名对账（精确名字，无通配/前缀删除；未使用 pkill）")
    absent: dict[str, bool] = {}
    for label in ("parent", "child"):
        name = results[label]["containerName"]
        gone, detail, listing = container_absent(name)
        absent[label] = gone
        print(f"  [{label}] {name}: confirmed_absent={gone} detail={detail} 同名容器列表={listing!r}")
    prefix_listing = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout.strip()
    glink_names = [name for name in prefix_listing.splitlines() if name.startswith("glink-harness-")]
    print(f"  docker ps -a 全部容器名: {prefix_listing!r}")
    print(f"  其中 glink-harness- 前缀容器: {glink_names or '无'}")

    print()
    print("child report.md（HTTP %s，%s bytes）" % (results["child"]["reportStatus"], len(results["child"]["report"])))
    print("-" * 60)
    print(sanitize(child_report, data_root))
    print("-" * 60)

    print()
    print("宿主日志尾部")
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-12:]:
        print(f"  {sanitize(line, data_root)}")

    parent_inspect = results["parent"].get("containerInspect") or {}
    child_inspect = results["child"].get("containerInspect") or {}

    def mount_facts(inspect: dict) -> tuple[bool, bool]:
        """``(home mounted rw at /home/acp, credential file mounted ro)``."""
        home_ok = any(
            mount.get("Destination") == CONTAINER_HOME and mount.get("RW") is True
            for mount in (inspect.get("mounts") or [])
        )
        credential_ok = any(
            str(mount.get("Destination", "")).startswith(f"{CONTAINER_HOME}/")
            and mount.get("RW") is False
            for mount in (inspect.get("mounts") or [])
        )
        return home_ok, credential_ok

    parent_home_mount, parent_credential_ro = mount_facts(parent_inspect)
    child_home_mount, child_credential_ro = mount_facts(child_inspect)
    parent_ok = (
        parent.get("status") == "finished"
        and results["parent"]["reportStatus"] == 200
        and bool(results["parent"]["report"])
        and bool((parent_meta.get("cleanup") or {}).get("released"))
        and absent.get("parent") is True
        and bool(parent_digests)
        and parent_home_mount
        and parent_credential_ro
    )
    child_ok = (
        child.get("status") == "finished"
        and results["child"]["reportStatus"] == 200
        and bool(results["child"]["report"])
        and bool((child_meta.get("cleanup") or {}).get("released"))
        and child.get("sessionId") == parent.get("sessionId")
        and child_ready.get("resumed") is True
        and child_ready.get("resumeMethod") == "session/resume"
        and bool(resumed_notices)
        and child_ready.get("cwd") == CONTAINER_WORKSPACE
        and child_meta.get("workspace") == parent_meta.get("workspace")
        and results["child"]["containerName"] != results["parent"]["containerName"]
        and bool(child_id) and bool(parent_id) and child_id != parent_id
        and child_home_mount
        and child_credential_ro
        and bool(shared)
        and bool(session_marked)
        and code in child_report
        and code in child_text
        and code not in parent_report
        and code not in str(child.get("prompt"))
        and absent.get("child") is True
        and exit_code is not None
        and not residual
    )
    ok = parent_ok and child_ok
    print()
    print(f"  parent 通过: {parent_ok}；child 通过: {child_ok}")
    print(
        f"[{'PASS' if ok else 'FAIL'}] ACP docker 真实续接冒烟"
        "（新容器/同 sessionId/代号回环/report 200/按名对账/只按已知 PID 与容器名清理）"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
