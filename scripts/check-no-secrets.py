#!/usr/bin/env python3
"""扫描运行态产物，确认凭据没有留在事件日志、报告或会话文件里。

用法::

    .venv/bin/python scripts/check-no-secrets.py [--data-root ~/.pi-glink]

凭据来源与宿主一致：`.env` 的 `OPENAI_API_KEY` / `E2B_API_KEY` /
`PI_GLINK_WEB_SEARCH_KEY`，以及运行期 `pi-agent/web-search.json`。
另外按形状扫描（sk-…、e2b_…、exa-…、NAME=value、Bearer …），
因此即使是宿主从未见过的第三方 token 也会被检出。

命中即 exit 1，并打印**脱敏后**的片段（不回声真实值）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "host"))

from pi_glink.config import build_config  # noqa: E402
from pi_glink.dotenv import parse_env_file  # noqa: E402
from pi_glink.redaction import (  # noqa: E402
    SecretRedactor,
    find_credential_shapes,
    is_placeholder_value,
)

#: File names worth scanning inside the data root.
_TARGET_GLOBS = (
    "runs/*/events.jsonl",
    "runs/*/meta.json",
    "workspaces/*/*.md",
    "workspaces/*/*.txt",
    "sessions/**/*.jsonl",
)


def build_redactor(repo_root: Path, data_root: Path) -> SecretRedactor:
    values = parse_env_file(repo_root / ".env")
    config = build_config(values, repo_root)
    redactor = SecretRedactor()
    for attribute in ("openai_api_key", "e2b_api_key", "web_search_key"):
        redactor.add(getattr(config, attribute, None))
    redactor.add_from_file(data_root / "pi-agent" / "web-search.json")
    return redactor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫描运行态产物里的凭据残留")
    parser.add_argument("--data-root", default=str(Path.home() / ".pi-glink"))
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument(
        "--show-placeholders",
        action="store_true",
        help="同时列出文档/示例形态的占位符命中（不计入失败）",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).expanduser()
    data_root = Path(args.data_root).expanduser()
    redactor = build_redactor(repo_root, data_root)
    known_values = sorted(redactor.known_values, key=len, reverse=True)

    if not data_root.is_dir():
        print(f"数据根不存在，跳过：{data_root}")
        return 0

    scanned = 0
    leaks: list[tuple[Path, int, str, str]] = []      # 真实凭据值
    shapes: list[tuple[Path, int, str, str]] = []     # 形态命中（占位符）
    for pattern in _TARGET_GLOBS:
        for path in sorted(data_root.glob(pattern)):
            if not path.is_file():
                continue
            scanned += 1
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                print(f"  读取失败 {path}: {exc}")
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                # 1) 真实凭据值：最硬的判据，直接命中即失败。
                if any(value in line for value in known_values):
                    leaks.append((path, number, "value", redactor.redact(line)[:160]))
                    continue
                # 2) 形态命中：排除文档/示例占位符（agent 读 README 会产生很多）。
                for candidate in find_credential_shapes(line):
                    if is_placeholder_value(candidate):
                        shapes.append((path, number, "placeholder", redactor.redact(line)[:120]))
                    else:
                        leaks.append((path, number, "shape", redactor.redact(line)[:160]))
                    break

    print(
        f"凭据来源 {len(known_values)} 条 · 扫描文件 {scanned} 个 · "
        f"真实命中 {len(leaks)} 处 · 占位符命中 {len(shapes)} 处"
    )
    if shapes and args.show_placeholders:
        print("\n[占位符，不计入失败]")
        for path, number, _kind, preview in shapes[:10]:
            print(f"  ok  {path}:{number}\n       {preview}")
    for path, number, kind, preview in leaks[:20]:
        print(f"  LEAK[{kind}] {path}:{number}\n       {preview}")
    if leaks:
        print(f"\n发现 {len(leaks)} 处凭据残留（已脱敏展示）。")
        return 1
    print("未发现真实凭据残留。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
