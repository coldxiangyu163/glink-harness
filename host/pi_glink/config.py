"""Immutable host configuration, resolved once at startup.

Variable names deliberately mirror FrontierAgent's ``.env`` semantics
(``OPENAI_*`` for the research model, ``E2B_*`` for the cloud sandbox,
``LOCAL_WEB_*`` for the server and executor) so the two projects can share one
operator mental model. Values are read once and frozen: a running host cannot
be relabelled by editing the environment afterwards.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .dotenv import parse_env_file
from .harnesses import (
    ACP_PERMISSION_OPTION_KINDS,
    ACP_PERMISSION_TOOL_KINDS,
    FAMILIES,
    HarnessPack,
    HarnessPackError,
    load_pack,
    pack_names,
)

#: 对话租约 TTL 的默认值与硬上限（秒）。真源在
#: :mod:`pi_glink.kernel.conversation_lease`（kernel 不可反向导入 config，
#: 否则与 kernel 包初始化成环）；两侧由 tests/test_kernel_conversation_lease.py
#: 的常量对齐断言钉住。
_DEFAULT_LEASE_TTL_SECONDS = 2 * 60 * 60
_MAX_LEASE_TTL_SECONDS = 24 * 60 * 60
#: Host-wide ceiling on live parked sandboxes (docs/adr/0020): the TTL bounds
#: how long each one stays, this bounds how many exist at once.
_DEFAULT_MAX_PARKED_SANDBOXES = 32

#: Re-exported from :mod:`pi_glink.harnesses`（清单解析器拥有这套词表）：ACP 权限白名单
#: 只能命名这些取值，避免拼写错误静默失效。见 ``HarnessCapabilities.permission_allow``。
__all__ = ["ACP_PERMISSION_OPTION_KINDS", "ACP_PERMISSION_TOOL_KINDS"]
_ACP_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

EXECUTORS = ("inproc", "docker", "e2b")
#: Kernel adapter families a run record can name; the pack manifest's ``family``
#: selects one. ``pi`` drives the pi kernel on all three faces; ``acp`` drives any
#: ACP agent (dsh / codex-acp / opencode) through one stdio JSON-RPC client.
HARNESS_FAMILIES = FAMILIES
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
#: pi-web-access's own Exa default; the egress proxy forwards ``/search`` here.
DEFAULT_WEB_SEARCH_UPSTREAM = "https://api.exa.ai"
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
    #: Repository-level methodology directory (``PI_GLINK_METHODOLOGY_DIR``,
    #: default ``<repo>/methodology``): the harness-independent work agreement
    #: (``work-agreement.md``) and the blueprint directory live here.
    methodology_dir: Path
    #: Repository-level session-blueprint directory (default
    #: ``<methodology_dir>/blueprints``; ``PI_GLINK_BLUEPRINTS_DIR`` overrides).
    #: The accepted blueprint names are derived from this directory at admission
    #: time and are also the accepted ``/<name> <topic>`` slash commands; a
    #: missing directory offers no names and never fails startup.
    blueprints_dir: Path
    workspace_root: Path
    openai_base_url: str
    openai_model: str
    #: The selected harness pack (``PI_GLINK_HARNESS``), held as the parsed
    #: manifest object itself: family, entry argv, capability matrix, capability
    #: prompt, HOME overlay and image requirement are all read from here. Nothing
    #: is copied field by field out of it, so the manifest stays the one source.
    #: Frozen at startup; there is deliberately no per-run API/query parameter
    #: and no front-end surface for it.
    pack: HarnessPack
    #: Root of the pack directories (``PI_GLINK_HARNESSES_DIR``).
    harnesses_dir: Path = Path("harnesses")
    #: Credential files to copy into each run's exclusive ``HOME``
    #: (``PI_GLINK_ACP_CREDENTIAL_FILES``): ``source=dest`` entries separated by
    #: ``;``. ``source`` is read only (a leading ``~/`` expands to the operator's
    #: home) and ``dest`` is a path under the run HOME. ACP agents read their
    #: credentials from their own state directory inside ``HOME``, so this is the
    #: per-agent injection point; values never enter the host environment.
    #: Each entry's credential values are added to the run's redaction boundary.
    acp_credential_files: tuple[tuple[str, str], ...] = ()
    #: Credential values to render into each run's exclusive ``HOME``
    #: (``PI_GLINK_ACP_CREDENTIAL_ENV``): a comma-separated list of environment
    #: key *names*. At run preparation the host renders one dsh-format
    #: ``.dsh/.credentials.yaml`` (``version: 1`` + a flat ``refs`` mapping) from
    #: the configured values, so credentials live in a single source (``.env``,
    #: 0600, git-ignored) instead of a parallel local file. Rendered values join
    #: the run's redaction boundary and the rendered copy is deleted at close-out
    #: (decision C), exactly like an injected file. Mutually exclusive with
    #: ``acp_credential_files`` (both render into ``HOME``; one source per host).
    acp_credential_env: tuple[str, ...] = ()
    #: The values paired with :attr:`acp_credential_env` (same order). Never in
    #: ``repr`` and never logged: the rendered copy inside a run's ``HOME`` is
    #: the only materialization.
    acp_credential_env_values: tuple[tuple[str, str], ...] = field(
        repr=False, default=()
    )
    #: Extra ``NAME=value`` entries for the ACP agent process
    #: (``PI_GLINK_ACP_ENV``, ``;``-separated). ``HOME`` may not be overridden:
    #: the per-run state root stays the isolation boundary. A value whose name
    #: looks credential-shaped joins the run's redaction boundary.
    acp_env: tuple[tuple[str, str], ...] = ()
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
    #: The search upstream the egress proxy forwards ``/search/…`` to
    #: (``PI_GLINK_WEB_SEARCH_UPSTREAM``, default the Exa API). Host-frozen.
    web_search_upstream: str = DEFAULT_WEB_SEARCH_UPSTREAM
    #: Credential egress proxy (docs/adr/0008). The listener address
    #: (``PI_GLINK_EGRESS_HOST``/``PI_GLINK_EGRESS_PORT``, port ``0`` = random),
    #: the public base URL a remote/containers face reaches it at
    #: (``PI_GLINK_EGRESS_PUBLIC_URL``; required for E2B) and the per-run quota
    #: (``0`` = unlimited).
    egress_host: str = "127.0.0.1"
    egress_port: int = 0
    egress_public_url: str = ""
    egress_max_requests_per_run: int = 0
    egress_max_tokens_per_run: int = 0
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
    #: 闲置对话租约的寿命（秒，docs/adr/0020）。一轮 ACP/E2B run 结束后其沙箱
    #: 可以泊车（pause）作为对话状态的热缓存，下次追问省掉新沙箱与回灌；这个
    #: 旋钮是闲置多久后由对账销毁。``0`` 禁用热路径（收尾直接销毁＝阶段 2 前
    #: 的行为）；硬上限
    #: :data:`~pi_glink.kernel.conversation_lease.MAX_LEASE_TTL_SECONDS`
    #: （24h）在启动校验里拒绝越界值。
    conversation_lease_ttl_seconds: int = _DEFAULT_LEASE_TTL_SECONDS
    max_parked_sandboxes: int = _DEFAULT_MAX_PARKED_SANDBOXES
    #: Interval of the online resource ledger sweep (seconds). The sweep asks
    #: the execution face whether the resources of an unconfirmed hold still
    #: exist, addressing only the identity persisted with the run. ``0`` disables
    #: it; the offline ``--unreleased`` / ``--mark-released`` path still works.
    resource_reconcile_seconds: int = 60

    @property
    def harness(self) -> str:
        """Kernel adapter family for every run (``pi`` | ``acp``): the pack's family.

        The family (not the pack name) is the run record's ``harness`` value.
        """
        return self.pack.family

    @property
    def harness_pack(self) -> str:
        """The selected harness pack name."""
        return self.pack.name

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
        """True when the host froze a search credential (``PI_GLINK_WEB_SEARCH_KEY``)."""
        return bool(self.web_search_key)

    @property
    def binds_loopback(self) -> bool:
        return self.host in _LOOPBACK_HOSTS

    @property
    def work_agreement_path(self) -> Path:
        """The harness-independent working agreement appended to every run."""
        return self.methodology_dir / "work-agreement.md"

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
        # Keys an operator explicitly declared as ACP credentials follow the
        # same "process environment over .env" precedence (rotation must not
        # silently keep the file's old value); undeclared variables stay out.
        for name in _declared_acp_credential_keys(merged):
            if name in os.environ:
                merged[name] = os.environ[name]
    else:
        merged.update(env)
    return build_config(merged, root)


def build_config(values: Mapping[str, str], repo_root: Path) -> HostConfig:
    host = _text(values, "LOCAL_WEB_HOST", "127.0.0.1")
    port = _port(values, "LOCAL_WEB_PORT", _DEFAULT_PORT)
    executor = _text(values, "LOCAL_WEB_EXECUTOR", "inproc").lower()
    harnesses_dir = _path(values, "PI_GLINK_HARNESSES_DIR", repo_root / "harnesses")
    pack = _select_harness_pack(values, harnesses_dir)
    acp_credential_files = _acp_credential_files(values)
    acp_credential_env_values = _acp_credential_env(values)
    acp_credential_env = tuple(name for name, _value in acp_credential_env_values)
    if acp_credential_files and acp_credential_env:
        raise ConfigError(
            "PI_GLINK_ACP_CREDENTIAL_FILES 与 PI_GLINK_ACP_CREDENTIAL_ENV 互斥："
            "两者都会往 run HOME 投递凭据，一次部署只选一个来源"
        )
    acp_env = _acp_env_entries(values)
    tenancy_mode = _text(values, "PI_GLINK_TENANCY", "local").lower()
    auth_mode = _text(values, "PI_GLINK_AUTH_MODE", "gateway").lower()
    model_credentials = _text(values, "PI_GLINK_MODEL_CREDENTIALS", "tenant").lower()

    if executor not in EXECUTORS:
        raise ConfigError(
            f"LOCAL_WEB_EXECUTOR must be one of {', '.join(EXECUTORS)}; got {executor!r}"
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
    if model_credentials == "shared" and pack.family == "pi":
        # Fail closed at startup: a "shared" deployment that cannot name its
        # credential source must not boot and later fall back to a tenant entry
        # or to the single-user local configuration. The pi family's source is
        # the frozen ``OPENAI_*`` trio; an ACP host names its source through the
        # ACP channels instead (:func:`_acp_credentials_follow_policy`).
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
        values, "PI_CODING_AGENT_DIR", harnesses_dir / pack.name
    )
    methodology_dir = _path(
        values, "PI_GLINK_METHODOLOGY_DIR", repo_root / "methodology"
    )
    blueprints_dir = _path(
        values, "PI_GLINK_BLUEPRINTS_DIR", methodology_dir / "blueprints"
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
        pack=pack,
        harnesses_dir=harnesses_dir,
        acp_credential_files=acp_credential_files,
        acp_credential_env=acp_credential_env,
        acp_credential_env_values=acp_credential_env_values,
        acp_env=acp_env,
        # The pi pack's entry names the kernel binary; the local override stays
        # an inproc debugging knob (isolated faces always run the image's entry).
        pi_bin=_text(values, "PI_GLINK_PI_BIN", pack.command[0]),
        agent_config_dir=agent_config_dir,
        methodology_dir=methodology_dir,
        blueprints_dir=blueprints_dir,
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
        web_search_upstream=_upstream(
            values, "PI_GLINK_WEB_SEARCH_UPSTREAM", DEFAULT_WEB_SEARCH_UPSTREAM
        ),
        egress_host=_text(values, "PI_GLINK_EGRESS_HOST", "127.0.0.1"),
        egress_port=_non_negative_integer(values, "PI_GLINK_EGRESS_PORT", 0),
        egress_public_url=_upstream(values, "PI_GLINK_EGRESS_PUBLIC_URL", ""),
        egress_max_requests_per_run=_non_negative_integer(
            values, "PI_GLINK_EGRESS_MAX_REQUESTS_PER_RUN", 0
        ),
        egress_max_tokens_per_run=_non_negative_integer(
            values, "PI_GLINK_EGRESS_MAX_TOKENS_PER_RUN", 0
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
        conversation_lease_ttl_seconds=_non_negative_integer(
            values,
            "PI_GLINK_CONVERSATION_LEASE_TTL_SECONDS",
            _DEFAULT_LEASE_TTL_SECONDS,
        ),
        max_parked_sandboxes=_positive_integer(
            values, "PI_GLINK_MAX_PARKED_SANDBOXES", _DEFAULT_MAX_PARKED_SANDBOXES
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
        # An unauthenticated host is never exposed beyond loopback. Remote
        # access goes through the local authentication gateway
        # (PI_GLINK_TENANCY=multitenant); no switch opens a remote bind.
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
    if config.harness not in HARNESS_FAMILIES:
        raise ConfigError(
            f"harness 族必须是 {' 或 '.join(HARNESS_FAMILIES)}；got {config.harness!r}"
        )
    if not config.pack.command:
        raise ConfigError(
            f"harness 包 {config.harness_pack!r} 缺少 entry.command"
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
    _acp_credentials_follow_policy(config)
    _egress_is_reachable(config)
    if config.conversation_lease_ttl_seconds > _MAX_LEASE_TTL_SECONDS:
        # 硬上限是对话租约的防线（docs/adr/0020）：闲置热缓存不能被配置成
        # 无限期的云端占用；越界拒绝启动，不做静默钳制。
        raise ConfigError(
            "PI_GLINK_CONVERSATION_LEASE_TTL_SECONDS 不得超过 "
            f"{_MAX_LEASE_TTL_SECONDS} 秒（当前 {config.conversation_lease_ttl_seconds}）；"
            "0 表示禁用热路径"
        )


def _egress_is_reachable(config: HostConfig) -> None:
    """Refuse an egress setup a pi run could never use (fail closed at startup).

    The real key never enters the execution environment (docs/adr/0008), so a
    face that cannot reach the proxy has no fallback: E2B needs the public URL;
    Docker's host-gateway URL is plain http on a non-loopback name, which
    pi-web-access refuses for its search base URL, so a host-frozen search key
    on Docker also needs an https public URL.
    """
    if not 0 <= config.egress_port <= 65535:
        raise ConfigError(f"PI_GLINK_EGRESS_PORT 必须在 0–65535 之间；当前 {config.egress_port}")
    # A public URL is the operator's forwarding statement; pairing it with a
    # fixed PI_GLINK_EGRESS_PORT is a deployment step (.env.example), not a
    # host rule: a random port is merely unreachable, never a key leak.
    if config.harness != "pi":
        return
    if config.executor == "e2b" and not config.egress_public_url:
        raise ConfigError(
            "LOCAL_WEB_EXECUTOR=e2b 需要 PI_GLINK_EGRESS_PUBLIC_URL：沙箱只经凭据出口代理"
            "访问模型与检索，真实 key 不进沙箱"
        )
    if (
        config.executor == "docker"
        and config.web_search_key
        and not config.egress_public_url.startswith("https://")
    ):
        raise ConfigError(
            "Docker 执行面配置了 PI_GLINK_WEB_SEARCH_KEY 时需要 https 的 "
            "PI_GLINK_EGRESS_PUBLIC_URL：pi-web-access 拒绝非 loopback 的 http 检索地址"
        )


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
    """Whether a process-environment variable belongs to this host's configuration.

    The allowlist keeps an unrelated variable from relabelling a run; it lists
    every key the host itself reads, not a prefix. ``OPENAI_CONTEXT_WINDOW``
    and ``OPENAI_MAX_TOKENS`` (the pi face's model metadata) belong here too.

    Old obligation: the two keys were missing from the list, so an exported
    value was silently filtered out and only a ``.env`` entry took effect --
    contradicting ``load_config``'s documented "process environment over
    ``.env``" precedence, which the other host-owned ``OPENAI_*`` keys keep.
    New obligation: they are inherited like the rest of the list and still
    go through ``build_config``'s fail-closed integer validation.
    """
    return key.startswith(("LOCAL_WEB_", "PI_GLINK_", "PI_CODING_AGENT_")) or key in {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "OPENAI_CONTEXT_WINDOW",
        "OPENAI_MAX_TOKENS",
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


def _declared_acp_credential_keys(values: Mapping[str, str]) -> tuple[str, ...]:
    """The syntactically valid key names ``PI_GLINK_ACP_CREDENTIAL_ENV`` declares.

    Only selects which process variables ``load_config`` inherits; malformed
    names are left for :func:`_acp_credential_env` to refuse at startup.
    """
    raw = _text(values, "PI_GLINK_ACP_CREDENTIAL_ENV", "")
    return tuple(
        name for name in (chunk.strip() for chunk in raw.split(","))
        if _ENV_KEY_RE.fullmatch(name)
    )


def _acp_credential_env(values: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    """Parse ``PI_GLINK_ACP_CREDENTIAL_ENV``: a comma-separated list of key names.

    Each name must be a plain upper-case environment key and its value must
    already be configured (``.env`` or the process environment): a declared key
    without a value refuses startup, never renders an empty credential. Returns
    ``(name, value)`` pairs; the caller keeps values out of repr and logs.
    """
    raw = _text(values, "PI_GLINK_ACP_CREDENTIAL_ENV", "")
    if not raw:
        return ()
    pairs: list[tuple[str, str]] = []
    for chunk in raw.split(","):
        name = chunk.strip()
        if not name:
            continue
        if not _ENV_KEY_RE.fullmatch(name):
            raise ConfigError(
                f"PI_GLINK_ACP_CREDENTIAL_ENV 的键名必须是环境变量名（大写字母/数字/下划线）；当前 {name!r}"
            )
        value = _text(values, name, "")
        if not value:
            raise ConfigError(
                f"PI_GLINK_ACP_CREDENTIAL_ENV 声明了 {name}，但它没有配置值（.env 或环境变量）"
            )
        pairs.append((name, value))
    return tuple(pairs)


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


def _acp_knobs_are_acp_only(config: HostConfig) -> None:
    """Refuse ACP-only knobs on a non-ACP pack instead of ignoring them.

    The command, session options and permission allowlist now live in the pack
    manifest (and are validated by its parser). The credential and env channels
    stay deployment configuration: an operator who sets them while the selected
    pack drives pi gets a startup error rather than a silent no-op. The env
    render channel (``PI_GLINK_ACP_CREDENTIAL_ENV``) additionally renders a
    **dsh-format** credentials file, so it is refused on non-dsh ACP packs
    instead of writing a file that pack's agent never reads.
    """
    if config.harness == "acp":
        if config.acp_credential_env and config.harness_pack != "dsh":
            raise ConfigError(
                "PI_GLINK_ACP_CREDENTIAL_ENV 渲染 dsh 格式的 .dsh/.credentials.yaml，"
                f"只在 dsh 包下生效；当前 harness_pack={config.harness_pack!r}"
            )
        return
    if config.acp_credential_files or config.acp_env or config.acp_credential_env:
        raise ConfigError(
            "PI_GLINK_ACP_CREDENTIAL_FILES / PI_GLINK_ACP_ENV / "
            "PI_GLINK_ACP_CREDENTIAL_ENV 只在 ACP 族"
            "（PI_GLINK_HARNESS=dsh|codex|opencode）下生效；当前 harness_pack="
            f"{config.harness_pack!r}"
        )


def _acp_credentials_follow_policy(config: HostConfig) -> None:
    """The ACP credential channels are model credentials under the policy.

    ``PI_GLINK_ACP_ENV`` / ``PI_GLINK_ACP_CREDENTIAL_FILES`` /
    ``PI_GLINK_ACP_CREDENTIAL_ENV`` hand the operator's credential to every ACP
    run, so they may not bypass the model-credential policy (docs/adr/0015,
    docs/adr/0016 for the env-rendering channel):

    * multitenant + ``tenant``: the policy is per-tenant credentials and the
      ACP per-tenant injection points do not exist yet (M2); the operator's
      channel would be an undeclared shared credential, so a host that
      configures it refuses to boot. The **whole channel** is treated as
      credentials — never by variable name — because a boundary that depends
      on naming conventions can be renamed past.
    * multitenant + ``shared``: the channels are the ACP host's named shared
      credential source, mirroring the pi family's frozen ``OPENAI_*`` trio
      (:func:`load_config`): a shared ACP host that cannot name its source
      must not boot and fail later per run.

    The single-user mode keeps the channels as-is: the operator's own
    credential for the operator's own runs is the documented local entry.
    """
    if config.harness != "acp" or not config.multi_tenant:
        return
    has_channel = bool(
        config.acp_env or config.acp_credential_files or config.acp_credential_env
    )
    if config.shared_model_credentials:
        if not has_channel:
            raise ConfigError(
                "PI_GLINK_MODEL_CREDENTIALS=shared 且 harness 为 ACP 族时需要 "
                "PI_GLINK_ACP_ENV / PI_GLINK_ACP_CREDENTIAL_FILES / "
                "PI_GLINK_ACP_CREDENTIAL_ENV 之一："
                "shared 部署必须能指出共享凭据来源，缺源不启动（ADR 0015/0016）"
            )
        return
    if has_channel:
        raise ConfigError(
            "多租户 tenant 政策下不允许配置 PI_GLINK_ACP_ENV / "
            "PI_GLINK_ACP_CREDENTIAL_FILES / PI_GLINK_ACP_CREDENTIAL_ENV："
            "ACP 通道是运营方共享凭据，而租户级"
            "注入点未实现（M2）；共享请显式配置 PI_GLINK_MODEL_CREDENTIALS=shared"
            "（ADR 0015/0016）"
        )


def _select_harness_pack(values: Mapping[str, str], harnesses_dir: Path) -> HarnessPack:
    """The one harness pack this host runs, or the startup refusal.

    ``PI_GLINK_HARNESS`` names a pack directory; anything else (including a
    retired value) is refused with the list of available packs. The manifest is
    the single place a harness's entry, capabilities and image pins live.
    """
    requested = _text(values, "PI_GLINK_HARNESS", "pi").lower()
    try:
        return load_pack(harnesses_dir, requested)
    except HarnessPackError as exc:
        available = pack_names(harnesses_dir)
        listing = "、".join(available) if available else "（无）"
        raise ConfigError(
            f"PI_GLINK_HARNESS={requested!r} 不可用：{exc}；可用包：{listing}"
        ) from exc


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


def _upstream(values: Mapping[str, str], key: str, default: str) -> str:
    """Freeze an egress-side base URL: https, or http only on a loopback host.

    The same rule pi-web-access applies to its own base URL: a credential or
    token must not travel in clear text beyond this machine. Credentials,
    queries and fragments in the URL are refused; a trailing slash is dropped.
    """
    raw = _text(values, key, default)
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ConfigError(f"{key} 必须是 http(s) URL: {raw!r}")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError(f"{key} 不接受内嵌凭据、查询或片段: {raw!r}")
    if parts.scheme == "http" and not is_loopback_hostname(parts.hostname):
        raise ConfigError(f"{key} 非 loopback 地址必须使用 https: {raw!r}")
    return raw.rstrip("/")


def is_loopback_hostname(hostname: str | None) -> bool:
    """``localhost``, ``127.0.0.0/8`` or ``::1`` (pi-web-access's own rule)."""
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


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
