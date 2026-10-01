"""Identity, session and request-protection rules of the host's HTTP surface.

One module owns the trust decisions: which credential header/cookie is believed
in which mode, what an unauthenticated answer looks like, the CSRF and
cross-site checks for state-changing requests, and the public configuration a
verified identity may see. Routing and streaming only consume the resulting
:class:`~glink_harness.tenancy.TenantContext`.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import urlsplit

from .auth import CSRF_HEADER, SESSION_COOKIE, AuthError
from .tenancy import ASSERTION_HEADER, TenantAuthError, TenantContext

logger = logging.getLogger("glink_harness.host")

#: Login/register/logout and admin invitations: the accounts-mode POST surface.
#: Every one of them enforces the same origin, Content-Type and (where a session
#: exists) CSRF rules as the run endpoints.
AUTH_POST_PATHS = frozenset(
    {
        "/api/auth/login",
        "/api/auth/register",
        "/api/auth/logout",
        "/api/admin/invitations",
        # The invitation token travels in a JSON body: a capability that can
        # create a member/admin account must never appear in a URL, a query
        # string, a Referer or an access log.
        "/api/invitations/check",
    }
)


def same_origin(url: str, expected: str) -> bool:
    """True when a browser-supplied Origin/Referer names this exact place.

    ``expected`` comes from the listening loopback address/port or the frozen
    public origin, never from the request's Host header. Parsed with :func:`urlsplit`;
    anything unparseable, a non-HTTP scheme or a mismatched netloc is refused.
    ``null`` origins (sandboxed iframes, file://) are refused as well.
    """
    if not expected:
        return False
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return False
    if "://" in expected:
        wanted = urlsplit(expected)
        return parts.netloc.lower() == wanted.netloc.lower() and parts.scheme == wanted.scheme
    return parts.netloc.lower() == expected.lower()


def assertion_expired(context: TenantContext | None) -> bool:
    """True when a verified identity may no longer act.

    Covers both a gateway assertion past its ``exp`` and an accounts session
    that was revoked, disabled, or idle/absolute-expired. Local mode
    (``context is None``) carries no identity, so nothing expires.
    """
    return context is not None and context.stale()


class RequestSecurityMixin:
    """Authentication, request protection and the public identity payloads."""

    # -- identity -------------------------------------------------------------

    def _authenticate(self) -> TenantContext | None:
        """Resolve the request's server-side identity, or ``None``.

        Accounts mode reads exactly one ``glink_harness_session`` cookie and checks
        it against the account store; the gateway assertion header is ignored
        entirely. Gateway mode is the opposite: the signed header is the only
        source and cookies are ignored. There is no fallback between the two,
        and local mode has no identity at all.
        """
        auth = self.server.auth
        if auth is not None:
            return auth.authenticate(self._session_token())
        tenancy = self.server.tenancy
        if tenancy is None or tenancy.authenticator is None:
            return None
        try:
            return tenancy.authenticator.authenticate(self.headers.get_all(ASSERTION_HEADER))
        except TenantAuthError as exc:
            logger.info("request rejected as unauthenticated: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001 - fail closed, never 500 on a hostile token
            # A malformed assertion must never turn into a 500: every
            # verification failure is an unauthenticated request. Only the
            # exception type is logged — never the token or its payload.
            logger.warning("assertion verification failed: %s", type(exc).__name__)
            return None

    def _session_token(self) -> str | None:
        """The single session cookie value, or ``None`` when absent/malformed."""
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except CookieError:
            return None
        morsel = jar.get(SESSION_COOKIE)
        if morsel is None:
            return None
        value = morsel.value
        return value if isinstance(value, str) and value else None

    def _unauthenticated_payload(self) -> dict[str, Any]:
        """The uniform 401 body.

        Accounts mode always names its mode so the browser knows to show the
        in-app login form. Gateway mode keeps the historical body for every
        route but ``/api/me`` (whose caller needs to know a gateway is in front
        of the host instead of a login page).
        """
        payload: dict[str, Any] = {"error": "unauthenticated"}
        if self.server.auth is not None:
            payload["mode"] = self.server.config.identity_mode
        return payload

    def _require_csrf(self, context: TenantContext | None) -> bool:
        """Enforce the session-bound CSRF token for accounts-mode mutations."""
        auth = self.server.auth
        if auth is None or context is None:
            return True
        if auth.csrf_ok(context, self.headers.get(CSRF_HEADER)):
            return True
        self._send_json(HTTPStatus.FORBIDDEN, {"error": "csrf_invalid"}, head_only=False)
        return False

    def _request_origin_allowed(self) -> bool:
        """Refuse a browser request whose own metadata says it is cross-site.

        A non-browser client (the gateway, curl, a smoke script) sends none of
        these headers, and cannot be tricked by a website into carrying the
        gateway's identity header. Any header that is present must agree that
        the request came from the actual listening loopback port or the frozen
        ``GLINK_PUBLIC_ORIGIN`` for a reverse proxy. Request-supplied Host
        and X-Forwarded-* are not trust roots (DNS rebinding can forge Host).
        """
        port = self.server.server_address[1]
        expected = [f"http://{host}:{port}" for host in ("127.0.0.1", "localhost", "[::1]")]
        public = self.server.config.public_origin
        if public:
            expected.append(public)
        origin = self.headers.get("Origin")
        if origin is not None and not any(same_origin(origin, candidate) for candidate in expected):
            return False
        referer = self.headers.get("Referer")
        if referer is not None and not any(same_origin(referer, candidate) for candidate in expected):
            return False
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in {"same-origin", "none"}:
            return False
        return True

    # -- accounts endpoints ---------------------------------------------------

    def _handle_auth_post(self, path: str) -> None:
        auth = self.server.auth
        if auth is None:
            # Gateway and local modes have no in-app login: the gateway owns
            # them, and the workbench must not grow a form with no backend.
            self._send_json(
                HTTPStatus.NOT_FOUND, {"error": "unknown_endpoint", "path": path}, head_only=False
            )
            return
        if not self._expects_json():
            self._send_json(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "unsupported_media_type", "message": "Content-Type 必须是 application/json"},
                head_only=False,
            )
            return
        if path == "/api/auth/login":
            self._login()
            return
        if path == "/api/auth/register":
            self._register()
            return
        if path == "/api/auth/logout":
            self._logout()
            return
        if path == "/api/admin/invitations":
            self._create_invitation()
            return
        if path == "/api/invitations/check":
            self._check_invitation()
            return
        self._send_json(
            HTTPStatus.NOT_FOUND, {"error": "unknown_endpoint", "path": path}, head_only=False
        )

    def _login(self) -> None:
        body = self._read_json_body()
        if body is None:
            return
        try:
            result = self.server.auth.login(
                body.get("email"),
                body.get("password"),
                client_ip=self._client_ip(),
                user_agent=self.headers.get("User-Agent", ""),
            )
        except AuthError as exc:
            self._send_auth_error(exc)
            return
        self._send_json(
            HTTPStatus.OK,
            result.identity,
            head_only=False,
            extra_headers={"Set-Cookie": self.server.auth.cookie_header(result.token)},
        )

    def _register(self) -> None:
        body = self._read_json_body()
        if body is None:
            return
        try:
            payload = self.server.auth.register(
                token=body.get("invite"),
                email=body.get("email"),
                name=body.get("name"),
                password=body.get("password"),
                client_ip=self._client_ip(),
            )
        except AuthError as exc:
            self._send_auth_error(exc)
            return
        # Registration deliberately does not issue a session: the contract is
        # "register, then log in", never a silently granted identity.
        self._send_json(HTTPStatus.CREATED, payload, head_only=False)

    def _logout(self) -> None:
        context = self._authenticate()
        if context is not None and not self._require_csrf(context):
            return
        self.server.auth.logout(self._session_token())
        self._send_json(
            HTTPStatus.OK,
            {"ok": True},
            head_only=False,
            extra_headers={"Set-Cookie": self.server.auth.clear_cookie_header()},
        )

    def _create_invitation(self) -> None:
        context = self._authenticate()
        if context is None:
            self._send_json(HTTPStatus.UNAUTHORIZED, self._unauthenticated_payload(), head_only=False)
            return
        if not self._require_csrf(context):
            return
        body = self._read_json_body()
        if body is None:
            return
        try:
            payload = self.server.auth.create_invitation(
                context, email=body.get("email"), role=body.get("role")
            )
        except AuthError as exc:
            self._send_auth_error(exc)
            return
        self._send_json(HTTPStatus.CREATED, payload, head_only=False)

    def _handle_me(self, *, head_only: bool) -> None:
        context = self._authenticate()
        mode = self.server.config.identity_mode
        if mode == "local":
            self._send_json(
                HTTPStatus.OK, {"mode": "local", "authenticated": False}, head_only=head_only
            )
            return
        if context is None:
            payload = self._unauthenticated_payload()
            payload["mode"] = mode
            self._send_json(HTTPStatus.UNAUTHORIZED, payload, head_only=head_only)
            return
        if self.server.auth is not None:
            try:
                payload = self.server.auth.me_payload(context)
            except AuthError as exc:
                self._send_auth_error(exc)
                return
            self._send_json(HTTPStatus.OK, payload, head_only=head_only)
            return
        # Gateway mode: the assertion carries ids and a role; there is no
        # display name to invent, so the organisation shows as its tenant id.
        self._send_json(
            HTTPStatus.OK,
            {
                "mode": "gateway",
                "authenticated": True,
                "user": {"id": context.user_id, "name": context.user_id, "role": context.role},
                "tenant": {"id": context.tenant_id, "name": context.tenant_id},
                "csrfToken": None,
                "roles": ["member", "admin"] if context.is_admin else ["member"],
            },
            head_only=head_only,
        )

    def _check_invitation(self) -> None:
        """Validate an invitation token from a JSON body (accounts mode only).

        Origin/Content-Type gating and the rate limiter are the same as the other
        auth POSTs; the token is read from the body so it cannot reach a request
        line, a proxy log or a same-origin Referer.
        """
        auth = self.server.auth
        if auth is None:  # pragma: no cover - the GET route answers this first
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "unknown_endpoint", "path": "/api/invitations/check"},
                head_only=False,
            )
            return
        body = self._read_json_body()
        if body is None:
            return
        token = body.get("token")
        try:
            payload = auth.check_invitation(token, client_ip=self._client_ip())
        except AuthError as exc:
            self._send_auth_error(exc)
            return
        self._send_json(HTTPStatus.OK, payload, head_only=False)

    def _send_auth_error(self, error: AuthError) -> None:
        payload: dict[str, Any] = {"error": error.code}
        if error.message:
            payload["message"] = error.message
        headers = {}
        if error.code == "rate_limited" and error.retry_after > 0:
            headers["Retry-After"] = str(int(error.retry_after) + 1)
        self._send_json(
            HTTPStatus(error.status), payload, head_only=False, extra_headers=headers
        )

    # -- public payloads ------------------------------------------------------

    def _public_config(self, context: TenantContext | None) -> dict[str, Any]:
        """Public configuration for the requesting identity.

        ``configured.model`` answers "can this caller start a run". In local
        mode that is the host's key; under the default multi-tenant policy it
        is the caller's own tenant credential; under the explicit ``shared``
        policy it is the one host-frozen credential every tenant uses — never
        whether another tenant has a key. No credential value, and no
        user-configurable model control, is part of this payload.
        """
        view = dict(self.server.config.public_view())
        if context is not None and self.server.tenancy is not None:
            if self.server.config.shared_model_credentials:
                view["configured"] = {
                    **view["configured"],
                    # 共享政策下就是宿主冻结的那套凭据：族感知的宿主级就绪
                    # （pi 族看 OPENAI key；ACP 族看具名凭据通道）。
                    "model": self.server.config.model_ready,
                }
            else:
                credentials = self.server.tenancy.credentials
                tenant = (
                    credentials.get(context.tenant_id)
                    if credentials is not None
                    else None
                )
                view["configured"] = {
                    **view["configured"],
                    "model": bool(tenant is not None and tenant.model_api_key),
                }
        return view
