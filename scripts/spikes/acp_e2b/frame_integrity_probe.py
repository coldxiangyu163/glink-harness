#!/usr/bin/env python3
"""E2B 双向 stdio 帧完整性实测（GLINK-ACP-04 / M3b 的先行步骤）。

测量对象是 E2B SDK 的 stdio 桥本身，而不是任何真实 agent：宿主用
``commands.run(cmd, background=True, stdin=True)`` 起一个后台进程，用
``handle.send_stdin(...)`` 写 JSON-RPC 记录，用 ``handle.__iter__()`` 读 stdout
分块（sandbox 端对端脚本：``rpc_echo_agent.py``）。M3b 要在这个通道上跑 ACP
agent，所以先在真实沙箱里回答四个问题：

* **保序**：连续写入是否按调用顺序到达，回包是否按请求顺序返回；
* **分帧**：host→agent 的字节是否原样到达（不截断/不改写），agent→host 的
  stdout 分块能否用仓库 LF-only 分帧（``kernel/framing.py``）还原成记录，
  不粘连、不截断；多帧一次写入、半帧分两次写入都要正确；
* **大帧**：单条 ≥64 KiB 的记录能否双向通过（64 KiB / 256 KiB / 1 MiB）；
* **共享语料**：``tests/fixtures/lf-framing.json``（U+2028/U+2029 是内容）
  双向逐帧一致。

结论为负面时停止 M3b 的实现（brief 的验收条件①）。脚本只创建并操作它自己的
沙箱、结束时按 sandbox id 终止（不做任何模式匹配或批量清理），不打印任何凭据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "host"))

from pi_glink.kernel.framing import split_lf_records, strip_trailing_cr_bytes  # noqa: E402

AGENT_NAME = "rpc_echo_agent.py"
AGENT_SOURCE = Path(__file__).resolve().parent / AGENT_NAME
FRAMING_CORPUS = REPO_ROOT / "tests" / "fixtures" / "lf-framing.json"
PATTERN = "0123456789abcdef"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def frame_bytes(payload: dict, *, lf: bytes = b"\n") -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + lf


class Bridge:
    """宿主侧：把 ``CommandHandle`` 的 stdout 流按仓库 LF-only 语义分帧。"""

    def __init__(self, handle: Any) -> None:
        self.handle = handle
        self._records: queue.Queue[tuple[int, dict] | None] = queue.Queue()
        self._buffer = b""
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()
        self.stream_error: str | None = None
        self.chunk_count = 0
        self.chunk_bytes = 0
        self.max_chunk = 0
        self.glued_chunks = 0
        self.mid_frame_chunks = 0
        self.non_json_records = 0
        self.stderr_chunks: list[str] = []

    # -- reader ---------------------------------------------------------------

    def _read_loop(self) -> None:
        try:
            for stdout, stderr, _pty in self.handle:
                if stderr:
                    self.stderr_chunks.append(str(stderr))
                if not stdout:
                    continue
                data = str(stdout).encode("utf-8")
                self.chunk_count += 1
                self.chunk_bytes += len(data)
                self.max_chunk = max(self.max_chunk, len(data))
                if not data.endswith(b"\n"):
                    self.mid_frame_chunks += 1
                self._buffer += data
                records, self._buffer = split_lf_records(self._buffer)
                if len(records) > 1:
                    self.glued_chunks += 1
                for raw in records:
                    text = strip_trailing_cr_bytes(raw).decode("utf-8", "replace")
                    try:
                        parsed = json.loads(text)
                    except json.JSONDecodeError:
                        self.non_json_records += 1
                        parsed = {"__unparsed__": text[:200]}
                    self._records.put((len(raw), parsed))
        except Exception as exc:  # noqa: BLE001 - 记录为流失败，由调用方判定
            self.stream_error = f"{type(exc).__name__}: {exc}"
        finally:
            if self._buffer:
                # LF-only 分帧：结尾没有 LF 的残片仍是一条记录（与 ACP 客户端一致）。
                self.mid_frame_chunks += 1
                text = strip_trailing_cr_bytes(self._buffer).decode("utf-8", "replace")
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    self.non_json_records += 1
                    parsed = {"__unparsed__": text[:200]}
                self._records.put((len(self._buffer), parsed))
            self._records.put(None)

    # -- API ------------------------------------------------------------------

    def send(self, data: str | bytes, *, request_timeout: float = 120.0) -> bytes:
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        self.handle.send_stdin(raw, request_timeout=request_timeout)
        return raw

    def wait_json(self, *, timeout: float = 60.0) -> tuple[int, dict] | None:
        try:
            item = self._records.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is None:
            # 再放回去，后面的等待仍会立刻返回 EOF。
            self._records.put(None)
            return None
        return item

    def drain(self, *, count: int, timeout: float = 60.0) -> list[dict]:
        items = []
        for _ in range(count):
            item = self.wait_json(timeout=timeout)
            if item is None:
                break
            items.append(item[1])
        return items

    def close_stdin(self) -> None:
        self.handle.close_stdin(request_timeout=60)

    def join(self, timeout: float = 30.0) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def summary(self) -> str:
        return (
            f"chunks={self.chunk_count} bytes={self.chunk_bytes} max_chunk={self.max_chunk} "
            f"glued_chunks={self.glued_chunks} mid_frame_chunks={self.mid_frame_chunks} "
            f"non_json={self.non_json_records} stream_error={self.stream_error or 'none'}"
        )


class Report:
    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((name, bool(ok), detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail}" if detail else ""), flush=True)
        return bool(ok)

    @property
    def failed(self) -> list[tuple[str, bool, str]]:
        return [item for item in self.checks if not item[1]]


def _ping(bridge: Bridge, report: Report, *, name: str, ident: Any, pad: str) -> tuple[int, dict] | None:
    frame = frame_bytes({"jsonrpc": "2.0", "id": ident, "method": "ping", "pad": pad})
    bridge.send(frame)
    item = bridge.wait_json(timeout=120.0)
    if item is None:
        report.check(name, False, "没有收到回包")
        return None
    return item


def check_small_round_trip(bridge: Bridge, report: Report) -> None:
    pad = "字符串与 emoji 🚀\u2028\u2029"
    frame = frame_bytes({"jsonrpc": "2.0", "id": 1, "method": "ping", "pad": pad})
    bridge.send(frame.decode("utf-8"))  # str 形态
    item = bridge.wait_json(timeout=60.0)
    ok = item is not None and item[1].get("pad") == pad and item[1].get("raw_sha256") == sha256(frame[:-1])
    report.check(
        "基础往返：send_stdin 接受 str，字节原样（raw_sha256 与 pad 一致）",
        ok,
        f"n={item[1].get('n') if item else None} raw_len={item[0] if item else None}",
    )
    frame2 = frame_bytes({"jsonrpc": "2.0", "id": 2, "method": "ping", "pad": "bytes"})
    bridge.send(frame2)  # bytes 形态
    item2 = bridge.wait_json(timeout=60.0)
    ok2 = item2 is not None and item2[1].get("pad") == "bytes" and item2[1].get("raw_sha256") == sha256(frame2[:-1])
    report.check("基础往返：send_stdin 接受 bytes，哈希一致", ok2)


def check_ordering(bridge: Bridge, report: Report, *, count: int, label: str, burst: bool) -> None:
    expected = []
    for index in range(count):
        ident = f"{label}-{index}"
        pad = f"pad-{label}-{index}-{'x' * (index % 7)}"
        frame = frame_bytes({"jsonrpc": "2.0", "id": ident, "method": "ping", "pad": pad})
        expected.append((ident, pad, frame))
        bridge.send(frame)
        if not burst:
            item = bridge.wait_json(timeout=60.0)
            if item is None:
                report.check(f"{label}：第 {index} 条无回包", False)
                return
            reply = item[1]
            if reply.get("id") != ident or reply.get("pad") != pad or reply.get("raw_sha256") != sha256(frame[:-1]):
                report.check(f"{label}：第 {index} 条回包内容不符", False, f"id={reply.get('id')}")
                return
    if burst:
        replies = bridge.drain(count=count, timeout=120.0)
        ok = len(replies) == count and all(
            reply.get("id") == ident and reply.get("pad") == pad and reply.get("raw_sha256") == sha256(frame[:-1])
            for reply, (ident, pad, frame) in zip(replies, expected)
        )
        seq = [reply.get("n") for reply in replies]
        report.check(
            f"{label}：{count} 条突发写入按序返回（id/pad/哈希全等）",
            ok,
            f"replies={len(replies)} n[:3]={seq[:3]} n[-1]={seq[-1] if seq else None}",
        )
    else:
        report.check(f"{label}：{count} 条逐条往返按序（id/pad/哈希全等）", True)


def check_split_and_multi(bridge: Bridge, report: Report, first_n: int | None) -> int:
    # 半帧 + 延迟 + 半帧：写入被拆成两次 send_stdin，仍必须是一条记录。
    payload = {"jsonrpc": "2.0", "id": "split", "method": "ping", "pad": "y" * 200}
    whole = frame_bytes(payload)
    half = len(whole) // 2
    bridge.send(whole[:half])
    time.sleep(0.3)
    bridge.send(whole[half:])
    item = bridge.wait_json(timeout=60.0)
    ok = (
        item is not None
        and item[1].get("id") == "split"
        and item[1].get("pad") == "y" * 200
        and item[1].get("raw_sha256") == sha256(whole[:-1])
    )
    report.check("分片写入：半帧 + 半帧（中间有间隔）→ 恰好一条记录", ok, f"n={item[1].get('n') if item else None}")
    # 单次写入两帧：宿主端必须能拆成两条记录。
    frame_a = frame_bytes({"jsonrpc": "2.0", "id": "multi-a", "method": "ping", "pad": "a"})
    frame_b = frame_bytes({"jsonrpc": "2.0", "id": "multi-b", "method": "ping", "pad": "b"})
    bridge.send(frame_a + frame_b)
    replies = bridge.drain(count=2, timeout=60.0)
    ok2 = (
        len(replies) == 2
        and replies[0].get("id") == "multi-a"
        and replies[1].get("id") == "multi-b"
        and replies[0].get("raw_sha256") == sha256(frame_a[:-1])
        and replies[1].get("raw_sha256") == sha256(frame_b[:-1])
    )
    report.check("单次写入两帧：宿主分帧得到两条独立记录", ok2, f"replies={len(replies)}")
    # agent → host 一次 write 连发三帧（粘连方向）。
    multi = frame_bytes({"jsonrpc": "2.0", "id": "multi-out", "method": "multi", "count": 3})
    bridge.send(multi)
    replies = bridge.drain(count=3, timeout=60.0)
    ok3 = len(replies) == 3 and [reply.get("part") for reply in replies] == [0, 1, 2]
    report.check("agent 单次 write 连发三帧：宿主分帧成三条记录且顺序正确", ok3, f"replies={len(replies)}")
    # CRLF 写入者：尾部 CR 单独剥离，原始哈希仍含 CR。
    crlf = frame_bytes({"jsonrpc": "2.0", "id": "crlf", "method": "ping", "pad": "cr"}, lf=b"\r\n")
    bridge.send(crlf)
    item = bridge.wait_json(timeout=60.0)
    ok4 = item is not None and item[1].get("cr") is True and item[1].get("raw_sha256") == sha256(crlf[:-1])
    report.check("CRLF 记录：宿主剥离尾部 CR，agent 收到的原始字节含 CR", ok4)
    return first_n or 0


def check_corpus(bridge: Bridge, report: Report) -> None:
    corpus = json.loads(FRAMING_CORPUS.read_text(encoding="utf-8"))
    frames = corpus["frames"]
    sent = []
    for index, value in enumerate(frames):
        lf = b"\r\n" if index % 2 else b"\n"
        frame = frame_bytes({"jsonrpc": "2.0", "id": f"corpus-{index}", "method": "echo", "value": value}, lf=lf)
        sent.append(frame)
        bridge.send(frame)
    replies = bridge.drain(count=len(frames), timeout=120.0)
    ok = len(replies) == len(frames) and all(
        reply.get("value") == value and reply.get("raw_sha256") == sha256(frame[:-1])
        for reply, value, frame in zip(replies, frames, sent)
    )
    # 共享语料里的 U+2028/U+2029 必须是内容：回包正文包含真实的分隔符字符。
    has_separators = any("\u2028" in json.dumps(reply.get("value"), ensure_ascii=False) for reply in replies)
    report.check(
        "共享语料 lf-framing.json 双向逐帧一致（CRLF/LF 混合，U+2028/U+2029 为内容）",
        ok and has_separators,
        f"frames={len(frames)} replies={len(replies)} separators_seen={has_separators}",
    )


def check_large_frames(bridge: Bridge, report: Report, sizes: list[int]) -> None:
    for size in sizes:
        pad = (PATTERN * (size // len(PATTERN) + 1))[:size]
        frame = frame_bytes({"jsonrpc": "2.0", "id": f"large-{size}", "method": "ping", "pad": pad})
        started = time.monotonic()
        try:
            bridge.send(frame)
        except Exception as exc:  # noqa: BLE001 - 规模上限本身就是测量结果
            report.check(f"大帧 host→agent {size} 字节", False, f"send_stdin 失败: {type(exc).__name__}: {exc}")
            continue
        item = bridge.wait_json(timeout=180.0)
        elapsed = time.monotonic() - started
        ok = (
            item is not None
            and item[1].get("pad") == pad
            and item[1].get("raw_sha256") == sha256(frame[:-1])
            and item[1].get("len") == len(frame) - 1
        )
        report.check(
            f"大帧 host→agent {size} 字节（{len(frame)} 字节记录）",
            ok,
            f"n={item[1].get('n') if item else None} elapsed={elapsed:.2f}s",
        )

    for size in sizes:
        frame = frame_bytes({"jsonrpc": "2.0", "id": f"emit-{size}", "method": "emit", "size": size})
        started = time.monotonic()
        bridge.send(frame)
        item = bridge.wait_json(timeout=180.0)
        elapsed = time.monotonic() - started
        if item is None:
            report.check(f"大帧 agent→host {size} 字节", False, "没有收到回包")
            continue
        raw_len, reply = item
        body = reply.get("body")
        expected = (PATTERN * (size // len(PATTERN) + 1))[:size] if size > 0 else ""
        ok = (
            isinstance(body, str)
            and len(body) == size
            and reply.get("body_sha256") == sha256(expected.encode("utf-8"))
            and raw_len >= size
        )
        report.check(
            f"大帧 agent→host {size} 字节（记录 {raw_len} 字节）",
            ok,
            f"body_len={len(body) if isinstance(body, str) else None} elapsed={elapsed:.2f}s",
        )


def run_checks(bridge: Bridge, report: Report, sizes: list[int]) -> None:
    check_small_round_trip(bridge, report)
    check_ordering(bridge, report, count=64, label="顺序", burst=False)
    check_ordering(bridge, report, count=128, label="突发", burst=True)
    check_split_and_multi(bridge, report, None)
    check_corpus(bridge, report)
    check_large_frames(bridge, report, sizes)


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
    parser = argparse.ArgumentParser(description="E2B 双向 stdio 帧完整性实测（M3b 先行步骤）")
    parser.add_argument("--template", default="base", help="沙箱模板（默认 base，绝不使用生产模板）")
    parser.add_argument("--sandbox-timeout", type=int, default=900)
    parser.add_argument("--env-file", default="", help="E2B_API_KEY 的回退来源（不打印值）")
    parser.add_argument(
        "--large-sizes",
        default="65536,262144,1048576",
        help="大帧测试的字节数（逗号分隔）",
    )
    args = parser.parse_args(argv)
    sizes = [int(item) for item in args.large_sizes.split(",") if item.strip()]

    api_key = resolve_api_key(args)
    if not api_key:
        print("缺少 E2B_API_KEY（进程环境或 --env-file）", file=sys.stderr)
        return 2
    if not AGENT_SOURCE.is_file():
        print(f"缺少沙箱端脚本: {AGENT_SOURCE}", file=sys.stderr)
        return 2

    from e2b import Sandbox

    print("E2B 双向 stdio 帧完整性实测（GLINK-ACP-04 / M3b）")
    print(f"日期: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"模板: {args.template}（从不触碰 glink-frontier-e2b 及其任何 tag）")
    print(f"仓库分帧模块: host/pi_glink/kernel/framing.py（LF-only）")
    try:
        import importlib.metadata as metadata

        print(f"e2b SDK: {metadata.version('e2b')}")
    except Exception:  # noqa: BLE001 - 版本只是证据补充
        pass
    print()

    report = Report()
    sandbox: Any | None = None
    exit_code = 0
    try:
        sandbox = Sandbox.create(template=args.template, timeout=args.sandbox_timeout, api_key=api_key)
        print(f"sandbox_id: {sandbox.sandbox_id}")
        version = sandbox.commands.run("python3 --version && uname -m && id -un && echo HOME=$HOME")
        print(f"沙箱环境: {version.stdout.strip().splitlines()}")
        sandbox.files.write(f"/home/user/{AGENT_NAME}", AGENT_SOURCE.read_bytes())
        handle = sandbox.commands.run(
            f"python3 /home/user/{AGENT_NAME}",
            background=True,
            stdin=True,
            cwd="/home/user",
            timeout=0,
        )
        print(f"后台进程 pid: {handle.pid}（stdin=True）\n")
        bridge = Bridge(handle)
        try:
            run_checks(bridge, report, sizes)
            report.check("stdout 无非法记录（每条完整记录都是合法 JSON）", bridge.non_json_records == 0,
                         f"non_json={bridge.non_json_records}")
            report.check("流读取无异常", bridge.stream_error is None, bridge.stream_error or "")
        finally:
            # 最后一项：EOF 语义与退出码。
            try:
                bridge.close_stdin()
                closed = True
                close_error = ""
            except Exception as exc:  # noqa: BLE001 - close_stdin 可用性是测量结果
                closed = False
                close_error = f"{type(exc).__name__}: {exc}"
            joined = bridge.join(timeout=30.0)
            report.check("close_stdin 可用（能对后台进程发出 EOF）", closed, close_error)
            if closed:
                try:
                    result = handle.wait()
                    report.check(
                        "stdin EOF 后进程正常退出（exit_code=0）",
                        result.exit_code == 0,
                        f"exit={result.exit_code} stderr_tail={result.stderr[-120:]!r}",
                    )
                except Exception as exc:  # noqa: BLE001 - 退出观察失败也是结论
                    report.check("stdin EOF 后进程正常退出（exit_code=0）", False,
                                 f"{type(exc).__name__}: {exc}")
            report.check("读取线程在 EOF 后结束（无悬空流）", joined)
        print(f"\n分块统计: {bridge.summary()}")
        if bridge.stderr_chunks:
            print(f"stderr: {''.join(bridge.stderr_chunks)[:400]!r}")
    finally:
        if sandbox is not None:
            killed = None
            try:
                killed = sandbox.kill()
            except Exception as exc:  # noqa: BLE001 - 终止失败必须出现在证据里
                killed = f"{type(exc).__name__}: {exc}"
            print(f"\n按 sandbox_id={sandbox.sandbox_id} 终止: {killed}")

    print("\n结果汇总")
    for name, ok, detail in report.checks:
        print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f" :: {detail}" if detail else ""))
    if report.failed:
        print(f"\n[FAIL] {len(report.failed)}/{len(report.checks)} 项未通过")
        exit_code = 1
    else:
        print(f"\n[PASS] {len(report.checks)}/{len(report.checks)} 项通过")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
