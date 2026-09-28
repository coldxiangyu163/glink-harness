"""Server-sent event streaming and its connection budget.

One run's stream is a chunked HTTP/1.1 response: the committed log is replayed
first, then the live bus tail follows, merged by ``seq``. :class:`StreamLimits`
bounds how many such connections the host — and, in multi-tenant mode, one
tenant — may hold open at once, so a client cannot turn the bus fan-out into an
unbounded number of threads and buffers. A refusal is answered *before* any
stream header is written, as a plain JSON error.
"""

from __future__ import annotations

import json
import logging
import threading
from http import HTTPStatus
from typing import Any, Mapping
from urllib.parse import parse_qs

from .events import IDLE_POLL
from .tenancy import TenantContext
from .web_security import assertion_expired

logger = logging.getLogger("pi_glink.host")


def persisted_sse(event: Mapping[str, Any]) -> str:
    """SSE frame for an event read back from events.jsonl (same shape as live frames)."""
    body = json.dumps(event, ensure_ascii=False)
    return f"id: {event.get('seq', '')}\nevent: {event['type']}\ndata: {body}\n\n"


def resume_offset(last_event_id: str | None, query: str) -> int:
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


class StreamLimits:
    """Bounded concurrent event streams: one global ceiling, one per tenant.

    Local mode carries no tenant key and therefore only uses the global ceiling.
    The counter is reserved before the run is even looked up and released by the
    stream's own ``finally``, so a normal end, a client disconnect, an expired
    identity and a slow-consumer disconnect all return their slot.
    """

    def __init__(self, *, global_limit: int, tenant_limit: int) -> None:
        if global_limit < 1 or tenant_limit < 1:
            raise ValueError("stream limits must be at least 1")
        self.global_limit = global_limit
        self.tenant_limit = tenant_limit
        self._lock = threading.Lock()
        self._global = 0
        self._tenants: dict[str, int] = {}

    def acquire(self, tenant_id: str | None) -> str | None:
        """Reserve one slot; returns the scope that refused, or ``None`` on success.

        The global ceiling is checked first: it bounds the whole host, so a
        tenant that is under its own ceiling can still be refused by the host
        total and the answer must name that ceiling.
        """
        with self._lock:
            if self._global >= self.global_limit:
                return "global"
            if tenant_id is not None and self._tenants.get(tenant_id, 0) >= self.tenant_limit:
                return "tenant"
            self._global += 1
            if tenant_id is not None:
                self._tenants[tenant_id] = self._tenants.get(tenant_id, 0) + 1
            return None

    def release(self, tenant_id: str | None) -> None:
        with self._lock:
            self._global = max(0, self._global - 1)
            if tenant_id is not None:
                remaining = self._tenants.get(tenant_id, 0) - 1
                if remaining > 0:
                    self._tenants[tenant_id] = remaining
                else:
                    self._tenants.pop(tenant_id, None)

    def limit_for(self, scope: str) -> int:
        return self.global_limit if scope == "global" else self.tenant_limit

    def active(self) -> tuple[int, dict[str, int]]:
        """Current counts, for diagnostics and regressions (never sent to clients)."""
        with self._lock:
            return self._global, dict(self._tenants)


class SseMixin:
    """The run event stream: authorization first, replay, then the live tail."""

    def _stream_run_events(
        self, run_id: str, query: str, context: TenantContext | None
    ) -> None:
        bus = self.server.bus
        store = self.server.store
        tenant_key = context.tenant_id if context is not None else None
        refused = self.server.streams.acquire(tenant_key)
        if refused is not None:
            # Checked before the run is looked up, so a refusal reveals neither
            # the existence of a run nor which tenant holds the connections.
            limit = self.server.streams.limit_for(refused)
            logger.info(
                "event stream refused run=%s scope=%s limit=%s", run_id, refused, limit
            )
            self._send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {
                    "error": "stream_limit",
                    "scope": refused,
                    "limit": limit,
                    "message": "stream connection limit reached; retry later",
                },
                head_only=False,
                extra_headers={"Retry-After": "5"},
            )
            return
        try:
            self._stream_run_events_authorized(run_id, query, context, bus, store)
        finally:
            self.server.streams.release(tenant_key)

    def _stream_run_events_authorized(
        self, run_id: str, query: str, context: TenantContext | None, bus: Any, store: Any
    ) -> None:
        # Authorization happens before a single response header is written. An
        # unknown run, another tenant's run and an ownerless (single-user) record all
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
        after = resume_offset(self.headers.get("Last-Event-ID"), query)
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
                if assertion_expired(context):
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
                self._write_event(persisted_sse(event))
            if subscription is not None:
                while True:
                    # A short-lived assertion must not outlive itself on a long
                    # stream: once it expires the stream is closed cleanly and
                    # the client must re-authenticate (and reconnect).
                    if assertion_expired(context):
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
