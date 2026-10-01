#!/usr/bin/env python3
"""扫描运行态产物，确认凭据没有留在事件日志、报告或会话文件里。

用法::

    .venv/bin/python scripts/check-no-secrets.py [--data-root ~/.glink-harness]

凭据来源与宿主一致：`.env` 的 `OPENAI_API_KEY` / `E2B_API_KEY` /
`GLINK_WEB_SEARCH_KEY`。
另外按形状扫描（sk-…、e2b_…、exa-…、NAME=value、Bearer …），
因此即使是宿主从未见过的第三方 token 也会被检出。

run 记录与事件存放在 SQLite（`runs/runs.db`）。数据库文件按页组织，较大的
记录会被拆到 overflow page（每页开头有页指针），WAL 也按页记录；直接把文件当
文本扫描会让跨页的凭据被截断而漏报。因此数据库以只读方式经 sqlite3 打开，
逐行扫描 `runs.record` 与 `events.body` 的完整值（WAL 由 SQLite 自身合并），
不直接读取 `runs.db` / `runs.db-wal` 的字节。

命中即 exit 1，并打印**脱敏后**的片段（不回声真实值）。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "host"))

from glink_harness.config import build_config  # noqa: E402
from glink_harness.dotenv import parse_env_file  # noqa: E402
from glink_harness.redaction import (  # noqa: E402
    SecretRedactor,
    find_credential_shapes,
    is_placeholder_value,
)

#: The run database, relative to the data root (``store.DATABASE_NAME``).
_DATABASE = "runs/runs.db"
#: Stored text columns of the run database, scanned value by value.
_DATABASE_COLUMNS = (
    ("runs", "record", "run_id"),
    ("events", "body", "run_id || '#' || seq"),
)
#: Plain-text files worth scanning inside the data root.
_TARGET_GLOBS = (
    "runs/*/artifacts/*.md",
    "runs/*/sessions/**/*.jsonl",
    "workspaces/*/*.md",
    "workspaces/*/*.txt",
)


def build_redactor(repo_root: Path, data_root: Path) -> SecretRedactor:
    values = parse_env_file(repo_root / ".env")
    config = build_config(values, repo_root)
    redactor = SecretRedactor()
    for attribute in ("openai_api_key", "e2b_api_key", "web_search_key"):
        redactor.add(getattr(config, attribute, None))
    # Rendered run files carry only revoked egress tokens (docs/adr/0008); the
    # real keys to look for are the host configuration's own.
    return redactor


def _database_values(path: Path) -> Iterator[tuple[str, str]]:
    """Yield ``(location, text)`` for every stored record and event body.

    Opened read-only through SQLite so overflow pages and WAL frames are
    reassembled into whole values; raw file bytes are never matched.
    """
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        for table, column, key in _DATABASE_COLUMNS:
            query = f"SELECT {key}, {column} FROM {table}"  # noqa: S608 - fixed names
            for row_key, value in connection.execute(query):
                if isinstance(value, bytes):
                    value = value.decode("utf-8", errors="replace")
                yield f"{path}[{table}.{column} {row_key}]", str(value)
    finally:
        connection.close()


def _file_values(data_root: Path) -> Iterator[tuple[str, str]]:
    for pattern in _TARGET_GLOBS:
        for path in sorted(data_root.glob(pattern)):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                print(f"  读取失败 {path}: {exc}")
                continue
            yield str(path), text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="扫描运行态产物里的凭据残留")
    parser.add_argument("--data-root", default=str(Path.home() / ".glink-harness"))
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
    leaks: list[tuple[str, int, str, str]] = []      # 真实凭据值
    shapes: list[tuple[str, int, str, str]] = []     # 形态命中（占位符）

    def scan(location: str, text: str) -> None:
        for number, line in enumerate(text.splitlines(), start=1):
            # 1) 真实凭据值：最硬的判据，直接命中即失败。
            if any(value in line for value in known_values):
                leaks.append((location, number, "value", redactor.redact(line)[:160]))
                continue
            # 2) 形态命中：排除文档/示例占位符（agent 读 README 会产生很多）。
            for candidate in find_credential_shapes(line):
                if is_placeholder_value(candidate):
                    shapes.append((location, number, "placeholder", redactor.redact(line)[:120]))
                else:
                    leaks.append((location, number, "shape", redactor.redact(line)[:160]))
                break

    database = data_root / _DATABASE
    if database.is_file():
        scanned += 1
        try:
            for location, text in _database_values(database):
                scan(location, text)
        except sqlite3.Error as exc:
            # 读不了数据库就不能声称无泄漏：按失败处理。
            print(f"  数据库读取失败 {database}: {exc}")
            return 1
    for location, text in _file_values(data_root):
        scanned += 1
        scan(location, text)

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
