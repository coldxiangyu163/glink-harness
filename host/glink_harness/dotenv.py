"""Minimal ``.env`` reader.

The host keeps its runtime dependencies small, and the files it reads use a
small, well-defined subset of shell syntax. An explicit parser keeps the
failure modes visible instead of hiding them behind a third-party package.

Supported syntax::

    KEY=value
    KEY="quoted value"
    KEY='quoted value'
    export KEY=value
    # full-line comment
    KEY=value            # trailing comment (a space is required before '#')

Not supported on purpose: multi-line values, command substitution, and
variable interpolation. Values are read once at startup and never re-read,
so a running host cannot be reconfigured by editing the file.
"""

from __future__ import annotations

from pathlib import Path

_COMMENT_PREFIX = " #"
_EXPORT_PREFIX = "export "


def parse_env_file(path: Path) -> dict[str, str]:
    """Return the key/value pairs of ``path``; a missing file yields ``{}``."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(_EXPORT_PREFIX):
            line = line[len(_EXPORT_PREFIX) :].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            continue
        values[key] = _clean_value(value.strip())
    return values


def _clean_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value.split(_COMMENT_PREFIX, 1)[0].rstrip()
