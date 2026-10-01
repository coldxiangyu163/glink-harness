#!/usr/bin/env python3
"""构建 / 发布 E2B 沙箱模板（可从仓库复现）。

用法::

    # 候选构建（不移动生产 default tag；--template 缺省且未设 GLINK_E2B_TEMPLATE_KEY
    # 时跟随 GLINK_HARNESS 所选包的 [image].target，默认包为 dsh → acp-worker；
    # 包名/清单无效即拒绝，不回退 pi-worker）：
    E2B_API_KEY=… .venv/bin/python scripts/build-e2b-template.py --tag candidate
    # 显式选择模板（sdk 构建方式的条目才可由本脚本构建）：
    E2B_API_KEY=… .venv/bin/python scripts/build-e2b-template.py --template pi-worker --tag candidate
    # 在候选沙箱内自检通过后，把 default 移到候选 build：
    .venv/bin/python scripts/build-e2b-template.py --promote-from candidate
    # 回滚：把 default 移回候选构建前的那个 build（构建候选时自动打 previous tag）
    .venv/bin/python scripts/build-e2b-template.py --promote-from previous

为什么用 Python SDK 而不是 `npx @e2b/cli template create`：
CLI 基于 Node 的 fetch，不认 socks5 代理；SDK 走 httpx，装上 socksio 即可在
需要代理的网络里完成构建。别名、Dockerfile 与 CPU / 内存默认值读取生成产物
docker/e2b/templates.json 的所选条目（由 scripts/generate-pack-artifacts.py
从 harnesses/<包>/harness.toml 生成），不在本脚本里另写一份；``builder=cli``
的条目（acp-worker）只能用它自己的 build_command（E2B CLI）构建，本脚本
会拒绝并提示对应命令。

候选流程（GLINK-PKG-01）：已有 default 的 alias 上，E2B 服务端会把最新构建设成
`default`，所以 ``--tag candidate`` 会先把当前 default build 标成 ``previous``，
构建后若 default 被服务端移到新 build，就立刻移回旧 build；新 alias 的首建若只带
候选 tag，服务端**不**设 default（2026-10-01 glink-harness-worker 首建实测），
candidate 确认后退出并提示显式 promote。只有显式
``--promote-from candidate`` 才会让 default 指向新 build。每一步都打印
build id 与 tag 现状，供交付证据记录，不输出任何凭据。只有构建前 SDK
明确 404 才视为首次构建，其它查询错误拒绝构建；构建/移回/promote 后必须读回
确认最终 tag 指向，否则非零退出并要求人工核对，不能将构建成功等同发布已确认。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from e2b import Template
from e2b.exceptions import SandboxException

REPO_ROOT = Path(__file__).resolve().parents[1]
# 复用宿主的包清单 fail-closed 解析（与 scripts/generate-pack-artifacts.py 同一入口），
# 不在本脚本另建第二套清单校验规则。
sys.path.insert(0, str(REPO_ROOT / "host"))
from glink_harness.harnesses import HarnessPackError, load_pack  # noqa: E402

#: 生成产物：E2B 模板参数（清单派生，见 scripts/generate-pack-artifacts.py）。
TEMPLATES = REPO_ROOT / "docker" / "e2b" / "templates.json"
#: 候选构建前的生产 build 会带上这个 tag，作为回滚句柄。
ROLLBACK_TAG = "previous"


def _default_template_key() -> str:
    """所选 harness 包的镜像目标（缺省包 dsh → acp-worker）；无效输入即拒绝。

    只在没有任何显式模板选择（``--template`` / ``GLINK_E2B_TEMPLATE_KEY``）时
    调用。包名非法、清单不可读/损坏/缺 ``[image].target`` 都直接拒绝——
    绝不回退 pi-worker：默认路径上选错目标意味着真实构建/改 tag 作用在错误
    模板上（fail-closed，与 harness 选择不自动回退的原义务一致）。
    """
    pack = (os.environ.get("GLINK_HARNESS") or "dsh").strip().lower() or "dsh"
    raw_dir = (os.environ.get("GLINK_HARNESSES_DIR") or "").strip()
    if raw_dir:
        harnesses_dir = Path(raw_dir).expanduser()
        if not harnesses_dir.is_absolute():
            harnesses_dir = REPO_ROOT / harnesses_dir
    else:
        harnesses_dir = REPO_ROOT / "harnesses"
    try:
        target = load_pack(harnesses_dir, pack).image.target
    except HarnessPackError as exc:
        raise SystemExit(
            f"无法从 GLINK_HARNESS={pack!r} 的包清单推断构建目标，拒绝继续：{exc}"
        ) from exc
    templates = json.loads(TEMPLATES.read_text(encoding="utf-8"))["templates"]
    if target not in templates:
        raise SystemExit(
            f"包 {pack!r} 的镜像目标 {target!r} 不在 {TEMPLATES.name} 中"
            f"（可用：{'、'.join(sorted(templates))}），拒绝继续"
        )
    return target


def _template_entry(key: str) -> dict:
    templates = json.loads(TEMPLATES.read_text(encoding="utf-8"))["templates"]
    if key not in templates:
        raise SystemExit(
            f"未知模板 {key!r}；可用：{'、'.join(sorted(templates))}"
        )
    return templates[key]


def _tags(alias: str, *, allow_missing: bool = False) -> dict[str, str]:
    """Only a pre-build SDK 404 means a genuinely new template.

    E2B get_tags uses TemplateException with status_code=404 (not the
    sandbox kill API's SandboxNotFoundException contract).
    """
    try:
        return {entry.tag: entry.build_id for entry in Template.get_tags(alias)}
    except SandboxException as exc:
        if allow_missing and exc.status_code == 404:
            return {}
        raise


def _confirmed_tags(alias: str, expected_default: str) -> dict[str, str]:
    tags = _tags(alias)
    if tags.get("default") != expected_default:
        raise RuntimeError("default build 未指向预期 build")
    for tag in sorted(tags):
        print(f"  tag {tag} -> build {tags[tag]}")
    return tags


def _promote(alias: str, source_tag: str) -> int:
    source = None
    try:
        source = _tags(alias).get(source_tag)
        if not source:
            print(f"没有 tag {source_tag}，无法提升 default", file=sys.stderr)
            return 2
        info = Template.assign_tags(f"{alias}:{source_tag}", "default")
        if info.build_id != source:
            raise RuntimeError("发布期间源 tag 已改变")
        _confirmed_tags(alias, source)
    except Exception as exc:  # noqa: BLE001 - a publishing command must fail closed
        print(f"发布未确认（{type(exc).__name__}）；请人工核对模板 {alias} 的 default，"
              f"预期 build={source or '未知'}。", file=sys.stderr)
        return 1
    print(f"default 已确认 -> build {source}（来自 tag {source_tag}）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建 glink-harness 的 E2B 模板")
    parser.add_argument(
        "--template",
        default=None,
        help="templates.json 里的模板键；缺省且未设 GLINK_E2B_TEMPLATE_KEY 时，"
        "才解析 GLINK_HARNESS 所选包的 [image].target（解析失败即拒绝，不回退）",
    )
    parser.add_argument("--alias", default="", help="覆盖模板 alias（默认取所选条目的 alias；GLINK_E2B_TEMPLATE 同效）")
    parser.add_argument("--cpu", type=int, default=0, help="覆盖 CPU 数（默认取所选条目）")
    parser.add_argument("--memory-mb", type=int, default=0, help="覆盖内存 MB（默认取所选条目）")
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

    # 惰性解析：用户显式选择模板时不触碰包清单，避免无关的 harness 默认误拒。
    template_key = args.template or (os.environ.get("GLINK_E2B_TEMPLATE_KEY") or "").strip()
    if not template_key:
        template_key = _default_template_key()
    entry = _template_entry(template_key)
    dockerfile = REPO_ROOT / entry["dockerfile"]
    args.alias = args.alias or os.environ.get("GLINK_E2B_TEMPLATE", entry["alias"])
    args.cpu = args.cpu or entry["cpu_count"]
    args.memory_mb = args.memory_mb or entry["memory_mb"]

    if not os.environ.get("E2B_API_KEY"):
        print("缺少 E2B_API_KEY", file=sys.stderr)
        return 2

    if args.promote_from:
        return _promote(args.alias, args.promote_from)
    if entry.get("builder") != "sdk":
        print(
            f"模板 {template_key} 由 E2B CLI 构建（builder=cli），本脚本只覆盖 SDK 面；"
            f"请改用生成产物里的 build_command：{entry.get('build_command', '（无）')}",
            file=sys.stderr,
        )
        return 2
    if args.tag in {"default", ROLLBACK_TAG}:
        print("候选 tag 不能覆盖 default 或 previous。", file=sys.stderr)
        return 2

    if not dockerfile.is_file():
        print(f"缺少 Dockerfile: {dockerfile}", file=sys.stderr)
        return 2

    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("ALL_PROXY")
    print(f"构建模板 alias={args.alias}（cpu={args.cpu}, memory={args.memory_mb}MB）")
    print(f"Dockerfile: {dockerfile.relative_to(REPO_ROOT)}")
    if proxy:
        print("代理: 已启用（不输出地址或凭据）")

    try:
        before = _tags(args.alias, allow_missing=True)
        producing_default = before.get("default")
        if args.tag and producing_default:
            info = Template.assign_tags(f"{args.alias}:default", ROLLBACK_TAG)
            if info.build_id != producing_default:
                raise RuntimeError("previous build 不匹配")
            if _tags(args.alias).get(ROLLBACK_TAG) != producing_default:
                raise RuntimeError("previous tag 未确认")
    except Exception as exc:  # noqa: BLE001 - uncertainty must not start a build
        print(f"无法确认生产 tags（{type(exc).__name__}），拒绝构建。", file=sys.stderr)
        return 1

    try:
        builder = Template().from_dockerfile(str(dockerfile))
        on_logs = (lambda entry: print(f"  [{entry.level}] {entry.message}")) if args.logs else None
        info = Template.build(
            builder, alias=args.alias, tags=[args.tag] if args.tag else None,
            cpu_count=args.cpu, memory_mb=args.memory_mb, on_build_logs=on_logs,
        )
        expected = producing_default if args.tag and producing_default else info.build_id
        tags = _tags(args.alias)  # no post-build error (including 404) is a first build
        if args.tag and tags.get(args.tag) != info.build_id:
            raise RuntimeError("候选 tag 未确认")
        if producing_default is None and args.tag:
            # 新 alias 的首建若只带候选 tag，服务端可能不设 default（2026-10-01
            # glink-harness-worker 首建实测仅有 candidate），也可能把 default 直接
            # 指向新 build；两者都是合法首建。default 指向别的 build 才是非预期。
            actual_default = tags.get("default")
            if actual_default is not None and actual_default != info.build_id:
                raise RuntimeError("首建 default 指向了别的 build")
            if actual_default is None:
                print(
                    f"首建完成：{args.tag} -> build {info.build_id}；default 未设置，"
                    f"沙箱自检通过后用 --promote-from {args.tag} 发布。"
                )
                return 0
        if tags.get("default") != expected:
            # Restore only a known candidate change; do not overwrite a third
            # build possibly published by another operator.
            if producing_default and args.tag and tags.get("default") == info.build_id:
                info_restore = Template.assign_tags(f"{args.alias}:{ROLLBACK_TAG}", "default")
                if info_restore.build_id != producing_default:
                    raise RuntimeError("回滚句柄已改变")
            else:
                raise RuntimeError("default 出现非预期变更")
        _confirmed_tags(args.alias, expected)
    except Exception as exc:  # noqa: BLE001 - a build is not a verified publication
        print(f"构建/发布未确认（{type(exc).__name__}）；请人工核对模板 {args.alias} 的 tags，"
              f"构建前 default={producing_default or '无'}。不得视为验收通过。", file=sys.stderr)
        return 1
    print(f"构建与 tags 已确认: template={info.name} buildId={info.build_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
