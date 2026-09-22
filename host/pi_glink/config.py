"""Immutable host configuration, resolved once at startup.

Variable names deliberately mirror FrontierAgent's ``.env`` semantics
(``OPENAI_*`` for the research model, ``E2B_*`` for the cloud sandbox,
``LOCAL_WEB_*`` for the server and executor) so the two projects can share one
operator mental model. Values are read once and frozen: a running host cannot
be relabelled by editing the environment afterwards.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .dotenv import parse_env_file

EXECUTORS = ("inproc", "docker", "e2b")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_DEFAULT_PORT = 7860
_DEFAULT_E2B_TEMPLATE = "glink-frontier-e2b"
_DEFAULT_CONTEXT_WINDOW = 400_000
_DEFAULT_MAX_OUTPUT_TOKENS = 65_536


class ConfigError(ValueError):
    """Raised when the environment describes a configuration we must not use."""


@dataclass(frozen=True, slots=True)
class HostConfig:
    """Everything the host may decide before a run exists."""

    repo_root: Path
    web_root: Path
    data_root: Path
    host: str
    port: int
    allow_remote: bool
    executor: str
    pi_bin: str
    agent_config_dir: Path
    session_dir: Path
    workspace_root: Path
    openai_base_url: str
    openai_model: str
    openai_api_key: str = field(repr=False, default="")
    context_window: int = _DEFAULT_CONTEXT_WINDOW
    max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS
    e2b_template: str = _DEFAULT_E2B_TEMPLATE
    has_e2b_key: bool = False
    log_level: str = "INFO"

    @property
    def has_openai_key(self) -> bool:
        """True when a key was configured; the value itself stays unprinted."""
        return bool(self.openai_api_key)

    @property
    def binds_loopback(self) -> bool:
        return self.host in _LOOPBACK_HOSTS

    def public_view(self) -> dict[str, object]:
        """Fields safe to send to the browser: no credentials, no host paths."""
        return {
            "executor": self.executor,
            "model": self.openai_model,
            "baseUrl": self.openai_base_url,
            "configured": {
                "model": self.has_openai_key and bool(self.openai_model),
                "e2b": self.has_e2b_key and self.executor == "e2b",
            },
        }


def load_config(
    env: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
    *,
    env_file: Path | None = None,
) -> HostConfig:
    """Build the frozen host configuration.

    Precedence is process environment over ``.env`` file, which is what an
    operator expects when they export a value for a single command.
    """
    root = (repo_root or _default_repo_root()).resolve()
    file_values = parse_env_file(env_file if env_file is not None else root / ".env")
    merged: dict[str, str] = dict(file_values)
    if env is None:
        # A real process only inherits the keys this host owns; unrelated
        # environment variables must not be able to relabel the run.
        merged.update(
            {key: value for key, value in os.environ.items() if _looks_configured(key)}
        )
    else:
        merged.update(env)
    return build_config(merged, root)


def build_config(values: Mapping[str, str], repo_root: Path) -> HostConfig:
    host = _text(values, "LOCAL_WEB_HOST", "127.0.0.1")
    port = _integer(values, "LOCAL_WEB_PORT", _DEFAULT_PORT)
    allow_remote = _flag(values, "LOCAL_WEB_ALLOW_REMOTE", False)
    executor = _text(values, "LOCAL_WEB_EXECUTOR", "inproc").lower()

    if executor not in EXECUTORS:
        raise ConfigError(
            f"LOCAL_WEB_EXECUTOR must be one of {', '.join(EXECUTORS)}; got {executor!r}"
        )
    if host not in _LOOPBACK_HOSTS and not allow_remote:
        raise ConfigError(
            f"LOCAL_WEB_HOST={host} is not loopback; set LOCAL_WEB_ALLOW_REMOTE=1 to allow it"
        )

    api_key = _text(values, "OPENAI_API_KEY", "")
    e2b_key = _text(values, "E2B_API_KEY", "")
    data_root = _path(values, "LOCAL_WEB_DATA_ROOT", Path.home() / ".pi-glink")
    agent_config_dir = _path(
        values, "PI_CODING_AGENT_DIR", repo_root / "agent"
    )

    return HostConfig(
        repo_root=repo_root,
        web_root=repo_root / "web",
        data_root=data_root,
        host=host,
        port=port,
        allow_remote=allow_remote,
        executor=executor,
        pi_bin=_text(values, "PI_GLINK_PI_BIN", "pi"),
        agent_config_dir=agent_config_dir,
        session_dir=_path(values, "PI_CODING_AGENT_SESSION_DIR", data_root / "sessions"),
        workspace_root=_path(values, "PI_GLINK_WORKSPACE_ROOT", data_root / "workspaces"),
        openai_base_url=_text(values, "OPENAI_BASE_URL", ""),
        openai_model=_text(values, "OPENAI_MODEL", ""),
        openai_api_key=api_key,
        context_window=_integer(
            values, "OPENAI_CONTEXT_WINDOW", _DEFAULT_CONTEXT_WINDOW
        ),
        max_output_tokens=_integer(
            values, "OPENAI_MAX_TOKENS", _DEFAULT_MAX_OUTPUT_TOKENS
        ),
        e2b_template=_text(values, "LOCAL_WEB_E2B_TEMPLATE", _DEFAULT_E2B_TEMPLATE),
        has_e2b_key=bool(e2b_key),
        log_level=_text(values, "LOCAL_WEB_LOG_LEVEL", "INFO").upper(),
    )


def _default_repo_root() -> Path:
    # host/pi_glink/config.py -> repository root
    return Path(__file__).resolve().parents[2]


def _looks_configured(key: str) -> bool:
    return key.startswith(("LOCAL_WEB_", "PI_GLINK_", "PI_CODING_AGENT_")) or key in {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "E2B_API_KEY",
    }


def _text(values: Mapping[str, str], key: str, default: str) -> str:
    return (values.get(key) or "").strip() or default


def _integer(values: Mapping[str, str], key: str, default: int) -> int:
    raw = _text(values, key, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer; got {raw!r}") from exc


def _flag(values: Mapping[str, str], key: str, default: bool) -> bool:
    raw = values.get(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _path(values: Mapping[str, str], key: str, default: Path) -> Path:
    raw = _text(values, key, "")
    return Path(raw).expanduser() if raw else default
