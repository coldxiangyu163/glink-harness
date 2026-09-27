"""Per-run configuration for an ACP agent process.

The ACP family is one client protocol over several agents (dsh, codex-acp,
opencode, …), so this module renders
only what every one of them needs from the host:

* **An exclusive state root.** ``runs/<id>/acp-agent`` is created owner-only and
  exposed to the child as ``HOME``: the in-process ACP agent writes ``~/.dsh``,
  ``~/.codex``, opencode state and any npm state inside this run's directory
  instead of the operator's home, so two runs cannot share agent state and a
  destroyed run leaves nothing behind. This is the ACP analogue of the pi face's
  per-run ``PI_CODING_AGENT_DIR``. Which agents additionally honour an
  agent-specific variable (``DSH_HOME``, ``CODEX_HOME``, …) is measured
  in M2 and recorded in ``docs/acp-integration-notes.md``.
* **A filtered environment.** The child gets the same minimal passthrough list
  the pi face gets (:data:`~pi_glink.kernel.agent_env.PASSTHROUGH_ENV`) plus the
  explicit ``PI_GLINK_ACP_ENV`` entries and this run's real ``HOME``.
  ``PI_GLINK_ACP_ENV`` cannot override ``HOME``: the per-run state root is the
  isolation boundary, so a configuration mistake cannot point the agent back at
  the operator's home.
* **Credential injection by copy.** ``PI_GLINK_ACP_CREDENTIAL_FILES`` names
  ``source=dest`` entries: the host copies the source **read-only** into
  ``<run HOME>/<dest>`` (parent directories 0700, file 0600), never touching the
  operator's own files. The values learned from the copied material are returned
  as :attr:`AcpRuntime.secrets` so the runner can add them to that run's
  redaction boundary: a credential the agent echoes into a stream, tool result
  or report is masked like every other secret the host knows. The values are
  never logged and never put into the child's command line or environment.

* **Credential injection by env.** ``PI_GLINK_ACP_ENV`` carries explicit
  ``NAME=value`` entries (an API-key style credential the agent reads from the
  environment, e.g. ``ANTHROPIC_API_KEY``). Values whose *name* looks like a
  credential join the same redaction boundary, so both channels behave alike.

The host never runs the command through a shell: ``PI_GLINK_ACP_COMMAND`` is
split with :func:`shlex.split` and executed as an argv, so a run cannot inherit
shell expansion from the configuration.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..config import ConfigError, HostConfig
from ..redaction import MIN_SECRET_LENGTH, find_credential_shapes, is_placeholder_value
from ..artifacts import ArtifactError, ArtifactTree
from .agent_env import PASSTHROUGH_ENV
from .sweep import SessionSweep

#: Per-run ACP agent directory inside the run directory.
ACP_RUNTIME_DIRNAME = "acp-agent"
#: The run's agent state root (``HOME``) below :data:`ACP_RUNTIME_DIRNAME`.
ACP_HOME_DIRNAME = "home"
#: A state root recovered from an E2B sandbox, below
#: :data:`ACP_RUNTIME_DIRNAME`. Only the E2B face writes it, and only after the
#: whole bounded recovery succeeded (see
#: :meth:`~pi_glink.kernel.acp_e2b_env.E2BAcpSandboxClient.recover_state`): the
#: sandbox destroys the agent's own state with it, so the host needs its own
#: copy before that happens — and the directory's existence is the durable
#: "父状态根回收成功" fact the ACP admission gate reads. It is a separate root
#: from :data:`ACP_HOME_DIRNAME` so the rendered credential copies (and, for a
#: follow-up, the inherited state that was uploaded) are never confused with
#: what the sandbox actually wrote.
ACP_RECOVERED_DIRNAME = "recovered"
#: Upper bound on one copied credential file. A credential store is small;
#: a bigger file is refused instead of silently consumed/stored.
CREDENTIAL_FILE_MAX_BYTES = 1 << 20

#: Bounds on the pack `home/` overlay (reviewed repository content): a bound
#: keeps one bad pack from turning every run's configuration render into an
#: unbounded copy.
OVERLAY_MAX_ENTRIES = 512
OVERLAY_MAX_FILE_BYTES = 100 * 1024
OVERLAY_MAX_TOTAL_BYTES = 4 * 1024 * 1024

#: Continuation state inherited from a parent run is bounded on all three axes
#: (file count, one file, total bytes). A research agent's own state is small;
#: the bounds keep one run's cache — or a hostile session file — from making the
#: next run copy without limit. Exceeding any bound refuses the follow-up
#: instead of silently resuming from a partial state. The per-file bound is the
#: E2B face's upload ceiling (``e2b_env.UPLOAD_MAX_BYTES``), so a state root that
#: inherits on one face is not undeliverable on another.
INHERIT_MAX_FILES = 4096
INHERIT_MAX_FILE_BYTES = 8 * 1024 * 1024
INHERIT_MAX_TOTAL_BYTES = 64 * 1024 * 1024
#: How much inherited text is scanned for credential-shaped values that join the
#: run's redaction boundary (this bounds the scan, not the copy).
INHERIT_SCAN_MAX_BYTES = 8 * 1024 * 1024


class AcpStateInheritError(RuntimeError):
    """A follow-up's parent state could not be inherited safely.

    Raised for a missing/unreadable parent state root, a bound violation or an
    unsafe entry. The caller refuses the follow-up: the alternative — resuming
    from a partial copy or from nothing — would present a continuation the
    agent does not actually have.
    """

#: Key-name hints for values that must join the redaction boundary even when a
#: scanner cannot recognise their shape (an opaque token, a short shared secret).
_SECRET_NAME_RE = re.compile(r"(?i)(key|token|secret|password|credential|auth)")
#: OAuth credential stores (opencode's ``auth.json``) name their fields exactly
#: ``access``/``refresh``, without a ``token`` suffix the generic rule could see.
#: Matching the whole field name (never a substring) keeps ordinary keys such as
#: ``accessMode`` out of the redaction boundary.
_OAUTH_SECRET_FIELDS = frozenset({"access", "refresh"})
#: ``key: value`` / ``key = value`` scalar lines inside the credential file
#: (dsh's ``.credentials.yaml`` is YAML; the JSON branch handles the rest).
_SCALAR_LINE_RE = re.compile(r"^\s*(?P<name>[A-Za-z0-9_.\-]+)\s*:\s*(?P<value>.+?)\s*$")


@dataclass(frozen=True, slots=True)
class AcpRuntime:
    """Where one ACP agent will keep its state, and which command runs it."""

    #: The run's exclusive state root (mode 0700, host-owned).
    directory: Path
    #: ``HOME`` handed to the agent process.
    home: Path
    #: The frozen command line, for the run record and diagnostics.
    command: str
    #: Credential values this run may render (copied files and
    #: credential-shaped ``PI_GLINK_ACP_ENV`` values), for the run's redactor.
    secrets: tuple[str, ...] = ()
    #: The credential destinations, relative to :attr:`home`, in the order they
    #: were injected. The container face uses these to mount each injected
    #: credential file read-only on top of the writable state root.
    credential_dests: tuple[str, ...] = ()

    def session_sweeps(self) -> tuple[SessionSweep, ...]:
        """ACP 写出的状态路径：本 run 的状态根，**只读扫描**不重写。

        Both roots are declared; a face creates only one of them (in-process
        and Docker write ``home``, the E2B face recovers the sandbox's root to
        ``recovered`` beside it), and a missing root is simply nothing to scan.

        ``rewrite=False`` is the documented obligation, not a shortcut: this
        tree is the agent's own session store, so rewriting it would corrupt
        the very session a follow-up resumes (``docs/acp-integration-notes.md``
        §10.2/§14.1). The sweep reads the text through a directory handle and
        folds any credential-shaped value into this run's redaction boundary —
        the same rule recovery and inheritance follow — leaving every byte the
        agent wrote untouched.
        """
        return (
            SessionSweep(root=self.home, rewrite=False),
            SessionSweep(root=self.directory / ACP_RECOVERED_DIRNAME, rewrite=False),
        )


def prepare_acp_runtime(
    config: HostConfig,
    *,
    runtime_dir: Path,
    operator_home: Path | None = None,
    inherit_from: Path | None = None,
) -> AcpRuntime:
    """Create one run's ACP state root, inject credentials, return its handle.

    ``runtime_dir`` must be the run's own directory (``<data_root>/runs/<id>/``
    subdirectory): rendering into a shared directory would let two runs
    overwrite each other's agent state. ``operator_home`` is the host user's home
    a credential source's leading ``~`` expands against (never the run HOME).

    ``inherit_from`` is the authorized parent run's ACP state root — its own
    ``runs/<parent>/acp-agent/home`` on the in-process and Docker faces, or the
    sandbox state root recovered to ``runs/<parent>/acp-agent/recovered`` on the
    E2B face — when this run continues that parent's session. It is copied into
    this run's own HOME — the ACP analogue
    of the pi face's sanitized session-file copy — after the fresh credentials
    are in place, and every configured credential destination is skipped: the
    child must render its own credential copy (so the run's redactor knows the
    values it may echo) rather than inherit the parent's. Values discovered in
    the inherited text are returned through :attr:`AcpRuntime.secrets` so they
    join this run's redaction boundary too.

    Credential sources are read only: the operator's own files are never
    written, moved or chmodded by this function.
    """
    if not config.acp_command:
        raise ConfigError(
            f"harness 包 {config.harness_pack!r} 缺少 command（ACP agent 的命令行）"
        )
    directory = Path(runtime_dir)
    home = directory / ACP_HOME_DIRNAME
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    os.chmod(home, 0o700)
    base_home = Path(operator_home) if operator_home is not None else Path.home()
    # The pack overlay lands first, then the credential files: a credential
    # destination must never lose to a same-path overlay entry (see
    # :func:`_apply_home_overlay`).
    _apply_home_overlay(config, home)
    secrets = list(_inject_credential_files(config, home, base_home))
    # ``PI_GLINK_ACP_ENV`` is the explicit env channel (an API-key style
    # credential that an agent reads from the environment). Its values join the
    # run's redaction boundary by the same rule the credential files use: only
    # a credential-looking *name* makes a value a secret, so an ordinary knob
    # (``ACME_MODE=workspace``) never becomes a redaction target.
    secrets.extend(_env_credential_values(config))
    credential_dests = tuple(dest for _source, dest in config.acp_credential_files)
    if inherit_from is not None:
        # After the injection above, so the fresh credential files win; the
        # destinations are excluded from the copy for the same reason.
        secrets.extend(
            inherit_acp_state(
                Path(inherit_from), home, skip=credential_dests
            )
        )
    return AcpRuntime(
        directory=directory,
        home=home,
        command=shlex.join(config.acp_command),
        secrets=_dedupe(secrets),
        credential_dests=credential_dests,
    )


def inherit_acp_state(
    parent_home: Path,
    home: Path,
    *,
    skip: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Copy an authorized parent's ACP state into this run's HOME (bounded).

    The copy is the ACP counterpart of the pi face's session-file breadcrumb: a
    ``session/resume`` only means something when the agent can find the session
    it is told to resume, and that session lives in the agent's own state root.

    Safety rules, in the order they are enforced:

    * both roots are walked through :class:`~pi_glink.artifacts.ArtifactTree`,
      so a symbolic link below either root is never followed and the copy can
      never land outside this run's own HOME;
    * only regular files are copied; device nodes, FIFOs and sockets are not
      inherited;
    * the copy is bounded (file count, per-file bytes, total bytes) and any
      bound violation raises :class:`AcpStateInheritError` instead of leaving a
      silently truncated state behind;
    * ``skip`` names this configuration's credential destinations (relative to
      HOME), which the caller has just re-rendered for this run: inheriting the
      parent's copies would shadow the fresh ones and put values into the run
      that its redactor was not built from.

    Credential-shaped values found in the copied text are returned so the caller
    can add them to this run's redaction boundary. The copied files themselves
    are **not** rewritten: unlike the pi session log, this tree is the agent's
    own session store — rewriting it would corrupt the very session the resume
    is meant to continue. The boundary is preserved by knowing the values
    instead.
    """
    source_dir = Path(parent_home)
    destination_dir = Path(home)
    if not destination_dir.is_dir():
        raise AcpStateInheritError(f"本次 run 的 ACP HOME 不存在: {destination_dir}")
    skipped = {_normalise_relative(item) for item in skip}
    try:
        source = ArtifactTree(source_dir)
    except ArtifactError as exc:
        raise AcpStateInheritError(f"父 run 的 ACP 状态目录不可用: {exc}") from exc
    with source, ArtifactTree(destination_dir) as destination:
        relatives = source.files(max_files=INHERIT_MAX_FILES + 1)
        if len(relatives) > INHERIT_MAX_FILES:
            raise AcpStateInheritError(
                f"父 run 的 ACP 状态文件数超过上限 {INHERIT_MAX_FILES}"
            )
        total = 0
        scanned = 0
        secrets: list[str] = []
        for relative in relatives:
            if relative in skipped:
                continue
            try:
                data = source.read(relative, max_bytes=INHERIT_MAX_FILE_BYTES)
            except ArtifactError as exc:
                raise AcpStateInheritError(
                    f"读取父 run 状态文件失败 {relative}: {exc}"
                ) from exc
            total += len(data)
            if total > INHERIT_MAX_TOTAL_BYTES:
                raise AcpStateInheritError(
                    f"父 run 的 ACP 状态超过总量上限 {INHERIT_MAX_TOTAL_BYTES} 字节"
                )
            try:
                destination.replace(
                    relative,
                    data,
                    max_bytes=INHERIT_MAX_FILE_BYTES,
                    create_parents=True,
                )
            except ArtifactError as exc:
                raise AcpStateInheritError(
                    f"写入继承的父 run 状态失败 {relative}: {exc}"
                ) from exc
            if scanned < INHERIT_SCAN_MAX_BYTES and b"\x00" not in data[:1024]:
                scanned += len(data)
                secrets.extend(
                    credential_secrets_from_text(data.decode("utf-8", errors="replace"))
                )
    return _dedupe(secrets)


def _normalise_relative(value: str) -> str:
    """One config credential destination as a forward-slash relative path."""
    return str(value).strip().lstrip("/")


def build_acp_argv(config: HostConfig) -> list[str]:
    """The agent's argv: the selected pack's frozen command, never shell-expanded."""
    argv = list(config.acp_command)
    if not argv:
        raise ConfigError(
            f"harness 包 {config.harness_pack!r} 的 command 为空；需要 ACP agent 的可执行命令"
        )
    return argv


def build_acp_env(
    config: HostConfig,
    runtime: AcpRuntime,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Child environment: minimal passthrough, per-run ``HOME``, explicit extras.

    ``base_env`` is filtered through :data:`PASSTHROUGH_ENV` (the same list the
    pi face uses) so a caller handing in ``os.environ`` cannot widen the child's
    view of host secrets; the only value this module overrides is ``HOME``, and
    the configuration entries applied afterwards were validated not to contain
    it.
    """
    source = dict(base_env) if base_env is not None else dict(os.environ)
    env = {name: source[name] for name in PASSTHROUGH_ENV if name in source}
    env["HOME"] = str(runtime.home)
    for name, value in config.acp_env:
        env[name] = value
    return env


def credential_secrets_from_text(text: str) -> tuple[str, ...]:
    """Credential values inside one credential file's text, in stable order.

    Three detectors run over the copied material:

    * **JSON** (codex/opencode ``auth.json``): values under a key whose name looks
      like a credential (``*_token``, ``*ApiKey``, ``*secret``, …) or is exactly
      ``access``/``refresh`` (opencode's OAuth store);
    * **scalar lines** (dsh ``.credentials.yaml``): ``key: value`` pairs with the
      same key-name rule;
    * **shape detection**: :func:`~pi_glink.redaction.find_credential_shapes`
      over the raw text, which finds provider-shaped values a key name alone
      would miss.

    Documentation placeholders (``your-key-here``) are skipped — they are not
    secrets, and masking them would only corrupt output. The result keeps the
    redactor's own minimum length rule.
    """
    found: list[str] = []
    _collect_json_values(_parsed_json(text), found)
    for line in text.splitlines():
        match = _SCALAR_LINE_RE.match(line)
        if match and _is_secret_field(match.group("name")):
            found.append(_unquote(match.group("value")))
    found.extend(find_credential_shapes(text))
    stable: list[str] = []
    for value in found:
        if not isinstance(value, str):
            continue
        text_value = value.strip()
        if len(text_value) < MIN_SECRET_LENGTH or is_placeholder_value(text_value):
            continue
        if text_value not in stable:
            stable.append(text_value)
    return tuple(stable)


def _env_credential_values(config: HostConfig) -> tuple[str, ...]:
    """Credential-shaped values of the explicit ``PI_GLINK_ACP_ENV`` entries.

    The env channel has no file to scan, so the variable *name* is the only
    hint: names matching the credential rule (``*_API_KEY``, ``*_TOKEN``,
    ``*_SECRET``, …) have their values added to the redaction boundary. Values
    below the redactor's minimum length and documentation placeholders are
    skipped, exactly like values found inside credential files.
    """
    values: list[str] = []
    for name, value in config.acp_env:
        if not _SECRET_NAME_RE.search(name):
            continue
        text_value = value.strip()
        if len(text_value) < MIN_SECRET_LENGTH or is_placeholder_value(text_value):
            continue
        values.append(text_value)
    return _dedupe(values)


def _apply_home_overlay(config: HostConfig, home: Path) -> None:
    """Copy the selected pack's ``home/`` overlay into the run HOME, 0600.

    The overlay is reviewed repository content (``harnesses/<pack>/home/``);
    symlinks are skipped so it can never point outside the pack, and a missing
    directory simply means this pack has no overlay. Files are written freshly
    with 0600 and owner-only parents. Credential injection runs **after** this
    function, so a credential destination always wins over an overlay entry with
    the same path.
    """
    overlay = config.pack_home
    if overlay is None:
        return
    entries = 0
    total = 0
    for source in sorted(overlay.rglob("*")):
        if source.is_symlink():
            continue
        relative = source.relative_to(overlay)
        if source.is_dir():
            target = home / relative
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            _chmod_private(target)
            continue
        if not source.is_file():
            continue
        entries += 1
        if entries > OVERLAY_MAX_ENTRIES:
            raise ConfigError(f"harness 包 home/ 覆盖层条目过多：{overlay}")
        data = source.read_bytes()
        if len(data) > OVERLAY_MAX_FILE_BYTES:
            raise ConfigError(f"harness 包 home/ 覆盖层单文件超过上限：{source}")
        total += len(data)
        if total > OVERLAY_MAX_TOTAL_BYTES:
            raise ConfigError(f"harness 包 home/ 覆盖层总量超过上限：{overlay}")
        target = home / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        current = target.parent
        while current != home and home in current.parents:
            _chmod_private(current)
            current = current.parent
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        handle = os.open(target, flags, 0o600)
        try:
            os.write(handle, data)
        finally:
            os.close(handle)
        _chmod_file_private(target)


def _inject_credential_files(
    config: HostConfig, home: Path, operator_home: Path
) -> tuple[str, ...]:
    """Copy every configured credential file into the run HOME, 0600.

    An unreadable or oversized source fails the run: a missing credential could
    otherwise turn into a run that appears to work but has no model access. The
    destination is opened ``O_CREAT|O_EXCL|O_NOFOLLOW`` so a pre-existing entry
    (including a symlink) cannot redirect the write out of the run HOME.
    """
    secrets: list[str] = []
    for source_text, dest_text in config.acp_credential_files:
        if source_text.startswith("~/"):
            source = operator_home / source_text[2:]
        elif source_text.startswith("~"):
            raise ConfigError(
                f"ACP 凭据来源不支持 ~user 形式：{source_text}（用绝对路径或 ~/…）"
            )
        else:
            source = Path(source_text)
            if not source.is_absolute():
                source = operator_home / source
        if not source.is_file():
            raise ConfigError(
                f"ACP 凭据来源不可读：{source}（PI_GLINK_ACP_CREDENTIAL_FILES）"
            )
        data = source.read_bytes()
        if len(data) > CREDENTIAL_FILE_MAX_BYTES:
            raise ConfigError(
                f"ACP 凭据文件超过 {CREDENTIAL_FILE_MAX_BYTES} 字节，拒绝复制：{source}"
            )
        target = home / dest_text
        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Every directory created below the run HOME stays owner-only;
        # ``mkdir(parents=True)`` only applies the mode to the last element.
        current = parent
        while current != home and home in current.parents:
            _chmod_private(current)
            current = current.parent
        # The pack overlay was written first, so a same-path entry can only be
        # repository content: the credential destination wins by replacing it.
        # ``O_NOFOLLOW`` still refuses a symlink (nothing may redirect the write
        # out of the run HOME), and the file is created 0600 either way.
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags, 0o600)
        except OSError as exc:
            raise ConfigError(f"ACP 凭据目标不可安全写入：{target}（{exc}）") from exc
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, data)
        finally:
            os.close(descriptor)
        secrets.extend(credential_secrets_from_text(data.decode("utf-8", errors="replace")))
    return _dedupe(secrets)


def _is_secret_field(key_hint: str) -> bool:
    """Does a (dotted) key path name a credential field?

    The generic rule matches credential-ish substrings anywhere in the path
    (``openai.access_token``). The OAuth rule matches only the last path element
    exactly, so ``openai.access`` is a credential while ``openai.accessMode``
    is not.
    """
    if _SECRET_NAME_RE.search(key_hint):
        return True
    return key_hint.rsplit(".", 1)[-1].strip().lower() in _OAUTH_SECRET_FIELDS


def _collect_json_values(document: object, found: list[str], key_hint: str = "") -> None:
    if isinstance(document, Mapping):
        for key, value in document.items():
            _collect_json_values(value, found, f"{key_hint}.{key}" if key_hint else str(key))
    elif isinstance(document, list):
        for item in document:
            _collect_json_values(item, found, key_hint)
    elif isinstance(document, str) and _is_secret_field(key_hint):
        found.append(document)


def _parsed_json(text: str) -> object:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _unquote(value: str) -> str:
    text_value = value.strip()
    if len(text_value) >= 2 and text_value[0] == text_value[-1] and text_value[0] in "\"'":
        return text_value[1:-1]
    return text_value


def _dedupe(values: list[str]) -> tuple[str, ...]:
    stable: list[str] = []
    for value in values:
        if value not in stable:
            stable.append(value)
    return tuple(stable)


def _chmod_private(directory: Path) -> None:
    """Best-effort 0700 on a newly created parent chain element."""
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass


def _chmod_file_private(path: Path) -> None:
    """Best-effort 0600 on a newly written file."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
