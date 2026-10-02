"""Materialize a temporary harness pack for tests.

The host selects a harness by pack name (``GLINK_HARNESS``) and an ACP pack's
entry argv and capability matrix (session options, permission allowlist,
subagent dispatch, child-progress source, resume) live in its ``harness.toml``
(``glink-harness/v2``). Tests that drive the ACP family with a fake agent
therefore write a throwaway pack here.

:func:`apply_acp_pack` reads test-only ``TEST_ACP_*`` keys from a test's env
dict (a command line, a ``;``-separated session map, a comma/space-separated
allowlist, the dispatch mode, the child-progress format and the resume flag),
turns them into manifest content and removes them. They are never host
variables.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

PACK_FORMAT = "glink-harness/v2"
#: Fixed image requirement for a test pack; the generator never reads it.
TEST_ARG = "TEST_AGENT_VERSION"
TEST_PACKAGE = "test-package"
TEST_VERSION = "0.0.0"


def acp_manifest(
    command: str | list[str],
    *,
    name: str = "dsh",
    session_config: tuple[tuple[str, str], ...] = (),
    permission_allow: tuple[str, ...] = (),
    subagent_dispatch: str = "",
    child_progress: str = "",
    resume: bool = True,
    patch_overlays: tuple[str, ...] = (),
) -> str:
    """The v2 manifest text for one ACP test pack.

    A string command is split with :func:`shlex.split` and serialized back as a
    TOML string array, so quoting inside the manifest is always valid.
    """
    argv = shlex.split(command) if isinstance(command, str) else list(command)
    if not argv:
        raise ValueError("test pack command must not be empty")
    lines = [
        f"format = {json.dumps(PACK_FORMAT)}",
        f"name = {json.dumps(name)}",
        'family = "acp"',
        "",
        "[entry]",
        f"command = {json.dumps(argv)}",
        "",
        "[capabilities]",
        f"resume = {'true' if resume else 'false'}",
    ]
    if subagent_dispatch:
        lines.append(f"subagent_dispatch = {json.dumps(subagent_dispatch)}")
    if child_progress:
        lines.append(f"child_progress = {json.dumps(child_progress)}")
    if permission_allow:
        lines.append("permission_allow = " + json.dumps(list(permission_allow)))
    if patch_overlays:
        lines.append("patch_overlays = " + json.dumps(list(patch_overlays)))
    if session_config:
        lines += ["", "[capabilities.session_config]"]
        lines += [f"{json.dumps(key)} = {json.dumps(value)}" for key, value in session_config]
    lines += [
        "",
        "[image]",
        'target = "acp-worker"',
        "",
        "[[image.packages]]",
        f"arg = {json.dumps(TEST_ARG)}",
        f"package = {json.dumps(TEST_PACKAGE)}",
        f"version = {json.dumps(TEST_VERSION)}",
        'role = "agent"',
    ]
    return "\n".join(lines) + "\n"


def write_acp_pack(root: Path, command: str | list[str], *, name: str = "dsh", **capabilities) -> Path:
    """Write ``root/harnesses/<name>/harness.toml``; return the harnesses root."""
    pack_dir = Path(root) / "harnesses" / name
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "harness.toml").write_text(
        acp_manifest(command, name=name, **capabilities), encoding="utf-8"
    )
    return pack_dir.parent


def apply_acp_pack(values: dict, tmp_path: Path) -> dict:
    """Turn the test-only ``TEST_ACP_*`` keys in ``values`` into a temporary pack.

    Selects the pack (``GLINK_HARNESS=dsh``) and removes the test keys.
    """
    command = values.pop("TEST_ACP_COMMAND", "")
    session = values.pop("TEST_ACP_SESSION_CONFIG", "")
    allow = values.pop("TEST_ACP_PERMISSION_ALLOW", "")
    dispatch = values.pop("TEST_ACP_SUBAGENT_DISPATCH", "")
    progress = values.pop("TEST_ACP_CHILD_PROGRESS", "")
    resume = values.pop("TEST_ACP_RESUME", "true")
    if not command:
        return values
    packs = write_acp_pack(
        tmp_path,
        command,
        session_config=parse_session_config(session),
        permission_allow=parse_permission_allow(allow),
        subagent_dispatch=dispatch,
        child_progress=progress,
        resume=resume != "false",
    )
    values["GLINK_HARNESS"] = "dsh"
    values["GLINK_HARNESSES_DIR"] = str(packs)
    return values


def parse_session_config(raw: str) -> tuple[tuple[str, str], ...]:
    """The ``id=value;…`` test syntax, as a manifest table would carry it."""
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
    """The comma/space-separated allowlist test syntax, deduplicated."""
    tokens: list[str] = []
    for token in re.split(r"[,\s]+", raw):
        if token and token not in tokens:
            tokens.append(token)
    return tuple(tokens)
