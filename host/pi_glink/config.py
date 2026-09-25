"""Immutable host configuration, resolved once at startup.

Variable names deliberately mirror FrontierAgent's ``.env`` semantics
(``OPENAI_*`` for the research model, ``E2B_*`` for the cloud sandbox,
``LOCAL_WEB_*`` for the server and executor) so the two projects can share one
operator mental model. Values are read once and frozen: a running host cannot
be relabelled by editing the environment afterwards.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .dotenv import parse_env_file

#: ACP permission options the host may select when a run is explicitly given
#: an allowlist (``PI_GLINK_ACP_PERMISSION_ALLOW``). The default host answer is
#: fail-closed; see :data:`HostConfig.acp_permission_allow`.
ACP_PERMISSION_OPTION_KINDS = ("allow_once", "allow_always")
#: The ACP tool-kind vocabulary (schema ``ToolKind``); only these tokens may be
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
_ACP_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

EXECUTORS = ("inproc", "docker", "e2b")
#: Kernel adapter families the host can drive. ``pi`` is the existing kernel
#: (all three execution faces); ``acp`` is the ACP client family, which in M1
#: runs on ``inproc`` only (see docs/multi-harness-plan.md).
HARNESSES = ("pi", "acp")
#: ``local`` keeps the single-user surface; ``multitenant`` requires the
#: gateway-asserted identity material and a container/sandbox execution face.
TENANCY_MODES = ("local", "multitenant")
#: Multi-tenant identity entry points. ``gateway`` keeps the signed-assertion
#: contract (default); ``accounts`` adds the in-app invitation/login surface
#: with server-side sessions. Both end in the same server-side TenantContext.
AUTH_MODES = ("gateway", "accounts")
#: Where a multi-tenant run's model credential comes from. ``tenant`` keeps the
#: original contract (a per-tenant ``modelApiKey`` in the credential file, a
#: missing key refuses admission); ``shared`` is the explicit SaaS policy where
#: the host freezes one credential at startup and every tenant uses it. The
#: shared policy is deliberately opt-in: it is never a global fallback for a
#: misconfigured ``tenant`` deployment.
MODEL_CREDENTIAL_POLICIES = ("tenant", "shared")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_DEFAULT_PORT = 7860
_DEFAULT_MAX_GLOBAL_RUNS = 4
_DEFAULT_MAX_TENANT_RUNS = 2
_DEFAULT_MAX_GLOBAL_STREAMS = 64
_DEFAULT_MAX_TENANT_STREAMS = 16
_DEFAULT_ASSERTION_MAX_SECONDS = 300
_DEFAULT_ASSERTION_SKEW_SECONDS = 30
#: 默认使用本仓库自己的云沙箱模板（含 Node + pi）；FrontierAgent 的
#: glink-frontier-e2b 是另一个产品的模板，只沿用它的变量语义，不复用它的模板。
_DEFAULT_E2B_TEMPLATE = "pi-glink-worker"
#: The ACP family's E2B template (``LOCAL_WEB_E2B_ACP_TEMPLATE``). The pi template
#: carries pi, not the ACP agents, so the ACP sandbox face gets its own dev
#: template (``docker/e2b/Dockerfile.acp-e2b``), built and self-checked in a real
#: sandbox before it was wired (see ``docs/acp-integration-notes.md`` M3b).
_DEFAULT_ACP_E2B_TEMPLATE = "glink-acp-worker"
#: Frozen default for the disposable worker image (shared with ``docker_env``).
DEFAULT_WORKER_IMAGE = "pi-glink-worker:latest"
#: Frozen default for the ACP worker image (``docker/Dockerfile.acp-worker``):
#: the pi worker image carries pi, not the ACP agents, so the ACP docker face
#: gets its own image instead of silently reusing the pi one.
DEFAULT_ACP_WORKER_IMAGE = "pi-glink-acp-worker:latest"
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
    #: Kernel adapter family for every run of this host: ``pi`` (default) or
    #: ``acp``. A host-level, startup-frozen choice; M1 deliberately has no
    #: per-run API/query parameter and no front-end surface for it.
    harness: str = "pi"
    #: Command line of the ACP agent process (``PI_GLINK_ACP_COMMAND``), e.g.
    #: ``"dsh acp"``. Split with :func:`shlex.split` and executed without a
    #: shell; frozen at startup so a run cannot be relabelled later. Only
    #: consulted when ``harness`` is ``acp``.
    acp_command: str = ""
    #: Credential files to copy into each run's exclusive ``HOME``
    #: (``PI_GLINK_ACP_CREDENTIAL_FILES``): ``source=dest`` entries separated by
    #: ``;``. ``source`` is read only (a leading ``~/`` expands to the operator's
    #: home) and ``dest`` is a path under the run HOME. ACP agents read their
    #: credentials from their own state directory inside ``HOME``, so this is the
    #: per-agent injection point; values never enter the host environment.
    #: Each entry's credential values are added to the run's redaction boundary.
    acp_credential_files: tuple[tuple[str, str], ...] = ()
    #: Extra ``NAME=value`` entries for the ACP agent process
    #: (``PI_GLINK_ACP_ENV``, ``;``-separated). ``HOME`` may not be overridden:
    #: the per-run state root stays the isolation boundary. A value whose name
    #: looks credential-shaped joins the run's redaction boundary.
    acp_env: tuple[tuple[str, str], ...] = ()
    #: Explicit allowlist for ACP permission answers
    #: (``PI_GLINK_ACP_PERMISSION_ALLOW``, comma/space separated). Empty (the
    #: default) keeps the fail-closed answer. Tokens are ACP option kinds
    #: (:data:`ACP_PERMISSION_OPTION_KINDS`) and/or tool kinds
    #: (:data:`ACP_PERMISSION_TOOL_KINDS`). An allow answer needs a matching
    #: option kind; when any tool kind is configured the request's tool kind
    #: must match too (a request without a tool kind is refused).
    acp_permission_allow: tuple[str, ...] = ()
    #: ACP session configuration applied right after ``session/new``
    #: (``PI_GLINK_ACP_SESSION_CONFIG``, ``;``-separated ``id=value`` entries;
    #: e.g. ``mode=auto``). An agent that does not advertise the option/value
    #: fails the run: an explicitly configured value is never silently dropped.
    acp_session_config: tuple[tuple[str, str], ...] = ()
    openai_api_key: str = field(repr=False, default="")
    context_window: int = _DEFAULT_CONTEXT_WINDOW
    max_output_tokens: int = _DEFAULT_MAX_OUTPUT_TOKENS
    e2b_template: str = _DEFAULT_E2B_TEMPLATE
    e2b_api_key: str = field(repr=False, default="")
    #: Frozen Docker worker image; re-read from the environment at run time it
    #: could silently relabel a run that was already accepted.
    worker_image: str = DEFAULT_WORKER_IMAGE
    #: Frozen ACP worker image (``LOCAL_WEB_ACP_WORKER_IMAGE``): the image the
    #: ACP family runs in, kept separate from :attr:`worker_image` because the
    #: two images carry different agents. Only consulted when ``harness`` is
    #: ``acp`` and ``executor`` is ``docker``.
    acp_worker_image: str = DEFAULT_ACP_WORKER_IMAGE
    #: Frozen E2B template for the ACP sandbox face (``LOCAL_WEB_E2B_ACP_TEMPLATE``),
    #: separate from :attr:`e2b_template` for the same reason
    #: :attr:`acp_worker_image` is separate from :attr:`worker_image`: the two
    #: templates carry different harnesses. Only consulted when ``harness`` is
    #: ``acp`` and ``executor`` is ``e2b``.
    acp_e2b_template: str = _DEFAULT_ACP_E2B_TEMPLATE
    log_level: str = "INFO"
    task_timeout_seconds: int = 1800
    web_search_key: str = field(repr=False, default="")
    web_search_config_path: Path | None = None
    #: ``local`` (single user) or ``multitenant`` (gateway-asserted identity).
    tenancy_mode: str = "local"
    #: Multi-tenant identity entry: ``gateway`` (signed assertion, default) or
    #: ``accounts`` (in-app invitation registration + login + server sessions).
    auth_mode: str = "gateway"
    #: Model credential policy: ``tenant`` (default, per-tenant key file) or
    #: ``shared`` (one host-frozen credential for every tenant; only valid in
    #: multi-tenant mode, where it is an explicit opt-in).
    model_credentials: str = "tenant"
    #: Gateway assertion signing key; required and owner-only in gateway mode.
    gateway_secret_file: Path | None = None
    #: Accounts database; default ``<data_root>/accounts/accounts.db``, must
    #: stay inside the data root and outside the repository. The directory is
    #: created owner-only (0700) and the file 0600. Only read in ``accounts`` mode.
    accounts_db: Path | None = None
    #: External HTTPS origin of the deployment, when the host sits behind a TLS
    #: terminating proxy. Frozen, trusted configuration: used for Origin checks
    #: instead of trusting ``X-Forwarded-*`` headers.
    public_origin: str = ""
    #: ``Secure`` attribute on the session cookie. Production is HTTPS-only;
    #: only a loopback HTTP debug run may switch it off explicitly.
    session_cookie_secure: bool = True
    session_absolute_seconds: int = 43200
    session_idle_seconds: int = 7200
    invite_ttl_seconds: int = 172800
    login_max_attempts: int = 5
    login_window_seconds: int = 300
    #: Login attempts per socket peer (the TLS proxy, when one is in front).
    #: ``X-Forwarded-For`` is deliberately not consulted, so this is a global
    #: budget for a proxied deployment; it bounds the password-hash work a
    #: client can trigger by rotating e-mail addresses.
    login_peer_max_attempts: int = 120
    login_peer_window_seconds: int = 60
    register_max_attempts: int = 10
    register_window_seconds: int = 900
    invite_check_max_attempts: int = 60
    invite_check_window_seconds: int = 300
    #: Per-tenant credential file; required and owner-only in multi-tenant mode.
    tenant_config_file: Path | None = None
    tenancy_audience: str = ""
    assertion_max_lifetime_seconds: int = _DEFAULT_ASSERTION_MAX_SECONDS
    assertion_clock_skew_seconds: int = _DEFAULT_ASSERTION_SKEW_SECONDS
    #: Concurrent-run ceilings. The global one covers the whole host, the tenant
    #: one covers a single tenant; both apply only in multi-tenant mode.
    max_global_runs: int = _DEFAULT_MAX_GLOBAL_RUNS
    max_tenant_runs: int = _DEFAULT_MAX_TENANT_RUNS
    #: Concurrent SSE connections. The global ceiling covers the whole host; the
    #: tenant one applies in multi-tenant mode only (local mode has no tenant).
    #: An over-limit connection is refused with 429 ``stream_limit`` before any
    #: stream header is written.
    max_global_streams: int = _DEFAULT_MAX_GLOBAL_STREAMS
    max_tenant_streams: int = _DEFAULT_MAX_TENANT_STREAMS
    #: Container resource ceilings (Docker face only; the image is never changed).
    docker_cpus: str = "2"
    docker_memory: str = "2g"
    docker_pids_limit: int = 512
    #: E2B sandbox lifetime ceiling; 0 keeps the adapter default (task timeout + 300s).
    e2b_sandbox_timeout_seconds: int = 0
    #: Interval of the online resource ledger sweep (seconds). The sweep asks
    #: the execution face whether the resources of an unconfirmed hold still
    #: exist, addressing only the identity persisted with the run. ``0`` disables
    #: it; the offline ``--unreleased`` / ``--mark-released`` path still works.
    resource_reconcile_seconds: int = 60

    @property
    def multi_tenant(self) -> bool:
        """True when the host runs the explicit multi-tenant surface."""
        return self.tenancy_mode == "multitenant"

    @property
    def identity_mode(self) -> str:
        """The effective identity entry: ``local``, ``gateway`` or ``accounts``."""
        return self.auth_mode if self.multi_tenant else "local"

    @property
    def shared_model_credentials(self) -> bool:
        """True when the host provides one frozen model credential for every tenant."""
        return self.model_credentials == "shared"

    @property
    def accounts_enabled(self) -> bool:
        return self.multi_tenant and self.auth_mode == "accounts"

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
            "authMode": self.identity_mode,
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
    port = _port(values, "LOCAL_WEB_PORT", _DEFAULT_PORT)
    executor = _text(values, "LOCAL_WEB_EXECUTOR", "inproc").lower()
    harness = _text(values, "PI_GLINK_HARNESS", "pi").lower()
    acp_command = _text(values, "PI_GLINK_ACP_COMMAND", "")
    acp_credential_files = _acp_credential_files(values)
    acp_env = _acp_env_entries(values)
    acp_permission_allow = _acp_permission_allow(values)
    acp_session_config = _acp_session_config(values)
    tenancy_mode = _text(values, "PI_GLINK_TENANCY", "local").lower()
    auth_mode = _text(values, "PI_GLINK_AUTH_MODE", "gateway").lower()
    model_credentials = _text(values, "PI_GLINK_MODEL_CREDENTIALS", "tenant").lower()

    if executor not in EXECUTORS:
        raise ConfigError(
            f"LOCAL_WEB_EXECUTOR must be one of {', '.join(EXECUTORS)}; got {executor!r}"
        )
    if harness not in HARNESSES:
        raise ConfigError(
            f"PI_GLINK_HARNESS must be one of {', '.join(HARNESSES)}; got {harness!r}"
        )
    if harness == "acp" and not acp_command:
        # Fail closed at startup: an ACP host with no agent command could only
        # fail every run later, or (worse) fall back to pi and mislabel it.
        raise ConfigError(
            "PI_GLINK_HARNESS=acp 需要 PI_GLINK_ACP_COMMAND（ACP agent 的命令行）"
        )
    if tenancy_mode not in TENANCY_MODES:
        raise ConfigError(
            f"PI_GLINK_TENANCY must be one of {', '.join(TENANCY_MODES)}; got {tenancy_mode!r}"
        )
    if auth_mode not in AUTH_MODES:
        raise ConfigError(
            f"PI_GLINK_AUTH_MODE must be one of {', '.join(AUTH_MODES)}; got {auth_mode!r}"
        )
    if model_credentials not in MODEL_CREDENTIAL_POLICIES:
        raise ConfigError(
            "PI_GLINK_MODEL_CREDENTIALS must be one of "
            f"{', '.join(MODEL_CREDENTIAL_POLICIES)}; got {model_credentials!r}"
        )
    if model_credentials == "shared" and tenancy_mode != "multitenant":
        # The shared policy exists for the multi-tenant SaaS deployment; it must
        # never be a way to label a single-user host as something else.
        raise ConfigError(
            "PI_GLINK_MODEL_CREDENTIALS=shared 需要 PI_GLINK_TENANCY=multitenant"
        )
    if model_credentials == "shared" and _text(values, "PI_GLINK_TENANT_CONFIG_FILE", ""):
        # The shared policy has exactly one credential source: the frozen host
        # configuration. A tenant credential file (even a credential-free
        # registry) would be a second, unread entry point, so it is refused
        # outright instead of being silently ignored.
        raise ConfigError(
            "PI_GLINK_MODEL_CREDENTIALS=shared 不接受 PI_GLINK_TENANT_CONFIG_FILE："
            "共享模式不读取任何租户凭据配置"
        )
    if auth_mode == "accounts" and tenancy_mode != "multitenant":
        # Never a silent downgrade to the unauthenticated local surface: the
        # in-app accounts mode only exists as a multi-tenant identity entry.
        raise ConfigError(
            "PI_GLINK_AUTH_MODE=accounts 需要 PI_GLINK_TENANCY=multitenant"
        )
    if tenancy_mode == "multitenant":
        _require_multitenant(values, executor, auth_mode, model_credentials)

    api_key = _text(values, "OPENAI_API_KEY", "")
    base_url = _text(values, "OPENAI_BASE_URL", "")
    model = _text(values, "OPENAI_MODEL", "")
    if model_credentials == "shared":
        # Fail closed at startup: a "shared" deployment that cannot name its
        # credential source must not boot and later fall back to a tenant entry
        # or to the single-user local configuration.
        missing = [
            name
            for name, value in (
                ("OPENAI_API_KEY", api_key),
                ("OPENAI_BASE_URL", base_url),
                ("OPENAI_MODEL", model),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "PI_GLINK_MODEL_CREDENTIALS=shared 需要宿主冻结 " + "、".join(missing)
            )
    e2b_key = _text(values, "E2B_API_KEY", "")
    data_root = _path(values, "LOCAL_WEB_DATA_ROOT", Path.home() / ".pi-glink")
    agent_config_dir = _path(
        values, "PI_CODING_AGENT_DIR", repo_root / "agent"
    )
    accounts_db = _optional_path(
        values, "PI_GLINK_ACCOUNTS_DB", data_root / "accounts" / "accounts.db"
    )
    if auth_mode == "accounts" and accounts_db is not None:
        _require_accounts_db(accounts_db, data_root, repo_root)
    public_origin = _origin(values, "PI_GLINK_PUBLIC_ORIGIN", "")

    config = HostConfig(
        repo_root=repo_root,
        web_root=repo_root / "web",
        data_root=data_root,
        host=host,
        port=port,
        executor=executor,
        harness=harness,
        acp_command=acp_command,
        acp_credential_files=acp_credential_files,
        acp_env=acp_env,
        acp_permission_allow=acp_permission_allow,
        acp_session_config=acp_session_config,
        pi_bin=_text(values, "PI_GLINK_PI_BIN", "pi"),
        agent_config_dir=agent_config_dir,
        session_dir=_path(values, "PI_CODING_AGENT_SESSION_DIR", data_root / "sessions"),
        workspace_root=_path(values, "PI_GLINK_WORKSPACE_ROOT", data_root / "workspaces"),
        openai_base_url=base_url,
        openai_model=model,
        openai_api_key=api_key,
        context_window=_positive_integer(
            values, "OPENAI_CONTEXT_WINDOW", _DEFAULT_CONTEXT_WINDOW
        ),
        max_output_tokens=_positive_integer(
            values, "OPENAI_MAX_TOKENS", _DEFAULT_MAX_OUTPUT_TOKENS
        ),
        e2b_template=_text(values, "LOCAL_WEB_E2B_TEMPLATE", _DEFAULT_E2B_TEMPLATE),
        e2b_api_key=e2b_key,
        worker_image=_text(values, "LOCAL_WEB_WORKER_IMAGE", DEFAULT_WORKER_IMAGE),
        acp_worker_image=_text(
            values, "LOCAL_WEB_ACP_WORKER_IMAGE", DEFAULT_ACP_WORKER_IMAGE
        ),
        acp_e2b_template=_text(
            values, "LOCAL_WEB_E2B_ACP_TEMPLATE", _DEFAULT_ACP_E2B_TEMPLATE
        ),
        log_level=_text(values, "LOCAL_WEB_LOG_LEVEL", "INFO").upper(),
        task_timeout_seconds=_positive_integer(
            values, "LOCAL_WEB_TASK_TIMEOUT_SECONDS", 1800
        ),
        web_search_key=_text(values, "PI_GLINK_WEB_SEARCH_KEY", ""),
        web_search_config_path=_optional_path(
            values,
            "PI_GLINK_WEB_SEARCH_CONFIG",
            _default_web_search_config(),
        ),
        tenancy_mode=tenancy_mode,
        auth_mode=auth_mode,
        model_credentials=model_credentials,
        gateway_secret_file=_optional_path(values, "PI_GLINK_GATEWAY_SECRET_FILE", None),
        accounts_db=accounts_db,
        public_origin=public_origin,
        session_cookie_secure=_flag(values, "PI_GLINK_COOKIE_SECURE", True),
        session_absolute_seconds=_positive_integer(
            values, "PI_GLINK_SESSION_ABSOLUTE_SECONDS", 43200
        ),
        session_idle_seconds=_positive_integer(values, "PI_GLINK_SESSION_IDLE_SECONDS", 7200),
        invite_ttl_seconds=_positive_integer(values, "PI_GLINK_INVITE_TTL_SECONDS", 172800),
        login_max_attempts=_positive_integer(values, "PI_GLINK_LOGIN_MAX_ATTEMPTS", 5),
        login_window_seconds=_positive_integer(values, "PI_GLINK_LOGIN_WINDOW_SECONDS", 300),
        login_peer_max_attempts=_positive_integer(
            values, "PI_GLINK_LOGIN_PEER_MAX_ATTEMPTS", 120
        ),
        login_peer_window_seconds=_positive_integer(
            values, "PI_GLINK_LOGIN_PEER_WINDOW_SECONDS", 60
        ),
        register_max_attempts=_positive_integer(values, "PI_GLINK_REGISTER_MAX_ATTEMPTS", 10),
        register_window_seconds=_positive_integer(
            values, "PI_GLINK_REGISTER_WINDOW_SECONDS", 900
        ),
        invite_check_max_attempts=_positive_integer(
            values, "PI_GLINK_INVITE_CHECK_MAX_ATTEMPTS", 60
        ),
        invite_check_window_seconds=_positive_integer(
            values, "PI_GLINK_INVITE_CHECK_WINDOW_SECONDS", 300
        ),
        tenant_config_file=_optional_path(values, "PI_GLINK_TENANT_CONFIG_FILE", None),
        tenancy_audience=_text(values, "PI_GLINK_TENANCY_AUDIENCE", ""),
        assertion_max_lifetime_seconds=_positive_integer(
            values, "PI_GLINK_ASSERTION_MAX_SECONDS", _DEFAULT_ASSERTION_MAX_SECONDS
        ),
        assertion_clock_skew_seconds=_non_negative_integer(
            values, "PI_GLINK_ASSERTION_SKEW_SECONDS", _DEFAULT_ASSERTION_SKEW_SECONDS
        ),
        max_global_runs=_positive_integer(
            values, "PI_GLINK_MAX_GLOBAL_RUNS", _DEFAULT_MAX_GLOBAL_RUNS
        ),
        max_tenant_runs=_positive_integer(
            values, "PI_GLINK_MAX_TENANT_RUNS", _DEFAULT_MAX_TENANT_RUNS
        ),
        max_global_streams=_positive_integer(
            values, "PI_GLINK_MAX_GLOBAL_STREAMS", _DEFAULT_MAX_GLOBAL_STREAMS
        ),
        max_tenant_streams=_positive_integer(
            values, "PI_GLINK_MAX_TENANT_STREAMS", _DEFAULT_MAX_TENANT_STREAMS
        ),
        docker_cpus=_text(values, "LOCAL_WEB_DOCKER_CPUS", "2"),
        docker_memory=_text(values, "LOCAL_WEB_DOCKER_MEMORY", "2g"),
        docker_pids_limit=_positive_integer(values, "LOCAL_WEB_DOCKER_PIDS_LIMIT", 512),
        e2b_sandbox_timeout_seconds=_non_negative_integer(
            values, "LOCAL_WEB_E2B_TIMEOUT_SECONDS", 0
        ),
        resource_reconcile_seconds=_non_negative_integer(
            values, "PI_GLINK_RESOURCE_RECONCILE_SECONDS", 60
        ),
    )
    validate_host_config(config)
    return config


def validate_host_config(config: HostConfig) -> None:
    """Re-check the construction invariants that a caller may override later.

    ``build_config`` runs this before returning, and the CLI runs it again after
    ``--host``/``--port`` overrides: an operator-supplied value must not be able
    to skip the loopback or range checks by arriving after construction.
    """
    if config.host not in _LOOPBACK_HOSTS:
        # One behaviour migration: an unauthenticated host may no longer be
        # exposed beyond loopback. Remote access goes through the local
        # authentication gateway (PI_GLINK_TENANCY=multitenant); the legacy
        # ``LOCAL_WEB_ALLOW_REMOTE`` switch no longer opens one.
        raise ConfigError(
            f"LOCAL_WEB_HOST={config.host} 不是 loopback；宿主只允许本机监听，"
            "远端访问必须由本机认证网关反代（PI_GLINK_TENANCY=multitenant）"
        )
    if not 0 <= config.port <= 65535:
        raise ConfigError(
            f"LOCAL_WEB_PORT 必须在 0–65535 之间（0 = 由内核分配临时端口）；当前 {config.port}"
        )
    if config.executor not in EXECUTORS:
        raise ConfigError(
            f"LOCAL_WEB_EXECUTOR must be one of {', '.join(EXECUTORS)}; got {config.executor!r}"
        )
    if config.harness not in HARNESSES:
        raise ConfigError(
            f"PI_GLINK_HARNESS must be one of {', '.join(HARNESSES)}; got {config.harness!r}"
        )
    if config.harness == "acp":
        if not config.acp_command:
            raise ConfigError(
                "PI_GLINK_HARNESS=acp 需要 PI_GLINK_ACP_COMMAND（ACP agent 的命令行）"
            )
    if config.tenancy_mode not in TENANCY_MODES:
        raise ConfigError(
            f"PI_GLINK_TENANCY must be one of {', '.join(TENANCY_MODES)}; got {config.tenancy_mode!r}"
        )
    if config.multi_tenant and config.executor == "inproc":
        raise ConfigError(
            "PI_GLINK_TENANCY=multitenant 要求 Docker/E2B 执行面；inproc 仅用于本地单用户调试"
        )
    _acp_knobs_are_acp_only(config)


def _require_multitenant(
    values: Mapping[str, str], executor: str, auth_mode: str, model_credentials: str
) -> None:
    """Reject a multi-tenant configuration that cannot be safe.

    The host only ever binds loopback (the local gateway proxies to it) and the
    execution face must be a container/sandbox (the in-process face shares the
    host's filesystem and is a single-user debug entry), and the credential
    file must be configured under the default ``tenant`` policy. Under the
    explicit ``shared`` policy no tenant credential file may be configured at
    all (see :func:`build_config`); gateway mode additionally needs the
    assertion audience and signing key. Missing material is a startup refusal,
    never a fallback to the unauthenticated local surface or to the other
    credential policy; the files themselves are loaded and permission-checked
    by :func:`pi_glink.tenancy.build_tenancy` before the host binds a port.
    """
    if executor == "inproc":
        raise ConfigError(
            "PI_GLINK_TENANCY=multitenant 要求 Docker/E2B 执行面；inproc 仅用于本地单用户调试"
        )
    required = [] if model_credentials == "shared" else ["PI_GLINK_TENANT_CONFIG_FILE"]
    if auth_mode == "gateway":
        required += ["PI_GLINK_GATEWAY_SECRET_FILE", "PI_GLINK_TENANCY_AUDIENCE"]
    for key in required:
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


def _acp_credential_files(values: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """Parse ``PI_GLINK_ACP_CREDENTIAL_FILES``: ``source=dest`` entries.

    ``dest`` must stay under the run HOME: an absolute path or a ``..`` segment
    would put a credential somewhere other than the run's own state directory
    (and could overwrite host files), so it is refused at startup. Two entries
    may not target the same destination.
    """
    raw = _text(values, "PI_GLINK_ACP_CREDENTIAL_FILES", "")
    if not raw:
        return ()
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for chunk in raw.split(";"):
        item = chunk.strip()
        if not item:
            continue
        source, separator, dest = item.partition("=")
        source, dest = source.strip(), dest.strip()
        if not separator or not source or not dest:
            raise ConfigError(
                f"PI_GLINK_ACP_CREDENTIAL_FILES 条目必须是 source=dest；当前 {item!r}"
            )
        parts = Path(dest).parts
        if Path(dest).is_absolute() or ".." in parts or dest.startswith("~"):
            raise ConfigError(
                f"PI_GLINK_ACP_CREDENTIAL_FILES 的目标必须是 run HOME 内的相对路径；当前 {dest!r}"
            )
        if dest in seen:
            raise ConfigError(f"PI_GLINK_ACP_CREDENTIAL_FILES 目标重复：{dest!r}")
        seen.add(dest)
        entries.append((source, dest))
    return tuple(entries)


def _acp_env_entries(values: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """Parse ``PI_GLINK_ACP_ENV``: ``NAME=value`` entries.

    ``HOME`` is the run's isolation boundary and may not be overridden from the
    configuration; a value may be empty (an empty proxy variable is meaningful).
    """
    raw = _text(values, "PI_GLINK_ACP_ENV", "")
    if not raw:
        return ()
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for chunk in raw.split(";"):
        item = chunk.strip()
        if not item:
            continue
        name, separator, value = item.partition("=")
        name, value = name.strip(), value.strip()
        if not separator or not _ACP_ENV_NAME_RE.match(name):
            raise ConfigError(
                f"PI_GLINK_ACP_ENV 条目必须是 NAME=value；当前 {item!r}"
            )
        if name == "HOME":
            raise ConfigError("PI_GLINK_ACP_ENV 不允许覆盖 HOME（每 run 状态根是隔离边界）")
        if name in seen:
            raise ConfigError(f"PI_GLINK_ACP_ENV 变量重复：{name}")
        seen.add(name)
        entries.append((name, value))
    return tuple(entries)


def _acp_permission_allow(values: Mapping[str, str]) -> tuple[str, ...]:
    """Parse ``PI_GLINK_ACP_PERMISSION_ALLOW``: option and/or tool kinds."""
    raw = _text(values, "PI_GLINK_ACP_PERMISSION_ALLOW", "")
    if not raw:
        return ()
    allowed = set(ACP_PERMISSION_OPTION_KINDS) | set(ACP_PERMISSION_TOOL_KINDS)
    tokens: list[str] = []
    for token in re.split(r"[,\s]+", raw):
        if not token:
            continue
        if token not in allowed:
            raise ConfigError(
                "PI_GLINK_ACP_PERMISSION_ALLOW 只接受 ACP 的 allow 选项类型或工具类型；"
                f"当前 {token!r}"
            )
        if token not in tokens:
            tokens.append(token)
    return tuple(tokens)


def _acp_session_config(values: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """Parse ``PI_GLINK_ACP_SESSION_CONFIG``: ``id=value`` entries."""
    raw = _text(values, "PI_GLINK_ACP_SESSION_CONFIG", "")
    if not raw:
        return ()
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for chunk in raw.split(";"):
        item = chunk.strip()
        if not item:
            continue
        config_id, separator, value = item.partition("=")
        config_id, value = config_id.strip(), value.strip()
        if not separator or not config_id or not value:
            raise ConfigError(
                f"PI_GLINK_ACP_SESSION_CONFIG 条目必须是 id=value；当前 {item!r}"
            )
        if config_id in seen:
            raise ConfigError(f"PI_GLINK_ACP_SESSION_CONFIG 选项重复：{config_id}")
        seen.add(config_id)
        entries.append((config_id, value))
    return tuple(entries)


def _acp_knobs_are_acp_only(config: HostConfig) -> None:
    """Refuse ACP-only knobs on a non-ACP host instead of ignoring them.

    ``PI_GLINK_ACP_COMMAND`` keeps its M1 behaviour (inert under ``pi``); every
    knob added in M2 changes what a run's process may see or do, so an operator
    who set one while the host drives pi gets a startup error rather than a
    silent no-op.
    """
    if config.harness == "acp":
        return
    if config.acp_credential_files or config.acp_env or config.acp_permission_allow or config.acp_session_config:
        raise ConfigError(
            "PI_GLINK_ACP_CREDENTIAL_FILES / PI_GLINK_ACP_ENV / "
            "PI_GLINK_ACP_PERMISSION_ALLOW / PI_GLINK_ACP_SESSION_CONFIG "
            "只在 PI_GLINK_HARNESS=acp 下生效；当前 harness="
            f"{config.harness!r}"
        )


def _text(values: Mapping[str, str], key: str, default: str) -> str:
    return (values.get(key) or "").strip() or default


def _integer(values: Mapping[str, str], key: str, default: int) -> int:
    raw = _text(values, key, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer; got {raw!r}") from exc


def _non_negative_integer(values: Mapping[str, str], key: str, default: int) -> int:
    value = _integer(values, key, default)
    if value < 0:
        raise ConfigError(f"{key} 不能为负值；当前 {value}")
    return value


def _positive_integer(values: Mapping[str, str], key: str, default: int) -> int:
    value = _integer(values, key, default)
    if value < 1:
        raise ConfigError(f"{key} 必须为 >= 1 的整数；当前 {value}")
    return value


def _port(values: Mapping[str, str], key: str, default: int) -> int:
    """A TCP port: 0 asks the kernel for an ephemeral one (tests, local debug)."""
    value = _integer(values, key, default)
    if not 0 <= value <= 65535:
        raise ConfigError(f"{key} 必须在 0–65535 之间（0 = 内核分配临时端口）；当前 {value}")
    return value


def _path(values: Mapping[str, str], key: str, default: Path) -> Path:
    raw = _text(values, key, "")
    return Path(raw).expanduser() if raw else default


def _optional_path(values: Mapping[str, str], key: str, default: Path | None) -> Path | None:
    raw = _text(values, key, "")
    if raw:
        return Path(raw).expanduser()
    return default


def _flag(values: Mapping[str, str], key: str, default: bool) -> bool:
    raw = _text(values, key, "1" if default else "0").lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{key} must be a boolean; got {raw!r}")


def _origin(values: Mapping[str, str], key: str, default: str) -> str:
    """Freeze the external HTTPS origin (scheme + host), or refuse the value."""
    raw = _text(values, key, default)
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ConfigError(f"{key} 必须是 http(s) 源（如 https://glink.example.com）: {raw!r}")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise ConfigError(f"{key} 只接受源，不接受路径/查询/片段: {raw!r}")
    return f"{parts.scheme}://{parts.netloc.lower()}"


def _require_outside_repository(path: Path, repo_root: Path, key: str) -> None:
    repository = Path(os.path.realpath(repo_root))
    resolved = Path(os.path.realpath(path))
    if resolved == repository or resolved.is_relative_to(repository):
        raise ConfigError(f"{key} 必须放在仓库之外: {path}")


def _require_accounts_db(accounts_db: Path, data_root: Path, repo_root: Path) -> None:
    """The account database belongs to one data root, outside the repository.

    Single-host exclusivity is a lock on ``data_root``; an account database
    elsewhere could be written by two hosts that each own a different root, so
    "inside this data root" is part of the ownership contract, not a layout
    preference. Paths are compared after ``realpath``, so a symlink inside the
    root cannot point the database at a shared file.
    """
    _require_outside_repository(accounts_db, repo_root, "PI_GLINK_ACCOUNTS_DB")
    root = Path(os.path.realpath(data_root))
    resolved = Path(os.path.realpath(accounts_db))
    if resolved == root or not resolved.is_relative_to(root):
        raise ConfigError(
            f"PI_GLINK_ACCOUNTS_DB 必须位于 LOCAL_WEB_DATA_ROOT 之内: {accounts_db}"
        )


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
