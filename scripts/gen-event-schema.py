#!/usr/bin/env python3
"""Render the product event schema into the front-end's generated module.

Source: ``host/pi_glink/event_schema.py`` (the single declaration).
Output: ``web/modules/event-schema.gen.mjs`` (never edited by hand).

    python scripts/gen-event-schema.py            # rewrite the output
    python scripts/gen-event-schema.py --check    # exit 1 when it is stale
    python scripts/gen-event-schema.py --output PATH [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "host"))

from pi_glink import event_schema  # noqa: E402

DEFAULT_OUTPUT = REPO_ROOT / "web/modules/event-schema.gen.mjs"

_JS_TYPES = {
    "string": "string",
    "integer": "number",
    "number": "number",
    "boolean": "boolean",
    "null": "null",
    "object": "Record<string, unknown>",
    "array": "Array<unknown>",
}


def _js_type(field: dict) -> str:
    if field.get("enum"):
        return " | ".join(json.dumps(value, ensure_ascii=False) for value in field["enum"])
    types = field["types"]
    if types is None:
        return "unknown"
    rendered = []
    for name in types:
        if name == "array" and field.get("items"):
            rendered.append(f"Array<{_JS_TYPES[field['items']]}>")
        else:
            rendered.append(_JS_TYPES[name])
    return " | ".join(rendered)


def _typedef_name(event_type: str) -> str:
    return "".join(part.capitalize() for part in event_type.split("_")) + "Payload"


def render() -> str:
    document = event_schema.schema_document()
    events = document["events"]
    lines = [
        "// 生成文件，勿手改。来源：host/pi_glink/event_schema.py",
        "// 重新生成：python scripts/gen-event-schema.py；scripts/check.sh 以 --check 校验它是最新的。",
        "",
    ]
    for event in events:
        lines.append("/**")
        if event["doc"]:
            lines.append(f" * {event['type']}（v{event['v']}）：{event['doc']}")
        else:
            lines.append(f" * {event['type']}（v{event['v']}）")
        lines.append(f" * @typedef {{object}} {_typedef_name(event['type'])}")
        for field in event["fields"]:
            name = field["name"] if field["required"] else f"[{field['name']}]"
            doc = f" {field['doc']}" if field.get("doc") else ""
            lines.append(f" * @property {{{_js_type(field)}}} {name}{doc}")
        lines.append(" */")
    lines.append("")
    lines.append(
        "/** 产品事件 schema：类型 → {v, terminal, open, fields}。 */"
    )
    body = json.dumps(
        {event["type"]: {k: event[k] for k in ("v", "terminal", "open", "fields")} for event in events},
        ensure_ascii=False,
        indent=2,
    )
    lines.append(f"export const EVENT_SCHEMA = Object.freeze({body});")
    lines.append("")
    lines.append("/** 全部产品事件类型（SSE 具名帧）。 */")
    lines.append(
        "export const EVENT_TYPES = Object.freeze("
        + json.dumps([event["type"] for event in events], ensure_ascii=False)
        + ");"
    )
    lines.append("")
    lines.append("/** 每个事件类型的 payload 版本；reducer 只应用 v 与此一致的帧。 */")
    lines.append(
        "export const EVENT_VERSIONS = Object.freeze("
        + json.dumps({event["type"]: event["v"] for event in events}, ensure_ascii=False)
        + ");"
    )
    lines.append("")
    lines.append("/** 权威终帧类型。 */")
    lines.append(
        "export const TERMINAL_EVENT_TYPES = new Set("
        + json.dumps([event["type"] for event in events if event["terminal"]], ensure_ascii=False)
        + ");"
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只校验，不写入")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    expected = render()
    if args.check:
        try:
            actual = args.output.read_text(encoding="utf-8")
        except OSError:
            actual = None
        if actual != expected:
            print(
                f"[FAIL] {args.output} 与 event_schema.py 不一致；"
                "运行 python scripts/gen-event-schema.py 重新生成。",
                file=sys.stderr,
            )
            return 1
        print(f"[OK] {args.output.name} 是最新的")
        return 0
    args.output.write_text(expected, encoding="utf-8")
    print(f"已生成 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
