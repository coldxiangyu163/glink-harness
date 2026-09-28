"""Product event vocabulary and the in-memory run event bus.

The browser never sees terminal text: every frame it receives is a structured
event built here. Two families exist:

* run lifecycle — owned by the host (``run_started`` … ``run_finished``);
* pi session projection — owned by the kernel adapter, which translates the
  documented pi headless events (``message_update``, ``tool_execution_*`` …)
  into the transport-normalised names below.

Frames on the wire reuse the SSE shape the existing workbench front-end
already consumes::

    id: <seq>
    event: <type>
    data: {"type": …, "v": …, "runId": …, "seq": …, "ts": …, "payload": {…}}

**Schema.** The payload of every type and its version ``v`` are declared once in
:mod:`pi_glink.event_schema`; ``commit`` validates each frame against it before
anything is persisted or shown, and the front-end constants are generated from
the same declaration (``web/modules/event-schema.gen.mjs``).

``seq`` is per run and monotonic, so a reconnecting client can resume with
``Last-Event-ID`` without replaying or skipping work.

**Commit before publish.** ``commit`` assigns the sequence number, runs the
caller's persist callback, and only then stores the frame in the ring and hands
it to live subscribers. A frame that could not be persisted therefore exists
nowhere: the browser cannot show a frame that is missing from
the store's ``events`` table, and no sequence number is burned for it. The durable log is
the replay source of truth; the ring is only the window between "the reader
reached the end of the table" and "the reader is attached to the live tail".
Commits of one run are serialized by a per-run lock; the persist callback (which
may wait for a disk sync) runs outside the bus-wide lock, so queries such as
``is_open``/``last_seq`` and new subscriptions never wait on disk and only ever
reflect frames whose persist already returned.

**Bounded subscribers.** A subscriber queue has a firm capacity. A client that
stops reading is disconnected (the stream ends, the browser reconnects with
``Last-Event-ID`` and replays from the log) instead of growing an unbounded
queue. The per-run ring is dropped when the run closes; run counters are a few
bytes per run and are the last thing to go.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping

from . import event_schema

logger = logging.getLogger("pi_glink.events")

# Run lifecycle (host-owned)
RUN_STARTED = "run_started"
RUN_STAGE = "run_stage"
RUN_FINISHED = "run_finished"
RUN_FAILED = "run_failed"
RUN_CANCELLED = "run_cancelled"

# pi session projection (kernel-owned)
SESSION_READY = "session_ready"
TURN_STARTED = "turn_started"
TURN_FINISHED = "turn_finished"
ASSISTANT_DELTA = "assistant_delta"
ASSISTANT_MESSAGE = "assistant_message"
THINKING_DELTA = "thinking_delta"
TOOL_STARTED = "tool_started"
TOOL_UPDATED = "tool_updated"
TOOL_FINISHED = "tool_finished"
# Typed subagent projection (kernel-owned): a dispatch through the ``subagent``
# tool is projected as these typed events instead of generic tool cards; the
# child run's authoritative record is runs/<id>/children/<n>/child.json and the
# terminal frame's ``children`` summary (docs/architecture.md §3.5).
SUBAGENT_STARTED = "subagent_started"
SUBAGENT_FINISHED = "subagent_finished"
# Running progress for a child run (R3a, docs/architecture.md §3.5
# 「运行中进度（R3a）」): a throttled, latest-value-wins transient fact — never
# part of any terminal verdict, never written to child.json.
SUBAGENT_UPDATED = "subagent_updated"
USAGE_REPORTED = "usage_reported"
KERNEL_STATUS = "kernel_status"

TERMINAL_EVENT_TYPES = frozenset({RUN_FINISHED, RUN_FAILED, RUN_CANCELLED})

#: Every product event type; must equal the schema's vocabulary.
EVENT_TYPES = frozenset(
    {
        RUN_STARTED, RUN_STAGE, RUN_FINISHED, RUN_FAILED, RUN_CANCELLED,
        SESSION_READY, TURN_STARTED, TURN_FINISHED, ASSISTANT_DELTA,
        ASSISTANT_MESSAGE, THINKING_DELTA, TOOL_STARTED, TOOL_UPDATED,
        TOOL_FINISHED, SUBAGENT_STARTED, SUBAGENT_FINISHED, SUBAGENT_UPDATED,
        USAGE_REPORTED, KERNEL_STATUS,
    }
)
if EVENT_TYPES != set(event_schema.BY_TYPE) or TERMINAL_EVENT_TYPES != event_schema.TERMINAL_TYPES:
    raise RuntimeError("events.py 的事件词表与 event_schema.SPECS 不一致")

#: Upper bound on frames a single run keeps for the replay-to-live handover.
_RING_CAPACITY = 4000
#: Upper bound on frames queued for one live subscriber before it is dropped.
#: The client then reconnects and replays from the durable log, so the bound
#: never loses committed history — it only ends a connection that stopped
#: reading.
_SUBSCRIBER_CAPACITY = 2000

PersistCallback = Callable[["RunEvent"], None]

#: Sentinel a subscriber can ask for while it waits for the next frame. It is
#: never a :class:`RunEvent`: callers use it to regain control (deadline and
#: expiry checks, heartbeats) without polling the durable log.
IDLE_POLL = object()


@dataclass(frozen=True, slots=True)
class RunEvent:
    """One frame in the run event stream."""

    type: str
    run_id: str
    seq: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    #: Payload schema version of ``type`` (:data:`event_schema.EVENT_VERSIONS`).
    v: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "v": self.v,
            "runId": self.run_id,
            "seq": self.seq,
            "ts": self.ts,
            "payload": dict(self.payload),
        }

    def to_sse(self) -> str:
        body = json.dumps(self.as_dict(), ensure_ascii=False)
        return f"id: {self.seq}\nevent: {self.type}\ndata: {body}\n\n"


class _Subscriber:
    """One live subscriber with a firm queue bound."""

    __slots__ = ("overflowed", "queue")

    def __init__(self, capacity: int) -> None:
        self.queue: queue.Queue[RunEvent | None] = queue.Queue(maxsize=capacity)
        self.overflowed = False

    def offer(self, item: RunEvent | None) -> None:
        if self.overflowed:
            return
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            # The consumer stopped reading. Mark it so the generator ends the
            # stream; dropping frames silently would fake continuity.
            self.overflowed = True


class EventBus:
    """Per-run fan-out with a bounded replay window and a bounded live tail.

    ``commit`` is the only writer used by the runner: it persists first, then
    publishes. ``publish`` remains for callers that own no durable log (tests,
    host-only frames) and is exactly ``commit`` without a persist callback.
    """

    def __init__(self, capacity: int = _RING_CAPACITY) -> None:
        self._capacity = capacity
        self._lock = threading.RLock()
        self._counters: dict[str, int] = {}
        self._history: dict[str, list[RunEvent]] = {}
        self._subscribers: dict[str, set[_Subscriber]] = {}
        self._closed: set[str] = set()
        self._commit_locks: dict[str, threading.Lock] = {}

    # -- writing --------------------------------------------------------------

    def commit(
        self,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        persist: PersistCallback | None = None,
    ) -> RunEvent:
        """Assign ``seq``, persist, then fan out.

        The run's commit lock keeps sequence assignment, the durable append and
        the live notification in one order per run. ``persist`` runs outside the
        bus-wide lock: until it returns, the frame is not counted, not in the
        ring and not offered to anyone. If it raises, the sequence number is not
        consumed and nothing is published: the frame is invisible everywhere
        rather than present in only one surface.

        The payload is validated against :mod:`pi_glink.event_schema` first;
        in strict mode a violation raises :class:`EventSchemaError` before any
        sequence number is assigned.
        """
        body = dict(payload or {})
        event_schema.check(event_type, body, source=f"bus run={run_id}")
        with self._lock:
            commit_lock = self._commit_locks.setdefault(run_id, threading.Lock())
        with commit_lock:
            with self._lock:
                seq = self._counters.get(run_id, 0) + 1
            event = RunEvent(
                type=event_type,
                run_id=run_id,
                seq=seq,
                payload=body,
                v=event_schema.EVENT_VERSIONS.get(event_type, 0),
            )
            if persist is not None:
                persist(event)
            with self._lock:
                self._counters[run_id] = seq
                history = self._history.setdefault(run_id, [])
                history.append(event)
                if len(history) > self._capacity:
                    del history[: len(history) - self._capacity]
                subscribers = tuple(self._subscribers.get(run_id, ()))
            for subscriber in subscribers:
                subscriber.offer(event)
        return event

    def publish(
        self, run_id: str, event_type: str, payload: Mapping[str, Any] | None = None
    ) -> RunEvent:
        """Publish without a durable log (host-only or test frames)."""
        return self.commit(run_id, event_type, payload)

    def close(self, run_id: str) -> None:
        """Mark end-of-stream, wake live subscribers and drop the ring.

        The closed flag also ends later subscriptions: a client that connects
        after the run finished replays from the store's ``events`` table instead of waiting
        on a queue nobody will write to again. The ring exists only for the
        replay→live handover, so it is released here; the durable log is not
        touched.
        """
        with self._lock:
            self._closed.add(run_id)
            subscribers = tuple(self._subscribers.get(run_id, ()))
            self._history.pop(run_id, None)
        for subscriber in subscribers:
            subscriber.offer(None)

    def subscribe(
        self, run_id: str, after: int = 0, *, idle_poll: object | None = None
    ) -> Iterator[RunEvent | object]:
        """Yield frames with ``seq > after``, then any frame published later.

        Registration happens when this method is called, not when iteration
        starts: a frame published between ``subscribe`` and the first ``next``
        is queued rather than racing the caller. The generator ends when the run
        closes, when the subscriber falls too far behind
        (:attr:`_Subscriber.overflowed`), or when the caller stops iterating.
        Callers that need the whole history read the durable log first and pass
        the last persisted sequence as ``after``.

        ``idle_poll`` (default: nothing) is yielded whenever the bounded wait
        for the next frame times out, letting a long-lived consumer (the SSE
        endpoint) check its own deadlines — for example an identity assertion
        expiring mid-stream — without busy-waiting or reading the log again.
        """
        with self._lock:
            pending = [
                event for event in self._history.get(run_id, ()) if event.seq > after
            ]
            subscriber: _Subscriber | None = None
            if run_id in self._counters and run_id not in self._closed:
                subscriber = _Subscriber(_SUBSCRIBER_CAPACITY)
                self._subscribers.setdefault(run_id, set()).add(subscriber)

        def _iterate() -> Iterator[RunEvent | object]:
            try:
                yield from pending
                if subscriber is None:
                    return
                while True:
                    if subscriber.overflowed:
                        logger.warning(
                            "subscriber for run=%s fell behind and was disconnected",
                            run_id,
                        )
                        return
                    try:
                        item = subscriber.queue.get(timeout=0.2)
                    except queue.Empty:
                        if idle_poll is not None:
                            yield idle_poll
                        continue
                    if item is None:
                        return
                    yield item
            finally:
                if subscriber is not None:
                    with self._lock:
                        listeners = self._subscribers.get(run_id)
                        if listeners is not None:
                            listeners.discard(subscriber)
                            if not listeners:
                                self._subscribers.pop(run_id, None)

        return _iterate()

    # -- queries --------------------------------------------------------------

    def last_seq(self, run_id: str) -> int:
        with self._lock:
            return self._counters.get(run_id, 0)

    def open_run(self, run_id: str) -> None:
        """Register a run so subscribers can attach before its first frame."""
        with self._lock:
            self._counters.setdefault(run_id, 0)
            self._history.setdefault(run_id, [])
            self._closed.discard(run_id)

    def is_open(self, run_id: str) -> bool:
        """True only while this process still owns the run's live stream."""
        with self._lock:
            return run_id in self._counters and run_id not in self._closed

    def forget(self, run_id: str) -> None:
        """Drop a finished run's counters, history and closed marker."""
        with self._lock:
            self._counters.pop(run_id, None)
            self._history.pop(run_id, None)
            self._closed.discard(run_id)
            self._commit_locks.pop(run_id, None)
