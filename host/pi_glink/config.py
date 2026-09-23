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
#: ``local`` keeps the single-user surface; ``multitenant`` requires the
#: gateway-asserted identity material and a container/sandbox execution face.
TENANCY_MODES = ("local", "multitenant")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_DEFAULT_PORT = 7860
_DEFAULT_MAX_GLOBAL_RUNS = 4
_DEFAULT_MAX_TENANT_RUNS = 2
_DEFAULT_ASSERTION_MAX_SECONDS = 300
_DEFAULT_ASSERTION_SKEW_SECONDS = 30
#: 默认使用本仓库自己的云沙箱模板（含 Node + pi）；FrontierAgent 的
#: glink-frontier-e2b 是另一个产品的模板，只沿用它的变量语义，不复用它的模板。
_DEFAULT_E2B_TEMPLATE = "pi-glink-worker"
#: Frozen default for the disposable worker image (shared with ``docker_env``).
DEFAULT_WORKER_IMAGE = "pi-glink-worker:latest"
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
    e2b_api_key: str = field(repr=False, default="")
    #: Frozen Docker worker image; re-read from the environment at run time it
    #: could silently relabel a run that was already accepted.
    worker_image: str = DEFAULT_WORKER_IMAGE
    log_level: str = "INFO"
    task_timeout_seconds: int = 1800
    web_search_key: str = field(repr=False, default="")
    web_search_config_path: Path | None = None
    #: ``local`` (single user) or ``multitenant`` (gateway-asserted identity).
    tenancy_mode: str = "local"
    #: Gateway assertion signing key; required and owner-only in multi-tenant mode.
    gateway_secret_file: Path | None = None
    #: Per-tenant credential file; required and owner-only in multi-tenant mode.
    tenant_config_file: Path | None = None
    tenancy_audience: str = ""
    assertion_max_lifetime_seconds: int = _DEFAULT_ASSERTION_MAX_SECONDS
    assertion_clock_skew_seconds: int = _DEFAULT_ASSERTION_SKEW_SECONDS
    #: Concurrent-run ceilings. The global one covers the whole host, the tenant
    #: one covers a single tenant; both apply only in multi-tenant mode.
    max_global_runs: int = _DEFAULT_MAX_GLOBAL_RUNS
    max_tenant_runs: int = _DEFAULT_MAX_TENANT_RUNS
    #: Container resource ceilings (Docker face only; the image is never changed).
    docker_cpus: str = "2"
    docker_memory: str = "2g"
    docker_pids_limit: int = 512
    #: E2B sandbox lifetime ceiling; 0 keeps the adapter default (task timeout + 300s).
    e2b_sandbox_timeout_seconds: int = 0

    @property
    def multi_tenant(self) -> bool:
        """True when the host runs the explicit multi-tenant surface."""
        return self.tenancy_mode == "multitenant"

    @property
    def has_openai_key(self) -> bool:
        """True when a key was configured; the value itself stays unprinted."""
        return bool(self.openai_api_key)

    @property
    def has_e2b_key(self) -> bool:
        """True when the cloud sandbox credential is configured."""
        return bool(self.e2b_api_key)

    @property
    def has_web_search_key(self) -> bool:
        """True when a usable credential source exists (env key or real file)."""
        if self.web_search_key:
            return True
        return self.web_search_config_path is not None and self.web_search_config_path.is_file()

    @property
    def binds_loopback(self) -> bool:
        return self.host in _LOOPBACK_HOSTS

    def public_view(self) -> dict[str, object]:
        """Fields safe to send to the browser: no credentials, no host paths."""
        return {
            "executor": self.executor,
            "model": self.openai_model,
            "baseUrl": self.openai_base_url,
            "multiTenant": self.multi_tenant,
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
    executor = _text(values, "LOCAL_WEB_EXECUTOR", "inproc").lower()
    tenancy_mode = _text(values, "PI_GLINK_TENANCY", "local").lower()

    if executor not in EXECUTORS:
        raise ConfigError(
            f"LOCAL_WEB_EXECUTOR must be one of {', '.join(EXECUTORS)}; got {executor!r}"
        )
    if tenancy_mode not in TENANCY_MODES:
        raise ConfigError(
            f"PI_GLINK_TENANCY must be one of {', '.join(TENANCY_MODES)}; got {tenancy_mode!r}"
        )
    if host not in _LOOPBACK_HOSTS:
        # One behaviour migration: an unauthenticated host may no longer be
        # exposed beyond loopback. Remote access goes through the local
        # authentication gateway (PI_GLINK_TENANCY=multitenant); the legacy
        # ``LOCAL_WEB_ALLOW_REMOTE`` switch no longer opens one.
        raise ConfigError(
            f"LOCAL_WEB_HOST={host} 不是 loopback；宿主只允许本机监听，"
            "远端访问必须由本机认证网关反代（PI_GLINK_TENANCY=multitenant）"
        )
    if tenancy_mode == "multitenant":
        _require_multitenant(values, executor)

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
        e2b_api_key=e2b_key,
        worker_image=_text(values, "LOCAL_WEB_WORKER_IMAGE", DEFAULT_WORKER_IMAGE),
        log_level=_text(values, "LOCAL_WEB_LOG_LEVEL", "INFO").upper(),
        task_timeout_seconds=_integer(values, "LOCAL_WEB_TASK_TIMEOUT_SECONDS", 1800),
        web_search_key=_text(values, "PI_GLINK_WEB_SEARCH_KEY", ""),
        web_search_config_path=_optional_path(
            values,
            "PI_GLINK_WEB_SEARCH_CONFIG",
            _default_web_search_config(),
        ),
        tenancy_mode=tenancy_mode,
        gateway_secret_file=_optional_path(values, "PI_GLINK_GATEWAY_SECRET_FILE", None),
        tenant_config_file=_optional_path(values, "PI_GLINK_TENANT_CONFIG_FILE", None),
        tenancy_audience=_text(values, "PI_GLINK_TENANCY_AUDIENCE", ""),
        assertion_max_lifetime_seconds=_integer(
            values, "PI_GLINK_ASSERTION_MAX_SECONDS", _DEFAULT_ASSERTION_MAX_SECONDS
        ),
        assertion_clock_skew_seconds=_integer(
            values, "PI_GLINK_ASSERTION_SKEW_SECONDS", _DEFAULT_ASSERTION_SKEW_SECONDS
        ),
        max_global_runs=_integer(values, "PI_GLINK_MAX_GLOBAL_RUNS", _DEFAULT_MAX_GLOBAL_RUNS),
        max_tenant_runs=_integer(values, "PI_GLINK_MAX_TENANT_RUNS", _DEFAULT_MAX_TENANT_RUNS),
        docker_cpus=_text(values, "LOCAL_WEB_DOCKER_CPUS", "2"),
        docker_memory=_text(values, "LOCAL_WEB_DOCKER_MEMORY", "2g"),
        docker_pids_limit=_integer(values, "LOCAL_WEB_DOCKER_PIDS_LIMIT", 512),
        e2b_sandbox_timeout_seconds=_integer(values, "LOCAL_WEB_E2B_TIMEOUT_SECONDS", 0),
    )


def _require_multitenant(values: Mapping[str, str], executor: str) -> None:
    """Reject a multi-tenant configuration that cannot be safe.

    The host only ever binds loopback (the local gateway proxies to it) and the
    execution face must be a container/sandbox (the in-process face shares the
    host's filesystem and is a single-user debug entry), and the identity /
    credential files must be configured. Missing material is a startup refusal,
    never a fallback to the unauthenticated local surface; the files themselves
    are loaded and permission-checked by :func:`pi_glink.tenancy.build_tenancy`
    before the host binds a port.
    """
    if executor == "inproc":
        raise ConfigError(
            "PI_GLINK_TENANCY=multitenant 要求 Docker/E2B 执行面；inproc 仅用于本地单用户调试"
        )
    for key in (
        "PI_GLINK_GATEWAY_SECRET_FILE",
        "PI_GLINK_TENANT_CONFIG_FILE",
        "PI_GLINK_TENANCY_AUDIENCE",
    ):
        if not _text(values, key, ""):
            raise ConfigError(f"{key} 在 PI_GLINK_TENANCY=multitenant 时必须配置")


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


def _path(values: Mapping[str, str], key: str, default: Path) -> Path:
    raw = _text(values, key, "")
    return Path(raw).expanduser() if raw else default


def _optional_path(values: Mapping[str, str], key: str, default: Path | None) -> Path | None:
    raw = _text(values, key, "")
    if raw:
        return Path(raw).expanduser()
    return default


def _default_web_search_config() -> Path | None:
    """The operator's own pi-web-access credential file, if they have one.

    pi-web-access reads its config from ``PI_CODING_AGENT_DIR``, which this host
    redirects to a runtime directory; without seeding the credential there, a
    keyed setup would silently fall back to keyless search. The host never
    invents its own credential format — it copies the file pi already uses.
    """
    candidates = (
        Path.home() / ".pi" / "agent" / "web-search.json",
        Path.home() / ".pi" / "web-search.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None
