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
  the pi face gets (:data:`~pi_glink.kernel.agent_env.PASSTHROUGH_ENV`) and no
  host credential. Credential injection is per-agent work (§4.5 of the plan);
  M1 has no credential to inject and must not hand the agent the host's.

The host never runs the command through a shell: ``PI_GLINK_ACP_COMMAND`` is
split with :func:`shlex.split` and executed as an argv, so a run cannot inherit
shell expansion from the configuration.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..config import ConfigError, HostConfig
from .agent_env import PASSTHROUGH_ENV

#: Per-run ACP agent directory inside the run directory.
ACP_RUNTIME_DIRNAME = "acp-agent"


@dataclass(frozen=True, slots=True)
class AcpRuntime:
    """Where one ACP agent will keep its state, and which command runs it."""

    #: The run's exclusive state root (mode 0700, host-owned).
    directory: Path
    #: ``HOME`` handed to the agent process.
    home: Path
    #: The frozen command line, for the run record and diagnostics.
    command: str


def prepare_acp_runtime(config: HostConfig, *, runtime_dir: Path) -> AcpRuntime:
    """Create one run's ACP state root and return its handle.

    ``runtime_dir`` must be the run's own directory (``<data_root>/runs/<id>/``
    subdirectory): rendering into a shared directory would let two runs
    overwrite each other's agent state. Nothing is copied from the repository
    here — an ACP agent owns its own configuration format, and M1 injects no
    host credential.
    """
    if not config.acp_command.strip():
        raise ConfigError(
            "PI_GLINK_HARNESS=acp 需要 PI_GLINK_ACP_COMMAND（ACP agent 的命令行）"
        )
    directory = Path(runtime_dir)
    home = directory / "home"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return AcpRuntime(directory=directory, home=home, command=config.acp_command.strip())


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
    """Child environment: minimal passthrough, per-run ``HOME``, no credentials.

    ``base_env`` is filtered through :data:`PASSTHROUGH_ENV` (the same list the
    pi face uses) so a caller handing in ``os.environ`` cannot widen the child's
    view of host secrets; the only value this module overrides is ``HOME``.
    """
    source = dict(base_env) if base_env is not None else dict(os.environ)
    env = {name: source[name] for name in PASSTHROUGH_ENV if name in source}
    env["HOME"] = str(runtime.home)
    return env
