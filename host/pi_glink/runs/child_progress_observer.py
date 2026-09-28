"""R3a per-run child-progress observer (docs/architecture.md §3.5).

The observer is a pure add-on over the committed typed events: it polls the
execution face's ``child_progress_source()`` seam while a run is open and emits
throttled ``subagent_updated`` frames (latest value wins). It never touches the
verdict, never writes ``child.json``, and never runs inside the admission lock.

One instance belongs to one run and owns its whole transient state: the
in-flight dispatch table, the sealed children, the polling thread, the stop
event, the closed flag, the throttle/change-detection maps and the
single-warning flag. The runner touches it only on the committed
started/finished frames (via :meth:`track`) and as the first finalize step
(via :meth:`stop`).

Locking contract (the observer is owned by ``plugins.ChildProgressPlugin``): the
in-flight table, the closed flag and the emitted/throttle maps are only read or
written under the run lock; the polling thread takes the run lock to compute
and emit one poll's frames, and the runner's emit callback commits under that
same lock. The bounded join and the source close in :meth:`stop` happen outside
the lock because the loop needs it to emit. ``track`` asserts none of this by
itself — its caller holds the run lock, as the runner does.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from .. import events

logger = logging.getLogger("pi_glink.runs.child_progress_observer")

#: R3a bounds: one polling thread per run at this cadence, at most one
#: ``subagent_updated`` per child per throttle window, a bounded backoff
#: between failed polls, and a bounded join when the run finalizes.
_PROGRESS_POLL_SECONDS = 1.0
_PROGRESS_THROTTLE_SECONDS = 2.0
_PROGRESS_MAX_BACKOFF_SECONDS = 5.0
_PROGRESS_JOIN_SECONDS = 5.0


class ChildProgressObserver:
    """Poll one run's progress source until finalize stops the observer.

    ``match_n`` is the harness family's association rule (pi: transcript
    ``index`` + same agent inside the in-flight call; ACP: exact ``call_id``).
    ``emit_frame`` commits one product frame and returns ``False`` when the
    durable log is failing; ``emit_warning`` emits the one ``kernel_status``
    warning a failing observer is allowed to produce. Both callbacks belong to
    the runner and are invoked on the observer's terms — the runner keeps its
    own frame-commit and phase rules.
    """

    def __init__(
        self,
        *,
        run_id: str,
        lock: threading.RLock,
        is_open: Callable[[], bool],
        match_n: Callable[[Any, Mapping[str, list[dict[str, Any]]]], int | None],
        source_factory: Callable[[], Any | None],
        emit_frame: Callable[[str, Mapping[str, Any]], bool],
        emit_warning: Callable[[str], None],
    ) -> None:
        self.run_id = run_id
        self.lock = lock
        self._is_open = is_open
        self._match_n = match_n
        self._source_factory = source_factory
        self._emit_frame = emit_frame
        self._emit_warning = emit_warning
        #: In-flight dispatch calls: toolCallId → children in task order
        #: (``{"n", "agent"}``); a call leaves the table when its finished
        #: event commits.
        self.calls: dict[str, list[dict[str, Any]]] = {}
        #: Children whose finished event committed: no more progress for them.
        self.finished_ns: set[int] = set()
        #: The one polling thread was started (or the seam honestly had none).
        self.started = False
        #: Once finalize stopped the observer no started event may start it
        #: again — frames can still commit between the stop and the verdict,
        #: and a thread started in that window would never be stopped.
        self.closed = False
        self.source: Any = None
        self.stop_event: threading.Event | None = None
        self.thread: threading.Thread | None = None
        self.warned = False
        #: Per-child latest emitted tuple ``(steps, lastToolName,
        #: lastToolTarget, bytes)`` and its emit time — the throttle and the
        #: change detection.
        self.emitted: dict[int, tuple[Any, ...]] = {}
        self.emit_ts: dict[int, float] = {}

    # -- runner call sites ----------------------------------------------------

    def track(self, event_type: str, payload: Mapping[str, Any]) -> None:
        """Maintain the in-flight dispatch table (caller holds the run lock).

        A pi ``tool_call`` dispatches several tasks; their ``subagent_started``
        events commit in task order, so an entry's position in the call's list
        is the task index that the transcript filename's ``index`` carries.
        ACP dispatches one child per call, hit exactly by ``toolCallId``. A
        committed ``subagent_finished`` removes the call and seals its
        children against further progress.
        """
        call_id = str(payload.get("toolCallId") or "")
        if event_type == events.SUBAGENT_STARTED:
            n = payload.get("n")
            if call_id and isinstance(n, int) and not isinstance(n, bool):
                self.calls.setdefault(call_id, []).append(
                    {"n": n, "agent": str(payload.get("agent") or "")}
                )
            self._start()
            return
        if call_id:
            self.calls.pop(call_id, None)
        ns = payload.get("ns")
        if isinstance(ns, list):
            for n in ns:
                if isinstance(n, int) and not isinstance(n, bool):
                    self.finished_ns.add(n)

    def stop(self) -> None:
        """Stop the observer: close under the run lock, then bounded join.

        The closed flag is set under the same lock :meth:`_start` checks it
        under, so a ``subagent_started`` committed between this stop and the
        verdict can no longer launch a thread. The join and the source close
        happen outside the lock because the loop takes the run lock to emit.
        The source is closed even when the join times out, so a late poll fails
        instead of emitting into a finalized run; the thread is a daemon and
        dies with the process.
        """
        with self.lock:
            self.closed = True
            stop = self.stop_event
            thread = self.thread
            source = self.source
            self.stop_event = None
            self.thread = None
            self.source = None
        if stop is not None:
            stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=_PROGRESS_JOIN_SECONDS)
        if source is not None:
            try:
                source.close()
            except Exception:  # noqa: BLE001 - closing must not block finalize
                logger.debug(
                    "child progress source close failed run=%s",
                    self.run_id,
                    exc_info=True,
                )

    # -- observer internals ---------------------------------------------------

    def _start(self) -> None:
        """Start the one polling thread on the first committed dispatch.

        Caller holds the run lock. The seam factory is the adapter's
        ``child_progress_source()`` — an ABC 默认空实现 like
        ``recovered_session``/``recover_subagents``; a face whose answer is
        ``None`` honestly has no progress and gets no thread. A run that never
        dispatches a subagent never reaches here, so it pays nothing.
        """
        if self.started or self.closed:
            return
        self.started = True
        try:
            source = self._source_factory()
        except Exception as exc:  # noqa: BLE001 - observer failure is a warning only
            self._warn(
                f"子代理进度观察源创建失败: {type(exc).__name__}: {exc}"
            )
            return
        if source is None:
            return
        stop = threading.Event()
        self.source = source
        self.stop_event = stop
        thread = threading.Thread(
            target=self._loop,
            args=(source, stop),
            name=f"child-progress-{self.run_id}",
            daemon=True,
        )
        self.thread = thread
        thread.start()

    def _loop(self, source: Any, stop: threading.Event) -> None:
        """Poll the source until finalize stops the thread.

        Latest-value-wins: a dropped intermediate frame changes nothing, so a
        poll cycle is cheap to repeat. A failing ``poll()`` only backs off
        (bounded by ``_PROGRESS_MAX_BACKOFF_SECONDS``) and the loop keeps
        retrying: a transient sandbox timeout must not blind the rest of the
        run (docs/adr/0006). The thread stops only at finalize; a failure is a
        ``kernel_status`` warning and never takes part in the verdict.
        """
        failures = 0
        try:
            while not stop.wait(_PROGRESS_POLL_SECONDS):
                try:
                    facts = source.poll()
                except Exception as exc:  # noqa: BLE001 - backoff, then retry
                    failures += 1
                    self._warn(
                        f"子代理进度观察失败: {type(exc).__name__}: {exc}"
                    )
                    stop.wait(
                        min(
                            _PROGRESS_POLL_SECONDS * failures,
                            _PROGRESS_MAX_BACKOFF_SECONDS,
                        )
                    )
                    continue
                failures = 0
                self._publish(facts)
        except Exception:  # noqa: BLE001 - the observer must never kill the run
            logger.exception("子代理进度观察线程异常退出 run=%s", self.run_id)
            self._warn("子代理进度观察线程异常退出")

    def _publish(self, facts: Sequence[Any]) -> None:
        """Map one poll's facts onto children and emit the throttled updates.

        Unmappable facts are dropped (no fabricated progress); a child whose
        finished event committed — or a run that left the open phase — gets
        nothing more. ``bytes`` and ``steps`` are cumulative snapshots kept
        monotonic across polls (a replaced transcript re-parses from zero but
        must not move the reported values backwards).
        """
        if not facts:
            return
        now = time.monotonic()
        with self.lock:
            if not self._is_open():
                return
            for fact in facts:
                mapped = self._map_fact(fact)
                if mapped is None:
                    continue
                n, steps, name, target, byte_count = mapped
                if n in self.finished_ns:
                    continue
                previous = self.emitted.get(n)
                if previous is not None:
                    # 快照语义：steps 与 bytes 都是累计值，transcript 被替换
                    # 重解析后事件流也不许回退（前端另有一层 Math.max 兜底，
                    # 事件本身先保证单调）。
                    byte_count = max(byte_count, previous[3])
                    steps = max(steps, previous[0])
                current = (steps, name, target, byte_count)
                if previous == current:
                    continue
                last_emit = self.emit_ts.get(n)
                if (
                    last_emit is not None
                    and now - last_emit < _PROGRESS_THROTTLE_SECONDS
                ):
                    continue
                payload = {
                    "n": n,
                    "steps": steps,
                    "lastToolName": name,
                    "lastToolTarget": target,
                    "bytes": byte_count,
                }
                if not self._emit_frame(events.SUBAGENT_UPDATED, payload):
                    # The durable log is failing; the run converges on that
                    # verdict, so stop pushing transient frames into it.
                    return
                self.emitted[n] = current
                self.emit_ts[n] = now

    def _map_fact(self, fact: Any) -> tuple[Any, ...] | None:
        """Pair one polled fact with its child ``n``; ``None`` fabricates nothing.

        通用字段校验（steps/bytes/name/target）留在这里；配对规则是族方法
        （pi：transcript ``index`` + ``agent`` 同名；ACP：``call_id`` 精确
        命中）。对不上即该 child 无进度。
        """
        steps = getattr(fact, "steps", None)
        byte_count = getattr(fact, "bytes", None)
        if (
            not isinstance(steps, int)
            or isinstance(steps, bool)
            or steps < 0
            or not isinstance(byte_count, int)
            or isinstance(byte_count, bool)
            or byte_count < 0
        ):
            return None
        name = getattr(fact, "last_tool_name", None)
        target = getattr(fact, "last_tool_target", None)
        name = name if isinstance(name, str) and name else None
        target = target if isinstance(target, str) and target else None
        n = self._match_n(fact, self.calls)
        if n is None:
            return None
        return (n, steps, name, target, byte_count)

    def _warn(self, detail: str) -> None:
        """An observer failure surfaces as at most one ``kernel_status`` warning."""
        logger.warning("run=%s %s", self.run_id, detail)
        with self.lock:
            if self.warned:
                return
            self.warned = True
        self._emit_warning(detail)
