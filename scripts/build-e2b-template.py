#!/usr/bin/env python3
"""构建 E2B 沙箱模板（可从仓库复现）。

用法::

    E2B_API_KEY=… HTTPS_PROXY=socks5h://127.0.0.1:1080 \
        .venv/bin/python scripts/build-e2b-template.py [--alias pi-glink-worker]

为什么用 Python SDK 而不是 `npx @e2b/cli template create`：
CLI 基于 Node 的 fetch，不认 socks5 代理；SDK 走 httpx，装上 socksio 即可在
需要代理的网络里完成构建。构建参数与 docker/e2b/Dockerfile.e2b 是同一份真源。

构建完成后模板会带 `default` tag；需要固定生产 tag 时用 SDK 的
``Template.assign_tags`` 显式移动（不要依赖 CLI 的隐式行为）。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from e2b import Template

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "docker" / "e2b" / "Dockerfile.e2b"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 pi-glink 的 E2B 模板")
    parser.add_argument("--alias", default=os.environ.get("PI_GLINK_E2B_TEMPLATE", "pi-glink-worker"))
    parser.add_argument("--cpu", type=int, default=2)
    parser.add_argument("--memory-mb", type=int, default=4096)
    parser.add_argument("--logs", action="store_true", help="打印构建日志")
    args = parser.parse_args(argv)

    if not os.environ.get("E2B_API_KEY"):
        print("缺少 E2B_API_KEY", file=sys.stderr)
        return 2
    if not DOCKERFILE.is_file():
        print(f"缺少 Dockerfile: {DOCKERFILE}", file=sys.stderr)
        return 2

    print(f"构建模板 alias={args.alias}（cpu={args.cpu}, memory={args.memory_mb}MB）")
    print(f"Dockerfile: {DOCKERFILE.relative_to(REPO_ROOT)}")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("ALL_PROXY")
    if proxy:
        print(f"代理: {proxy}")

    builder = Template().from_dockerfile(str(DOCKERFILE))
    on_logs = (lambda entry: print(f"  [{entry.level}] {entry.message}")) if args.logs else None
    info = Template.build(
        builder,
        alias=args.alias,
        cpu_count=args.cpu,
        memory_mb=args.memory_mb,
        on_build_logs=on_logs,
    )
    print(f"构建完成: template={info.name} buildId={info.build_id} tags={info.tags}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
