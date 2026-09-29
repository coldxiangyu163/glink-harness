#!/usr/bin/env python3
"""Smoke: one real research run, from the HTTP API to a report on disk.

Usage::

    .venv/bin/python scripts/smoke-local-run.py \
        --prompt "…" [--base-url http://127.0.0.1:7860]

Requires a running host (``scripts/run-local-web.sh``) with credentials in
``.env``. Exits non-zero when the run does not finish with a report, so it can
be wired into CI or a manual acceptance log.

The script never reads the event log from disk: it verifies through the same
HTTP surface the browser uses (run view, report download, SSE stream).
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

DEFAULT_PROMPT = (
    "先查看当前工作区有哪些文件，然后写出 report.md：用中文说明你刚做了什么，"
    "并给出至少一条可核验的来源链接（需要时可用 web_search 检索）。"
)
_TIMEOUT_SECONDS = 900


def _request(base_url: str, path: str, *, method: str = "GET", body: dict | None = None):
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{base_url}{path}", data=payload, method=method)
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="pi-glink 真实 run 冒烟 / real run smoke")
    parser.add_argument("--base-url", default="http://127.0.0.1:7860")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--timeout", type=float, default=_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)

    status, body, _headers = _request(
        args.base_url, "/api/runs", method="POST", body={"prompt": args.prompt}
    )
    if status != 201:
        print(f"[fail] 创建 run 失败 status={status} body={body[:300]!r}")
        return 1
    run = json.loads(body)["run"]
    run_id = run["runId"]
    print(f"[ok] run 已创建 runId={run_id} status={run['status']}")

    started = time.monotonic()
    final = run
    while time.monotonic() - started < args.timeout:
        status, body, _headers = _request(args.base_url, f"/api/runs/{run_id}")
        if status != 200:
            print(f"[fail] 读取 run 失败 status={status}")
            return 1
        final = json.loads(body)["run"]
        if final["status"] != "running":
            break
        elapsed = time.monotonic() - started
        print(f"    … running {elapsed:5.1f}s events={final['eventCount']}", flush=True)
        time.sleep(3)
    else:
        print(f"[fail] run 在 {args.timeout}s 内没有结束")
        return 1

    elapsed = round(time.monotonic() - started, 1)
    print(
        f"[{'ok' if final['status'] == 'finished' and final['hasReport'] else 'fail'}] "
        f"run 收尾 status={final['status']} stopReason={final['stopReason']} "
        f"耗时={elapsed}s events={final['eventCount']} sessionId={final['sessionId']}"
    )
    if final.get("error"):
        print(f"    error: {final['error']}")
    if final.get("usage"):
        print(f"    usage: {json.dumps(final['usage'], ensure_ascii=False)}")

    status, body, headers = _request(args.base_url, f"/api/runs/{run_id}/report")
    if status != 200:
        print(f"[fail] 报告不可下载 status={status}")
        return 1
    text = body.decode("utf-8")
    print(f"[ok] 报告可下载 {headers.get('Content-Type')} {len(body)} bytes")
    print("----- report.md 前 12 行 -----")
    print("\n".join(text.splitlines()[:12]))
    print("------------------------------")

    # The stream itself is part of the contract: a reconnect must replay it.
    status, body, _headers = _request(args.base_url, f"/api/runs/{run_id}/events")
    if status != 200:
        print(f"[fail] 事件流不可读 status={status}")
        return 1
    head = body.decode("utf-8", errors="replace")
    kinds = sorted({line.split(": ", 1)[1] for line in head.splitlines() if line.startswith("event: ")})
    print(f"[ok] 事件流可重放，帧类型: {', '.join(kinds)}")

    ok = final["status"] == "finished" and final["hasReport"]
    print("[done] 冒烟通过" if ok else "[done] 冒烟未通过")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
