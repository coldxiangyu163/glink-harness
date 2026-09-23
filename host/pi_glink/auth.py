"""站点内账号会话：登录/注册/注销编排与 cookie/CSRF 语义。

``PI_GLINK_AUTH_MODE=accounts`` 下，宿主自己完成身份入口：

* 登录成功后由服务端签发高熵会话令牌，浏览器只拿到 ``HttpOnly`` +
  ``SameSite=Lax``（HTTPS 下再加 ``Secure``）的 cookie；令牌原文只出现在这一个
  响应头里，数据库只存摘要；
* 每次受权读写都重新从服务端记录解析 ``tenantId/userId/role/status``，因此停用
  用户/租户、注销、绝对或空闲过期都会即时失效；SSE 长连接通过
  :meth:`AccountsAuth.context_for_session` 注入的 liveness 探测器在轮询间隔内断开；
* 登录与注册有固定窗口限速；未知账号与错误密码在 store 层执行同量散列，HTTP 层
  返回同一个 401 与同一个错误码；
* 状态变更 POST 必须携带与当前会话绑定的 CSRF 头（``/api/me`` 下发），因此即使
  cookie 因同源发送，跨站请求也拿不到令牌；Origin/Referer/Sec-Fetch-Site 与
  JSON Content-Type 是第二道闸。

这个模块不做任何 HTTP 拼装：它只接收已解析的参数并抛出带机器码的
:class:`AuthError`，由 ``app.py`` 转成状态码与 JSON。
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any

from .accounts import (
    ROLE_ADMIN,
    TENANT_ACTIVE,
    USER_ACTIVE,
    AccountsError,
    AccountsStore,
    RateLimiter,
    normalize_email,
)
from .config import HostConfig
from .tenancy import TenantContext

#: Session cookie name. Kept short and specific; never mirrored into the page.
SESSION_COOKIE = "pi_glink_session"
#: Header a caller must echo for authenticated state-changing requests.
CSRF_HEADER = "X-Pi-Glink-CSRF"
#: The audience recorded on accounts-derived contexts (gateway keeps the
#: configured audience string).
ACCOUNTS_AUDIENCE = "accounts"
#: Codes the HTTP layer knows how to turn into statuses.
AUTH_ERROR_STATUS = {
    "invalid_credentials": 401,
    "unauthenticated": 401,
    "account_disabled": 403,
    "tenant_disabled": 403,
    "forbidden": 403,
    "csrf_invalid": 403,
    "forbidden_origin": 403,
    "invalid_email": 400,
    "invalid_name": 400,
    "invalid_password": 400,
    "invalid_role": 400,
    "invalid_request": 400,
    "invitation_invalid": 400,
    "email_in_use": 409,
    "rate_limited": 429,
    "unsupported_media_type": 415,
}


class AuthError(RuntimeError):
    """A refused login/register/session operation with a stable machine code."""

    def __init__(self, code: str, message: str = "", *, retry_after: float = 0.0) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message
        self.retry_after = retry_after

    @property
    def status(self) -> int:
        return AUTH_ERROR_STATUS.get(self.code, 403)


@dataclass(frozen=True, slots=True)
class SessionLogin:
    """The result of one successful login: the cookie token and the public identity."""

    token: str
    identity: dict[str, Any]


def account_identity(user, tenant, csrf_token: str) -> dict[str, Any]:
    """Public identity payload for ``/api/me`` and login: display fields only."""
    return {
        "mode": "accounts",
        "authenticated": True,
        "user": {
            "id": user.user_id,
            "name": user.name,
            "email": user.email,
            "role": user.role,
        },
        "tenant": {"id": tenant.tenant_id, "name": tenant.name},
        "csrfToken": csrf_token,
        "roles": ["member", "admin"] if user.role == ROLE_ADMIN else ["member"],
    }


class AccountsAuth:
    """Server-side session authentication for the in-app accounts surface."""

    mode = "accounts"

    def __init__(self, config: HostConfig, store: AccountsStore) -> None:
        self.config = config
        self.store = store
        self.login_limiter = RateLimiter(
            limit=config.login_max_attempts, window=config.login_window_seconds
        )
        #: Peer budget in *front* of the per-account one: without it, rotating
        #: e-mail addresses would run an unbounded number of scrypt hashes.
        self.login_peer_limiter = RateLimiter(
            limit=config.login_peer_max_attempts, window=config.login_peer_window_seconds
        )
        self.register_limiter = RateLimiter(
            limit=config.register_max_attempts, window=config.register_window_seconds
        )
        self.invite_limiter = RateLimiter(
            limit=config.invite_check_max_attempts, window=config.invite_check_window_seconds
        )

    # -- login / logout -------------------------------------------------------

    def login(self, email: Any, password: Any, *, client_ip: str, user_agent: str = "") -> SessionLogin:
        canonical = normalize_email(email)
        # Peer first, and unconditionally: the per-account bucket below is keyed
        # by the (client-supplied) address, so an attacker could rotate it to
        # reach ``verify_credentials`` forever. The peer is the socket address —
        # ``X-Forwarded-For`` is never trusted, so behind a proxy every user
        # shares one budget (see docs/architecture.md §10.6 for the trade-off).
        peer_key = f"login-peer:{client_ip}"
        if not self.login_peer_limiter.allow(peer_key):
            raise AuthError(
                "rate_limited",
                "尝试次数过多，请稍后再试",
                retry_after=self.login_peer_limiter.retry_after(peer_key),
            )
        key = f"login:{canonical or 'invalid'}"
        if not self.login_limiter.allow(key):
            raise AuthError(
                "rate_limited",
                "尝试次数过多，请稍后再试",
                retry_after=self.login_limiter.retry_after(key),
            )
        user = self.store.verify_credentials(email, password)
        if user is None:
            raise AuthError("invalid_credentials", "邮箱或密码不正确")
        if user.status != USER_ACTIVE:
            raise AuthError("account_disabled", "账号已停用")
        tenant = self.store.get_tenant(user.tenant_id)
        if tenant is None or tenant.status != TENANT_ACTIVE:
            raise AuthError("tenant_disabled", "组织已停用")
        token, csrf, _record = self.store.create_session(
            user_id=user.user_id,
            absolute_seconds=self.config.session_absolute_seconds,
            idle_seconds=self.config.session_idle_seconds,
            user_agent=user_agent,
        )
        self.login_limiter.reset(key)
        return SessionLogin(token=token, identity=account_identity(user, tenant, csrf))

    def logout(self, token: str | None) -> bool:
        if not token:
            return False
        return self.store.revoke_session(token)

    # -- request authentication ----------------------------------------------

    def authenticate(self, token: str | None) -> TenantContext | None:
        context = self.store.authenticate_session(
            token, idle_seconds=self.config.session_idle_seconds
        )
        if context is None:
            return None
        return self.context_for_session(context)

    def context_for_session(self, context) -> TenantContext:
        """Project a store session into the common :class:`TenantContext`.

        The validator is the SSE liveness probe: it re-reads the session (and
        the joined user/tenant status) at most once per poll, and renews the
        idle window so an actively streaming run keeps its session alive while
        inactivity still expires it.
        """
        digest = context.session.session_digest
        store = self.store
        idle = self.config.session_idle_seconds
        validator = lambda: store.refresh_session(digest, idle_seconds=idle)  # noqa: E731
        return TenantContext(
            tenant_id=context.tenant.tenant_id,
            user_id=context.user.user_id,
            role=context.user.role,
            audience=ACCOUNTS_AUDIENCE,
            expires_at=context.session.expires_at,
            auth_mode="accounts",
            session_digest=digest,
            csrf_token=context.session.csrf_token,
            validator=validator,
        )

    def csrf_ok(self, context: TenantContext, candidate: Any) -> bool:
        if context.session_digest is None or context.csrf_token is None:
            return False
        if not isinstance(candidate, str) or not candidate:
            return False
        return hmac.compare_digest(context.csrf_token, candidate)

    # -- cookies --------------------------------------------------------------

    def cookie_header(self, token: str) -> str:
        attributes = [
            f"{SESSION_COOKIE}={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={max(60, int(self.config.session_absolute_seconds))}",
        ]
        if self.config.session_cookie_secure:
            attributes.append("Secure")
        return "; ".join(attributes)

    def clear_cookie_header(self) -> str:
        attributes = [
            f"{SESSION_COOKIE}=",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            "Max-Age=0",
        ]
        if self.config.session_cookie_secure:
            attributes.append("Secure")
        return "; ".join(attributes)

    # -- registration ---------------------------------------------------------

    def register(
        self, *, token: Any, email: Any, name: Any, password: Any, client_ip: str
    ) -> dict[str, Any]:
        register_key = f"register:{client_ip}"
        if not self.register_limiter.allow(register_key):
            raise AuthError(
                "rate_limited",
                "注册尝试次数过多，请稍后再试",
                retry_after=self.register_limiter.retry_after(register_key),
            )
        invitation = self.store.inspect_invitation(token)
        if invitation is None or invitation.used_at is not None or invitation.expires_at <= self.store.now():
            raise AuthError("invitation_invalid", "邀请无效或已使用")
        canonical = normalize_email(email)
        invited = normalize_email(invitation.email)
        if canonical is None or invited is None or not hmac.compare_digest(canonical, invited):
            # The invitation binds the mailbox: a client cannot register a
            # different email (or a malformed one) against someone's invite.
            raise AuthError("invitation_invalid", "邀请无效或已使用")
        try:
            user = self.store.register(token=token, name=name, password=password)
        except AccountsError as exc:
            raise AuthError(exc.code, exc.message) from exc
        return {"registered": True, "email": user.email}

    def check_invitation(self, token: Any, *, client_ip: str) -> dict[str, Any]:
        invite_key = f"invite:{client_ip}"
        if not self.invite_limiter.allow(invite_key):
            raise AuthError(
                "rate_limited",
                "查看邀请次数过多，请稍后再试",
                retry_after=self.invite_limiter.retry_after(invite_key),
            )
        invitation = self.store.inspect_invitation(token)
        if (
            invitation is None
            or invitation.used_at is not None
            or invitation.expires_at <= self.store.now()
        ):
            return {"valid": False}
        tenant = self.store.get_tenant(invitation.tenant_id)
        if tenant is None or tenant.status != TENANT_ACTIVE:
            return {"valid": False}
        return {
            "valid": True,
            "invitation": {
                "email": invitation.email,
                "role": invitation.role,
                "tenant": {"id": tenant.tenant_id, "name": tenant.name},
                "expiresAt": invitation.expires_at,
            },
        }

    def create_invitation(
        self, context: TenantContext, *, email: Any, role: Any
    ) -> dict[str, Any]:
        if not context.is_admin:
            raise AuthError("forbidden", "只有管理员可以邀请成员")
        try:
            record, token = self.store.create_invitation(
                tenant_id=context.tenant_id,
                email=email,
                role=role,
                created_by=context.user_id,
                ttl_seconds=self.config.invite_ttl_seconds,
            )
        except AccountsError as exc:
            raise AuthError(exc.code, exc.message) from exc
        tenant = self.store.get_tenant(record.tenant_id)
        return {
            "invitation": {
                "code": token,
                "email": record.email,
                "role": record.role,
                "expiresAt": record.expires_at,
                "tenant": {
                    "id": record.tenant_id,
                    "name": tenant.name if tenant is not None else record.tenant_id,
                },
            }
        }

    # -- public identity ------------------------------------------------------

    def me_payload(self, context: TenantContext) -> dict[str, Any]:
        """Display identity for ``/api/me`` — never keys, paths or other users."""
        user = self.store.get_user(context.user_id)
        tenant = self.store.get_tenant(context.tenant_id)
        if user is None or tenant is None:
            raise AuthError("unauthenticated", "会话对应的账号已不存在")
        csrf = context.csrf_token or self.store.csrf_token(context.session_digest or "") or ""
        return account_identity(user, tenant, csrf)
