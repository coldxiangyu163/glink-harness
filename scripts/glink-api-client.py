#!/usr/bin/env python3
"""Reference API-only client for a glink-harness host (multi-harness-plan §6, R2).

Standard library only, and deliberately **not** part of the host package: this
script proves the service can be driven end to end through its HTTP/SSE
interface alone — create a run, stream its events live to the terminal frame,
read the run detail, then replay the committed history and check the two passes
agree. A gateway, an ops script or any caller's backend would consume the same
surface the same way (non-browser clients send no Origin header and are not
subject to the browser same-origin checks).

Frames arrive as SSE over chunked HTTP/1.1 (``http.client`` decodes chunking
transparently). The stream's idle tail is kept alive by ``: keepalive``
comment frames every 15 seconds, which the SSE spec tells clients to ignore;
this parser does exactly that. The socket timeout stays below the server's
60-second per-operation deadline, and a total deadline bounds the whole run —
the heartbeat wakes the read loop at least every 15 seconds so the budget
check can fire, and a silent server surfaces as a socket timeout that the
loop then classifies against the total deadline.
"""

from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
from urllib.parse import urlparse

#: The frames after which the run has no more events, ever.
TERMINAL_EVENT_TYPES = frozenset({"run_finished", "run_failed", "run_cancelled"})
#: Below the host's per-socket-operation deadline; the 15s heartbeat keeps an
#: idle stream from ever hitting it.
SOCKET_TIMEOUT_SECONDS = 45.0


class ClientError(RuntimeError):
    """The API refused or malformed a step of the reference flow."""


def connect(base_url: str) -> tuple[str, int]:
    parsed = urlparse(base_url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise ClientError(f"--base-url 需要 http://host:port 形式，收到 {base_url!r}")
    return parsed.hostname, parsed.port or 80


def create_run(
    host: str, port: int, prompt: str, *, idempotency_key: str | None = None
) -> str:
    """POST /api/runs → the admitted run id (the JSON media type is required).

    ``idempotency_key`` rides the ``Idempotency-Key`` header (R3): a retry of
    the same key replays the original admission — same 201, same run id — so a
    lost creation response never turns into a second execution.
    """
    connection = http.client.HTTPConnection(host, port, timeout=SOCKET_TIMEOUT_SECONDS)
    try:
        body = json.dumps({"prompt": prompt}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        connection.request("POST", "/api/runs", body=body, headers=headers)
        response = connection.getresponse()
        if response.status != 201:
            # 错误体可能是反代的 HTML 页：先保住状态码，再按 replace 解码。
            detail = response.read().decode("utf-8", "replace")
            raise ClientError(f"创建 run 失败: HTTP {response.status} {detail}")
        payload = json.loads(response.read() or b"{}")
        return str(payload["run"]["runId"])
    finally:
        connection.close()


def parse_frame(block: str) -> tuple[int | None, str | None, dict | None]:
    """One SSE block → (seq, event type, payload); comment-only blocks are (…, None, None)."""
    seq: int | None = None
    kind: str | None = None
    payload: dict | None = None
    for line in block.split("\n"):
        if not line or line.startswith(":"):
            continue  # empty separator or a keepalive comment
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "id":
            seq = int(value)
        elif field == "event":
            kind = value
        elif field == "data":
            try:
                payload = json.loads(value)
            except ValueError:
                payload = {"raw": value}
    return seq, kind, payload


def stream_events(
    host: str, port: int, run_id: str, *, stop_at_terminal: bool, deadline: float
) -> list[tuple[int | None, str | None, dict | None]]:
    """GET /api/runs/{id}/events — live tail or full replay, as one frame list.

    ``stop_at_terminal`` is the live pass: the connection closes itself once
    the terminal frame is seen (nothing can follow it). The replay pass reads
    until the server ends the stream, which for a terminal run is right after
    the last committed frame.
    """
    connection = http.client.HTTPConnection(host, port, timeout=SOCKET_TIMEOUT_SECONDS)
    frames: list[tuple[int | None, str | None, dict | None]] = []
    try:
        connection.request("GET", f"/api/runs/{run_id}/events")
        response = connection.getresponse()
        if response.status != 200:
            detail = response.read().decode("utf-8", "replace")
            raise ClientError(f"打开事件流失败: HTTP {response.status} {detail}")
        # ``connection.sock`` 在这里可能已经是 None：流响应带 ``Connection:
        # close``，``getresponse`` 会判定 will_close 并立即关闭连接对象——
        # socket 的所有权已移交给 response，通过 connection 再也摸不到它。
        # 总截止时间因此靠「socket 超时(45s) + 心跳(15s)唤醒循环」兑现：
        # 服务器守约时每次读最多阻塞一个心跳周期，循环顶部的预算检查即可
        # 打断；服务器静默违约时 read1 在 45s 后抛 TimeoutError，由下方
        # 分支按总截止时间还是网络故障归类。
        buffer = b""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClientError("超过总截止时间，事件流仍未结束")
            try:
                chunk = response.read1(65536)
            except TimeoutError:
                if time.monotonic() > deadline:
                    # 不是服务器静默超过 45s，是 run 本身超过了总截止时间。
                    raise ClientError("超过总截止时间，事件流仍未结束") from None
                raise
            if not chunk:
                break  # the server ended the stream
            # 帧在字节层积累，只在完整块上解码：多字节 UTF-8 字符可能被
            # TCP/chunk 边界切断，按 chunk 独立解码会在此处崩溃。
            buffer += chunk
            while b"\n\n" in buffer:
                block, buffer = buffer.split(b"\n\n", 1)
                frame = parse_frame(block.decode("utf-8"))
                if frame[1] is None:
                    continue  # keepalive comment, no event semantics
                frames.append(frame)
                if stop_at_terminal and frame[1] in TERMINAL_EVENT_TYPES:
                    return frames
        return frames
    finally:
        connection.close()


def run_detail(host: str, port: int, run_id: str) -> dict:
    connection = http.client.HTTPConnection(host, port, timeout=SOCKET_TIMEOUT_SECONDS)
    try:
        connection.request("GET", f"/api/runs/{run_id}")
        response = connection.getresponse()
        if response.status != 200:
            detail = response.read().decode("utf-8", "replace")
            raise ClientError(f"读取 run 详情失败: HTTP {response.status} {detail}")
        payload = json.loads(response.read() or b"{}")
        return dict(payload["run"])
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通过 API 完成 glink-harness 执行与重放的参考客户端")
    parser.add_argument("--base-url", default="http://127.0.0.1:7860", help="宿主地址（http://host:port）")
    parser.add_argument("--prompt", required=True, help="要执行的提示")
    parser.add_argument("--idempotency-key", default=None, help="创建幂等键（同键同请求重放原 run；缺省不启用）")
    parser.add_argument("--timeout", type=float, default=300.0, help="总截止时间（秒）")
    arguments = parser.parse_args(argv)

    host, port = connect(arguments.base_url)
    deadline = time.monotonic() + arguments.timeout
    try:
        run_id = create_run(host, port, arguments.prompt, idempotency_key=arguments.idempotency_key)
        print(f"run: {run_id}")

        live = stream_events(host, port, run_id, stop_at_terminal=True, deadline=deadline)
        terminal = live[-1][1] if live else None
        print(f"stream: {len(live)} frames, terminal: {terminal}")

        detail = run_detail(host, port, run_id)
        print(
            f"status: {detail['status']} stopReason: {detail['stopReason']} "
            f"hasReport: {detail['hasReport']}"
        )

        replay = stream_events(host, port, run_id, stop_at_terminal=False, deadline=deadline)
        sequences_match = [frame[0] for frame in live] == [frame[0] for frame in replay]
        print(f"replay: {len(replay)} frames, sequences match: {sequences_match}")
        if terminal not in TERMINAL_EVENT_TYPES:
            raise ClientError(f"直播段没有读到终帧，最后一帧是 {terminal!r}")
        if not sequences_match:
            raise ClientError("重放与直播的 seq 序列不一致")
    except (ClientError, OSError, KeyError, ValueError) as exc:
        print(f"参考客户端失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
