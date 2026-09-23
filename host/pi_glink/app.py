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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from .artifacts import ArtifactError, ArtifactTree
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
)

logger = logging.getLogger("pi_glink.host")

_RUN_EVENTS_RE = re.compile(r"^/api/runs/(?P<run_id>[A-Za-z0-9_-]{1,64})/events$")
_RUN_STOP_RE = re.compile(r"^/api/runs/(?P<run_id>[A-Za-z0-9_-]{1,64})/stop$")
_RUN_REPORT_RE = re.compile(r"^/api/runs/(?P<run_id>[A-Za-z0-9_-]{1,64})/report$")
_RUN_RE = re.compile(r"^/api/runs/(?P<run_id>[A-Za-z0-9_-]{1,64})$")
_MAX_REQUEST_BYTES = 64 * 1024
_HEARTBEAT_SECONDS = 15.0


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
    ) -> None:
        self.config = config
        self.bus = bus
        self.store = store
        self.manager = manager
        self.assets = assets
        self.ownership = ownership
        #: Frozen multi-tenant trust material; ``None`` in local mode.
        self.tenancy = tenancy
        self.stopping = threading.Event()
        super().__init__((config.host, config.port), HostRequestHandler)

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
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
        try:
            # Multi-tenant POSTs carry the gateway-injected identity, and a
            # browser can be made to send one cross-site; refuse requests whose
            # own origin metadata says they came from another site.
            if self.server.tenancy is not None and not self._request_origin_allowed():
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {"error": "forbidden", "message": "cross-site request refused"},
                    head_only=False,
                )
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
            logger.exception("unhandled error serving POST %s", self.path)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"}, head_only=False
            )

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
            logger.exception("unhandled error serving %s", self.path)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal_error"},
                head_only=head_only,
            )

    def _handle_api(self, path: str, query: str, *, head_only: bool) -> None:
        # The health probe stays reachable without an identity (a gateway or
        # process supervisor may poll it), but in multi-tenant mode it never
        # reports another tenant's active run. Every other /api route requires a
        # verified assertion before it reads or reveals anything.
        if path == "/api/health":
            self._send_json(HTTPStatus.OK, self._health_payload(), head_only=head_only)
            return
        context = self._authenticate()
        if self.server.tenancy is not None and context is None:
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                {"error": "unauthenticated"},
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
        """Verify the gateway assertion, or ``None`` in the local mode.

        The assertion is read from exactly one header value; duplicate headers
        are refused instead of choosing one. Nothing from the body, the query
        string or any other header can label the request.
        """
        tenancy = self.server.tenancy
        if tenancy is None:
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

    def _request_origin_allowed(self) -> bool:
        """Refuse a browser request whose own metadata says it is cross-site.

        A non-browser client (the gateway, curl, a smoke script) sends none of
        these headers, and cannot be tricked by a website into carrying the
        gateway's identity header. Any header that is present must agree that
        the request came from this origin.
        """
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if origin is not None and not _same_origin(origin, host):
            return False
        referer = self.headers.get("Referer")
        if referer is not None and not _same_origin(referer, host):
            return False
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in {"same-origin", "none"}:
            return False
        return True

    def _public_config(self, context: TenantContext | None) -> dict[str, Any]:
        """Public configuration for the requesting identity.

        ``configured.model`` answers "can this caller start a run": in local
        mode that is the host's key, in multi-tenant mode the caller's own
        tenant credential — never whether another tenant has one.
        """
        view = dict(self.server.config.public_view())
        if context is not None and self.server.tenancy is not None:
            tenant = self.server.tenancy.credentials.get(context.tenant_id)
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
                HTTPStatus.UNAUTHORIZED, {"error": "unauthenticated"}, head_only=False
            )
            return
        body = self._read_json_body()
        if body is None:
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
                HTTPStatus.UNAUTHORIZED, {"error": "unauthenticated"}, head_only=False
            )
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
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_REQUEST_BYTES:
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
        self, status: HTTPStatus, payload: dict[str, Any], *, head_only: bool
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("%s - %s", self.address_string(), format % args)

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.warning("%s - %s", self.address_string(), format % args)


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
    """True when a verified identity has passed its expiration.

    Local mode (``context is None``) carries no assertion, so nothing expires.
    """
    return context is not None and context.expired()


def _same_origin(url: str, host: str) -> bool:
    """True when a browser-supplied Origin/Referer names this exact host.

    Parsed with :func:`urlsplit`; anything unparseable, a non-HTTP scheme or a
    mismatched netloc is refused. ``null`` origins (sandboxed iframes, file://)
    are refused as well.
    """
    if not host:
        return False
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        return False
    return parts.netloc.lower() == host.lower()


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
    config: HostConfig, *, client_factory: ClientFactory | None = None
) -> PiGlinkServer:
    """Build a ready-to-serve host (used by tests and by ``main``).

    ``client_factory`` exists for tests: production always talks to the real
    ``pi`` binary through :class:`~pi_glink.kernel.rpc.PiRpcClient`.
    """
    # Multi-tenant material is loaded before anything is bound or written: a
    # missing or over-permissive key file refuses to start instead of falling
    # back to the unauthenticated local surface.
    tenancy = build_tenancy(config)
    # Ownership comes first: only a process that holds the data root may
    # conclude that a ``running`` record has no live owner. A second host on
    # the same data root (same or another port) stops here, before touching
    # any record.
    ownership = DataRootOwnership.acquire(config.data_root)
    try:
        bus = EventBus()
        store = RunStore(config.data_root / "runs")
        manager = RunManager(
            config,
            bus,
            store,
            client_factory=client_factory or PiRpcClient,
            ownership=ownership,
            credential_store=tenancy.credentials if tenancy is not None else None,
        )
        # Runs left ``running`` by a previous process cannot continue; close
        # them out honestly before the host serves history. External leftovers
        # are not touched — their identity is recorded for an operator decision.
        manager.reconcile()
        return PiGlinkServer(
            config,
            bus,
            store,
            manager,
            WebAssets(config.web_root),
            ownership=ownership,
            tenancy=tenancy,
        )
    except BaseException:
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
    args = parser.parse_args(argv)
    if args.mark_released and not (args.note or "").strip():
        parser.error("--mark-released 需要 --note 说明如何核对了资源释放")

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
            build_tenancy(config)
        except ConfigError as exc:
            print(f"配置错误 / configuration error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(config.public_view(), ensure_ascii=False, indent=2))
        return 0
    if args.unreleased or args.mark_released:
        return _resource_command(config, args.mark_released, args.note)

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
