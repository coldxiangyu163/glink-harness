#!/usr/bin/env python3
"""Write a temporary ACP harness pack for the live probes.

The live probes take an arbitrary agent command line — often an npx-pinned
version that deliberately differs from the image ARG. Since GLINK-ARCH-6 the
host selects a harness by pack name and refuses the retired
``PI_GLINK_ACP_COMMAND`` / ``PI_GLINK_ACP_SESSION_CONFIG`` /
``PI_GLINK_ACP_PERMISSION_ALLOW`` variables, so a probe materializes the same
facts as a temporary ``harness.toml`` and selects it with ``PI_GLINK_HARNESS``.

``write_probe_pack`` returns the temporary harnesses root; callers set
``PI_GLINK_HARNESS=<name>`` and ``PI_GLINK_HARNESSES_DIR=<returned path>``.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

#: The probe pack's identity fields; the command is the fact under test.
PROBE_PACK_NAME = "probe"
PROBE_PACKAGE = "probe-package"
PROBE_VERSION = "0.0.0"


def write_probe_pack(
    root: Path,
    command: str,
    *,
    name: str = PROBE_PACK_NAME,
    session_config: str = "",
    permission_allow: str = "",
) -> Path:
    """Write ``<root>/harnesses/<name>/harness.toml``; return the harnesses root."""
    argv = shlex.split(command)
    if not argv:
        raise ValueError("probe command must not be empty")
    lines = [
        'format = "pi-glink-harness/v1"',
        f"name = {json.dumps(name)}",
        'family = "acp"',
        f"command = {json.dumps(argv)}",
        f"package = {json.dumps(PROBE_PACKAGE)}",
        f"version = {json.dumps(PROBE_VERSION)}",
    ]
    entries = parse_session_config(session_config)
    if entries:
        lines += ["", "[session_config]"]
        lines += [f"{key} = {json.dumps(value)}" for key, value in entries]
    allow = parse_permission_allow(permission_allow)
    if allow:
        lines += ["", f"permission_allow = {json.dumps(allow)}"]
    pack_dir = Path(root) / "harnesses" / name
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "harness.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return pack_dir.parent


def parse_session_config(raw: str) -> tuple[tuple[str, str], ...]:
    """The ``--session-config`` syntax (``id=value;…``) as manifest entries."""
    entries: list[tuple[str, str]] = []
    for chunk in raw.split(";"):
        item = chunk.strip()
        if not item:
            continue
        config_id, separator, value = item.partition("=")
        if not separator or not config_id.strip() or not value.strip():
            raise ValueError(f"bad session config entry: {item!r}")
        entries.append((config_id.strip(), value.strip()))
    return tuple(entries)


def parse_permission_allow(raw: str) -> tuple[str, ...]:
    """The ``--permission-allow`` syntax (comma/space separated), deduplicated."""
    tokens: list[str] = []
    for token in re.split(r"[,\s]+", raw):
        if token and token not in tokens:
            tokens.append(token)
    return tuple(tokens)
