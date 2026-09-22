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
    data: {"runId": …, "seq": …, "ts": …, "payload": {…}}

``seq`` is per run and monotonic, so a reconnecting client can resume with
``Last-Event-ID`` without replaying or skipping work.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

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
USAGE_REPORTED = "usage_reported"
KERNEL_STATUS = "kernel_status"

TERMINAL_EVENT_TYPES = frozenset({RUN_FINISHED, RUN_FAILED, RUN_CANCELLED})

#: Upper bound on frames a single run keeps for late subscribers.
_RING_CAPACITY = 4000


@dataclass(frozen=True, slots=True)
class RunEvent:
    """One frame in the run event stream."""

    type: str
    run_id: str
    seq: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "runId": self.run_id,
            "seq": self.seq,
            "ts": self.ts,
            "payload": dict(self.payload),
        }

    def to_sse(self) -> str:
        body = json.dumps(self.as_dict(), ensure_ascii=False)
        return f"id: {self.seq}\nevent: {self.type}\ndata: {body}\n\n"


class EventBus:
    """Per-run fan-out with replay for reconnecting subscribers.

    A run keeps its recent frames in a bounded ring. Subscribers created before
    the run ends keep a live queue; late subscribers replay the ring and then
    switch to the live tail, which is what makes ``Last-Event-ID`` recovery
    correct without a persistence layer in front of it.
    """

    def __init__(self, capacity: int = _RING_CAPACITY) -> None:
        self._capacity = capacity
        self._lock = threading.Lock()
        self._counters: dict[str, int] = {}
        self._history: dict[str, list[RunEvent]] = {}
        self._subscribers: dict[str, set[queue.Queue[RunEvent | None]]] = {}
        self._closed: set[str] = set()

    def publish(
        self, run_id: str, event_type: str, payload: Mapping[str, Any] | None = None
    ) -> RunEvent:
        with self._lock:
            seq = self._counters.get(run_id, 0) + 1
            self._counters[run_id] = seq
            event = RunEvent(
                type=event_type, run_id=run_id, seq=seq, payload=dict(payload or {})
            )
            history = self._history.setdefault(run_id, [])
            history.append(event)
            if len(history) > self._capacity:
                del history[: len(history) - self._capacity]
            subscribers = tuple(self._subscribers.get(run_id, ()))
        for subscriber in subscribers:
            subscriber.put(event)
        return event

    def close(self, run_id: str) -> None:
        """Mark end-of-stream and wake live subscribers.

        The closed flag also ends later subscriptions: a client that connects
        after the run finished must replay the history and then get EOF, not
        block forever on a queue nobody will write to again.
        """
        with self._lock:
            self._closed.add(run_id)
            subscribers = tuple(self._subscribers.get(run_id, ()))
        for subscriber in subscribers:
            subscriber.put(None)

    def subscribe(self, run_id: str, after: int = 0) -> Iterator[RunEvent]:
        """Yield frames with ``seq > after``, then any frame published later."""
        with self._lock:
            pending = [event for event in self._history.get(run_id, ()) if event.seq > after]
            subscriber: queue.Queue[RunEvent | None] | None = (
                queue.Queue() if run_id not in self._closed else None
            )
            if subscriber is not None:
                self._subscribers.setdefault(run_id, set()).add(subscriber)
        try:
            for event in pending:
                yield event
            if subscriber is None:
                return
            while True:
                item = subscriber.get()
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
        with self._lock:
            return run_id in self._counters

    def forget(self, run_id: str) -> None:
        """Drop a finished run's history once nothing can reconnect to it."""
        with self._lock:
            self._counters.pop(run_id, None)
            self._history.pop(run_id, None)
            self._closed.discard(run_id)
