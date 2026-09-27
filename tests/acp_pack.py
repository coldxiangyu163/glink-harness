"""Materialize a temporary harness pack for tests.

The host selects a harness by pack name (``PI_GLINK_HARNESS``) and an ACP pack's
argv, session options and permission allowlist live in its ``harness.toml``; the
retired ``PI_GLINK_ACP_COMMAND`` / ``PI_GLINK_ACP_SESSION_CONFIG`` /
``PI_GLINK_ACP_PERMISSION_ALLOW`` variables are refused at startup. Tests that
drive the ACP family with a fake agent therefore write a throwaway pack here
instead of setting those variables.

The helpers are test-only: they keep the previous call sites' syntax (a command
line, a ``;``-separated session map, a comma/space-separated allowlist) and turn
it into manifest content.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

#: Fixed identity fields for a test pack; command/session/permission vary.
PACK_FORMAT = "pi-glink-harness/v1"
TEST_PACKAGE = "test-package"
TEST_VERSION = "0.0.0"


def write_acp_pack(
    root: Path,
    command: str,
    *,
    name: str = "dsh",
    session_config: tuple[tuple[str, str], ...] = (),
    permission_allow: tuple[str, ...] = (),
) -> Path:
    """Write ``root/harnesses/<name>/harness.toml``; return the harnesses root.

    The command is split with :func:`shlex.split` (the retired variable's
    semantics) and serialized back as a TOML string array, so quoting inside the
    manifest is always valid.
    """
    import shlex

    argv = shlex.split(command)
    if not argv:
        raise ValueError("test pack command must not be empty")
    lines = [
        f"format = {json.dumps(PACK_FORMAT)}",
        f"name = {json.dumps(name)}",
        'family = "acp"',
        f"command = {json.dumps(argv)}",
        f"package = {json.dumps(TEST_PACKAGE)}",
        f"version = {json.dumps(TEST_VERSION)}",
    ]
    if session_config:
        lines.append("")
        lines.append("[session_config]")
        lines.extend(f"{key} = {json.dumps(value)}" for key, value in session_config)
    if permission_allow:
        lines.append("")
        lines.append(
            "permission_allow = " + json.dumps(list(permission_allow))
        )
    pack_dir = Path(root) / "harnesses" / name
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "harness.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return pack_dir.parent


def apply_acp_pack(values: dict, tmp_path: Path) -> dict:
    """Rewrite the retired ACP knobs in ``values`` into a temporary pack.

    Test helpers keep building their env dicts with ``PI_GLINK_ACP_COMMAND``
    (and optionally the session/allow knobs); this turns them into the manifest
    the host now requires, selects the pack, and removes the retired keys.
    """
    command = values.pop("PI_GLINK_ACP_COMMAND", "")
    session = values.pop("PI_GLINK_ACP_SESSION_CONFIG", "")
    allow = values.pop("PI_GLINK_ACP_PERMISSION_ALLOW", "")
    if not command:
        return values
    packs = write_acp_pack(
        tmp_path,
        command,
        session_config=parse_session_config(session),
        permission_allow=parse_permission_allow(allow),
    )
    values["PI_GLINK_HARNESS"] = "dsh"
    values["PI_GLINK_HARNESSES_DIR"] = str(packs)
    return values


def parse_session_config(raw: str) -> tuple[tuple[str, str], ...]:
    """The retired ``id=value;…`` syntax, as a manifest table would carry it."""
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
    """The retired comma/space-separated allowlist, deduplicated."""
    tokens: list[str] = []
    for token in re.split(r"[,\s]+", raw):
        if token and token not in tokens:
            tokens.append(token)
    return tuple(tokens)
