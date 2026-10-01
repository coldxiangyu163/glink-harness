#!/usr/bin/env python3
"""从 harness 包清单生成镜像构建产物（清单是唯一真源）。

生成且入库的产物：

* 四个 Dockerfile 里的 ``ARG <NAME>_VERSION=<version>`` 行——值取自声明该镜像
  （``[image].target``）的包清单 ``[[image.packages]]``；
* ``docker/e2b/templates.json``——每个 worker 镜像的 E2B 模板参数（别名、
  Dockerfile、CPU / 内存下限、构建参数与构建命令）；
* pi 族包目录下 ``settings.json`` 的 ``packages`` 字段——``role = "pi-package"``
  条目的 ``npm:<name>@<version>``。

用法::

    python3 scripts/generate-pack-artifacts.py          # 改写产物
    python3 scripts/generate-pack-artifacts.py --check  # 只比对，漂移时 exit 1

规则（任一违反即报错、不写任何文件）：

* 已声明的 ARG 在所属镜像的每个 Dockerfile 里必须**恰好出现一次**；
* Dockerfile 里出现的 ``ARG *_VERSION`` 必须由某个清单声明（未声明的版本钉点
  不允许手写，否则又多出一处真源）；
* 同一镜像内同名 ARG 被不同包声明为不同包名 / 版本即冲突。

Node 基础镜像 tag 与镜像源 ARG（``DEBIAN_MIRROR`` / ``NPM_REGISTRY``）不是包的
属性，仍在 Dockerfile 里手写，由 tests/test_repository_boundaries.py 另行约束。
本脚本不构建镜像、不访问网络。
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "host"))

from glink_harness.harnesses import (  # noqa: E402
    IMAGE_TARGETS,
    HarnessPack,
    HarnessPackError,
    load_pack,
    pack_names,
)

#: E2B 模板清单（生成产物）的相对路径与格式标记。
TEMPLATES_PATH = "docker/e2b/templates.json"
TEMPLATES_FORMAT = "glink-harness-e2b-templates/v1"


@dataclass(frozen=True)
class ImageBuild:
    """一个 worker 镜像的仓库构建知识（不是包的属性）。"""

    dockerfiles: tuple[str, ...]
    e2b_alias: str
    e2b_dockerfile: str
    #: 构建方式：``sdk``（scripts/build-e2b-template.py）或 ``cli``（E2B CLI；
    #: ACP 模板必须用 CLI，Python SDK 的 from_dockerfile 会丢多行 ENV）。
    e2b_builder: str


#: 每个 ``[image].target`` 对应的 Dockerfile 与 E2B 模板。键集合必须与
#: :data:`glink_harness.harnesses.IMAGE_TARGETS` 一致。
IMAGES: dict[str, ImageBuild] = {
    "pi-worker": ImageBuild(
        dockerfiles=("docker/Dockerfile.worker", "docker/e2b/Dockerfile.e2b"),
        e2b_alias="glink-harness-worker",
        e2b_dockerfile="docker/e2b/Dockerfile.e2b",
        e2b_builder="sdk",
    ),
    "acp-worker": ImageBuild(
        dockerfiles=("docker/Dockerfile.acp-worker", "docker/e2b/Dockerfile.acp-e2b"),
        e2b_alias="glink-acp-worker",
        e2b_dockerfile="docker/e2b/Dockerfile.acp-e2b",
        e2b_builder="cli",
    ),
}

_ARG_LINE_RE = re.compile(r"^ARG ([A-Z][A-Z0-9_]*_VERSION)=(\S*)[ \t]*$", re.MULTILINE)


class GenerationError(RuntimeError):
    """清单与产物无法一致生成（冲突、缺失或未声明的钉点）。"""


def load_packs(harnesses_dir: Path) -> tuple[HarnessPack, ...]:
    packs: list[HarnessPack] = []
    for name in pack_names(harnesses_dir):
        try:
            packs.append(load_pack(harnesses_dir, name))
        except HarnessPackError as exc:
            raise GenerationError(str(exc)) from exc
    if not packs:
        raise GenerationError(f"{harnesses_dir} 下没有任何 harness 包")
    return tuple(packs)


def image_build_args(packs: tuple[HarnessPack, ...], target: str) -> dict[str, str]:
    """一个镜像的 ``ARG -> version``；同名 ARG 声明冲突即报错。"""
    declared: dict[str, tuple[str, str, str]] = {}
    for pack in packs:
        if pack.image.target != target:
            continue
        for item in pack.image.packages:
            previous = declared.get(item.arg)
            if previous is not None and previous[:2] != (item.package, item.version):
                raise GenerationError(
                    f"镜像 {target} 的 ARG {item.arg} 声明冲突："
                    f"{previous[2]} 声明 {previous[0]}@{previous[1]}，"
                    f"{pack.name} 声明 {item.package}@{item.version}"
                )
            declared[item.arg] = (item.package, item.version, pack.name)
    return {arg: value[1] for arg, value in sorted(declared.items())}


def render_dockerfile(text: str, build_args: dict[str, str], where: str) -> str:
    """把 ``ARG <NAME>_VERSION=`` 行改写为清单声明的版本。"""
    seen: dict[str, int] = {}
    for match in _ARG_LINE_RE.finditer(text):
        name = match.group(1)
        if name not in build_args:
            raise GenerationError(
                f"{where} 的 ARG {name} 没有任何包清单声明（版本钉点只能来自 harness.toml）"
            )
        seen[name] = seen.get(name, 0) + 1
    for name in build_args:
        count = seen.get(name, 0)
        if count != 1:
            raise GenerationError(
                f"{where} 中 ARG {name}= 必须恰好出现一次（实际 {count} 次）"
            )
    return _ARG_LINE_RE.sub(lambda m: f"ARG {m.group(1)}={build_args[m.group(1)]}", text)


def render_templates(packs: tuple[HarnessPack, ...]) -> str:
    templates: dict[str, object] = {}
    for target, build in IMAGES.items():
        members = [pack for pack in packs if pack.image.target == target]
        if not members:
            continue
        cpu = max(pack.image.cpu_count for pack in members)
        memory = max(pack.image.memory_mb for pack in members)
        if build.e2b_builder == "cli":
            command = shlex.join(
                [
                    "npx", "-y", "@e2b/cli", "template", "create", build.e2b_alias,
                    "--dockerfile", build.e2b_dockerfile,
                    "--cpu-count", str(cpu), "--memory-mb", str(memory),
                ]
            )
        else:
            command = (
                f".venv/bin/python scripts/build-e2b-template.py --template {target}"
                " --tag candidate"
            )
        templates[target] = {
            "alias": build.e2b_alias,
            "dockerfile": build.e2b_dockerfile,
            "cpu_count": cpu,
            "memory_mb": memory,
            "packs": sorted(pack.name for pack in members),
            "build_args": image_build_args(packs, target),
            "builder": build.e2b_builder,
            "build_command": command,
        }
    document = {
        "format": TEMPLATES_FORMAT,
        "generated_by": "scripts/generate-pack-artifacts.py（勿手改；改 harnesses/<包>/harness.toml 后重新生成）",
        "templates": templates,
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def render_settings(text: str, pack: HarnessPack, where: str) -> str:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GenerationError(f"{where} 不是合法 JSON：{exc}") from exc
    if not isinstance(document, dict):
        raise GenerationError(f"{where} 必须是 JSON 对象")
    document["packages"] = [
        f"npm:{item.package}@{item.version}" for item in pack.image.by_role("pi-package")
    ]
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def render_all(repo_root: Path, harnesses_dir: Path | None = None) -> dict[str, str]:
    """所有生成产物：``相对路径 -> 期望内容``（从当前文件与清单渲染，不写盘）。"""
    if set(IMAGES) != set(IMAGE_TARGETS):
        raise GenerationError("生成器的镜像表与 glink_harness.harnesses.IMAGE_TARGETS 不一致")
    root = Path(repo_root)
    packs = load_packs(Path(harnesses_dir) if harnesses_dir else root / "harnesses")
    outputs: dict[str, str] = {}
    for target, build in IMAGES.items():
        build_args = image_build_args(packs, target)
        for relative in build.dockerfiles:
            path = root / relative
            if not path.is_file():
                raise GenerationError(f"缺少 Dockerfile：{relative}")
            outputs[relative] = render_dockerfile(
                path.read_text(encoding="utf-8"), build_args, relative
            )
    outputs[TEMPLATES_PATH] = render_templates(packs)
    for pack in packs:
        if pack.family != "pi":
            continue
        settings = pack.directory / "settings.json"
        if not settings.is_file():
            continue
        relative = settings.relative_to(root).as_posix() if settings.is_relative_to(root) else str(settings)
        outputs[relative] = render_settings(settings.read_text(encoding="utf-8"), pack, relative)
    return outputs


def drift(repo_root: Path, outputs: dict[str, str]) -> list[str]:
    """与仓库现状不一致的产物路径。"""
    stale: list[str] = []
    for relative, expected in outputs.items():
        path = Path(repo_root) / relative
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        if current != expected:
            stale.append(relative)
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 harness 包清单生成镜像构建产物")
    parser.add_argument("--check", action="store_true", help="只比对，产物漂移时 exit 1")
    args = parser.parse_args(argv)
    try:
        outputs = render_all(REPO_ROOT)
    except GenerationError as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        return 2
    stale = drift(REPO_ROOT, outputs)
    if args.check:
        for relative in stale:
            print(f"产物与清单不一致：{relative}", file=sys.stderr)
        return 1 if stale else 0
    for relative in stale:
        (REPO_ROOT / relative).write_text(outputs[relative], encoding="utf-8")
        print(f"已生成 {relative}")
    if not stale:
        print("产物已与清单一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
