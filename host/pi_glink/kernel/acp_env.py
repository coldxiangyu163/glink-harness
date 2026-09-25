"""Per-run configuration for an ACP agent process.

The ACP family is one client protocol over several agents (dsh, codex-acp,
claude-code-acp, …), so this module renders only what every one of them needs
from the host:

* **An exclusive state root.** ``runs/<id>/acp-agent`` is created owner-only and
  exposed to the child as ``HOME``: the in-process ACP agent writes ``~/.claude``,
  ``~/.codex``, dsh sessions and any npm state inside this run's directory
  instead of the operator's home, so two runs cannot share agent state and a
  destroyed run leaves nothing behind. This is the ACP analogue of the pi face's
  per-run ``PI_CODING_AGENT_DIR``. Which agents additionally honour an
  agent-specific variable (``CODEX_HOME``, ``CLAUDE_CONFIG_DIR``, …) is measured
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
from .agent_env import PASSTHROUGH_ENV

#: Per-run ACP agent directory inside the run directory.
ACP_RUNTIME_DIRNAME = "acp-agent"
#: Upper bound on one copied credential file. A credential store is small;
#: a bigger file is refused instead of silently consumed/stored.
CREDENTIAL_FILE_MAX_BYTES = 1 << 20

#: Key-name hints for values that must join the redaction boundary even when a
#: scanner cannot recognise their shape (an opaque token, a short shared secret).
_SECRET_NAME_RE = re.compile(r"(?i)(key|token|secret|password|credential|auth)")
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


def prepare_acp_runtime(
    config: HostConfig,
    *,
    runtime_dir: Path,
    operator_home: Path | None = None,
) -> AcpRuntime:
    """Create one run's ACP state root, inject credentials, return its handle.

    ``runtime_dir`` must be the run's own directory (``<data_root>/runs/<id>/``
    subdirectory): rendering into a shared directory would let two runs
    overwrite each other's agent state. ``operator_home`` is the host user's home
    a credential source's leading ``~`` expands against (never the run HOME).

    Credential sources are read only: the operator's own files are never
    written, moved or chmodded by this function.
    """
    if not config.acp_command.strip():
        raise ConfigError(
            "PI_GLINK_HARNESS=acp 需要 PI_GLINK_ACP_COMMAND（ACP agent 的命令行）"
        )
    directory = Path(runtime_dir)
    home = directory / "home"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    os.chmod(home, 0o700)
    base_home = Path(operator_home) if operator_home is not None else Path.home()
    secrets = list(_inject_credential_files(config, home, base_home))
    # ``PI_GLINK_ACP_ENV`` is the explicit env channel (an API-key style
    # credential that an agent reads from the environment). Its values join the
    # run's redaction boundary by the same rule the credential files use: only
    # a credential-looking *name* makes a value a secret, so an ordinary knob
    # (``ACME_MODE=workspace``) never becomes a redaction target.
    secrets.extend(_env_credential_values(config))
    return AcpRuntime(
        directory=directory,
        home=home,
        command=config.acp_command.strip(),
        secrets=_dedupe(secrets),
        credential_dests=tuple(dest for _source, dest in config.acp_credential_files),
    )


def build_acp_argv(config: HostConfig) -> list[str]:
    """The agent's argv: the frozen command line, split, never shell-expanded."""
    argv = shlex.split(config.acp_command)
    if not argv:
        raise ConfigError(
            "PI_GLINK_ACP_COMMAND 解析后为空；需要给出 ACP agent 的可执行命令"
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

    * **JSON** (codex ``auth.json``): values under a key whose name looks like a
      credential (``*_token``, ``*ApiKey``, ``*secret``, …);
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
        if match and _SECRET_NAME_RE.search(match.group("name")):
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
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags, 0o600)
        except FileExistsError as exc:
            raise ConfigError(f"ACP 凭据目标已存在，拒绝覆盖：{target}") from exc
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, data)
        finally:
            os.close(descriptor)
        secrets.extend(credential_secrets_from_text(data.decode("utf-8", errors="replace")))
    return _dedupe(secrets)


def _collect_json_values(document: object, found: list[str], key_hint: str = "") -> None:
    if isinstance(document, Mapping):
        for key, value in document.items():
            _collect_json_values(value, found, f"{key_hint}{key}")
    elif isinstance(document, list):
        for item in document:
            _collect_json_values(item, found, key_hint)
    elif isinstance(document, str) and _SECRET_NAME_RE.search(key_hint):
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
