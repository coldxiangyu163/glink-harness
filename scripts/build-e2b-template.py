#!/usr/bin/env python3
"""构建 / 发布 E2B 沙箱模板（可从仓库复现）。

用法::

    # 候选构建（不移动生产 default tag）：
    E2B_API_KEY=… .venv/bin/python scripts/build-e2b-template.py --tag candidate
    # 在候选沙箱内自检通过后，把 default 移到候选 build：
    .venv/bin/python scripts/build-e2b-template.py --promote-from candidate
    # 回滚：把 default 移回候选构建前的那个 build（构建候选时自动打 previous tag）
    .venv/bin/python scripts/build-e2b-template.py --promote-from previous

为什么用 Python SDK 而不是 `npx @e2b/cli template create`：
CLI 基于 Node 的 fetch，不认 socks5 代理；SDK 走 httpx，装上 socksio 即可在
需要代理的网络里完成构建。构建参数与 docker/e2b/Dockerfile.e2b 是同一份真源。

候选流程（GLINK-PKG-01）：E2B 服务端会把最新构建设成 `default`，所以
``--tag candidate`` 会先把当前 default build 标成 ``previous``，构建后若
default 被服务端移到新 build，就立刻移回旧 build；只有显式
``--promote-from candidate`` 才会让 default 指向新 build。每一步都打印
build id 与 tag 现状，供交付证据记录，不输出任何凭据。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from e2b import Template

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "docker" / "e2b" / "Dockerfile.e2b"
#: 候选构建前的生产 build 会带上这个 tag，作为回滚句柄。
ROLLBACK_TAG = "previous"


def _tags(alias: str) -> dict[str, str]:
    """当前 tag → build id；模板尚未存在时返回空。"""
    try:
        return {entry.tag: entry.build_id for entry in Template.get_tags(alias)}
    except Exception as exc:  # noqa: BLE001 - 新模板第一次构建前没有 tag
        print(f"读取模板 tags 失败（首次构建可忽略）: {type(exc).__name__}: {exc}")
        return {}


def _print_tags(alias: str) -> dict[str, str]:
    tags = _tags(alias)
    for tag in sorted(tags):
        print(f"  tag {tag} -> build {tags[tag]}")
    return tags


def _promote(alias: str, source_tag: str) -> int:
    tags = _tags(alias)
    source = tags.get(source_tag)
    if not source:
        print(f"没有 tag {source_tag}，无法提升 default", file=sys.stderr)
        return 2
    info = Template.assign_tags(f"{alias}:{source_tag}", "default")
    print(f"default -> build {info.build_id}（来自 tag {source_tag}）tags={info.tags}")
    print(f"模板 {alias} 当前 tags：")
    _print_tags(alias)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 pi-glink 的 E2B 模板")
    parser.add_argument("--alias", default=os.environ.get("PI_GLINK_E2B_TEMPLATE", "pi-glink-worker"))
    parser.add_argument("--cpu", type=int, default=2)
    parser.add_argument("--memory-mb", type=int, default=4096)
    parser.add_argument(
        "--tag",
        default="",
        help="候选 tag：只构建不发布；default 会被服务端移动时立即移回构建前的 build",
    )
    parser.add_argument(
        "--promote-from",
        default="",
        help=f"把 default 指向该 tag 当前指向的 build（自检通过后的发布步骤；回滚用 {ROLLBACK_TAG}）",
    )
    parser.add_argument("--logs", action="store_true", help="打印构建日志")
    args = parser.parse_args(argv)

    if not os.environ.get("E2B_API_KEY"):
        print("缺少 E2B_API_KEY", file=sys.stderr)
        return 2

    if args.promote_from:
        return _promote(args.alias, args.promote_from)

    if not DOCKERFILE.is_file():
        print(f"缺少 Dockerfile: {DOCKERFILE}", file=sys.stderr)
        return 2

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("ALL_PROXY")
    print(f"构建模板 alias={args.alias}（cpu={args.cpu}, memory={args.memory_mb}MB）")
    print(f"Dockerfile: {DOCKERFILE.relative_to(REPO_ROOT)}")
    if proxy:
        print(f"代理: {proxy}")

    before = _tags(args.alias)
    producing_default = before.get("default")
    if args.tag and producing_default:
        # 当前生产 build 必须保持可定位：服务端会把新构建设成 default，
        # 没有这个 tag 就没有句柄能把它移回去，也没有回滚句柄。
        Template.assign_tags(f"{args.alias}:default", ROLLBACK_TAG)
        print(f"{ROLLBACK_TAG} -> build {producing_default}（候选构建前的生产 build）")

    builder = Template().from_dockerfile(str(DOCKERFILE))
    on_logs = (lambda entry: print(f"  [{entry.level}] {entry.message}")) if args.logs else None
    info = Template.build(
        builder,
        alias=args.alias,
        tags=[args.tag] if args.tag else None,
        cpu_count=args.cpu,
        memory_mb=args.memory_mb,
        on_build_logs=on_logs,
    )
    print(f"构建完成: template={info.name} buildId={info.build_id} tags={info.tags}")

    tags = _print_tags(args.alias)
    if args.tag and producing_default and tags.get("default") == info.build_id:
        # 服务端把候选设成了生产 default：立刻移回，等自检通过再 promote。
        Template.assign_tags(f"{args.alias}:{ROLLBACK_TAG}", "default")
        print(f"default 已移回 build {producing_default}（候选 build {info.build_id} 仅带 tag {args.tag}）")
        _print_tags(args.alias)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
