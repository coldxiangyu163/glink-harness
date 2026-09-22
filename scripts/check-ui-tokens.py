#!/usr/bin/env python3
"""Check the workbench token layer against the 百鉴 UI standard library.

`web/styles.css` copies the standard library's token values verbatim (palette,
semantic roles, scale, typography, elevation, motion). This script is the guard
that keeps the copy honest: it resolves both stylesheets' custom properties and
fails on any value that drifted.

Usage:

    scripts/check-ui-tokens.py                        # default library path
    scripts/check-ui-tokens.py --library <repo>       # explicit path
    scripts/check-ui-tokens.py --print-diff           # show every comparison

Exit codes: 0 = aligned, 1 = drift, 2 = library stylesheet not found.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_LIBRARY = Path("/Users/coldxiangyu/work/BR/glink-ui-library")
REPO_ROOT = Path(__file__).resolve().parent.parent
WORKBENCH_CSS = REPO_ROOT / "web" / "styles.css"
LIBRARY_CSS = Path("src/styles/globals.css")

#: Tokens the workbench consumes. Anything not listed here is the library's
#: business, not the workbench's — the guard only checks what it actually uses.
CONTRACT: tuple[str, ...] = (
    # L0.1 palette
    *[f"--n-{step}" for step in (0, 50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950)],
    *[f"--p-{step}" for step in (50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950)],
    *[f"--su-{name}" for name in ("base", "hover", "active", "soft", "soft-hover", "border")],
    *[f"--wa-{name}" for name in ("base", "hover", "active", "soft", "soft-hover", "border")],
    *[f"--de-{name}" for name in ("base", "hover", "active", "soft", "soft-hover", "border")],
    "--n-900-rgb",
    "--n-scrim-rgb",
    # L0.2 semantic (light)
    "--background",
    "--foreground",
    "--card",
    "--card-foreground",
    "--popover",
    "--popover-foreground",
    "--overlay-scrim",
    "--primary",
    "--primary-hover",
    "--primary-active",
    "--primary-foreground",
    "--primary-soft",
    "--primary-soft-hover",
    "--primary-border",
    "--primary-border-hover",
    "--primary-emphasis",
    "--primary-link",
    "--secondary",
    "--secondary-hover",
    "--secondary-active",
    "--secondary-foreground",
    "--muted",
    "--muted-foreground",
    "--accent",
    "--accent-foreground",
    "--surface-disabled",
    "--ink-3",
    "--hover",
    "--hover-2",
    "--destructive",
    "--destructive-hover",
    "--destructive-active",
    "--destructive-foreground",
    "--destructive-soft",
    "--destructive-soft-hover",
    "--destructive-border",
    "--destructive-text",
    "--success",
    "--success-hover",
    "--success-active",
    "--success-foreground",
    "--success-soft",
    "--success-soft-hover",
    "--success-border",
    "--success-text",
    "--warning",
    "--warning-hover",
    "--warning-active",
    "--warning-foreground",
    "--warning-soft",
    "--warning-soft-hover",
    "--warning-border",
    "--warning-text",
    "--info",
    "--info-hover",
    "--info-active",
    "--info-foreground",
    "--info-soft",
    "--info-soft-hover",
    "--info-border",
    "--info-text",
    "--border",
    "--border-strong",
    "--input",
    "--ring",
    "--chart-1",
    "--chart-2",
    "--chart-3",
    "--chart-4",
    "--chart-5",
    "--sidebar",
    "--sidebar-soft",
    "--sidebar-foreground",
    "--sidebar-accent",
    "--sidebar-accent-foreground",
    "--sidebar-border",
    "--sidebar-ring",
    "--sidebar-brand-bg",
    "--sidebar-brand-bg-hover",
    "--sidebar-active-text",
    # elevation
    "--elev-hairline",
    "--elev-control",
    "--elev-thumb",
    "--elev-button",
    "--elev-surface",
    "--elev-card-hover",
    "--elev-floating",
    "--elev-overlay",
    "--elev-sidebar",
    # scale
    *[f"--control-height-{name}" for name in ("3xs", "2xs", "xs", "sm", "md", "lg")],
    *[f"--corner-{name}" for name in ("xs", "sm", "md", "lg", "xl", "2xl", "3xl", "full")],
    *[f"--space-{name}" for name in ("0", "0_5", "1", "1_5", "2", "3", "4", "5", "6", "7", "8", "10", "12")],
    # typography
    *[f"--text-{name}" for name in ("mini", "caption", "body", "body-lg", "title-lg", "h1", "h0", "display")],
    # motion
    *[f"--dur-{step}" for step in (1, 2, 3, 4, 5)],
    "--ease-strong",
    "--ease-soft",
)

_DECLARATION_RE = re.compile(r"(?P<name>--[A-Za-z0-9_-]+)\s*:\s*(?P<value>[^;]+);")
_VAR_RE = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)\s*\)")


def block(css: str, selector: str) -> str:
    """Body of the first ``selector { … }`` block, braces balanced.

    Parsed block-by-block rather than file-wide on purpose: the standard library
    redefines the whole semantic layer inside ``.dark``, and a file-wide scan
    would compare the workbench's light theme against the library's dark one.
    """
    search_from = 0
    while True:
        start = css.find(selector, search_from)
        if start == -1:
            return ""
        brace = css.find("{", start)
        if brace == -1:
            return ""
        if css[start + len(selector) : brace].strip():
            search_from = brace
            continue
        depth = 0
        for index in range(brace, len(css)):
            if css[index] == "{":
                depth += 1
            elif css[index] == "}":
                depth -= 1
                if depth == 0:
                    return css[brace + 1 : index]
        return ""


def declarations(css: str) -> dict[str, str]:
    """Every custom property declared in the given block, normalised."""
    found: dict[str, str] = {}
    for match in _DECLARATION_RE.finditer(css):
        found[match.group("name")] = normalise(match.group("value"))
    return found


def normalise(value: str) -> str:
    text = " ".join(value.split())
    # `calc(infinity * 1px)` and `calc(infinity*1px)` are the same value.
    text = re.sub(r"\s*\*\s*", "*", text)
    return text


def resolve(name: str, table: dict[str, str], seen: tuple[str, ...] = ()) -> str:
    """Resolve ``var()`` references so a copy can be compared by value."""
    if name in seen:  # cycle: report it instead of recursing forever
        return table.get(name, "")
    value = table.get(name)
    if value is None:
        return ""

    def replace(match: re.Match[str]) -> str:
        inner = resolve(match.group(1), table, (*seen, name))
        return inner or match.group(0)

    return normalise(_VAR_RE.sub(replace, value))


def compare(workbench: dict[str, str], library: dict[str, str]) -> tuple[list[str], list[str], int]:
    mismatches: list[str] = []
    missing: list[str] = []
    checked = 0
    for token in CONTRACT:
        if token not in library:
            continue
        checked += 1
        expected = resolve(token, library)
        if token not in workbench:
            missing.append(f"{token}: missing (library: {expected})")
            continue
        actual = resolve(token, workbench)
        if actual != expected:
            mismatches.append(f"{token}: workbench={actual} library={expected}")
    return mismatches, missing, checked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library", type=Path, default=DEFAULT_LIBRARY)
    parser.add_argument("--print-diff", action="store_true", help="print the full comparison")
    args = parser.parse_args(argv)

    library_path = args.library / LIBRARY_CSS if args.library.is_dir() else args.library
    if not library_path.is_file():
        print(f"[skip] 标准库 token 文件不存在：{library_path}", file=sys.stderr)
        return 2
    if not WORKBENCH_CSS.is_file():  # pragma: no cover - repository invariant
        print(f"[fail] 缺少工作台样式：{WORKBENCH_CSS}", file=sys.stderr)
        return 1

    workbench = declarations(block(WORKBENCH_CSS.read_text(encoding="utf-8"), ":root"))
    library_css = library_path.read_text(encoding="utf-8")
    library = {
        **declarations(block(library_css, ":root")),
        # 排版阶梯与阴影映射声明在 @theme inline 里
        **declarations(block(library_css, "@theme inline")),
    }
    mismatches, missing, checked = compare(workbench, library)

    if args.print_diff:
        for token in CONTRACT:
            if token not in library:
                continue
            print(f"{token}\t{resolve(token, library)}\t{resolve(token, workbench)}")

    if mismatches or missing:
        print(f"[fail] {len(mismatches) + len(missing)} 项 token 与标准库不一致（共比对 {checked} 项）")
        for line in (*missing, *mismatches):
            print(f"  - {line}")
        return 1

    print(f"[ok] {checked} 项 token 与标准库逐值一致（{library_path}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
