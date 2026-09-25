#!/usr/bin/env python3
"""沙箱端回显 agent：为 E2B 双向 stdio 帧完整性实测提供对端。

被测对象是 E2B SDK 的 stdio 桥（宿主 ``send_stdin`` 写、``CommandHandle`` 读），
不是任何真实 agent。这个脚本只用标准库，按 LF 切分原始字节（U+2028/U+2029 是
内容而非边界），把每条完整记录的**原始字节哈希**回报给宿主，让宿主可以判定
字节是否原样到达。协议（每条一行 JSON，UTF-8）：

* ``{"id": …, "method": "ping", "pad": "…"}`` → 回报 ``pad`` 与哈希；
* ``{"id": …, "method": "echo", "value": <任意 JSON>}`` → 原样回报 ``value``；
* ``{"id": …, "method": "emit", "size": <int>}`` → 回报长度为 ``size`` 的确定性正文；
* ``{"id": …, "method": "multi", "count": <int>}`` → 一次 ``os.write`` 连发多帧
  （专门检验宿主端多帧粘连时的 LF 分帧）；
* ``{"id": …, "method": "exit"}`` → 回报后退出；
* 其余/非法输入 → 回报 ``error`` 字段。

每条回报都带 ``n``（已处理记录序号，从 1 开始）、``raw_sha256``（本条输入记录
不含结尾 LF 的原始字节哈希）、``len`` 与 ``cr``（记录是否以 CR 结尾）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

#: 大帧正文的确定性模式（与宿主侧同源；换行符绝不出现）。
PATTERN = "0123456789abcdef"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(frames: list[dict]) -> None:
    """Write one or more reply frames as a single LF-terminated byte block."""
    payload = b"".join(
        json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        for frame in frames
    )
    os.write(1, payload)


def _body_for(size: int) -> str:
    if size <= 0:
        return ""
    return (PATTERN * (size // len(PATTERN) + 1))[:size]


def _handle(raw: bytes, n: int) -> None:
    cr = raw.endswith(b"\r")
    text = raw[:-1] if cr else raw
    out: dict = {"n": n, "raw_sha256": _sha256(raw), "len": len(raw), "cr": cr}
    try:
        request = json.loads(text.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 回报而不是崩溃
        out["error"] = f"parse:{type(exc).__name__}"
        _write([out])
        return
    if not isinstance(request, dict):
        out["error"] = "not-object"
        _write([out])
        return
    out["id"] = request.get("id")
    out["text_sha256"] = _sha256(text)
    method = request.get("method")
    if method == "ping":
        out["pad"] = request.get("pad")
        _write([out])
    elif method == "echo":
        out["value"] = request.get("value")
        _write([out])
    elif method == "emit":
        size = int(request.get("size") or 0)
        body = _body_for(size)
        out["body"] = body
        out["body_sha256"] = _sha256(body.encode("utf-8"))
        _write([out])
    elif method == "multi":
        count = int(request.get("count") or 1)
        frames = [
            {
                "id": request.get("id"),
                "n": n,
                "part": index,
                "raw_sha256": _sha256(raw),
            }
            for index in range(count)
        ]
        _write(frames)
    elif method == "exit":
        _write([out])
        sys.exit(0)
    else:
        out["error"] = f"unknown-method:{method}"
        _write([out])


def main() -> int:
    buffer = b""
    count = 0
    while True:
        chunk = os.read(0, 262144)
        if not chunk:
            break
        buffer += chunk
        while True:
            index = buffer.find(b"\n")
            if index < 0:
                break
            raw = buffer[:index]
            buffer = buffer[index + 1 :]
            count += 1
            _handle(raw, count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
