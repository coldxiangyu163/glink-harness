"""Gateway-asserted tenant identity and per-tenant credentials.

This is the single-host multi-tenant mode's trust boundary. The host never
implements login: a local authentication gateway completes the upstream login
and injects one short-lived, HMAC-SHA256-signed identity assertion into every
request it proxies. The host verifies that assertion with a secret it reads
from a restricted file **outside this repository** and derives the request's
:class:`TenantContext` from the signed payload only.

Design rules (see ``docs/architecture.md`` §10):

* the assertion carries at least ``tenantId``, ``userId``, ``role``,
  ``audience`` and ``exp``; nothing else is trusted;
* the signature is checked with :func:`hmac.compare_digest`
  (constant-time), the audience must match the frozen configuration, and the
  assertion must be short-lived (``exp`` within a bounded window and not
  already expired); an expiration in the past is refused without a grace
  window, so a verified context is never already expired;
* a correctly signed but malformed payload (non-string role, wrong field
  types) is refused as :class:`TenantAuthError`, never as a type error, so the
  request layer always answers a uniform 401;
* a client cannot label itself: request bodies, query parameters and other
  headers never feed :class:`TenantContext`;
* duplicate assertion headers are refused instead of picking one — that is the
  host-side half of "the gateway strips a client-supplied header of the same
  name";
* tenant model / search credentials are read from a second restricted file
  (owner-only, outside the repository). A tenant without a model key is
  refused admission; there is deliberately no fallback to the host's global
  ``.env`` key or to the operator's own pi configuration.

The secret files are intentionally *not* regular repository content and are
never logged, echoed or copied into run directories.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import stat
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .accounts import AccountsStore
from .config import ConfigError, HostConfig

logger = logging.getLogger("pi_glink.tenancy")

#: HTTP header the gateway must inject (and a client must not be able to keep).
ASSERTION_HEADER = "X-Pi-Glink-Identity"
#: Token shape version; the prefix is signed so it cannot be confused.
ASSERTION_VERSION = "v1"
ROLES = frozenset({"member", "admin"})
#: Minimum secret material accepted from the key file.
MIN_SECRET_BYTES = 32
MAX_SECRET_BYTES = 64 * 1024
#: Ceiling on one assertion token (a request header must not be unbounded).
MAX_ASSERTION_BYTES = 4096
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")


class TenantAuthError(RuntimeError):
    """No trustworthy assertion: missing, malformed, forged or expired (401)."""


class TenantForbidden(RuntimeError):
    """A valid identity without the permission for this action (403)."""


class TenantNotConfigured(TenantForbidden):
    """The tenant exists in the assertion but has no usable credentials (403)."""


@dataclass(frozen=True, slots=True)
class TenantContext:
    """The request's identity, derived only from a verified identity source.

    Gateway mode derives it from a signed assertion; accounts mode derives it
    from a server-side session record. Both modes only ever carry server-side
    facts: a client cannot label itself in either one.
    """

    tenant_id: str
    user_id: str
    role: str
    audience: str
    expires_at: float
    request_id: str | None = None
    #: ``gateway`` or ``accounts`` — which entry point authenticated the request.
    auth_mode: str = "gateway"
    #: Accounts mode: the session digest this context came from (never the token).
    session_digest: str | None = None
    #: Accounts mode: the session's CSRF double-submit token (never logged).
    csrf_token: str | None = field(default=None, repr=False, compare=False)
    #: Accounts mode: a cheap liveness probe (revocation, disable, idle timeout)
    #: evaluated between SSE polls. It must not write and must be bounded.
    validator: "Callable[[], bool] | None" = field(default=None, repr=False, compare=False)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def expired(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) >= self.expires_at

    def stale(self, now: float | None = None) -> bool:
        """True when this identity may no longer act (expiry or server-side invalidation)."""
        if self.expired(now):
            return True
        if self.validator is not None:
            return not self.validator()
        return False


@dataclass(frozen=True, slots=True)
class TenantCredentials:
    """One tenant's run credentials (never printed, never returned to clients)."""

    tenant_id: str
    model_api_key: str = field(repr=False, default="")
    web_search_key: str = field(repr=False, default="")


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (binascii.Error, ValueError) as exc:
        raise TenantAuthError("assertion payload is not valid base64url") from exc


def _sign(secret: bytes, signing_input: bytes) -> bytes:
    return hmac.new(secret, signing_input, hashlib.sha256).digest()


def _validate_identity(tenant_id: Any, user_id: Any) -> tuple[str, str]:
    for name, value in (("tenantId", tenant_id), ("userId", user_id)):
        if not isinstance(value, str) or not _ID_PATTERN.match(value):
            raise TenantAuthError(f"assertion {name} is missing or malformed")
    return tenant_id, user_id


def issue_assertion(
    secret: bytes,
    *,
    tenant_id: str,
    user_id: str,
    audience: str,
    role: str = "member",
    ttl_seconds: int = 120,
    now: float | None = None,
    request_id: str | None = None,
) -> str:
    """Mint one assertion — the reference implementation for the gateway.

    Production gateways mint their own tokens with the same secret and payload
    contract; tests use this function to synthesise assertions without a live
    gateway. The payload is signed exactly as serialized, so verification does
    not depend on a JSON key order.
    """
    try:
        tenant_id, user_id = _validate_identity(tenant_id, user_id)
    except TenantAuthError as exc:
        # Minting is a caller-side API: a malformed identity is a programming
        # error, not an authentication failure.
        raise ValueError(str(exc)) from exc
    if not isinstance(audience, str) or audience == "":
        raise ValueError("audience must be a non-empty string")
    if not isinstance(role, str) or role not in ROLES:
        raise ValueError(f"role must be one of {sorted(ROLES)}")
    issued = time.time() if now is None else float(now)
    payload: dict[str, Any] = {
        "tenantId": tenant_id,
        "userId": user_id,
        "role": role,
        "audience": audience,
        "iat": int(issued),
        "exp": int(issued + max(1, int(ttl_seconds))),
    }
    if request_id:
        payload["requestId"] = request_id
    encoded = _b64encode(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    signing_input = f"{ASSERTION_VERSION}.{encoded}".encode("ascii")
    return f"{ASSERTION_VERSION}.{encoded}.{_b64encode(_sign(secret, signing_input))}"


def verify_assertion(
    secret: bytes,
    token: str,
    *,
    audience: str,
    now: float | None = None,
    max_lifetime_seconds: int = 300,
    clock_skew_seconds: int = 30,
) -> TenantContext:
    """Verify one assertion and return its identity, or raise.

    Every check the objective names happens here: signature (constant-time),
    audience, expiration and a bounded lifetime, plus the identity fields'
    types. ``role`` is part of the signed payload, so a member cannot promote
    itself by changing a header; a non-string role (JSON array/object/number)
    is refused with :class:`TenantAuthError` instead of raising a type error.

    **Expiry is absolute.** A token whose ``exp`` has passed is refused at
    verification time — there is deliberately no grace window for an expired
    token, so a verified :class:`TenantContext` is never already expired. The
    frozen clock-skew tolerance applies only to the *issue* time (a gateway
    whose clock runs slightly ahead) and to the accepted lifetime bound.
    """
    if not isinstance(token, str) or not token or len(token) > MAX_ASSERTION_BYTES:
        raise TenantAuthError("assertion is missing or oversized")
    try:
        version, encoded, signature = token.split(".")
    except ValueError as exc:
        raise TenantAuthError("assertion is not a v1 token") from exc
    if version != ASSERTION_VERSION:
        raise TenantAuthError("assertion version is not supported")
    signing_input = f"{version}.{encoded}".encode("ascii")
    expected = _sign(secret, signing_input)
    if not hmac.compare_digest(expected, _b64decode(signature)):
        raise TenantAuthError("assertion signature does not verify")
    try:
        payload = json.loads(_b64decode(encoded).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TenantAuthError("assertion payload is not JSON") from exc
    if not isinstance(payload, Mapping):
        raise TenantAuthError("assertion payload is not an object")

    tenant_id, user_id = _validate_identity(payload.get("tenantId"), payload.get("userId"))
    if payload.get("audience") != audience:
        raise TenantAuthError("assertion audience does not match this host")
    role = payload.get("role")
    if not isinstance(role, str) or role not in ROLES:
        raise TenantAuthError("assertion role is not recognized")

    stamp = time.time() if now is None else float(now)
    expires = payload.get("exp")
    issued = payload.get("iat")
    if not isinstance(expires, int) or isinstance(expires, bool):
        raise TenantAuthError("assertion expiration is missing")
    if not isinstance(issued, int) or isinstance(issued, bool):
        raise TenantAuthError("assertion issue time is missing")
    skew = max(0, int(clock_skew_seconds))
    if expires <= stamp:
        # No grace window: accepting an assertion that is already expired would
        # return a context whose ``expired()`` answers True while the request
        # was authorized — the two answers must agree.
        raise TenantAuthError("assertion has expired")
    if expires - issued > max(1, int(max_lifetime_seconds)) + 2 * skew:
        raise TenantAuthError("assertion lifetime exceeds the accepted bound")
    if issued > stamp + skew or stamp - issued > max(1, int(max_lifetime_seconds)) + 2 * skew:
        raise TenantAuthError("assertion is not within its validity window")

    request_id = payload.get("requestId")
    return TenantContext(
        tenant_id=tenant_id,
        user_id=user_id,
        role=role,
        audience=audience,
        expires_at=float(expires),
        request_id=request_id if isinstance(request_id, str) and request_id else None,
    )


class GatewayAuthenticator:
    """Verify the gateway assertion using one frozen secret."""

    def __init__(
        self,
        secret: bytes,
        *,
        audience: str,
        max_lifetime_seconds: int = 300,
        clock_skew_seconds: int = 30,
    ) -> None:
        if len(secret) < MIN_SECRET_BYTES:
            raise ConfigError(
                f"网关签名密钥至少需要 {MIN_SECRET_BYTES} 字节随机内容（当前 {len(secret)}）"
            )
        if not audience:
            raise ConfigError("PI_GLINK_TENANCY_AUDIENCE 不能为空")
        self._secret = secret
        self.audience = audience
        self._max_lifetime = max(1, int(max_lifetime_seconds))
        self._skew = max(0, int(clock_skew_seconds))

    @classmethod
    def from_secret_file(
        cls,
        path: Path,
        *,
        repo_root: Path,
        audience: str,
        max_lifetime_seconds: int = 300,
        clock_skew_seconds: int = 30,
    ) -> "GatewayAuthenticator":
        secret = read_restricted_file(
            path, repo_root=repo_root, what="网关签名密钥文件"
        )
        return cls(
            secret,
            audience=audience,
            max_lifetime_seconds=max_lifetime_seconds,
            clock_skew_seconds=clock_skew_seconds,
        )

    def authenticate(self, header_values: Sequence[str] | None) -> TenantContext:
        """Authenticate one request's assertion header values.

        ``header_values`` is what the HTTP layer read for
        :data:`ASSERTION_HEADER`. Missing, duplicated or untrusted values raise
        :class:`TenantAuthError`; the caller answers a uniform 401.
        """
        if not header_values:
            raise TenantAuthError("request carries no identity assertion")
        if len(header_values) != 1:
            # A gateway that left the client's copy in place would present two
            # values; refusing is safer than trusting either one.
            raise TenantAuthError("request carries more than one identity assertion")
        return verify_assertion(
            self._secret,
            header_values[0],
            audience=self.audience,
            max_lifetime_seconds=self._max_lifetime,
            clock_skew_seconds=self._skew,
        )


class TenantCredentialStore:
    """Per-tenant run credentials, loaded once from a restricted file.

    File format (JSON; every value is a synthetic placeholder in the docs)::

        {
          "tenants": {
            "tenant-alpha": {"modelApiKey": "…", "webSearchKey": "…"},
            "tenant-beta":  {"modelApiKey": "…"}
          }
        }

    ``modelApiKey`` is mandatory: a tenant without one is refused admission
    instead of silently using the host's global key. ``webSearchKey`` is
    optional and, when absent, the run renders no search credential at all
    (pi-web-access falls back to keyless search, exactly as in local mode).
    """

    def __init__(self, entries: Mapping[str, TenantCredentials]) -> None:
        self._entries = dict(entries)

    @classmethod
    def from_file(cls, path: Path, *, repo_root: Path) -> "TenantCredentialStore":
        raw = read_restricted_file(
            path, repo_root=repo_root, what="租户凭据配置文件", min_bytes=1
        )
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"租户凭据配置不是合法 JSON: {path}: {exc}") from exc
        if not isinstance(document, Mapping):
            raise ConfigError(f"租户凭据配置必须是 JSON 对象: {path}")
        tenants = document.get("tenants")
        if not isinstance(tenants, Mapping) or not tenants:
            raise ConfigError(f"租户凭据配置缺少非空的 tenants 对象: {path}")
        entries: dict[str, TenantCredentials] = {}
        for tenant_id, entry in tenants.items():
            if not isinstance(tenant_id, str) or not _ID_PATTERN.match(tenant_id):
                raise ConfigError(f"租户 id 非法: {tenant_id!r}")
            if not isinstance(entry, Mapping):
                raise ConfigError(f"租户 {tenant_id} 的配置必须是对象")
            model_key = entry.get("modelApiKey")
            if not isinstance(model_key, str) or not model_key.strip():
                raise ConfigError(f"租户 {tenant_id} 缺少 modelApiKey")
            search_key = entry.get("webSearchKey")
            if search_key is not None and not isinstance(search_key, str):
                raise ConfigError(f"租户 {tenant_id} 的 webSearchKey 必须是字符串")
            entries[tenant_id] = TenantCredentials(
                tenant_id=tenant_id,
                model_api_key=model_key.strip(),
                web_search_key=(search_key or "").strip(),
            )
        return cls(entries)

    def get(self, tenant_id: str | None) -> TenantCredentials | None:
        if not isinstance(tenant_id, str):
            return None
        return self._entries.get(tenant_id)

    def require(self, tenant_id: str) -> TenantCredentials:
        found = self.get(tenant_id)
        if found is None or not found.model_api_key:
            raise TenantNotConfigured(f"租户 {tenant_id} 未配置模型凭据")
        return found

    def tenant_ids(self) -> frozenset[str]:
        return frozenset(self._entries)


@dataclass(frozen=True, slots=True)
class Tenancy:
    """Everything the multi-tenant mode freezes at startup.

    ``mode`` names the identity entry: ``gateway`` carries the assertion
    authenticator, ``accounts`` carries the SQLite account store. Tenant run
    credentials are needed by both.
    """

    credentials: TenantCredentialStore
    authenticator: GatewayAuthenticator | None = None
    mode: str = "gateway"
    accounts: "AccountsStore | None" = None

    def require_run_credentials(self, tenant_id: str) -> TenantCredentials:
        return self.credentials.require(tenant_id)

    def close(self) -> None:
        if self.accounts is not None:
            self.accounts.close()


def load_identity_material(config: HostConfig) -> Tenancy | None:
    """Load and validate the identity trust material **without** opening the
    account database.

    Reading the credential/secret files has no data-root side effect, so this is
    safe to call for validation alone (``--print-config``). The accounts SQLite
    store is opened by :func:`build_tenancy`, which must only run while this
    process owns ``config.data_root``: creating the file, running the schema
    migration and switching on WAL are data-root writes (see
    :func:`pi_glink.app.create_server`).

    Raises :class:`ConfigError` when the mode is enabled but any required file
    is missing, unreadable, over-permissive or inside the repository. The
    caller treats that as "refuse to start": multi-tenant mode never falls back
    to the unauthenticated local surface.
    """
    if not config.multi_tenant:
        return None
    if config.tenant_config_file is None:
        raise ConfigError("PI_GLINK_TENANT_CONFIG_FILE 在 PI_GLINK_TENANCY=multitenant 时必须配置")
    credentials = TenantCredentialStore.from_file(
        config.tenant_config_file, repo_root=config.repo_root
    )
    if config.auth_mode == "accounts":
        if config.accounts_db is None:
            raise ConfigError("PI_GLINK_AUTH_MODE=accounts 需要 PI_GLINK_ACCOUNTS_DB")
        return Tenancy(credentials=credentials, mode="accounts")
    if config.gateway_secret_file is None:
        raise ConfigError("PI_GLINK_GATEWAY_SECRET_FILE 在 PI_GLINK_TENANCY=multitenant 时必须配置")
    authenticator = GatewayAuthenticator.from_secret_file(
        config.gateway_secret_file,
        repo_root=config.repo_root,
        audience=config.tenancy_audience,
        max_lifetime_seconds=config.assertion_max_lifetime_seconds,
        clock_skew_seconds=config.assertion_clock_skew_seconds,
    )
    logger.info(
        "多租户模式已启用：audience=%s，已配置租户=%d", config.tenancy_audience, len(credentials.tenant_ids())
    )
    return Tenancy(
        credentials=credentials, authenticator=authenticator, mode="gateway"
    )


def build_tenancy(config: HostConfig) -> Tenancy | None:
    """Resolve the identity material, opening the account database when needed.

    **Ownership prerequisite**: in ``accounts`` mode this creates
    ``accounts.db`` (and its WAL sidecars) and runs the schema migration. That
    is a data-root write, so callers must hold the exclusive
    :class:`~pi_glink.ownership.DataRootOwnership` for ``config.data_root``
    first — otherwise a second host on the same data root would modify the
    account database before its ownership check could refuse it.
    """
    tenancy = load_identity_material(config)
    if tenancy is None or tenancy.mode != "accounts":
        return tenancy
    if config.accounts_db is None:  # defensive: load_identity_material checked it
        raise ConfigError("PI_GLINK_AUTH_MODE=accounts 需要 PI_GLINK_ACCOUNTS_DB")
    store = AccountsStore(config.accounts_db)
    logger.info(
        "账号模式已启用：账号库=%s，已配置租户=%d",
        config.accounts_db,
        len(tenancy.credentials.tenant_ids()),
    )
    return replace(tenancy, accounts=store)


def read_restricted_file(
    path: Path,
    *,
    repo_root: Path | None,
    what: str,
    min_bytes: int = MIN_SECRET_BYTES,
) -> bytes:
    """Read a secret file that must live outside the repository, owner-only.

    The check is deliberately strict and fails closed: anything that is not a
    regular file readable only by its owner (mode ``0600`` or tighter) is
    refused. Symlinks are refused because their target is what would actually
    be read. The file is read through a descriptor opened with ``O_NOFOLLOW``
    and the descriptor's identity is compared with the ``stat`` that approved
    it, so the check and the read cannot disagree.
    """
    target = Path(path)
    if repo_root is not None:
        repository = Path(os.path.realpath(repo_root))
        resolved = Path(os.path.realpath(target))
        if resolved == repository or resolved.is_relative_to(repository):
            raise ConfigError(f"{what}必须放在仓库之外: {target}")
    try:
        info = os.stat(target, follow_symlinks=False)
    except OSError as exc:
        raise ConfigError(f"{what}不可读: {target}: {exc}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ConfigError(f"{what}必须是普通文件（不接受符号链接、目录或设备）: {target}")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise ConfigError(f"{what}权限过宽（要求 0600 或更严，当前 {mode:04o}）: {target}")
    if not mode & 0o400:
        raise ConfigError(f"{what}当前用户不可读（当前 {mode:04o}）: {target}")
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as exc:
        raise ConfigError(f"{what}打开失败: {target}: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ConfigError(f"{what}在检查与读取之间被替换: {target}")
        raw = os.read(descriptor, MAX_SECRET_BYTES)
    finally:
        os.close(descriptor)
    raw = raw.strip()
    if len(raw) < max(1, int(min_bytes)):
        raise ConfigError(f"{what}内容过短（至少 {min_bytes} 字节）: {target}")
    return raw
