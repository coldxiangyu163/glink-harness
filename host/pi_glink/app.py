"""HTTP entry point for the pi-glink host.

One process serves the workbench front-end and the run event stream. There is
no web framework in the loop: the surface is small (health, public config, a
per-run SSE stream and static assets), and keeping it on the standard library
makes ``python -m pi_glink.app`` the only thing an operator has to run.

Run events are never stored here — the bus is the live fan-out plus a bounded
replay window; durable run metadata, the committed event log and artifacts
belong to :mod:`pi_glink.store`. A host process owns its data root exclusively
(:mod:`pi_glink.ownership`) before it reconciles or serves anything.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import threading
from dataclasses import replace
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from .accounts import AccountsError, AccountsStore
from .artifacts import ArtifactError, ArtifactTree
from .auth import CSRF_HEADER, SESSION_COOKIE, AccountsAuth, AuthError
from .config import ConfigError, HostConfig, load_config
from .events import EventBus, IDLE_POLL
from .kernel.runner import (
    ClientFactory,
    REPORT_MAX_BYTES,
    FollowupUnavailable,
    ResourcesUnreleased,
    RunBusy,
    RunManager,
    RunQuotaExceeded,
)
from .kernel.rpc import PiRpcClient
from .ownership import DataRootBusy, DataRootOwnership
from .static_files import Asset, WebAssets
from .store import RunRecord, RunStore
from .tenancy import (
    ASSERTION_HEADER,
    Tenancy,
    TenantAuthError,
    TenantContext,
    TenantNotConfigured,
    build_tenancy,
    load_identity_material,
)

logger = logging.getLogger("pi_glink.host")

_RUN_EVENTS_RE = re.compile(r"^/api/runs/(?P<run_id>[A-Za-z0-9_-]{1,64})/events$")
_RUN_STOP_RE = re.compile(r"^/api/runs/(?P<run_id>[A-Za-z0-9_-]{1,64})/stop$")
_RUN_REPORT_RE = re.compile(r"^/api/runs/(?P<run_id>[A-Za-z0-9_-]{1,64})/report$")
_RUN_RE = re.compile(r"^/api/runs/(?P<run_id>[A-Za-z0-9_-]{1,64})$")
_MAX_REQUEST_BYTES = 64 * 1024
_HEARTBEAT_SECONDS = 15.0
#: Login/register/logout and admin invitations: the accounts-mode POST surface.
#: Every one of them enforces the same origin, Content-Type and (where a session
#: exists) CSRF rules as the run endpoints.
_AUTH_POST_PATHS = frozenset(
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
#: Run-body fields a client must never use to pick a model or supply a
#: credential. The provider is a server-side decision in every mode (the local
#: ``OPENAI_*`` configuration or the explicit multi-tenant policy), so these
#: names are refused outright instead of being silently ignored. Identity-ish
#: fields stay ignored (and are pinned by a regression) because they never
#: feed ``TenantContext``.
_FORBIDDEN_RUN_BODY_FIELDS = frozenset(
    {
        "model",
        "modelid",
        "provider",
        "apikey",
        "modelapikey",
        "baseurl",
        "openaiapikey",
        "openaibaseurl",
        "openaimodel",
        "websearchkey",
        "credentials",
    }
)


class PiGlinkServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying the frozen config, bus, store and manager.

    When built by :func:`create_server` it also carries the data-root
    ownership, released by :meth:`server_close`.
    """

    daemon_threads = True
    allow_reuse_address = True
    #: Accept a burst of concurrent connections: the admission ceiling (not the
    #: listen backlog) must be what refuses a request, otherwise a client sees a
    #: connection reset instead of an explainable 429.
    request_queue_size = 64

    def __init__(
        self,
        config: HostConfig,
        bus: EventBus,
        store: RunStore,
        manager: RunManager,
        assets: WebAssets,
        *,
        ownership: DataRootOwnership | None = None,
        tenancy: Tenancy | None = None,
        auth: AccountsAuth | None = None,
    ) -> None:
        self.config = config
        self.bus = bus
        self.store = store
        self.manager = manager
        self.assets = assets
        self.ownership = ownership
        #: Frozen multi-tenant trust material; ``None`` in local mode.
        self.tenancy = tenancy
        #: Identity backend for accounts mode; ``None`` in gateway and local mode.
        self.auth = auth
        #: The account store, when accounts mode is active (tests and operator
        #: tooling read it through the server that owns it).
        self.accounts: AccountsStore | None = auth.store if auth is not None else None
        self.stopping = threading.Event()
        super().__init__((config.host, config.port), HostRequestHandler)

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            # The ledger sweep is host lifecycle: stop it before the manager and
            # the data-root lock go away, so no probe runs against a closed host.
            self.manager.stop_resource_watch()
            if self.tenancy is not None:
                self.tenancy.close()
            if self.ownership is not None:
                self.ownership.release()


class HostRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "pi-glink"

    server: PiGlinkServer

    # -- routes ---------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server contract
        self._dispatch(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802 - http.server contract
        self._dispatch(head_only=True)

    def do_POST(self) -> None:  # noqa: N802 - http.server contract
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        # A handler that never reads the request body (stop, logout) would leave
        # those bytes in the socket; with HTTP/1.1 keep-alive the next request on
        # the same connection then starts with the leftover body — the server
        # parses it as a malformed request line and answers 501. The finally
        # block below drains whatever a handler did not consume.
        self._body_consumed = False
        try:
            # Multi-tenant POSTs carry a server-derived identity (an injected
            # assertion or a session cookie), and a browser can be made to send
            # one cross-site; refuse requests whose own origin metadata says
            # they came from another site. This runs before any credential is
            # checked — including login and registration.
            if self.server.tenancy is not None and not self._request_origin_allowed():
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {"error": "forbidden", "message": "cross-site request refused"},
                    head_only=False,
                )
                return
            if path in _AUTH_POST_PATHS:
                self._handle_auth_post(path)
                return
            if path == "/api/runs":
                self._start_run()
                return
            stop_match = _RUN_STOP_RE.match(path)
            if stop_match is not None:
                self._stop_run(stop_match.group("run_id"))
                return
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "unknown_endpoint", "path": path},
                head_only=False,
            )
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - client left
            self.close_connection = True
        except Exception:  # pragma: no cover - defensive: never kill the server
            logger.exception("unhandled error serving POST %s", self._log_target())
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"}, head_only=False
            )
        finally:
            if not getattr(self, "_body_consumed", True):
                self._drain_body()

    def _drain_body(self) -> None:
        """Read and discard an unconsumed request body.

        Keeps an HTTP/1.1 keep-alive connection in sync when a route ignores the
        body (``stop``, ``logout``). The read is bounded by the declared length
        and by :data:`_MAX_REQUEST_BYTES`; an oversized body closes the
        connection instead of being drained.
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            length = 0
        if length > _MAX_REQUEST_BYTES:
            self.close_connection = True
            return
        remaining = max(0, length)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _dispatch(self, *, head_only: bool) -> None:
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        try:
            if path.startswith("/api/"):
                self._handle_api(path, parts.query, head_only=head_only)
                return
            self._handle_static(path, head_only=head_only)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - client left
            self.close_connection = True
        except Exception:  # pragma: no cover - defensive: never kill the server
            logger.exception("unhandled error serving %s", self._log_target())
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal_error"},
                head_only=head_only,
            )

    def _handle_api(self, path: str, query: str, *, head_only: bool) -> None:
        # The health probe stays reachable without an identity (a gateway or
        # process supervisor may poll it), but in multi-tenant mode it never
        # reports another tenant's active run. Every other /api route requires a
        # verified identity before it reads or reveals anything.
        if path == "/api/health":
            self._send_json(HTTPStatus.OK, self._health_payload(), head_only=head_only)
            return
        # ``/api/me`` answers for every mode: local mode reports the single-user
        # surface, gateway and accounts modes report the authenticated identity
        # (and answer 401 with the mode when there is none). The browser needs
        # this before it can decide whether to show a login form at all.
        if path == "/api/me":
            self._handle_me(head_only=head_only)
            return
        if path == "/api/invitations/check":
            # The check is a POST with a JSON body. A GET whose query carries a
            # token is refused before the query is read (and before anything can
            # log it), so the old URL form is dead rather than merely deprecated.
            if self.server.auth is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "unknown_endpoint", "path": "/api/invitations/check"},
                    head_only=head_only,
                )
            else:
                self._send_json(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"error": "method_not_allowed", "message": "邀请校验只接受 POST JSON 请求"},
                    head_only=head_only,
                    extra_headers={"Allow": "POST"},
                )
            return
        context = self._authenticate()
        if self.server.tenancy is not None and context is None:
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                self._unauthenticated_payload(),
                head_only=head_only,
            )
            return
        if path == "/api/config":
            self._send_json(
                HTTPStatus.OK,
                self._public_config(context),
                head_only=head_only,
            )
            return
        if path == "/api/runs":
            records = (
                self.server.store.list_for(context.tenant_id, limit=100)
                if context is not None
                else self.server.store.list(limit=100)
            )
            self._send_json(
                HTTPStatus.OK,
                {"runs": [self._run_view_live(record) for record in records]},
                head_only=head_only,
            )
            return
        events_match = _RUN_EVENTS_RE.match(path)
        if events_match is not None:
            self._stream_run_events(events_match.group("run_id"), query, context)
            return
        report_match = _RUN_REPORT_RE.match(path)
        if report_match is not None:
            self._send_report(report_match.group("run_id"), context, head_only=head_only)
            return
        run_match = _RUN_RE.match(path)
        if run_match is not None:
            self._send_run(run_match.group("run_id"), context, head_only=head_only)
            return
        self._send_json(
            HTTPStatus.NOT_FOUND, {"error": "unknown_endpoint", "path": path},
            head_only=head_only,
        )

    # -- identity -------------------------------------------------------------

    def _authenticate(self) -> TenantContext | None:
        """Resolve the request's server-side identity, or ``None``.

        Accounts mode reads exactly one ``pi_glink_session`` cookie and checks
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

    def _client_ip(self) -> str:
        """The socket peer address.

        Deliberately not ``X-Forwarded-For``: a TLS-terminating proxy makes
        every request look like loopback, and a client-supplied header must
        never label a request (it would hand an attacker a free rate-limit
        bypass). Limits behind a proxy therefore act as a global throttle, and
        the per-account login limit is the primary protection.
        """
        return str(self.client_address[0]) if self.client_address else "unknown"

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

    def _expects_json(self) -> bool:
        raw = self.headers.get("Content-Type", "")
        return raw.split(";", 1)[0].strip().lower() == "application/json"

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

    def _request_origin_allowed(self) -> bool:
        """Refuse a browser request whose own metadata says it is cross-site.

        A non-browser client (the gateway, curl, a smoke script) sends none of
        these headers, and cannot be tricked by a website into carrying the
        gateway's identity header. Any header that is present must agree that
        the request came from this origin — either the host the request was
        addressed to, or the frozen ``PI_GLINK_PUBLIC_ORIGIN`` when a TLS
        terminating proxy presents a different external origin. Client-supplied
        ``X-Forwarded-*`` headers are never consulted.
        """
        host = self.headers.get("Host", "")
        expected = [host] if host else []
        public = self.server.config.public_origin
        if public:
            expected.append(public)
        origin = self.headers.get("Origin")
        if origin is not None and not any(_same_origin(origin, candidate) for candidate in expected):
            return False
        referer = self.headers.get("Referer")
        if referer is not None and not any(_same_origin(referer, candidate) for candidate in expected):
            return False
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in {"same-origin", "none"}:
            return False
        return True

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
                    "model": bool(
                        self.server.config.has_openai_key
                        and self.server.config.openai_model
                    ),
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

    def _handle_static(self, path: str, *, head_only: bool) -> None:
        asset = self.server.assets.resolve(path)
        if asset is None:
            self._send_json(
                HTTPStatus.NOT_FOUND, {"error": "not_found", "path": path},
                head_only=head_only,
            )
            return
        self._send_asset(asset, head_only=head_only)

    # -- payloads -------------------------------------------------------------

    def _health_payload(self) -> dict[str, Any]:
        config = self.server.config
        pi_path = shutil.which(config.pi_bin)
        payload: dict[str, Any] = {
            "status": "ok",
            "executor": config.executor,
            "pi": {"bin": config.pi_bin, "available": pi_path is not None},
            "configured": config.public_view()["configured"],
        }
        # An unauthenticated probe must not disclose whether (or which) runs are
        # active in the multi-tenant surface.
        if self.server.tenancy is None:
            payload["activeRunId"] = self.server.manager.active_run_id
        return payload

    # -- run endpoints --------------------------------------------------------

    def _start_run(self) -> None:
        context = self._authenticate()
        if self.server.tenancy is not None and context is None:
            self._send_json(
                HTTPStatus.UNAUTHORIZED, self._unauthenticated_payload(), head_only=False
            )
            return
        if not self._require_csrf(context):
            return
        body = self._read_json_body()
        if body is None:
            return
        # The model and its credential are server-side decisions: a client that
        # tries to select or supply either one is refused before any execution
        # face exists, instead of quietly running with an ignored field.
        supplied = sorted(
            name for name in body if name.strip().lower() in _FORBIDDEN_RUN_BODY_FIELDS
        )
        if supplied:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "invalid_request",
                    "message": "模型与凭据只能由服务端配置，客户端字段不被接受",
                    "fields": supplied,
                },
                head_only=False,
            )
            return
        prompt = body.get("prompt")
        if not isinstance(prompt, str):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_request", "message": "prompt must be a string"},
                head_only=False,
            )
            return
        parent_run_id = body.get("parentRunId")
        if parent_run_id is not None and (not isinstance(parent_run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", parent_run_id)):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"}, head_only=False)
            return
        try:
            record = self.server.manager.start(
                prompt, context, parent_run_id=parent_run_id
            )
        except FollowupUnavailable as exc:
            # A follow-up may only continue a run its own tenant can read: an
            # unknown, other-tenant or ownerless parent is the same 404 as an
            # unknown run, and nothing of the parent is read before this gate.
            self._send_json(
                HTTPStatus.CONFLICT if exc.code != "unknown_run" else HTTPStatus.NOT_FOUND,
                {"error": exc.code},
                head_only=False,
            )
            return
        except RunQuotaExceeded as quota:
            # Explain the ceiling, never who else is using it.
            self._send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {
                    "error": "quota_exceeded",
                    "scope": quota.scope,
                    "limit": quota.limit,
                    "message": "concurrency limit reached; retry later",
                },
                head_only=False,
            )
            return
        except RunBusy as busy:
            payload: dict[str, Any] = {
                "error": "run_busy",
                "message": "another run is still active",
            }
            if not self.server.config.multi_tenant:
                payload["activeRunId"] = busy.active_run_id
            self._send_json(HTTPStatus.CONFLICT, payload, head_only=False)
            return
        except ResourcesUnreleased as held:
            payload = {
                "error": "resources_unreleased",
                "message": "earlier runs hold execution resources that are not confirmed released",
            }
            # The held run ids belong to their owners: a tenant must not learn
            # another tenant's identifiers from a refusal.
            if not self.server.config.multi_tenant:
                payload["runIds"] = held.run_ids
            self._send_json(HTTPStatus.CONFLICT, payload, head_only=False)
            return
        except TenantNotConfigured as exc:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "forbidden", "message": str(exc)},
                head_only=False,
            )
            return
        except ConfigError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "not_configured", "message": str(exc)},
                head_only=False,
            )
            return
        except ValueError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_request", "message": str(exc)},
                head_only=False,
            )
            return
        self._send_json(
            HTTPStatus.CREATED, {"run": self._run_view_live(record)}, head_only=False
        )

    def _stop_run(self, run_id: str) -> None:
        context = self._authenticate()
        if self.server.tenancy is not None and context is None:
            self._send_json(
                HTTPStatus.UNAUTHORIZED, self._unauthenticated_payload(), head_only=False
            )
            return
        if not self._require_csrf(context):
            return
        decision = self.server.manager.stop(run_id, context)
        if decision.accepted:
            self._send_json(
                HTTPStatus.OK,
                {"requested": True, "status": decision.status},
                head_only=False,
            )
        elif decision.reason == "unknown":
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "unknown_run", "requested": False, "runId": run_id},
                head_only=False,
            )
        elif decision.reason == "forbidden":
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "forbidden", "requested": False},
                head_only=False,
            )
        else:
            # Past the run's commit point: the answer is the committed status,
            # not a promise that the run will be stopped.
            self._send_json(
                HTTPStatus.CONFLICT,
                {
                    "error": "run_not_cancellable",
                    "requested": False,
                    "status": decision.status,
                    "reason": decision.reason,
                },
                head_only=False,
            )

    def _run_view_live(self, record: RunRecord) -> dict[str, Any]:
        """Run view with the live event count while the run is still open.

        ``meta.json`` is only rewritten at state transitions, so the persisted
        count is stale during a run; an open run's count comes from the bus.
        Once the run is closed the persisted count is authoritative (the bus
        drops its per-run window at close).
        """
        live = (
            self.server.bus.last_seq(record.run_id)
            if self.server.bus.is_open(record.run_id)
            else None
        )
        return _run_view(
            record, live_event_count=live, has_report=self._report_available(record)
        )

    def _report_available(self, record: RunRecord) -> bool:
        """Whether the report endpoint would serve this record's report.

        The flag and the download use the same location rule and the same
        refusals, so ``hasReport`` never promises a file the endpoint refuses.
        """
        location = self.server.store.report_location(record)
        if location is None:
            return False
        try:
            with ArtifactTree(location.root) as tree:
                tree.size(location.relative, max_bytes=REPORT_MAX_BYTES)
        except (ArtifactError, OSError):
            return False
        return True

    def _send_run(
        self, run_id: str, context: TenantContext | None, *, head_only: bool
    ) -> None:
        record = (
            self.server.store.get_for(run_id, context.tenant_id)
            if context is not None
            else self.server.store.get(run_id)
        )
        if record is None:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "unknown_run", "runId": run_id},
                head_only=head_only,
            )
            return
        self._send_json(
            HTTPStatus.OK, {"run": self._run_view_live(record)}, head_only=head_only
        )

    def _send_report(
        self, run_id: str, context: TenantContext | None, *, head_only: bool
    ) -> None:
        record = (
            self.server.store.get_for(run_id, context.tenant_id)
            if context is not None
            else self.server.store.get(run_id)
        )
        # ``report_path`` is not a download capability: only the run's own
        # host artifact, or the report inside the run's own workspace written
        # by earlier versions, may be served — read-only, through a directory
        # handle that never follows a link. An unknown run, another tenant's run
        # and a legacy ownerless record all answer the same 404.
        location = self.server.store.report_location(record) if record else None
        if location is None:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "no_report", "runId": run_id},
                head_only=head_only,
            )
            return
        try:
            with ArtifactTree(location.root) as tree:
                data = tree.read(location.relative, max_bytes=REPORT_MAX_BYTES)
        except (ArtifactError, OSError) as exc:
            logger.warning(
                "report refused run=%s layout=%s: %s", run_id, location.layout, exc
            )
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "no_report", "runId": run_id},
                head_only=head_only,
            )
            return
        # The report is a product surface too: a credential the model echoed into
        # it must not reach the browser. The header records whether it happened.
        # The redaction boundary is the record's own tenant credentials, so one
        # tenant's report is never compared against another tenant's secrets.
        original = data.decode("utf-8", errors="replace")
        redactor = self.server.manager.redactor_for_record(record)
        redacted = redactor.redact(original)
        self._send_bytes(
            redacted.encode("utf-8"),
            "text/markdown; charset=utf-8",
            "no-cache",
            head_only=head_only,
            extra_headers={"X-Redacted": "true" if redacted != original else "false"},
        )

    def _read_json_body(self) -> dict[str, Any] | None:
        # Mark the body as handled: this method either consumes it or closes the
        # connection, and ``do_POST`` must not drain it a second time.
        self._body_consumed = True
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_REQUEST_BYTES:
            if length > _MAX_REQUEST_BYTES:
                # Never reuse a connection whose unread body we are not willing
                # to drain; the client must reconnect cleanly.
                self.close_connection = True
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_request", "message": "missing or oversized body"},
                head_only=False,
            )
            return None
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_request", "message": "body must be JSON"},
                head_only=False,
            )
            return None
        if not isinstance(payload, dict):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_request", "message": "body must be a JSON object"},
                head_only=False,
            )
            return None
        return payload

    # -- responses ------------------------------------------------------------

    def _send_json(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        *,
        head_only: bool,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_asset(self, asset: Asset, *, head_only: bool) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", asset.content_type)
        self.send_header("Content-Length", str(len(asset.body)))
        self.send_header("Cache-Control", asset.cache_control)
        self.end_headers()
        if not head_only:
            self.wfile.write(asset.body)

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        cache_control: str,
        *,
        head_only: bool,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    # -- server-sent events ---------------------------------------------------

    def _stream_run_events(
        self, run_id: str, query: str, context: TenantContext | None
    ) -> None:
        bus = self.server.bus
        store = self.server.store
        # Authorization happens before a single response header is written. An
        # unknown run, another tenant's run and an ownerless legacy record all
        # answer the same 404, so the stream cannot be used to probe existence.
        if context is not None and store.get_for(run_id, context.tenant_id) is None:
            self._send_json(
                HTTPStatus.NOT_FOUND, {"error": "unknown_run", "runId": run_id},
                head_only=False,
            )
            return
        live = bus.is_open(run_id)
        # The durable log is the replay source of truth; the bus only knows runs
        # started by this host process and is used for the live tail. A run from
        # an earlier process is replayed from events.jsonl — otherwise every
        # history entry would 404 after a restart.
        persisted = store.events_path(run_id).is_file()
        if not live and not persisted:
            self._send_json(
                HTTPStatus.NOT_FOUND, {"error": "unknown_run", "runId": run_id},
                head_only=False,
            )
            return
        after = _resume_offset(self.headers.get("Last-Event-ID"), query)
        # Subscribe *before* the response headers are written: the subscriber
        # captures the live tail from this moment, so a run that closes between
        # the liveness check and the header write cannot swallow frames that
        # exist only in the bus ring (persisted frames are replayed from the log
        # either way). Reading the log still happens after subscribing.
        subscription = (
            bus.subscribe(run_id, after=after, idle_poll=IDLE_POLL) if live else None
        )
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            # The body below is written as HTTP/1.1 chunks (_write_event); without
            # this header a client reads the chunk-size lines as part of the stream.
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
        except BaseException:
            if subscription is not None:
                subscription.close()
            raise
        self.close_connection = True
        logger.info("event stream attached run=%s after=%s live=%s", run_id, after, live)
        try:
            last = after
            for event in store.iter_events(run_id, after=after):
                # Expiry is checked on every frame, replay included: a long
                # history (or a slow reader) must not keep serving frames past
                # the assertion's expiration, with or without a live
                # subscription. The stream then ends cleanly and the client
                # re-authenticates.
                if _assertion_expired(context):
                    logger.info(
                        "event stream ended: assertion expired during replay run=%s",
                        run_id,
                    )
                    return
                seq = event.get("seq")
                if isinstance(seq, int):
                    if seq <= last:
                        continue
                    last = seq
                self._write_event(_persisted_sse(event))
            if subscription is not None:
                while True:
                    # A short-lived assertion must not outlive itself on a long
                    # stream: once it expires the stream is closed cleanly and
                    # the client must re-authenticate (and reconnect).
                    if _assertion_expired(context):
                        logger.info(
                            "event stream ended: assertion expired run=%s", run_id
                        )
                        break
                    try:
                        item = next(subscription)
                    except StopIteration:
                        break
                    if item is IDLE_POLL:
                        continue
                    event = item
                    if event.seq <= last:
                        continue
                    last = event.seq
                    self._write_event(event.to_sse())
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if subscription is not None:
                subscription.close()
            logger.info("event stream detached run=%s", run_id)
            self._write_end_of_stream()

    def _write_event(self, frame: str) -> None:
        body = frame.encode("utf-8")
        self.wfile.write(f"{len(body):X}\r\n".encode("ascii") + body + b"\r\n")
        self.wfile.flush()

    def _write_end_of_stream(self) -> None:
        """Terminate the chunked response so a read sees a clean EOF."""
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _sanitized_log_argument(self, value: Any) -> str:
        """A log argument with any query string/fragment removed.

        Invitation codes and session tokens must not be written to logs (which
        are commonly shipped to a collector). Our own URLs never carry them, but
        a hostile or outdated client can still try; everything from the first
        ``?`` or ``#`` on is dropped before the message is formatted.
        """
        text = str(value)
        for marker in ("?", "#"):
            head, found, _tail = text.partition(marker)
            if found:
                text = head
        return text

    def _log_target(self) -> str:
        """The request path without query/fragment, safe to log."""
        target = urlsplit(self.path).path
        return self._sanitized_log_argument(target or self.path)

    def _log_request(self, level: int, format: str, args: tuple[Any, ...]) -> None:
        safe = tuple(self._sanitized_log_argument(argument) for argument in args)
        try:
            message = format % safe
        except (TypeError, ValueError):  # pragma: no cover - unusual custom format
            message = self._log_target()
        logger.log(level, "%s - %s", self.address_string(), message)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        self._log_request(logging.DEBUG, format, args)

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        self._log_request(logging.WARNING, format, args)


def _persisted_sse(event: Mapping[str, Any]) -> str:
    """SSE frame for an event read back from events.jsonl (same shape as live frames)."""
    body = json.dumps(event, ensure_ascii=False)
    return f"id: {event.get('seq', '')}\nevent: {event['type']}\ndata: {body}\n\n"


def _run_view(
    record: RunRecord, *, live_event_count: int | None = None, has_report: bool = False
) -> dict[str, Any]:
    """Public projection of a run: no host paths, no credentials."""
    duration = None
    if record.finished_at is not None:
        duration = round(record.finished_at - record.created_at, 3)
    return {
        "runId": record.run_id,
        "status": record.status,
        "prompt": record.prompt,
        "parentRunId": record.parent_run_id,
        "createdAt": record.created_at,
        "finishedAt": record.finished_at,
        "durationSeconds": duration,
        "sessionId": record.session_id,
        "hasReport": has_report,
        "stopReason": record.stop_reason,
        "error": record.error,
        "eventCount": record.event_count if live_event_count is None else live_event_count,
        "usage": record.usage,
        "redacted": record.redacted,
    }


def _assertion_expired(context: TenantContext | None) -> bool:
    """True when a verified identity may no longer act.

    Covers both a gateway assertion past its ``exp`` and an accounts session
    that was revoked, disabled, or idle/absolute-expired. Local mode
    (``context is None``) carries no identity, so nothing expires.
    """
    return context is not None and context.stale()


def _same_origin(url: str, expected: str) -> bool:
    """True when a browser-supplied Origin/Referer names this exact place.

    ``expected`` is either the ``Host`` header (``host[:port]``) or the frozen
    public origin (``scheme://host[:port]``). Parsed with :func:`urlsplit`;
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


def _resume_offset(last_event_id: str | None, query: str) -> int:
    """Resolve the resume cursor: header wins, then ``?after=``, else 0."""
    candidates = [last_event_id]
    candidates.extend(parse_qs(query).get("after", []))
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            return max(0, int(candidate))
        except (TypeError, ValueError):
            continue
    return 0


def create_server(
    config: HostConfig,
    *,
    client_factory: ClientFactory | None = None,
    docker_probe: Callable[[str], tuple[bool, str]] | None = None,
    sandbox_probe: Callable[[str], tuple[bool, str]] | None = None,
) -> PiGlinkServer:
    """Build a ready-to-serve host (used by tests and by ``main``).

    ``client_factory`` and the two identity probes exist for tests: production
    always talks to the real ``pi`` binary, the real Docker CLI and the real E2B
    SDK. The probes are what the online resource-ledger sweep calls; they never
    run inside the admission lock.

    Ordering is part of the safety contract:

    1. the data-root lock is acquired **first**, before any read or write under
       the data root — including account-database creation and migration;
    2. identity material is loaded (a missing or over-permissive key file
       refuses to start instead of falling back to the unauthenticated local
       surface);
    3. the run store is reconciled and the port is bound last;
    4. only then does the periodic resource sweep start.
    """
    # Ownership comes first: only a process that holds the data root may
    # conclude that a ``running`` record has no live owner, or create/rewrite
    # the account database. A second host on the same data root (same or
    # another port) stops here, before touching any record.
    ownership = DataRootOwnership.acquire(config.data_root)
    tenancy: Tenancy | None = None
    try:
        # Accounts mode opens/creates/migrates the SQLite database here, under
        # the lock; gateway mode only reads files.
        tenancy = build_tenancy(config)
        if config.accounts_enabled and (tenancy is None or tenancy.accounts is None):
            # Defensive: never serve accounts mode without its store.
            raise ConfigError("PI_GLINK_AUTH_MODE=accounts 账号库初始化失败")
        # Accounts mode gets its session backend here; gateway and local mode
        # have no in-app login at all.
        auth = AccountsAuth(config, tenancy.accounts) if config.accounts_enabled else None
        bus = EventBus()
        store = RunStore(config.data_root / "runs")
        manager = RunManager(
            config,
            bus,
            store,
            client_factory=client_factory or PiRpcClient,
            ownership=ownership,
            credential_store=tenancy.credentials if tenancy is not None else None,
            docker_probe=docker_probe,
            sandbox_probe=sandbox_probe,
        )
        # Runs left ``running`` by a previous process cannot continue; close
        # them out honestly before the host serves history. External leftovers
        # are not touched here — their identity is recorded for the operator,
        # and the periodic sweep below is the only thing that may confirm one.
        manager.reconcile()
        server = PiGlinkServer(
            config,
            bus,
            store,
            manager,
            WebAssets(config.web_root),
            ownership=ownership,
            tenancy=tenancy,
            auth=auth,
        )
        # After startup reconciliation: one sweep immediately, then every
        # configured interval (0 disables it). Background, daemon, never inside
        # the admission lock.
        manager.start_resource_watch()
        return server
    except BaseException:
        if tenancy is not None:
            tenancy.close()
        ownership.release()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pi-glink",
        description="本地工作台宿主 / local workbench host (pi kernel)",
    )
    parser.add_argument("--host", help="覆盖 LOCAL_WEB_HOST")
    parser.add_argument("--port", type=int, help="覆盖 LOCAL_WEB_PORT")
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="打印生效配置（不含密钥）后退出",
    )
    parser.add_argument(
        "--unreleased",
        action="store_true",
        help="列出执行资源未确认释放的 run 后退出（需宿主未运行）",
    )
    parser.add_argument(
        "--mark-released",
        metavar="RUN_ID",
        help="记录人工裁决：已核对该 run 的执行资源已释放（需宿主未运行，须配合 --note）",
    )
    parser.add_argument("--note", help="人工裁决的核对说明")
    # -- operator onboarding (accounts mode) ---------------------------------
    parser.add_argument(
        "--create-invite",
        action="store_true",
        help="为租户签发一份一次性管理员/成员邀请（需宿主未运行；tenant 策略要求该租户已有模型凭据）",
    )
    parser.add_argument("--tenant", help="--create-invite：租户 id（tenant 策略下必须已有模型凭据）")
    parser.add_argument("--tenant-name", help="--create-invite：组织显示名（默认沿用已有名称）")
    parser.add_argument("--email", help="--create-invite：受邀邮箱")
    parser.add_argument("--name", help="--create-invite：受邀人姓名（仅用于提示）")
    parser.add_argument(
        "--role",
        choices=("admin", "member"),
        default="admin",
        help="--create-invite：受邀角色（默认 admin）",
    )
    parser.add_argument(
        "--invite-ttl-seconds",
        type=int,
        default=None,
        help="--create-invite：邀请有效期秒数（默认使用配置值）",
    )
    args = parser.parse_args(argv)
    if args.mark_released and not (args.note or "").strip():
        parser.error("--mark-released 需要 --note 说明如何核对了资源释放")
    if args.create_invite:
        missing = [
            name
            for name in ("tenant", "email")
            if not (getattr(args, name) or "").strip()
        ]
        if missing:
            parser.error("--create-invite 需要 " + "、".join(f"--{name}" for name in missing))

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"配置错误 / configuration error: {exc}", file=sys.stderr)
        return 2
    if args.host or args.port:
        config = replace(
            config,
            host=args.host or config.host,
            port=args.port or config.port,
        )
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.print_config:
        try:
            # Validation only: never open the account database here. Creating or
            # migrating it is a data-root write that requires the host lock.
            load_identity_material(config)
        except ConfigError as exc:
            print(f"配置错误 / configuration error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(config.public_view(), ensure_ascii=False, indent=2))
        return 0
    if args.unreleased or args.mark_released:
        return _resource_command(config, args.mark_released, args.note)
    if args.create_invite:
        return _create_invite_command(config, args)

    try:
        server = create_server(config)
    except DataRootBusy as exc:
        print(f"启动失败 / refused: {exc}", file=sys.stderr)
        return 2
    except ConfigError as exc:
        print(f"配置错误 / configuration error: {exc}", file=sys.stderr)
        return 2
    url = f"http://{config.host}:{server.server_address[1]}/"
    pi_path = shutil.which(config.pi_bin)
    print(f"pi-glink 宿主已启动 / host listening: {url}")
    print(f"执行面 executor: {config.executor}")
    print(f"内核 pi: {pi_path or f'未找到 ({config.pi_bin}) / not found'}")
    print(
        "模型 model: "
        + (config.openai_model or "未配置 / unset")
        + (" (已配置密钥 / key set)" if config.has_openai_key else " (缺少密钥 / no key)")
    )
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，正在退出 / interrupted, shutting down")
    finally:
        # Cancel and reap this process's runs before the HTTP server disappears;
        # a run that does not release in time is reported, not hidden.
        unreleased = server.manager.shutdown(timeout=15.0)
        server.server_close()
    if unreleased:
        print(
            "警告：以下 run 的终态或执行资源尚未确认，需要人工对账："
            + ", ".join(unreleased)
            + "（核对后用 --mark-released RUN_ID --note … 记录裁决）",
            file=sys.stderr,
        )
        return 1
    return 0


def _create_invite_command(config: HostConfig, args: argparse.Namespace) -> int:
    """Operator onboarding: mint one invitation for a credentialed tenant.

    Runs offline with the same exclusive data-root ownership as the host (so it
    cannot race a live host) and never creates a tenant credential, never
    contacts the network, and never sends mail. The invitation code is printed
    exactly once so the operator can deliver it through a channel they trust.
    """
    if not config.accounts_enabled:
        print(
            "配置错误 / configuration error: 需要 PI_GLINK_TENANCY=multitenant 且 "
            "PI_GLINK_AUTH_MODE=accounts 才能创建站内邀请",
            file=sys.stderr,
        )
        return 2
    # The data root comes first for the same reason as the host: opening the
    # account database creates/migrates it, which must never race a live host.
    try:
        ownership = DataRootOwnership.acquire(config.data_root)
    except DataRootBusy as exc:
        print(f"{exc}；请先停止宿主再创建邀请", file=sys.stderr)
        return 2
    tenancy: Tenancy | None = None
    try:
        try:
            tenancy = build_tenancy(config)
        except ConfigError as exc:
            print(f"配置错误 / configuration error: {exc}", file=sys.stderr)
            return 2
        if tenancy is None or tenancy.accounts is None:
            print("配置错误 / configuration error: 账号库不可用", file=sys.stderr)
            return 2
        tenant = (args.tenant or "").strip()
        if config.shared_model_credentials:
            # The explicit SaaS policy: the invite only needs the frozen host
            # model configuration. No tenant entry and no per-tenant key is
            # consulted (the tenant registry is created on first invitation).
            if not (config.openai_api_key and config.openai_base_url and config.openai_model):
                print(
                    "配置错误 / configuration error: PI_GLINK_MODEL_CREDENTIALS=shared "
                    "需要宿主冻结 OPENAI_API_KEY、OPENAI_BASE_URL、OPENAI_MODEL",
                    file=sys.stderr,
                )
                return 2
        else:
            if tenancy.credentials is None:
                # Defensive: the ``tenant`` policy always loads the store; a
                # missing one is a configuration error, never a fallback.
                print("配置错误 / configuration error: 租户凭据配置不可用", file=sys.stderr)
                return 2
            try:
                tenancy.credentials.require(tenant)
            except TenantNotConfigured as exc:
                print(f"拒绝创建邀请: {exc}", file=sys.stderr)
                return 2
        existing = tenancy.accounts.get_tenant(tenant)
        display = (args.tenant_name or "").strip() or (existing.name if existing else tenant)
        tenancy.accounts.upsert_tenant(tenant, display)
        ttl = args.invite_ttl_seconds or config.invite_ttl_seconds
        try:
            record, code = tenancy.accounts.create_invitation(
                tenant_id=tenant,
                email=args.email,
                role=args.role,
                created_by="operator-cli",
                ttl_seconds=ttl,
            )
        except AccountsError as exc:
            print(f"拒绝创建邀请: {exc.message}", file=sys.stderr)
            return 2
        register_path = f"/register#invite={code}"
        print("邀请已创建（仅本次显示，请通过安全渠道传递；不要在普通日志或工单里留存）：")
        print(f"  组织: {display} ({record.tenant_id})")
        print(f"  邮箱: {record.email}")
        print(f"  角色: {record.role}")
        print(f"  有效期至: {record.expires_at:.0f}（约 {ttl // 3600} 小时）")
        if args.name:
            print(f"  受邀人: {args.name}")
        print(f"  注册入口: {register_path}")
        if config.public_origin:
            print(f"  完整链接: {config.public_origin}{register_path}")
        return 0
    finally:
        if tenancy is not None:
            tenancy.close()
        ownership.release()


def _resource_command(config: HostConfig, run_id: str | None, note: str | None) -> int:
    """Operator view and adjudication of unconfirmed execution resources.

    Runs with the same exclusive data-root ownership as the host, so it cannot
    race a live host; it never contacts Docker/E2B or removes anything.
    """
    try:
        ownership = DataRootOwnership.acquire(config.data_root)
    except DataRootBusy as exc:
        print(f"{exc}；请先停止宿主再执行资源裁决", file=sys.stderr)
        return 2
    try:
        manager = RunManager(
            config, EventBus(), RunStore(config.data_root / "runs"), ownership=ownership
        )
        manager.reconcile()
        if run_id:
            try:
                manager.adjudicate_release(run_id, note or "")
            except (KeyError, ValueError, RunBusy) as exc:
                print(f"裁决被拒绝: {exc}", file=sys.stderr)
                return 1
            print(f"已记录人工裁决：run {run_id} 的执行资源已释放")
        print(json.dumps(manager.unreleased_resources(), ensure_ascii=False, indent=2))
        return 0
    finally:
        ownership.release()


if __name__ == "__main__":
    raise SystemExit(main())
