"""Credential egress proxy: the real model/search keys never enter a run.

Decision D4 (docs/adr/0008-egress-proxy.md, docs/multitenancy-plan.md §8).
The host process runs one small HTTP forwarder on its **own loopback
listener** (separate from the web port). A run receives only

* the proxy base URL as its execution face sees it (in-process: loopback;
  Docker: the host gateway or the configured public URL; E2B: the configured
  public URL — without one the run is refused, never given the real key), and
* one short-lived, high-entropy **token** bound to ``run_id`` + tenant.

The token is rendered into pi's own ``auth.json`` / ``web-search.json`` in the
place a real key used to be. The proxy checks it, replaces it with the frozen
upstream credential and forwards to the host-frozen upstream
(``OPENAI_BASE_URL`` for ``/model/…``, ``GLINK_WEB_SEARCH_UPSTREAM`` for
``/search/…``). Tokens live only in memory: a host restart invalidates every
one of them, and the runner revokes a run's token when its verdict commit is
attempted (the kernel is already gone by then).

The proxy is also the run's **usage meter**: it reads the upstream's own
``usage`` object (OpenAI-compatible, streamed or not) from every model
response and keeps a per-run total. That total is the authoritative usage the
terminal frame and the run record carry for the pi family (the kernel's own
reports stay a separate, non-authoritative field). One per-tenant quota hook
limits requests and/or tokens per run; there is no general policy framework.

The listener only exists while at least one grant is live: it binds on the
first :meth:`EgressProxy.issue` and stops when the last grant is revoked.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping

from .kernel.archive_channel import (
    ARCHIVE_KIND_STATE,
    ARCHIVE_READ_TIMEOUT_SECONDS,
    ArchiveChannel,
    ArchiveGrant,
    read_state_manifest,
)

logger = logging.getLogger("glink_harness.egress")

#: Route prefixes (the kernel-view base URLs are ``<origin>/model`` and
#: ``<origin>/search``).
MODEL_ROUTE = "model"
SEARCH_ROUTE = "search"
# pi 0.86.1 openai-completions and pi-web-access 0.31.0 keyed Exa calls.
# Recheck these exact consumer paths when upgrading the pinned packages.
_ALLOWED_PATHS = {
    "/model/chat/completions": (MODEL_ROUTE, "chat/completions"),
    "/search/search": (SEARCH_ROUTE, "search"),
    "/search/answer": (SEARCH_ROUTE, "answer"),
}
#: The archive routes (``/archive/state``) served for the ACP sandbox face
#: (docs/adr/0018). Same listener, separate capability (``pga_`` tokens).
ARCHIVE_ROUTE = "archive"
#: Request body ceiling; model requests carry the whole context.
MAX_REQUEST_BYTES = 32 * 1024 * 1024
#: How much of a non-streamed JSON response is kept for usage parsing.
MAX_METER_BYTES = 8 * 1024 * 1024
#: Longest single SSE line kept for usage parsing (longer lines are skipped).
MAX_SSE_LINE_BYTES = 1024 * 1024
#: Upstream socket timeout (seconds between bytes, not the whole response).
UPSTREAM_TIMEOUT_SECONDS = 600
#: Headers never copied between the kernel and the upstream.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "accept-encoding",
    "authorization",
    "x-api-key",
    "cookie",
    # An upstream must not reinterpret our authorized method/path via headers.
    "x-http-method-override",
    "x-method-override",
    "x-original-url",
    "x-rewrite-url",
}
_RESPONSE_DROP = _HOP_BY_HOP | {"set-cookie", "content-encoding"}


@dataclass(frozen=True, slots=True)
class EgressQuota:
    """Per-run ceilings from the tenant quota hook; ``0`` means unlimited.

    ``max_requests`` counts every forwarded request (model and search);
    ``max_tokens`` counts the upstream-reported ``total_tokens`` of model
    responses. A request is refused (429) once a ceiling is reached — the
    request that crosses the token ceiling still completes, because its usage
    is only known after the upstream answered.
    """

    max_requests: int = 0
    max_tokens: int = 0


@dataclass
class EgressGrant:
    """One run's capability: the token plus what it unlocks. Memory only."""

    token: str = field(repr=False)
    run_id: str
    tenant: str | None
    model_id: str
    model_key: str = field(repr=False, default="")
    search_key: str = field(repr=False, default="")
    quota: EgressQuota = field(default_factory=EgressQuota)
    requests: int = 0
    search_requests: int = 0
    input: int = 0
    output: int = 0
    cache_read: int = 0
    total_tokens: int = 0
    #: Model responses that carried no parsable ``usage`` object.
    unmetered: int = 0
    #: Requests refused by the quota hook.
    refused: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def has_search(self) -> bool:
        return bool(self.search_key)

    def usage(self) -> dict[str, Any]:
        """The metered run total, in the product's usage field names."""
        with self.lock:
            return {
                "source": "egress",
                "requests": self.requests,
                "searchRequests": self.search_requests,
                "input": self.input,
                "output": self.output,
                "cacheRead": self.cache_read,
                "totalTokens": self.total_tokens,
                "unmetered": self.unmetered,
                "refused": self.refused,
            }


class EgressProxy:
    """The host's credential forwarder (one per :class:`RunManager`)."""

    def __init__(
        self,
        *,
        model_upstream: str,
        search_upstream: str,
        host: str = "127.0.0.1",
        port: int = 0,
        quota_for: Callable[[str | None], EgressQuota] | None = None,
    ) -> None:
        self.model_upstream = model_upstream.rstrip("/")
        self.search_upstream = search_upstream.rstrip("/")
        self.host = host
        self._configured_port = port
        self._quota_for = quota_for or (lambda _tenant: EgressQuota())
        #: The archive channel rides the same listener but keeps its own
        #: grant table, so an egress token can never move state and an
        #: archive token can never reach an upstream (docs/adr/0018).
        self.archive = ArchiveChannel()
        #: sha256(token) -> grant. Looking a grant up by the token's digest
        #: keeps the comparison off the raw secret.
        self._grants: dict[str, EgressGrant] = {}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @classmethod
    def from_config(cls, config: Any) -> "EgressProxy":
        max_requests = int(getattr(config, "egress_max_requests_per_run", 0) or 0)
        max_tokens = int(getattr(config, "egress_max_tokens_per_run", 0) or 0)
        quota = EgressQuota(max_requests=max_requests, max_tokens=max_tokens)
        return cls(
            model_upstream=config.openai_base_url,
            search_upstream=config.web_search_upstream,
            host=config.egress_host,
            port=config.egress_port,
            quota_for=lambda _tenant: quota,
        )

    # -- grants ----------------------------------------------------------------

    def issue(
        self,
        *,
        run_id: str,
        tenant: str | None,
        model_key: str,
        model_id: str,
        search_key: str = "",
    ) -> EgressGrant:
        """Mint a run's token (binding the listener if it is not up yet)."""
        grant = EgressGrant(
            token="pge_" + secrets.token_urlsafe(32),
            run_id=run_id,
            tenant=tenant,
            model_key=model_key,
            model_id=model_id,
            search_key=search_key,
            quota=self._quota_for(tenant),
        )
        with self._lock:
            self._ensure_listening()
            self._grants[_digest(grant.token)] = grant
        return grant

    def revoke(self, token: str) -> dict[str, Any] | None:
        """Invalidate ``token``; returns its final usage (``None`` if unknown).

        The listener stops once no grant is live, so an idle host has no open
        egress port. The stop is synchronous and happens under the lock: the
        listening socket is closed before a concurrent :meth:`issue` can bind
        again, which a fixed ``port`` (required with a public URL) depends on.
        """
        with self._lock:
            grant = self._grants.pop(_digest(token), None)
            if not self._grants and not self.archive.live_grants:
                self._stop_listening()
        return grant.usage() if grant is not None else None

    def grant_for(self, token: str) -> EgressGrant | None:
        with self._lock:
            return self._grants.get(_digest(token))

    # -- archive grants ---------------------------------------------------------

    def issue_archive(
        self,
        *,
        run_id: str,
        conversation_dir: Any,
        credential_dests: tuple[str, ...] = (),
    ) -> ArchiveGrant:
        """Mint one run's archive token (binding the shared listener).

        The archive capability is scoped to exactly one conversation's state
        directory and this run's publish identity; it carries no upstream
        credential and never grants the model/search routes.
        """
        grant = self.archive.issue(
            run_id=run_id,
            conversation_dir=conversation_dir,
            credential_dests=credential_dests,
        )
        with self._lock:
            self._ensure_listening()
        return grant

    def revoke_archive(self, token: str) -> ArchiveGrant | None:
        """Invalidate one archive token; the listener follows the shared rule."""
        grant = self.archive.revoke(token)
        if grant is not None:
            with self._lock:
                if not self._grants and not self.archive.live_grants:
                    self._stop_listening()
        return grant

    @property
    def port(self) -> int | None:
        with self._lock:
            return self._server.server_address[1] if self._server is not None else None

    def close(self) -> None:
        with self._lock:
            self._grants.clear()
            self.archive.close()
            self._stop_listening()

    def _ensure_listening(self) -> None:
        if self._server is not None:
            return
        server = _EgressServer((self.host, self._configured_port), _EgressHandler)
        server.proxy = self
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="glink-harness-egress",
            daemon=True,
        )
        thread.start()
        self._server, self._thread = server, thread
        logger.info("egress proxy listening on %s:%s", self.host, server.server_address[1])

    def _stop_listening(self) -> None:
        """Close the listener and wait until its socket is released (lock held).

        ``shutdown`` only waits for the ``serve_forever`` loop (at most one
        ``poll_interval``); request handlers run on their own daemon threads and
        never call back into this path, so holding the lock cannot deadlock.
        """
        server, thread = self._server, self._thread
        self._server, self._thread = None, None
        if server is None:
            return
        try:
            server.shutdown()
        finally:
            server.server_close()
        if thread is not None:
            thread.join()

    # -- forwarding --------------------------------------------------------------

    def upstream_for(self, route: str) -> str:
        return self.model_upstream if route == MODEL_ROUTE else self.search_upstream


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class _EgressServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64
    proxy: EgressProxy


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would carry the real credential elsewhere."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        return None


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _opener_for(url: str) -> urllib.request.OpenerDirector:
    handlers: list[Any] = [_NoRedirect()]
    if _is_loopback_host(urllib.parse.urlsplit(url).hostname):
        # A loopback upstream (a test double, a local gateway) is never sent
        # through the operator's HTTP(S)_PROXY.
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


class _EgressHandler(BaseHTTPRequestHandler):
    server: _EgressServer
    server_version = "glink-harness-egress"
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Paths may carry query strings; tokens never travel in them, but the
        # access log stays at debug level anyway.
        logger.debug("egress %s", format % args)

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def _refuse(self, status: int, code: str) -> None:
        body = json.dumps({"error": {"code": code, "message": code}}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _route(self) -> tuple[str, str] | None:
        # Exact wire path: no encoded aliases, suffixes, query or fragment.
        return _ALLOWED_PATHS.get(self.path) if self.command == "POST" else None

    def _token(self) -> str:
        authorization = self.headers.get("Authorization", "")
        if authorization[:7].lower() == "bearer ":
            return authorization[7:].strip()
        return (self.headers.get("x-api-key") or "").strip()

    def _handle(self) -> None:
        proxy = self.server.proxy
        if self.path.split("/", 3)[:2] == ["", ARCHIVE_ROUTE]:
            self._archive()
            return
        route = self._route()
        if route is None:
            self._refuse(404, "unknown_route")
            return
        name, suffix = route
        token = self._token()
        grant = proxy.grant_for(token) if token else None
        if grant is None:
            self._refuse(401, "invalid_token")
            return
        if name == SEARCH_ROUTE and not grant.has_search:
            self._refuse(403, "search_not_granted")
            return
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self._refuse(411, "length_required")
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._refuse(400, "bad_length")
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._refuse(413, "request_too_large")
            return
        body = self.rfile.read(length) if length else b""
        if name == MODEL_ROUTE:
            types = self.headers.get_all("Content-Type") or []
            if (len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json"
                    or not _matches_model(body, grant.model_id)):
                self._refuse(403, "model_not_granted")
                return
        with grant.lock:
            quota = grant.quota
            used = grant.requests + grant.search_requests
            if (quota.max_requests and used >= quota.max_requests) or (
                quota.max_tokens and grant.total_tokens >= quota.max_tokens
            ):
                grant.refused += 1
                refused = True
            else:
                refused = False
                if name == MODEL_ROUTE:
                    grant.requests += 1
                else:
                    grant.search_requests += 1
        if refused:
            logger.warning("egress quota reached run=%s tenant=%s", grant.run_id, grant.tenant)
            self._refuse(429, "quota_exceeded")
            return
        self._forward(proxy, grant, name, suffix, body)

    def _archive(self) -> None:
        """One archive request: POST (reclaim) or GET (backfill) on /archive/state.

        The token is archive-scoped (``pga_``): an egress token is not accepted
        here and an archive token is not accepted on the model/search routes
        (those look a different table up and answer 401). Bodies stream to disk
        with a socket read timeout, and the response never echoes the token.
        """
        proxy = self.server.proxy
        split = urllib.parse.urlsplit(self.path)
        parts = split.path.split("/")
        if (
            len(parts) != 3
            or parts[0] != ""
            or parts[1] != ARCHIVE_ROUTE
            or parts[2] != ARCHIVE_KIND_STATE
            or split.query
            or "\\" in split.path
        ):
            self._refuse(404, "unknown_route")
            return
        token = self._token()
        grant = proxy.archive.grant_for(token) if token else None
        if grant is None:
            self._refuse(401, "invalid_token")
            return
        try:
            # A held-open socket must not pin the handler thread forever.
            self.connection.settimeout(ARCHIVE_READ_TIMEOUT_SECONDS)
        except OSError:  # pragma: no cover - defensive
            pass
        if self.command == "POST":
            if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                self._refuse(411, "length_required")
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._refuse(400, "bad_length")
                return
            if length <= 0:
                self._refuse(400, "bad_length")
                return
            try:
                facts = proxy.archive.receive(grant, self.rfile, length=length)
            except Exception as exc:  # noqa: BLE001 - one bounded error reply
                logger.warning(
                    "archive receive failed run=%s: %s", grant.run_id, type(exc).__name__
                )
                self._refuse(413 if "超过上限" in str(exc) else 400, "archive_rejected")
                return
            body = json.dumps({"status": "published", **facts}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.command == "GET":
            if read_state_manifest(grant.conversation_dir) is None:
                self._refuse(404, "state_unavailable")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-tar")
            self.end_headers()
            try:
                proxy.archive.serve(grant, self.wfile)
            except (OSError, ValueError) as exc:
                logger.info(
                    "archive serve ended early run=%s: %s", grant.run_id, type(exc).__name__
                )
            return
        self._refuse(405, "method_not_allowed")

    def _forward(
        self, proxy: EgressProxy, grant: EgressGrant, name: str, suffix: str, body: bytes
    ) -> None:
        url = f"{proxy.upstream_for(name)}/{suffix}"
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in _HOP_BY_HOP
        }
        headers["Accept-Encoding"] = "identity"
        if name == MODEL_ROUTE:
            headers["Authorization"] = f"Bearer {grant.model_key}"
        else:
            headers["x-api-key"] = grant.search_key
        request = urllib.request.Request(
            url, data=body if self.command == "POST" else None, headers=headers, method=self.command
        )
        try:
            response = _opener_for(url).open(request, timeout=UPSTREAM_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as exc:
            response = exc
        except (urllib.error.URLError, OSError) as exc:
            logger.warning(
                "egress upstream unreachable run=%s route=%s: %s", grant.run_id, name, type(exc).__name__
            )
            self._refuse(502, "upstream_unreachable")
            return
        meter = _UsageMeter() if name == MODEL_ROUTE else None
        status = int(getattr(response, "status", None) or response.getcode() or 502)
        try:
            self.send_response(status)
            content_type = ""
            for key, value in response.headers.items():
                if key.lower() in _RESPONSE_DROP:
                    continue
                if key.lower() == "content-type":
                    content_type = value
                self.send_header(key, value)
            self.end_headers()
            if meter is not None:
                meter.streaming = "text/event-stream" in content_type.lower()
            while True:
                chunk = response.read1(64 * 1024) if hasattr(response, "read1") else response.read(64 * 1024)
                if not chunk:
                    break
                if meter is not None:
                    meter.feed(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, ValueError) as exc:
            logger.info("egress stream ended early run=%s: %s", grant.run_id, type(exc).__name__)
        finally:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
            if meter is not None and 200 <= status < 300:
                usage = meter.result()
                with grant.lock:
                    if usage is None:
                        grant.unmetered += 1
                    else:
                        grant.input += usage.get("input", 0)
                        grant.output += usage.get("output", 0)
                        grant.cache_read += usage.get("cacheRead", 0)
                        grant.total_tokens += usage.get("totalTokens", 0)


def _matches_model(body: bytes, model_id: str) -> bool:
    """Reject ambiguous/non-UTF8 JSON before injecting the upstream key."""
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ValueError("non-JSON numeric constant")

    try:
        document = json.loads(body.decode("utf-8"), object_pairs_hook=pairs,
                              parse_constant=invalid_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    return (isinstance(document, dict) and isinstance(document.get("model"), str)
            and document["model"] == model_id and bool(model_id))


class _UsageMeter:
    """Pick the upstream's own ``usage`` object out of one model response.

    Streamed (SSE) responses are parsed line by line and the **last** chunk
    carrying ``usage`` wins (OpenAI-compatible streams send it once, at the end;
    some gateways repeat a growing total). A JSON body is parsed whole (bounded).
    """

    def __init__(self) -> None:
        self.streaming = False
        self._buffer = bytearray()
        self._line = bytearray()
        self._usage: Mapping[str, Any] | None = None
        self._overflow = False

    def feed(self, chunk: bytes) -> None:
        if not self.streaming:
            if len(self._buffer) + len(chunk) > MAX_METER_BYTES:
                self._overflow = True
                return
            self._buffer.extend(chunk)
            return
        for byte_line in chunk.splitlines(keepends=True):
            if len(self._line) + len(byte_line) <= MAX_SSE_LINE_BYTES:
                self._line.extend(byte_line)
            else:
                self._line.clear()
                self._line.extend(b"\x00")  # poison: skip this over-long line
            if byte_line.endswith((b"\n", b"\r")):
                self._consume_line(bytes(self._line))
                self._line.clear()

    def _consume_line(self, raw: bytes) -> None:
        line = raw.strip()
        if not line.startswith(b"data:") or b"\x00" in line:
            return
        data = line[5:].strip()
        if not data or data == b"[DONE]":
            return
        self._take(data)

    def _take(self, data: bytes) -> None:
        try:
            document = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return
        if isinstance(document, Mapping) and isinstance(document.get("usage"), Mapping):
            self._usage = document["usage"]

    def result(self) -> dict[str, int] | None:
        if self.streaming:
            if self._line:
                self._consume_line(bytes(self._line))
                self._line.clear()
        elif not self._overflow and self._buffer:
            self._take(bytes(self._buffer))
        return normalize_usage(self._usage) if self._usage is not None else None


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return 0
    return int(value)


def normalize_usage(usage: Mapping[str, Any]) -> dict[str, int]:
    """OpenAI-compatible ``usage`` → the product's usage field names."""
    prompt = _count(usage.get("prompt_tokens"))
    completion = _count(usage.get("completion_tokens"))
    total = _count(usage.get("total_tokens")) or prompt + completion
    details = usage.get("prompt_tokens_details")
    cached = _count(details.get("cached_tokens")) if isinstance(details, Mapping) else 0
    return {"input": prompt, "output": completion, "cacheRead": cached, "totalTokens": total}
