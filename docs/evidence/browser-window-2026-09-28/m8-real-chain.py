#!/usr/bin/env python3
"""On an isolated local/inproc host, create five genuine sequential model turns.

Usage: python m8-real-chain.py http://127.0.0.1:<random-port> > m8-real-chain.json
Only run against a newly-created disposable data root; no credentials in output.
"""
import json
import sys
import time
import urllib.error
import urllib.request

base = sys.argv[1].rstrip("/")


def request(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


runs = []
parent = None
prompts = [
    "请使用 bash 执行 python3 --version，然后读取 README.md 的前 20 行，简要总结并将中文结论写入 report.md。",
    "接着用中文用一句话概括刚才观察的版本号，并将结果写入 report.md。",
    "继续追问：刚才读取的是哪个文件？用一句话回答并写入 report.md。",
    "再追问：请用一句话解释为什么报告要标记待核验，并写入 report.md。",
    "最后追问：总结前四轮的主题，用一句话回答并写入 report.md。",
]
for n, prompt in enumerate(prompts, 1):
    payload = {"prompt": prompt}
    if parent:
        payload["parentRunId"] = parent
    initial = request("/api/runs", payload)["run"]
    run_id = initial["runId"]
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        record = request("/api/runs/" + run_id)["run"]
        if record["status"] != "running":
            break
        time.sleep(1)
    else:
        raise TimeoutError(run_id)
    result = {key: record.get(key) for key in ("runId", "parentRunId", "status", "eventCount", "hasReport", "stopReason", "cleanup", "reportStatus")}
    result["turn"] = n
    runs.append(result)
    print(json.dumps(result, ensure_ascii=False), file=sys.stderr, flush=True)
    if record["status"] != "finished":
        raise RuntimeError(f"turn {n} not finished: {record['status']}")
    parent = run_id
print(json.dumps({"baseUrl": base, "turns": runs}, ensure_ascii=False, indent=2))
