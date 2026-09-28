#!/usr/bin/env python3
"""ACP E2B 回收失败 → 追问准入拒绝实测（GLINK-ACP-15 / M9）。

M8 的准入规则：e2b 面追问只在「父 run 同 harness + 已结束 + 最新轮次 + 有
sessionId + 有 resume 能力位 + 父状态根回收成功（``acp-agent/recovered`` 存在且是
目录）」时受理。回收失败是**整份拒绝**（不发布 recovered 根），因此追问必须在起沙箱
**之前**被拒（§14.1/§14.3）。

本探针用真实宿主 + 真实 E2B 沙箱 + 真实模型跑一次父 run，并在它的回合结束前把
状态根撑过单文件上限（``docker``/SDK 之外用 SDK 连接同一 sandbox 写合成大文件），
让**真实 finalize 里的回收真的失败**。判据：

* 父 run 终态 ``finished``，末条 ``session_ready.stateRootRecovered=false``，
  终态帧 ``artifactFailures`` 记着上限（拒绝事实入产品事件流）；
* 宿主 run 目录里既没有 ``acp-agent/recovered`` 也没有 ``.recovered.staging``
  （不留半份）；父 ``sessionId`` 与 resume 能力位确实记录在案（否则追问被拒是因为
  别的原因，不构成对本规则的验证）；
* 追问 ``POST /api/runs``（``parentRunId``）→ HTTP 409 ``session_unavailable``，
  且**没有新建 run、没有新建沙箱**（前后 ``GET /api/runs`` 与 ``Sandbox.list()`` 对比）；
* 父沙箱按持久化 id 终止（``confirm_sandbox_absent``），宿主只按持有 PID 收尾。

它**消耗一次真实模型调用**与一个真实沙箱。``E2B_API_KEY`` 从 ``--env-file``（或
进程环境）读取，不打印、不落证据；凭据只读复制进每 run HOME。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "host"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "spikes"))
from acp_pack import PROBE_PACK_NAME, write_probe_pack  # noqa: E402

from pi_glink.kernel.acp_e2b_env import SANDBOX_ACP_HOME  # noqa: E402
from pi_glink.kernel.acp_env import INHERIT_MAX_FILE_BYTES  # noqa: E402
from pi_glink.kernel.acp_execution import acp_resume_method  # noqa: E402
from pi_glink.kernel.e2b_env import confirm_sandbox_absent  # noqa: E402

TERMINAL = {"finished", "failed", "cancelled"}

#: A prompt that keeps the turn alive long enough for the synthetic bloat to land
#: before finalize (the injection happens as soon as ``session_ready`` exists).
PARENT_PROMPT = (
    "请做一次简短研究：用 300 到 400 字、3 条要点说明 ACP 这类客户端-代理协议对宿主"
    "接入多种 agent 的价值，并附 2 条可核验的来源链接（需要时可使用 web_search/"
    "web_fetch）；把结果写入工作区 report.md，回复里用一句话总结即可。"
)


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


def sanitize(text: str, workdir: Path) -> str:
    return text.replace(str(workdir), "$TMP/" + workdir.name).replace(str(Path.home()), "~")


def read_meta(run_dir: Path) -> dict:
    path = run_dir / "meta.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def read_events(run_dir: Path) -> list[dict]:
    path = run_dir / "events.jsonl"
    if not path.is_file():
        return []
    events: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def payloads(events: list[dict], event_type: str) -> list[dict]:
    return [event["payload"] for event in events if event.get("type") == event_type]


def running_sandbox_ids(api_key: str) -> tuple[list[str], str]:
    """The SDK's own list of running sandboxes (id only, best effort).

    ``Sandbox.list`` returns a paginator (``has_next``/``next_items()``), not an
    iterable: calling ``list()`` on it raises. Both sides of the follow-up
    attempt are measured with the same helper, so a missing page is visible as
    its own failure instead of silently comparing two empty lists.
    """
    try:
        from e2b import Sandbox

        paginator = Sandbox.list(limit=50, api_key=api_key)
        items = []
        while paginator.has_next:
            items.extend(paginator.next_items())
    except Exception as exc:  # noqa: BLE001 - recorded as a fact
        return [], f"Sandbox.list 失败: {type(exc).__name__}: {exc}"
    ids: list[str] = []
    for item in items:
        sandbox_id = getattr(item, "sandbox_id", None) or getattr(item, "sandboxID", None)
        if sandbox_id:
            ids.append(str(sandbox_id))
    return ids, ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ACP E2B 回收失败后追问准入拒绝实测（1 次真实模型调用）"
    )
    parser.add_argument("--agent", default="dsh --profile acp")
    parser.add_argument("--template", default="glink-acp-worker")
    parser.add_argument(
        "--credential", default="~/.dsh/.credentials.yaml=.dsh/.credentials.yaml"
    )
    parser.add_argument("--env", action="append", default=[], help="NAME=value，可重复")
    parser.add_argument("--env-file", default="")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--bloat-bytes",
        type=int,
        default=INHERIT_MAX_FILE_BYTES + 1024 * 1024,
        help="合成状态文件大小（默认超过单文件上限 1 MiB）",
    )
    parser.add_argument("--workdir", default="")
    parser.add_argument("--log", default="")
    args = parser.parse_args(argv)

    api_key = resolve_api_key(args)
    if not api_key:
        print("缺少 E2B_API_KEY（进程环境或 --env-file）", file=sys.stderr)
        return 2
    if args.bloat_bytes <= INHERIT_MAX_FILE_BYTES:
        print(
            f"--bloat-bytes 必须大于单文件上限 {INHERIT_MAX_FILE_BYTES}",
            file=sys.stderr,
        )
        return 2

    workdir = (
        Path(args.workdir)
        if args.workdir
        else Path(tempfile.mkdtemp(prefix="glink-acp15-e2b-refusal-"))
    )
    workdir.mkdir(parents=True, exist_ok=True)
    os.chmod(workdir, 0o700)
    data_root = workdir / "data"
    log_path = Path(args.log) if args.log else workdir / "host.log"
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    packs = write_probe_pack(workdir / "probe", args.agent)
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT / "host"),
            "PI_GLINK_HARNESS": PROBE_PACK_NAME,
            "PI_GLINK_HARNESSES_DIR": str(packs),
            "LOCAL_WEB_EXECUTOR": "e2b",
            "LOCAL_WEB_E2B_ACP_TEMPLATE": args.template,
            "LOCAL_WEB_DATA_ROOT": str(data_root),
            "LOCAL_WEB_TASK_TIMEOUT_SECONDS": str(args.timeout),
            "PI_GLINK_RESOURCE_RECONCILE_SECONDS": "0",
            "E2B_API_KEY": api_key,
            "PI_GLINK_ACP_CREDENTIAL_FILES": args.credential,
        }
    )
    if args.env:
        env["PI_GLINK_ACP_ENV"] = ";".join(args.env)

    print("ACP M9 E2B 回收失败 → 追问准入拒绝实测")
    print("=" * 48)
    print(f"日期: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"宿主命令: python -m pi_glink.app --port {port}（harness=acp, executor=e2b）")
    print(f"agent 命令: {args.agent}")
    print(f"模板: {args.template}（dev 模板，未重建）")
    print(f"凭据目标: {args.credential.split('=', 1)[-1]}（只读复制进每 run HOME）")
    print(f"数据根: {sanitize(str(data_root), workdir)}")
    print(f"任务超时: {args.timeout}s")
    print(
        f"合成超限文件: {SANDBOX_ACP_HOME}/bloat.bin = {args.bloat_bytes} B"
        f"（单文件上限 {INHERIT_MAX_FILE_BYTES} B；在 run 回合内用 SDK 直连同一沙箱写入）"
    )
    print()

    exit_code: int | None = None
    run_id = ""
    sandbox_id = ""
    injection: dict = {}
    refusal: dict = {}
    sandbox_ids_before: list[str] = []
    sandbox_ids_after: list[str] = []
    run_list_before: list[str] = []
    run_list_after: list[str] = []
    with log_path.open("w", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            [sys.executable, "-m", "pi_glink.app", "--port", str(port)],
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

            created = httpx.post(f"{base}/api/runs", json={"prompt": PARENT_PROMPT}, timeout=30)
            print(f"[父 run] POST /api/runs -> HTTP {created.status_code}")
            if created.status_code != 201:
                print(created.text[:600])
                return 1
            run_id = created.json()["run"]["runId"]
            run_dir = data_root / "runs" / run_id
            print(f"[父 run] runId: {run_id}")

            # --- injection: wait for the sandbox to exist and the session to be
            # ready (upload finished, agent started), then write one over-limit
            # file into the sandbox state root through the real SDK.
            from e2b import Sandbox

            inject_deadline = time.monotonic() + args.timeout
            meta: dict = {}
            session_ready = False
            while time.monotonic() < inject_deadline:
                meta = read_meta(run_dir)
                sandbox_id = str(meta.get("sandbox_id") or "")
                events = read_events(run_dir)
                session_ready = bool(payloads(events, "session_ready"))
                if sandbox_id and session_ready:
                    break
                if meta.get("status") in TERMINAL:
                    break
                time.sleep(0.3)
            injection["sandboxId"] = sandbox_id
            injection["sessionReadySeen"] = session_ready
            injection["runStatusAtInject"] = meta.get("status")
            print(
                f"[注入] 等待 sandbox_id/session_ready: sandbox_id={sandbox_id} "
                f"session_ready={session_ready} status={meta.get('status')}"
            )
            if sandbox_id and session_ready and meta.get("status") not in TERMINAL:
                try:
                    box = Sandbox.connect(sandbox_id, api_key=api_key)
                    command = (
                        f"head -c {args.bloat_bytes} /dev/zero > {SANDBOX_ACP_HOME}/bloat.bin"
                    )
                    started = time.monotonic()
                    result = box.commands.run(command, timeout=300)
                    injection["command"] = command
                    injection["exitCode"] = getattr(result, "exit_code", None)
                    injection["seconds"] = round(time.monotonic() - started, 2)
                    check = box.commands.run(
                        f"node -e \"console.log(require('fs').statSync('{SANDBOX_ACP_HOME}/bloat.bin').size)\"",
                        timeout=60,
                    )
                    text = str(getattr(check, "stdout", "") or "").strip()
                    injection["bloatSizeInSandbox"] = int(text) if text.isdigit() else text
                except Exception as exc:  # noqa: BLE001 - the failure is the evidence
                    injection["error"] = f"{type(exc).__name__}: {exc}"
                print(f"[注入] {json.dumps(injection, ensure_ascii=False)}")
            else:
                injection["error"] = "注入前 run 已终态（沙箱未观察到或回合已结束）"
                print(f"[注入] {injection['error']}")

            # --- wait for the parent to finish (its finalize runs the real recovery).
            deadline = time.monotonic() + args.timeout + 180
            meta = {}
            while time.monotonic() < deadline:
                meta = read_meta(run_dir)
                if meta.get("status") in TERMINAL:
                    break
                time.sleep(1)
            print(
                f"[父 run] 终态: {meta.get('status')} report={meta.get('report_status')} "
                f"session_id={meta.get('session_id')}"
            )

            # --- admission attempt for a follow-up against the failed parent.
            sandbox_ids_before, before_error = running_sandbox_ids(api_key)
            listing = httpx.get(f"{base}/api/runs", timeout=30)
            run_list_before = sorted(
                str(item.get("runId")) for item in (listing.json().get("runs") or [])
            )
            print(f"[追问准入] 拒绝前：运行中沙箱 {sandbox_ids_before or '无'}；run 数 {len(run_list_before)}")
            followup = httpx.post(
                f"{base}/api/runs",
                json={"prompt": "连续性追问：上一个会话里我要求你记住的那个代号是什么？", "parentRunId": run_id},
                timeout=30,
            )
            refusal = {
                "status": followup.status_code,
                "body": followup.text[:400],
            }
            print(f"[追问准入] POST /api/runs（parentRunId={run_id}）-> HTTP {followup.status_code}")
            print(f"[追问准入] 响应: {followup.text[:400]}")
            sandbox_ids_after, after_error = running_sandbox_ids(api_key)
            listing = httpx.get(f"{base}/api/runs", timeout=30)
            run_list_after = sorted(
                str(item.get("runId")) for item in (listing.json().get("runs") or [])
            )
            print(f"[追问准入] 拒绝后：运行中沙箱 {sandbox_ids_after or '无'}；run 数 {len(run_list_after)}")
            if before_error or after_error:
                print(f"[追问准入] Sandbox.list 说明: {before_error or after_error}")
            sandbox_list_ok = not (before_error or after_error)
        finally:
            process.terminate()
            try:
                exit_code = process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                exit_code = process.wait(timeout=10)
            print(f"宿主按 PID {process.pid} 终止: exit={exit_code}")

    if not run_id:
        print("[FAIL] 没有拿到 runId")
        return 1
    run_dir = data_root / "runs" / run_id
    meta = read_meta(run_dir)
    events = read_events(run_dir)
    terminal_payloads = payloads(events, "run_finished") + payloads(events, "run_failed")
    artifact_failures = (
        [str(item) for item in terminal_payloads[-1].get("artifactFailures") or []]
        if terminal_payloads
        else ["缺少终态帧"]
    )
    ready = payloads(events, "session_ready")
    first_ready = ready[0] if ready else {}
    last_ready = ready[-1] if ready else {}
    capabilities = first_ready.get("agentCapabilities") or {}
    recorded_resume_method = acp_resume_method(capabilities)
    acp_dir = run_dir / "acp-agent"

    print()
    print("父 run 回收事实")
    print(f"  runId: {run_id}  status={meta.get('status')} modelStopReason={meta.get('model_stop_reason')}")
    print(f"  sessionId: {meta.get('session_id')}")
    print(f"  session_ready 条数: {len(ready)}")
    print(
        "  首帧: "
        + json.dumps(
            {
                key: first_ready.get(key)
                for key in ("harness", "sessionId", "resumed", "stateRootRecovered", "sandboxId")
            },
            ensure_ascii=False,
        )
    )
    print(
        "  末帧: "
        + json.dumps(
            {
                key: last_ready.get(key)
                for key in ("resumed", "stateRootRecovered", "sandboxId")
            },
            ensure_ascii=False,
        )
    )
    print(f"  记录的能力位 sessionCapabilities: {json.dumps(capabilities.get('sessionCapabilities'), ensure_ascii=False)}")
    print(f"  记录的能力位判定（生产函数 acp_resume_method）: {recorded_resume_method}")
    print(f"  loadSession: {capabilities.get('loadSession')}")
    print(f"  终态帧 artifactFailures: {json.dumps(artifact_failures, ensure_ascii=False)}")
    print(f"  宿主 acp-agent 目录存在: {acp_dir.is_dir()}；条目: {sorted(item.name for item in acp_dir.iterdir()) if acp_dir.is_dir() else []}")
    print(f"  acp-agent/recovered 存在: {(acp_dir / 'recovered').exists()}")
    print(f"  acp-agent/.recovered.staging 存在: {(acp_dir / '.recovered.staging').exists()}")
    print(
        f"  cleanup: {json.dumps(meta.get('cleanup'), ensure_ascii=False)}"
    )

    print()
    print("追问准入事实")
    print(f"  POST 返回: HTTP {refusal.get('status')} body={refusal.get('body')}")
    print(f"  拒绝前运行中沙箱: {sandbox_ids_before or '无'}")
    print(f"  拒绝后运行中沙箱: {sandbox_ids_after or '无'}")
    print(f"  新建沙箱: {sorted(set(sandbox_ids_after) - set(sandbox_ids_before)) or '无'}")
    print(f"  拒绝前 run 列表: {run_list_before}")
    print(f"  拒绝后 run 列表: {run_list_after}")
    print(f"  新建 run: {sorted(set(run_list_after) - set(run_list_before)) or '无'}")

    absent, detail = confirm_sandbox_absent(str(meta.get("sandbox_id") or ""), api_key=api_key)
    print()
    print("沙箱按 id 对账")
    print(f"  sandbox_id={meta.get('sandbox_id')} confirmed_absent={absent} detail={detail}")

    print()
    print("宿主日志尾部")
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-12:]:
        print(f"  {sanitize(line, workdir)}")

    injected = (
        injection.get("sessionReadySeen") is True
        and injection.get("exitCode") == 0
        and injection.get("bloatSizeInSandbox") == args.bloat_bytes
    )
    parent_ok = (
        meta.get("status") == "finished"
        and bool(meta.get("session_id"))
        and recorded_resume_method == "session/resume"
        and last_ready.get("stateRootRecovered") is False
        and any("上限" in item for item in artifact_failures)
        and not (acp_dir / "recovered").exists()
        and not (acp_dir / ".recovered.staging").exists()
        and absent is True
        and injected
    )
    refusal_ok = (
        refusal.get("status") == 409
        and "session_unavailable" in str(refusal.get("body"))
        and run_list_after == run_list_before
        and not (set(sandbox_ids_after) - set(sandbox_ids_before))
        and sandbox_list_ok
    )
    ok = parent_ok and refusal_ok and exit_code is not None
    print()
    print(f"  注入生效: {injected}；父 run 判据: {parent_ok}；追问拒绝判据: {refusal_ok}")
    print(
        f"[{'PASS' if ok else 'FAIL'}] E2B 回收失败 → 追问准入拒绝"
        "（真实回收失败 / 末帧 stateRootRecovered=false / 409 session_unavailable / 不新建 run 与沙箱 / 按 id 终止）"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
