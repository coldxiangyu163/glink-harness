"""Harness packs: the one place a harness's differences live.

A pack is a directory ``harnesses/<pack>/`` holding a ``harness.toml`` manifest
(``format = "pi-glink-harness/v2"``), an optional ``capabilities.md`` prompt
fragment and an optional ``home/`` overlay for the ACP family. The manifest is
the **single source** of everything a harness differs in:

* ``family`` — which adapter family drives it (``pi`` | ``acp``);
* ``[entry]`` — the agent's argv (ACP) or the kernel binary (pi);
* ``[capabilities]`` — the capability matrix family code reads instead of
  recognising a pack by name: ``resume``, ``subagent_dispatch``,
  ``child_progress``, ``session_config`` and ``permission_allow``;
* ``[image]`` — the image requirement: which worker image carries the pack,
  its E2B resource floor, and every upstream npm package + exact version the
  image pins (``[[image.packages]]``). The Dockerfile ``ARG`` lines, the E2B
  template parameters and pi's ``settings.json`` package list are *generated*
  from these entries (``scripts/generate-pack-artifacts.py``); the repository
  test compares the generated artifacts with the manifests.

Parsing is all-or-nothing: any unknown key, missing required key, floating
version or symlinked entry refuses the whole pack. A deployment selects one pack
with ``PI_GLINK_HARNESS``; a typo must fail startup, never silently fall back to
another harness. The pack directory is repository content: symlinks are skipped
so an operator's filesystem cannot become an overlay source. Manifests of the
retired ``pi-glink-harness/v1`` format are refused (no migration).
"""

from __future__ import annotations

import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The manifest format this module understands. An unknown value (including the
#: retired ``pi-glink-harness/v1``) is refused so a schema change cannot be
#: half-read by an older host.
PACK_FORMAT = "pi-glink-harness/v2"
#: Manifest and package-member names.
MANIFEST_NAME = "harness.toml"
CAPABILITIES_NAME = "capabilities.md"
HOME_DIRNAME = "home"
#: Adapter families a pack may select. The family (not the pack name) is the
#: run record's ``harness`` value: switching packs of the same family keeps the
#: record semantics the follow-up gate already compares.
FAMILIES = ("pi", "acp")
#: Worker image targets a pack may name in ``[image].target``, and the family
#: each one carries. The files an image is built from are repository build
#: knowledge and live in the artifact generator; the host only needs to refuse
#: a pack that names an image of another family.
IMAGE_TARGETS = {"pi-worker": "pi", "acp-worker": "acp"}
#: ``[[image.packages]]`` roles. ``kernel``: the pi kernel itself (exactly one
#: in a pi pack). ``pi-package``: a pi package the pi images prebake into
#: ``/opt/pi-packages`` and pi's ``settings.json`` loads. ``agent``: an ACP
#: agent installed globally in the ACP images (at least one in an ACP pack).
PACKAGE_ROLES = {"pi": ("kernel", "pi-package"), "acp": ("agent",)}
#: Bounds: the manifest and the capability prompt travel through argv (pi face)
#: or a sandbox command string (E2B face), so neither may be unbounded.
MANIFEST_MAX_BYTES = 16 * 1024
CAPABILITIES_MAX_BYTES = 16 * 1024
#: Home-overlay bounds (repository content, but a bound keeps one bad pack from
#: turning every run's configuration render into an unbounded copy).
OVERLAY_MAX_ENTRIES = 512
OVERLAY_MAX_FILE_BYTES = 100 * 1024
OVERLAY_MAX_TOTAL_BYTES = 4 * 1024 * 1024
#: Default E2B resource floor when a pack's ``[image]`` does not raise it.
DEFAULT_CPU_COUNT = 2
DEFAULT_MEMORY_MB = 4096

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

#: How an ACP agent's ``subagent`` tool reports dispatch (capability
#: ``subagent_dispatch``). ``sync``: the tool call ends when the child ends, so
#: its terminal status is the child's outcome. ``async``: the tool call ends as
#: soon as the dispatch is accepted and the child keeps running (dsh), so a
#: ``completed`` status is only the acceptance, never the child's completion.
SUBAGENT_DISPATCH_MODES = ("sync", "async")
#: Running child-progress sources an ACP pack may declare (capability
#: ``child_progress``). ``dsh-session-zstd``: the agent writes dsh's private
#: ``session.v4.jsonl.zstd`` files under its state root
#: (:mod:`pi_glink.kernel.acp_child_progress` owns the format and the path).
#: Undeclared means the face honestly has no running progress.
CHILD_PROGRESS_FORMATS = ("dsh-session-zstd",)

_PACK_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$")
_ARG_RE = re.compile(r"^[A-Z][A-Z0-9_]*_VERSION$")
_NPM_NAME_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
_TOP_LEVEL_KEYS = frozenset({"format", "name", "family", "entry", "capabilities", "image"})
_ENTRY_KEYS = frozenset({"command"})
_CAPABILITY_KEYS = frozenset(
    {"resume", "subagent_dispatch", "child_progress", "session_config", "permission_allow"}
)
#: Capabilities only an ACP pack may declare (the pi family's equivalents are
#: product code: the foreground guard, the pi child-progress source).
_ACP_ONLY_CAPABILITIES = frozenset(
    {"subagent_dispatch", "child_progress", "session_config", "permission_allow"}
)
_IMAGE_KEYS = frozenset({"target", "cpu_count", "memory_mb", "packages"})
_IMAGE_PACKAGE_KEYS = frozenset({"arg", "package", "version", "role"})


class HarnessPackError(ValueError):
    """A harness pack that must not be used (reported as a startup failure)."""


@dataclass(frozen=True, slots=True)
class HarnessCapabilities:
    """The capability matrix a pack declares; family code reads only this."""

    #: Whether a follow-up may continue this pack's sessions at all. ``False``
    #: refuses every follow-up at admission (``session_unavailable``); ``True``
    #: still leaves the family's own runtime facts to decide (ACP: the
    #: negotiated ``sessionCapabilities.resume``/``loadSession`` bit).
    resume: bool = False
    subagent_dispatch: str = "sync"
    child_progress: str | None = None
    session_config: tuple[tuple[str, str], ...] = ()
    permission_allow: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ImagePackage:
    """One upstream npm package the pack's worker image pins."""

    #: The Dockerfile ``ARG`` (``*_VERSION``) the generator writes the version to.
    arg: str
    package: str
    version: str
    role: str


@dataclass(frozen=True, slots=True)
class ImageRequirement:
    """Which worker image carries the pack and what it must contain."""

    target: str
    cpu_count: int = DEFAULT_CPU_COUNT
    memory_mb: int = DEFAULT_MEMORY_MB
    packages: tuple[ImagePackage, ...] = ()

    def by_role(self, role: str) -> tuple[ImagePackage, ...]:
        return tuple(item for item in self.packages if item.role == role)


@dataclass(frozen=True, slots=True)
class HarnessPack:
    """One parsed, validated pack. Frozen: it is a startup-frozen deployment fact.

    The host configuration holds this object itself (``HostConfig.pack``);
    nothing is copied field by field out of it.
    """

    name: str
    family: str
    directory: Path
    command: tuple[str, ...]
    capabilities: HarnessCapabilities
    image: ImageRequirement
    #: The capability prompt fragment (``capabilities.md``), read once at parse
    #: time so the system prompt is bytes-stable for the host's lifetime.
    prompt: str = ""
    prompt_path: Path | None = None
    home: Path | None = None
    #: Parsed manifest document (for tooling that renders artifacts; not read
    #: by the run path).
    document: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def command_line(self) -> str:
        """The argv as one display string (used by rendering and evidence)."""
        return shlex.join(self.command)

    def prebaked_pi_packages(self) -> tuple[str, ...]:
        """npm names of the pi packages the pack's image prebakes (pi family)."""
        return tuple(item.package for item in self.image.by_role("pi-package"))


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

    _refuse_unknown(document, _TOP_LEVEL_KEYS, "", manifest)
    if document.get("format") != PACK_FORMAT:
        raise HarnessPackError(
            f"harness 清单 format 必须是 {PACK_FORMAT!r}：{manifest}"
            "（旧格式不再可读，不做迁移）"
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

    command = _parse_entry(document, family, manifest)
    capabilities = _parse_capabilities(document, family, manifest)
    image = _parse_image(document, family, manifest)

    prompt_file = directory / CAPABILITIES_NAME
    prompt = ""
    prompt_path: Path | None = None
    if prompt_file.exists() or prompt_file.is_symlink():
        if not prompt_file.is_file() or prompt_file.is_symlink():
            raise HarnessPackError(f"capabilities.md 必须是普通文件：{prompt_file}")
        if prompt_file.stat().st_size > CAPABILITIES_MAX_BYTES:
            raise HarnessPackError(
                f"capabilities.md 超过 {CAPABILITIES_MAX_BYTES} 字节：{prompt_file}"
            )
        prompt = prompt_file.read_text(encoding="utf-8").strip()
        prompt_path = prompt_file

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
        capabilities=capabilities,
        image=image,
        prompt=prompt,
        prompt_path=prompt_path,
        home=home_path,
        document=document,
    )


def _refuse_unknown(table: dict, allowed: frozenset[str], where: str, manifest: Path) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        prefix = f"[{where}] " if where else ""
        raise HarnessPackError(
            f"harness 清单 {prefix}含未知键 {'、'.join(unknown)}：{manifest}（未知键一律拒绝）"
        )


def _table(document: dict, key: str, manifest: Path) -> dict:
    raw = document.get(key)
    if not isinstance(raw, dict):
        raise HarnessPackError(f"harness 清单必须声明 [{key}] 表：{manifest}")
    return raw


def _parse_entry(document: dict, family: str, manifest: Path) -> tuple[str, ...]:
    entry = _table(document, "entry", manifest)
    _refuse_unknown(entry, _ENTRY_KEYS, "entry", manifest)
    raw = entry.get("command")
    if not isinstance(raw, list) or not raw:
        raise HarnessPackError(f"harness 清单 entry.command 必须是非空 argv 数组：{manifest}")
    command: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise HarnessPackError(
                f"harness 清单 entry.command 的每一项都必须是非空字符串：{manifest}"
            )
        command.append(item)
    if family == "pi" and len(command) != 1:
        # The pi argv is built by the host (``--mode rpc`` etc.); the entry only
        # names the image-local kernel binary.
        raise HarnessPackError(
            f"pi 包的 entry.command 只声明内核可执行名（一项）：{manifest}"
        )
    return tuple(command)


def _parse_capabilities(document: dict, family: str, manifest: Path) -> HarnessCapabilities:
    table = _table(document, "capabilities", manifest)
    _refuse_unknown(table, _CAPABILITY_KEYS, "capabilities", manifest)
    if family != "acp":
        for key in sorted(_ACP_ONLY_CAPABILITIES & set(table)):
            raise HarnessPackError(
                f"harness 清单 capabilities.{key} 只对 family=\"acp\" 生效：{manifest}"
            )
    resume = table.get("resume")
    if not isinstance(resume, bool):
        raise HarnessPackError(
            f"harness 清单 capabilities.resume 必须显式声明为 true 或 false：{manifest}"
        )
    dispatch = table.get("subagent_dispatch", "sync")
    if dispatch not in SUBAGENT_DISPATCH_MODES:
        raise HarnessPackError(
            f"harness 清单 capabilities.subagent_dispatch 只能是 "
            f"{' 或 '.join(SUBAGENT_DISPATCH_MODES)}：{manifest}（{dispatch!r}）"
        )
    progress = table.get("child_progress")
    if progress is not None and progress not in CHILD_PROGRESS_FORMATS:
        raise HarnessPackError(
            f"harness 清单 capabilities.child_progress 只能是 "
            f"{'、'.join(CHILD_PROGRESS_FORMATS)}：{manifest}（{progress!r}）"
        )
    return HarnessCapabilities(
        resume=resume,
        subagent_dispatch=dispatch,
        child_progress=progress,
        session_config=_parse_session_config(table, manifest),
        permission_allow=_parse_permission_allow(table, manifest),
    )


def _parse_session_config(table: dict, manifest: Path) -> tuple[tuple[str, str], ...]:
    raw = table.get("session_config")
    if raw is None:
        return ()
    if not isinstance(raw, dict) or not raw:
        raise HarnessPackError(
            f"harness 清单 capabilities.session_config 必须是非空表：{manifest}"
        )
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


def _parse_permission_allow(table: dict, manifest: Path) -> tuple[str, ...]:
    raw = table.get("permission_allow")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise HarnessPackError(
            f"harness 清单 capabilities.permission_allow 必须是字符串数组：{manifest}"
        )
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


def _parse_image(document: dict, family: str, manifest: Path) -> ImageRequirement:
    table = _table(document, "image", manifest)
    _refuse_unknown(table, _IMAGE_KEYS, "image", manifest)
    target = table.get("target")
    if not isinstance(target, str) or IMAGE_TARGETS.get(target) != family:
        choices = "、".join(key for key, value in IMAGE_TARGETS.items() if value == family)
        raise HarnessPackError(
            f"harness 清单 image.target 必须是本族的镜像（{choices}）：{manifest}（{target!r}）"
        )
    cpu = _positive_int(table, "cpu_count", DEFAULT_CPU_COUNT, manifest)
    memory = _positive_int(table, "memory_mb", DEFAULT_MEMORY_MB, manifest)
    raw_packages = table.get("packages")
    if not isinstance(raw_packages, list) or not raw_packages:
        raise HarnessPackError(
            f"harness 清单必须至少声明一条 [[image.packages]]：{manifest}"
        )
    roles = PACKAGE_ROLES[family]
    packages: list[ImagePackage] = []
    seen_args: set[str] = set()
    seen_names: set[str] = set()
    for raw in raw_packages:
        if not isinstance(raw, dict):
            raise HarnessPackError(f"[[image.packages]] 的每一项都必须是表：{manifest}")
        _refuse_unknown(raw, _IMAGE_PACKAGE_KEYS, "image.packages", manifest)
        arg = raw.get("arg")
        if not isinstance(arg, str) or not _ARG_RE.fullmatch(arg):
            raise HarnessPackError(
                f"image.packages.arg 必须是 *_VERSION 形式的 Dockerfile ARG 名：{manifest}（{arg!r}）"
            )
        package = raw.get("package")
        if not isinstance(package, str) or not _NPM_NAME_RE.fullmatch(package):
            raise HarnessPackError(
                f"image.packages.package 必须是 npm 包名：{manifest}（{package!r}）"
            )
        version = raw.get("version")
        if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
            raise HarnessPackError(
                f"image.packages.version 必须是精确版本：{manifest}（{package}: {version!r}）"
            )
        role = raw.get("role")
        if role not in roles:
            raise HarnessPackError(
                f"image.packages.role 在 family={family!r} 下只能是 {'、'.join(roles)}："
                f"{manifest}（{package}: {role!r}）"
            )
        if arg in seen_args or package in seen_names:
            raise HarnessPackError(f"image.packages 重复声明 {arg}/{package}：{manifest}")
        seen_args.add(arg)
        seen_names.add(package)
        packages.append(ImagePackage(arg=arg, package=package, version=version, role=role))
    image = ImageRequirement(
        target=target, cpu_count=cpu, memory_mb=memory, packages=tuple(packages)
    )
    if family == "pi" and len(image.by_role("kernel")) != 1:
        raise HarnessPackError(f"pi 包必须恰好声明一个 role=\"kernel\" 的包：{manifest}")
    return image


def _positive_int(table: dict, key: str, default: int, manifest: Path) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HarnessPackError(f"harness 清单 image.{key} 必须是正整数：{manifest}（{value!r}）")
    return value
