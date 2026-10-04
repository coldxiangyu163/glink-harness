#!/usr/bin/env python3
"""ACP E2B 状态根回收上限触顶实测（GLINK-ACP-15 / M9，**不消耗模型**）。

ACP-13 的 e2b 状态根回收是有界的（4096 文件 / 8 MiB 单文件 / 64 MiB 总量 /
深度 12），触顶即「整份拒绝、不留半份、终态帧 artifactFailures 记录」——该路径
此前只有合成覆盖（§14.1、§14.8）。本探针用**真实 E2B 沙箱 + 真实 SDK + 生产回收
函数**（``AcpSandboxTransfer.recover_state()``，与 run finalize 调用的是同一份）
实测触顶：

* 每个轴一个真实沙箱：``E2BSandbox.open()`` 后先 ``upload_home()``（本探针无凭据，只建状态
  根），再用沙箱内命令写合成状态文件（**不经过宿主上传通道**，所以 8 MiB 以上的
  单文件也能落在状态根里）；
* 调用生产的 ``recover_state()``，记录 verdict（ok/failures/文件数/字节数）；
* 判据（每个轴）：整份拒绝（ok=False 且 failures 指明对应上限）、不留半份
  （宿主 run 的 ``acp-agent/recovered`` 与 ``.recovered.staging`` 都不存在）、
  沙箱内状态源保持原样（拒绝是只读判断，不改写沙箱）、拒绝事实可观测
  （failures 文本 + 沙箱侧文件数/字节数）；
* 对照组（``control``）：两个小文件时回收成功并发布，证明失败是上限造成的而不是
  这条路径坏了；发布内容与沙箱内容逐字节一致；
* 结束时只按 ``sandbox_id`` 终止（``confirm_sandbox_absent``，SDK 单 id 操作），
  没有模式匹配、没有批量清理。

轴（``--axes``，逗号分隔）：
* ``control``：2 个小文件 → 期望成功；
* ``single``：1 个 9 MiB 文件（> 8 MiB 单文件上限）；
* ``total``：9 × 8 MiB = 72 MiB（每个文件都在单文件上限内，> 64 MiB 总量上限）；
* ``count``：4100 个空文件（> 4096 文件数上限）。该轴要在线读 4096 个文件才触发
  计数拒绝，耗时明显高于另外两个轴，默认不在一次运行里跑（见 evidence 的取舍说明）。

``E2B_API_KEY`` 从 ``--env-file``（或进程环境）读取，不打印、不落证据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "host"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "spikes"))
from acp_pack import PROBE_PACK_NAME, write_probe_pack  # noqa: E402

from glink_harness.config import build_config  # noqa: E402
from glink_harness.kernel.acp_env import (  # noqa: E402
    INHERIT_MAX_FILE_BYTES,
    INHERIT_MAX_FILES,
    INHERIT_MAX_TOTAL_BYTES,
    prepare_acp_runtime,
)
from glink_harness.kernel.acp_e2b_env import (  # noqa: E402
    ACP_STATE_MAX_DEPTH,
    AcpSandboxTransfer,
)
from glink_harness.kernel.e2b_env import confirm_sandbox_absent  # noqa: E402
from glink_harness.kernel.sandbox import E2BSandbox, Launch, SANDBOX_FAMILY_STATE_HOME  # noqa: E402

#: One synthetic state tree per axis. ``seed`` commands run inside the sandbox;
#: ``files``/``bytes`` are the state facts the recovery has to judge.
AXES: dict[str, dict] = {
    "control": {
        "note": "对照组：两个小文件（普通状态形状）→ 期望整份发布成功",
        "seed": [
            f"mkdir -p {SANDBOX_FAMILY_STATE_HOME}/sub",
            f"printf '%s' '{{\"remembered\":\"ALFA-1000\"}}' > {SANDBOX_FAMILY_STATE_HOME}/state-a.json",
            f"printf '%s' 'tiny' > {SANDBOX_FAMILY_STATE_HOME}/sub/state-b.txt",
        ],
        "files": 2,
        "bytes": len('{"remembered":"ALFA-1000"}') + len("tiny"),
        "expect_ok": True,
        "expect_fragment": "",
    },
    "single": {
        "note": "轴 1（单文件）：9 MiB 文件 > 8 MiB 单文件上限",
        "seed": [
            f"printf '%s' '{{\"remembered\":\"BRAVO-2000\"}}' > {SANDBOX_FAMILY_STATE_HOME}/state.json",
            f"head -c 9437184 /dev/zero > {SANDBOX_FAMILY_STATE_HOME}/bloat.bin",
        ],
        "files": 2,
        "bytes": len('{"remembered":"BRAVO-2000"}') + 9437184,
        "expect_ok": False,
        "expect_fragment": "上限",
    },
    "total": {
        "note": "轴 2（总量）：9 × 8 MiB = 72 MiB > 64 MiB 总量上限（单文件都在上限内）",
        "seed": [
            f"printf '%s' '{{\"remembered\":\"CHARLIE-3000\"}}' > {SANDBOX_FAMILY_STATE_HOME}/state.json",
            "for i in $(seq 1 9); do "
            f"head -c 8388608 /dev/zero > {SANDBOX_FAMILY_STATE_HOME}/part$i.bin; done",
        ],
        "files": 10,
        "bytes": len('{"remembered":"CHARLIE-3000"}') + 9 * 8388608,
        "expect_ok": False,
        "expect_fragment": "总量",
    },
    "count": {
        "note": "轴 3（文件数）：4100 个空文件 > 4096 文件数上限",
        "seed": [
            f"mkdir -p {SANDBOX_FAMILY_STATE_HOME}/many",
            "for i in $(seq 1 4100); do : > "
            f"{SANDBOX_FAMILY_STATE_HOME}/many/f$i.dat; done",
        ],
        "files": 4100,
        "bytes": 0,
        "expect_ok": False,
        "expect_fragment": "文件数",
    },
}


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


def sandbox_file_facts(sandbox: object) -> tuple[int | None, int | None, str]:
    """``(file count, total bytes, failure)`` as the sandbox itself reports them.

    Measured inside the sandbox with the image's own ``node`` (the template is a
    Node image; no reliance on extra coreutils/awk): these are the source facts
    the bounded recovery has to judge, and they also prove a refusal never
    rewrites the sandbox.
    """
    script = (
        'const fs=require("fs"),p=require("path");let n=0,b=0;'
        'const walk=(d)=>{for(const e of fs.readdirSync(d,{withFileTypes:true})){'
        'const f=p.join(d,e.name);'
        'if(e.isDirectory())walk(f);else if(e.isFile()){n++;b+=fs.statSync(f).size;}}};'
        f'walk("{SANDBOX_FAMILY_STATE_HOME}");console.log(n);console.log(b);'
    )
    result = sandbox.commands.run(f"node -e '{script}'", timeout=300)
    stdout = str(getattr(result, "stdout", "") or "").strip()
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if len(lines) < 2 or not lines[0].isdigit() or not lines[1].isdigit():
        return None, None, f"沙箱内状态统计不可解析: {stdout[:200]!r}"
    return int(lines[0]), int(lines[1]), ""


def listing_shape(sandbox: object) -> str:
    """One live fact about the real SDK's recursive listing entry shape.

    ``_read_file_bounded`` consults an entry's ``size`` before reading and falls
    back to ``files.get_info``/a streamed read; the code path is only as good as
    the real SDK's payload shape, so the probe records it instead of assuming it.
    """
    try:
        entries = sandbox.files.list(SANDBOX_FAMILY_STATE_HOME, depth=ACP_STATE_MAX_DEPTH)
    except Exception as exc:  # noqa: BLE001 - recorded as a fact
        return f"listing 失败: {type(exc).__name__}: {exc}"
    for entry in entries:
        kind = getattr(getattr(entry, "type", None), "value", getattr(entry, "type", None))
        return (
            f"entry 数={len(entries)} 首条: type={kind!r} "
            f"path={getattr(entry, 'path', None)!r} size={getattr(entry, 'size', '<无该属性>')!r}"
        )
    return f"entry 数={len(entries)}（空）"


def run_axis(axis: str, args: argparse.Namespace, api_key: str, workdir: Path) -> dict:
    spec = AXES[axis]
    axis_dir = workdir / axis
    packs = write_probe_pack(axis_dir / "probe", "dsh --profile acp")
    values = {
        "GLINK_HARNESS": PROBE_PACK_NAME,
        "GLINK_HARNESSES_DIR": str(packs),
        "LOCAL_WEB_EXECUTOR": "e2b",
        "LOCAL_WEB_DATA_ROOT": str(axis_dir / "data"),
        "LOCAL_WEB_E2B_ACP_TEMPLATE": args.template,
        "LOCAL_WEB_TASK_TIMEOUT_SECONDS": str(args.timeout),
        "E2B_API_KEY": api_key,
    }
    config = build_config(values, REPO_ROOT)
    run_dir = axis_dir / "data" / "runs" / f"limit-{axis}"
    runtime = prepare_acp_runtime(config, runtime_dir=run_dir / "acp-agent")
    sandbox = E2BSandbox(
        config=config, run_id=f"limit-{axis}", workspace=axis_dir / "workspace"
    )
    facts: dict = {
        "axis": axis,
        "note": spec["note"],
        "runtimeDir": sanitize(str(runtime.directory), workdir),
        "seeded": {"files": spec["files"], "bytes": spec["bytes"]},
    }
    try:
        # 只建沙箱与状态根，不启动 agent：launch 只需要 ACP 模板。
        sandbox.open(
            Launch(argv=(), template=config.acp_e2b_template),
            timeout_seconds=config.task_timeout_seconds + 300,
        )
        facts["sandboxId"] = sandbox.sandbox_id
        print(f"  [step] 沙箱已创建 sandbox_id={sandbox.sandbox_id}", flush=True)
        if sandbox.handle is None:
            facts["seedError"] = "沙箱句柄缺失（open 未建立）"
            return facts
        transfer = AcpSandboxTransfer(
            sandbox.handle, runtime, workspace=axis_dir / "workspace"
        )
        transfer.upload_home()
        for index, command in enumerate(spec["seed"], start=1):
            print(f"  [step] 种子命令 {index}/{len(spec['seed'])}", flush=True)
            result = sandbox.handle.commands.run(command, timeout=args.seed_timeout)
            exit_code = getattr(result, "exit_code", 0)
            if exit_code not in (0, None):
                facts["seedError"] = (
                    f"沙箱内种子命令 exit={exit_code}: "
                    f"{str(getattr(result, 'stderr', ''))[:200]}"
                )
                return facts
        before_files, before_bytes, failure = sandbox_file_facts(sandbox.handle)
        facts["sandboxStateBefore"] = {"files": before_files, "bytes": before_bytes}
        print(
            f"  [step] 沙箱内状态: files={before_files} bytes={before_bytes}", flush=True
        )
        if failure:
            facts["seedError"] = failure
            return facts
        facts["listingShape"] = listing_shape(sandbox.handle)
        print(f"  [step] {facts['listingShape']}", flush=True)

        started = time.monotonic()
        print("  [step] 开始 recover_state()", flush=True)
        state = transfer.recover_state()
        facts["recoverSeconds"] = round(time.monotonic() - started, 2)
        facts["ok"] = state.ok
        facts["recoveredFiles"] = len(state.files)
        facts["recoveredBytes"] = state.bytes
        facts["failures"] = list(state.failures)

        published = runtime.directory / "recovered"
        staging = runtime.directory / ".recovered.staging"
        facts["publishedDirExists"] = published.is_dir()
        facts["stagingDirExists"] = staging.exists()
        facts["agentDirEntries"] = sorted(item.name for item in runtime.directory.iterdir())
        facts["publishedEntries"] = (
            sorted(str(item.relative_to(published)) for item in published.rglob("*"))
            if published.is_dir()
            else []
        )
        if published.is_dir():
            digests = {
                str(item.relative_to(published)): hashlib.sha256(item.read_bytes()).hexdigest()
                for item in published.rglob("*")
                if item.is_file()
            }
            facts["publishedDigests"] = digests
            if axis == "control":
                facts["expectedDigests"] = {
                    "state-a.json": hashlib.sha256(
                        b'{"remembered":"ALFA-1000"}'
                    ).hexdigest(),
                    "sub/state-b.txt": hashlib.sha256(b"tiny").hexdigest(),
                }

        after_files, after_bytes, failure = sandbox_file_facts(sandbox.handle)
        facts["sandboxStateAfter"] = {"files": after_files, "bytes": after_bytes}
        if failure:
            facts["sandboxStateAfterError"] = failure
    except Exception as exc:  # noqa: BLE001 - the failure is the evidence
        facts["error"] = f"{type(exc).__name__}: {exc}"
        facts["sandboxId"] = sandbox.sandbox_id
    finally:
        close_failures = sandbox.close().failures
        facts["closeFailures"] = list(close_failures)
        sandbox_id = facts.get("sandboxId") or ""
        absent, detail = confirm_sandbox_absent(sandbox_id, api_key=api_key)
        facts["sandboxAbsent"] = absent
        facts["sandboxAbsentDetail"] = detail
    return facts


def axis_verdict(facts: dict, spec: dict) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if facts.get("error") or facts.get("seedError"):
        reasons.append(facts.get("error") or facts.get("seedError"))
    if facts.get("sandboxStateBefore", {}).get("files") != spec["files"]:
        reasons.append(
            f"沙箱内状态文件数 {facts.get('sandboxStateBefore', {}).get('files')} != 预期 {spec['files']}"
        )
    if facts.get("sandboxStateBefore", {}).get("bytes") != spec["bytes"]:
        reasons.append(
            f"沙箱内状态字节数 {facts.get('sandboxStateBefore', {}).get('bytes')} != 预期 {spec['bytes']}"
        )
    if facts.get("ok") is not spec["expect_ok"]:
        reasons.append(f"recover_state().ok={facts.get('ok')} != 预期 {spec['expect_ok']}")
    failures = facts.get("failures") or []
    if spec["expect_ok"]:
        if failures:
            reasons.append(f"成功回收却带 failures={failures}")
        if not facts.get("publishedDirExists"):
            reasons.append("成功回收却没有发布目录")
        if facts.get("publishedDigests") != facts.get("expectedDigests"):
            reasons.append("发布内容与沙箱内合成内容不一致")
        if facts.get("stagingDirExists"):
            reasons.append("成功回收后残留 staging 目录")
    else:
        if not failures or not any(spec["expect_fragment"] in item for item in failures):
            reasons.append(
                f"拒绝原因里没有 {spec['expect_fragment']!r}: {failures}"
            )
        if facts.get("publishedDirExists"):
            reasons.append("回收失败却发布了 recovered 目录（半份状态）")
        if facts.get("stagingDirExists"):
            reasons.append("回收失败后残留 staging 目录（半份状态）")
        if facts.get("recoveredFiles"):
            reasons.append("回收失败却返回了已收集文件")
    before = facts.get("sandboxStateBefore")
    after = facts.get("sandboxStateAfter")
    if before and after and before != after:
        reasons.append(f"回收改了沙箱状态: {before} -> {after}")
    if facts.get("closeFailures"):
        reasons.append(f"沙箱 kill 失败: {facts['closeFailures']}")
    if facts.get("sandboxAbsent") is not True:
        reasons.append(f"沙箱未按 id 确认终止: {facts.get('sandboxAbsentDetail')}")
    return not reasons, reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ACP E2B 状态根回收上限触顶实测（无模型）")
    parser.add_argument("--axes", default="control,single,total")
    parser.add_argument("--template", default="glink-acp-worker")
    parser.add_argument("--env-file", default="", help="含 E2B_API_KEY 的冻结配置")
    parser.add_argument("--timeout", type=int, default=900, help="run 侧任务超时（决定沙箱寿命上界）")
    parser.add_argument("--seed-timeout", type=int, default=600, help="沙箱内种子命令超时")
    parser.add_argument("--workdir", default="")
    args = parser.parse_args(argv)

    api_key = resolve_api_key(args)
    if not api_key:
        print("缺少 E2B_API_KEY（进程环境或 --env-file）", file=sys.stderr)
        return 2
    axes = [item.strip() for item in args.axes.split(",") if item.strip()]
    unknown = [axis for axis in axes if axis not in AXES]
    if unknown:
        print(f"未知轴: {unknown}（可选 {sorted(AXES)}）", file=sys.stderr)
        return 2

    workdir = Path(args.workdir) if args.workdir else Path(
        tempfile.mkdtemp(prefix="glink-acp15-e2b-recover-limits-")
    )
    workdir.mkdir(parents=True, exist_ok=True)
    os.chmod(workdir, 0o700)

    try:
        from importlib.metadata import version

        sdk_version = version("e2b")
    except Exception:  # noqa: BLE001 - the version is informational
        sdk_version = "未知"

    print("ACP M9 E2B 状态根回收上限触顶实测（真实沙箱，无模型）")
    print("=" * 56)
    print(f"日期: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"模板: {args.template}（dev 模板，未重建）")
    print(f"e2b SDK: {sdk_version}")
    print("回收函数: glink_harness.kernel.acp_e2b_env.AcpSandboxTransfer.recover_state()（生产路径）")
    print(
        f"上限: 文件数 {INHERIT_MAX_FILES} / 单文件 {INHERIT_MAX_FILE_BYTES} B / "
        f"总量 {INHERIT_MAX_TOTAL_BYTES} B / 深度 {ACP_STATE_MAX_DEPTH}"
    )
    print(f"轴: {', '.join(axes)}")
    print(f"工作目录: {sanitize(str(workdir), workdir)}")
    print()

    results: list[dict] = []
    verdicts: list[bool] = []
    for axis in axes:
        spec = AXES[axis]
        print(f"--- 轴 {axis}：{spec['note']}")
        facts = run_axis(axis, args, api_key, workdir)
        ok, reasons = axis_verdict(facts, spec)
        verdicts.append(ok)
        results.append(facts)
        print(f"  sandbox_id: {facts.get('sandboxId', '')}")
        print(f"  种子命令: {len(spec['seed'])} 条（沙箱内执行，不走宿主上传通道）")
        print(f"  沙箱内状态（before）: {json.dumps(facts.get('sandboxStateBefore'), ensure_ascii=False)}")
        print(f"  真实 listing 形状: {facts.get('listingShape')}")
        print(f"  回收耗时: {facts.get('recoverSeconds')}s")
        print(
            "  recover_state(): ok={ok} files={files} bytes={bytes}".format(
                ok=facts.get("ok"),
                files=facts.get("recoveredFiles"),
                bytes=facts.get("recoveredBytes"),
            )
        )
        print(f"  failures: {json.dumps(facts.get('failures'), ensure_ascii=False)}")
        print(f"  宿主 recovered 目录存在: {facts.get('publishedDirExists')}")
        print(f"  宿主 .recovered.staging 存在: {facts.get('stagingDirExists')}")
        print(f"  宿主 acp-agent 目录条目: {facts.get('agentDirEntries')}")
        print(f"  已发布条目: {facts.get('publishedEntries')}")
        print(f"  沙箱内状态（after）: {json.dumps(facts.get('sandboxStateAfter'), ensure_ascii=False)}")
        print(f"  沙箱 kill failures: {facts.get('closeFailures')}")
        print(
            f"  沙箱按 id 对账: confirmed_absent={facts.get('sandboxAbsent')} "
            f"detail={facts.get('sandboxAbsentDetail')}"
        )
        if facts.get("error") or facts.get("seedError"):
            print(f"  错误: {facts.get('error') or facts.get('seedError')}")
        print(f"  [{'PASS' if ok else 'FAIL'}] 轴 {axis}")
        for reason in reasons:
            print(f"    - {reason}")
        print()

    print()
    print("汇总")
    print("----")
    for facts, ok in zip(results, verdicts, strict=True):
        print(
            f"  {facts['axis']}: ok={facts.get('ok')} files={facts.get('recoveredFiles')} "
            f"bytes={facts.get('recoveredBytes')} published={facts.get('publishedDirExists')} "
            f"staging={facts.get('stagingDirExists')} absent={facts.get('sandboxAbsent')} "
            f"→ [{'PASS' if ok else 'FAIL'}]"
        )
    overall = bool(verdicts) and all(verdicts)
    print(
        f"[{'PASS' if overall else 'FAIL'}] E2B 状态根回收上限触顶实测"
        "（整份拒绝 / 不留半份 / 拒绝可观测 / 按 id 终止）"
    )
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
