"""Harness packs: the one place a harness's differences live.

A pack is a directory ``harnesses/<pack>/`` holding a ``harness.toml`` manifest
(``format = "pi-glink-harness/v1"``), an optional ``capabilities.md`` prompt
fragment and an optional ``home/`` overlay for the ACP family. The manifest
decides everything host-side needs before a run exists: which adapter family
drives it (``family = "pi" | "acp"``), the ACP agent's argv, the upstream
package + exact version the container images pin, and any packaged session
configuration or permission allowlist.

Parsing is all-or-nothing: any unknown key, missing required key, floating
version or symlinked entry refuses the whole pack. A deployment selects one pack
with ``PI_GLINK_HARNESS``; a typo must fail startup, never silently fall back to
another harness. The pack directory is repository content: symlinks are skipped
so an operator's filesystem cannot become an overlay source.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

#: The manifest format this module understands. An unknown value is refused so a
#: future schema change cannot be half-read by an older host.
PACK_FORMAT = "pi-glink-harness/v1"
#: Manifest and package-member names.
MANIFEST_NAME = "harness.toml"
CAPABILITIES_NAME = "capabilities.md"
HOME_DIRNAME = "home"
#: Adapter families a pack may select. The family (not the pack name) is the
#: run record's ``harness`` value: switching packs of the same family keeps the
#: record semantics the follow-up gate already compares.
FAMILIES = ("pi", "acp")
#: Bounds: the manifest and the capability prompt travel through argv (pi face)
#: or a sandbox command string (E2B face), so neither may be unbounded.
MANIFEST_MAX_BYTES = 16 * 1024
CAPABILITIES_MAX_BYTES = 16 * 1024
#: Home-overlay bounds (repository content, but a bound keeps one bad pack from
#: turning every run's configuration render into an unbounded copy).
OVERLAY_MAX_ENTRIES = 512
OVERLAY_MAX_FILE_BYTES = 100 * 1024
OVERLAY_MAX_TOTAL_BYTES = 4 * 1024 * 1024

#: ACP permission option kinds (schema ``PermissionOptionKind``) an explicit
#: allowlist may name. Kept here (not in ``config``) so the manifest parser owns
#: the vocabulary it validates; the config module re-exports them.
ACP_PERMISSION_OPTION_KINDS = ("allow_once", "allow_always")
#: The ACP tool-kind vocabulary (schema ``ToolKind``): only these tokens may be
#: named in the permission allowlist, so a typo cannot silently stop matching.
ACP_PERMISSION_TOOL_KINDS = (
    "read",
    "edit",
    "delete",
    "move",
    "search",
    "execute",
    "think",
    "fetch",
    "switch_mode",
    "other",
)

#: How an ACP agent's ``subagent`` tool reports dispatch (manifest
#: ``subagent_dispatch``). ``sync``: the tool call ends when the child ends, so
#: its terminal status is the child's outcome. ``async``: the tool call ends as
#: soon as the dispatch is accepted and the child keeps running (dsh), so a
#: ``completed`` status is only the acceptance, never the child's completion.
SUBAGENT_DISPATCH_MODES = ("sync", "async")

_PACK_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$")
_TOP_LEVEL_KEYS = frozenset(
    {
        "format",
        "name",
        "family",
        "command",
        "package",
        "version",
        "session_config",
        "permission_allow",
        "subagent_dispatch",
    }
)


class HarnessPackError(ValueError):
    """A harness pack that must not be used (reported as a startup failure)."""


@dataclass(frozen=True, slots=True)
class HarnessPack:
    """One parsed, validated pack. Frozen: it is a startup-frozen deployment fact."""

    name: str
    family: str
    directory: Path
    command: tuple[str, ...] = ()
    package: str = ""
    version: str = ""
    session_config: tuple[tuple[str, str], ...] = ()
    permission_allow: tuple[str, ...] = ()
    subagent_dispatch: str = "sync"
    capabilities: Path | None = None
    home: Path | None = None

    def capabilities_text(self) -> str:
        """The pack's capability prompt fragment, or ``""`` when it has none."""
        if self.capabilities is None:
            return ""
        return self.capabilities.read_text(encoding="utf-8").strip()

    def command_line(self) -> str:
        """The argv as one display string (used by rendering and evidence)."""
        import shlex

        return shlex.join(self.command)


def pack_names(harnesses_dir: Path) -> tuple[str, ...]:
    """The derivable pack names under ``harnesses_dir``, sorted.

    Only a directory holding a regular ``harness.toml`` counts; symlinks are
    skipped (the accepted set must be repository content, not a path into the
    operator's filesystem). A missing directory simply offers no names.
    """
    directory = Path(harnesses_dir)
    if not directory.is_dir():
        return ()
    names: list[str] = []
    for child in sorted(directory.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        manifest = child / MANIFEST_NAME
        if manifest.is_file() and not manifest.is_symlink():
            names.append(child.name)
    return tuple(names)


def load_pack(harnesses_dir: Path, name: str) -> HarnessPack:
    """Parse and validate one pack, or raise :class:`HarnessPackError`."""
    if not _PACK_NAME_RE.fullmatch(name or ""):
        raise HarnessPackError(f"harness 包名不合法：{name!r}")
    directory = Path(harnesses_dir) / name
    if directory.is_symlink() or not directory.is_dir():
        raise HarnessPackError(
            f"harness 包不存在：{directory}（可选包见 PI_GLINK_HARNESSES_DIR）"
        )
    manifest = directory / MANIFEST_NAME
    if not manifest.is_file() or manifest.is_symlink():
        raise HarnessPackError(f"harness 包缺少清单：{manifest}")
    raw = manifest.read_bytes()
    if len(raw) > MANIFEST_MAX_BYTES:
        raise HarnessPackError(f"harness 清单超过 {MANIFEST_MAX_BYTES} 字节：{manifest}")
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise HarnessPackError(f"harness 清单不是合法 UTF-8 TOML：{manifest}: {exc}") from exc

    unknown = sorted(set(document) - _TOP_LEVEL_KEYS)
    if unknown:
        raise HarnessPackError(
            f"harness 清单含未知键 {'、'.join(unknown)}：{manifest}（未知键一律拒绝）"
        )
    if document.get("format") != PACK_FORMAT:
        raise HarnessPackError(
            f"harness 清单 format 必须是 {PACK_FORMAT!r}：{manifest}"
        )
    declared = document.get("name")
    if declared != name:
        raise HarnessPackError(
            f"harness 清单 name={declared!r} 与目录名 {name!r} 不一致：{manifest}"
        )
    family = document.get("family")
    if family not in FAMILIES:
        raise HarnessPackError(
            f"harness 清单 family 必须是 {' 或 '.join(FAMILIES)}：{manifest}"
        )

    command = _parse_command(document, manifest) if family == "acp" else ()
    package = ""
    version = ""
    session_config: tuple[tuple[str, str], ...] = ()
    permission_allow: tuple[str, ...] = ()
    subagent_dispatch = "sync"
    if family == "acp":
        package = _non_empty_text(document, "package", manifest)
        version = _parse_version(document, manifest)
        session_config = _parse_session_config(document, manifest)
        permission_allow = _parse_permission_allow(document, manifest)
        subagent_dispatch = _parse_subagent_dispatch(document, manifest)
    else:
        for key in (
            "command",
            "package",
            "version",
            "session_config",
            "permission_allow",
            "subagent_dispatch",
        ):
            if key in document:
                raise HarnessPackError(
                    f"harness 清单 {key} 只对 family=\"acp\" 生效：{manifest}"
                )

    capabilities = directory / CAPABILITIES_NAME
    if capabilities.exists():
        if not capabilities.is_file() or capabilities.is_symlink():
            raise HarnessPackError(f"capabilities.md 必须是普通文件：{capabilities}")
        if capabilities.stat().st_size > CAPABILITIES_MAX_BYTES:
            raise HarnessPackError(
                f"capabilities.md 超过 {CAPABILITIES_MAX_BYTES} 字节：{capabilities}"
            )

    home = directory / HOME_DIRNAME
    home_path: Path | None = None
    if home.exists() or home.is_symlink():
        if family != "acp":
            raise HarnessPackError(f"home/ 覆盖层只对 family=\"acp\" 生效：{home}")
        if home.is_symlink() or not home.is_dir():
            raise HarnessPackError(f"home/ 覆盖层必须是目录：{home}")
        home_path = home

    return HarnessPack(
        name=name,
        family=family,
        directory=directory,
        command=command,
        package=package,
        version=version,
        session_config=session_config,
        permission_allow=permission_allow,
        subagent_dispatch=subagent_dispatch,
        capabilities=capabilities if capabilities.exists() else None,
        home=home_path,
    )


def _parse_command(document: dict, manifest: Path) -> tuple[str, ...]:
    raw = document.get("command")
    if not isinstance(raw, list) or not raw:
        raise HarnessPackError(f"harness 清单 command 必须是非空 argv 数组：{manifest}")
    command: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise HarnessPackError(
                f"harness 清单 command 的每一项都必须是非空字符串：{manifest}"
            )
        command.append(item)
    return tuple(command)


def _non_empty_text(document: dict, key: str, manifest: Path) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HarnessPackError(f"harness 清单 {key} 必须是非空字符串：{manifest}")
    return value.strip()


def _parse_version(document: dict, manifest: Path) -> str:
    value = _non_empty_text(document, "version", manifest)
    if not _VERSION_RE.fullmatch(value):
        raise HarnessPackError(f"harness 清单 version 必须是精确版本：{manifest}（{value!r}）")
    return value


def _parse_session_config(document: dict, manifest: Path) -> tuple[tuple[str, str], ...]:
    raw = document.get("session_config")
    if raw is None:
        return ()
    if not isinstance(raw, dict) or not raw:
        raise HarnessPackError(f"harness 清单 session_config 必须是非空表：{manifest}")
    entries: list[tuple[str, str]] = []
    for config_id, value in raw.items():
        if not isinstance(config_id, str) or not config_id.strip():
            raise HarnessPackError(f"session_config 的选项名不能为空：{manifest}")
        if not isinstance(value, str) or not value.strip():
            raise HarnessPackError(
                f"session_config.{config_id} 的取值必须是非空字符串：{manifest}"
            )
        entries.append((config_id.strip(), value.strip()))
    return tuple(entries)


def _parse_permission_allow(document: dict, manifest: Path) -> tuple[str, ...]:
    raw = document.get("permission_allow")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise HarnessPackError(f"harness 清单 permission_allow 必须是字符串数组：{manifest}")
    allowed = set(ACP_PERMISSION_OPTION_KINDS) | set(ACP_PERMISSION_TOOL_KINDS)
    tokens: list[str] = []
    for item in raw:
        if not isinstance(item, str) or item not in allowed:
            raise HarnessPackError(
                f"permission_allow 只接受 ACP 的 allow 选项类型或工具类型：{manifest}"
                f"（{item!r}）"
            )
        if item not in tokens:
            tokens.append(item)
    return tuple(tokens)


def _parse_subagent_dispatch(document: dict, manifest: Path) -> str:
    raw = document.get("subagent_dispatch", "sync")
    if raw not in SUBAGENT_DISPATCH_MODES:
        raise HarnessPackError(
            f"harness 清单 subagent_dispatch 只能是 {' 或 '.join(SUBAGENT_DISPATCH_MODES)}："
            f"{manifest}（{raw!r}）"
        )
    return raw
