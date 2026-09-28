"""The run's frame commit path (Observe).

Every product event of a run — projected kernel frames, host-owned notices,
the terminal frame — is committed here: redacted by the run's own boundary,
appended to the durable log, then shown on the bus. A frame that cannot be
committed is not shown either; the failure becomes an execution fact.

After a frame is committed the run's lifecycle plugins
(:mod:`pi_glink.runs.plugins`) see it on the same critical section, so a
document they maintain never lags behind the committed event log.

The pure folds of execution facts (:func:`record_model_result`,
:func:`accumulate_usage`) live here too: they are how Observe turns frames
into facts, and they take no host state.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Sequence

from .. import events
from ..kernel.execution import SessionInfo
from ..kernel.harness import HarnessFamily
from ..store import RunStore
from .errors import EventLogError
from .secrets import RunSecretBoundary
from .state import LOGGER_NAME, OPEN, LiveExecutionFacts, RunExecution

logger = logging.getLogger(LOGGER_NAME)


def record_model_result(facts: LiveExecutionFacts, frame: Mapping[str, Any]) -> None:
    """Fold one raw kernel frame's model result into the execution facts."""
    frame_type = frame.get("type")
    if frame_type == "message_end":
        message = frame.get("message")
        if isinstance(message, Mapping) and message.get("role") == "assistant":
            stop = message.get("stopReason") or message.get("stop_reason")
            if isinstance(stop, str) and stop:
                facts.model_stop_reason = stop
            error = message.get("errorMessage")
            facts.model_error = error if isinstance(error, str) and error.strip() else None
    elif frame_type == "auto_retry_end" and not frame.get("success"):
        final = frame.get("finalError")
        if isinstance(final, str) and final.strip():
            facts.model_error = final
    elif frame_type == "acp_prompt_result":
        # ``session/prompt``'s answer: the ACP adapter translates it into this
        # frame, and the stop reason is recorded verbatim (the ACP vocabulary
        # is mapped to the product verdict by the Decide stage).
        stop = frame.get("stopReason")
        facts.model_stop_reason = stop if isinstance(stop, str) and stop else None


def accumulate_usage(
    family: HarnessFamily,
    facts: Any,
    frame: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Fold one usage snapshot into the run total — the family's algorithm.

    pi sums per-response snapshots deduped by message identity (no price is
    invented: ``cost`` is summed only when pi reported it); ACP replaces the
    total with the latest cumulative context snapshot. The totals stay on the
    run's execution facts (``usage_total``/``usage_keys``); the family only
    computes.
    """
    return family.accumulate_usage(frame, payload, facts.usage_total, facts.usage_keys)


class RunChannel:
    """Commit frames for open runs; one instance serves every run of a manager."""

    def __init__(
        self,
        *,
        store: RunStore,
        bus: Any,
        secrets_of: Callable[[RunExecution], RunSecretBoundary],
        family_of: Callable[[RunExecution], HarnessFamily],
        plugins_of: Callable[[RunExecution], Sequence[Any]],
    ) -> None:
        self.store = store
        self.bus = bus
        self._secrets_of = secrets_of
        self._family_of = family_of
        self._plugins_of = plugins_of

    # -- commits ---------------------------------------------------------------

    def emit(
        self,
        execution: RunExecution,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        terminal: bool = False,
    ) -> events.RunEvent:
        """Commit one frame: redact, persist, show, then hand it to the plugins.

        Belt-and-braces: projection already redacts, but host-owned events
        (run_finished, kernel_status, …) are built by the host, so the run's own
        boundary runs again on the way out. The terminal frame is committed
        durably (see :meth:`RunStore.commit_terminal`).
        """
        safe_payload = (
            self._secrets_of(execution).redact_payload(payload) if payload is not None else None
        )
        run_id = execution.record.run_id
        write = self.store.commit_terminal if terminal else self.store.append_event
        try:
            event = self.bus.commit(
                run_id,
                event_type,
                safe_payload,
                persist=lambda event: write(run_id, event.as_dict()),
            )
        except OSError as exc:
            raise EventLogError(f"事件日志写入失败: {type(exc).__name__}: {exc}") from exc
        if not terminal:
            # The typed event is committed; the plugins now land what they
            # derive from it on the same commit path (the run lock is held).
            for plugin in self._plugins_of(execution):
                plugin.on_committed(execution, event_type, safe_payload or {})
        return event

    def emit_frame(
        self, execution: RunExecution, event_type: str, payload: Mapping[str, Any]
    ) -> bool:
        """Commit one frame of an open run (caller holds the run lock)."""
        try:
            self.emit(execution, event_type, payload)
        except EventLogError as exc:
            facts = execution.execution_facts
            facts.event_log_error = facts.event_log_error or str(exc)
            logger.error("event log failed run=%s: %s", execution.record.run_id, exc)
            return False
        return True

    def emit_open(
        self, execution: RunExecution, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        """Commit a host-owned frame while the run is open (raises on log failure)."""
        with execution.lock:
            if execution.phase != OPEN:
                return
            self.emit(execution, event_type, payload)

    def warn(
        self,
        execution: RunExecution,
        message: str,
        detail: str,
        *,
        label: str,
        log: bool = True,
    ) -> None:
        """A ``kernel_status`` warning of the open run; a log failure is only logged.

        ``label`` names the warning in the error line written when the warning
        itself cannot be persisted (``report``, ``child document``, …).
        """
        run_id = execution.record.run_id
        if log:
            logger.warning("run=%s %s", run_id, detail)
        try:
            self.emit_open(
                execution,
                events.KERNEL_STATUS,
                {"level": "warning", "message": message, "detail": detail},
            )
        except EventLogError as exc:
            logger.error("%s warning could not be persisted run=%s: %s", label, run_id, exc)

    def flush(self, execution: RunExecution) -> None:
        """Release the projector's withheld tail (caller holds the run lock)."""
        projector = execution.projector
        if projector is None:
            return
        for kind, payload in projector.flush():
            if not self.emit_frame(execution, kind, payload):
                break

    # -- kernel frames ---------------------------------------------------------

    def on_frame(self, execution: RunExecution, frame: Mapping[str, Any]) -> None:
        """Project one raw kernel frame and commit what it becomes."""
        with execution.lock:
            if execution.phase != OPEN or execution.projector is None:
                # After the verdict commit nothing may follow the terminal frame.
                logger.debug(
                    "frame after commit dropped run=%s type=%s",
                    execution.record.run_id,
                    frame.get("type"),
                )
                return
            for event_type, payload in execution.projector.project(frame):
                if event_type == events.USAGE_REPORTED:
                    payload = accumulate_usage(
                        self._family_of(execution), execution.execution_facts, frame, payload
                    )
                    if payload is None:
                        continue
                if not self.emit_frame(execution, event_type, payload):
                    # The durable log is the fact source: a frame that cannot
                    # be committed is not shown either.
                    return
            record_model_result(execution.execution_facts, frame)

    def publish_session_ready(self, execution: RunExecution, session: SessionInfo) -> None:
        """Persist the kernel's session identity and announce ``session_ready``."""
        run_id = execution.record.run_id
        # Already a host path: the adapter maps the sandbox's view back
        # (``Sandbox.host_path``) before reporting it.
        session_file = session.session_file
        if session.session_id or session_file:
            # 与 ``session_id`` 同一先例的写回点：agent 身份是内核自己报告
            # 的事实（ACP 协商结果里的 ``agentInfo.name``），启动后才知道，
            # 所以不进 create、在这里随同一次 update 落盘。不伪造、已知才写：
            # pi 协议不报 agent 身份（payload 无 ``agentInfo``，天然不写），
            # pi 的模型 id 也不冒充 agent。
            changes: dict[str, Any] = {
                "session_id": session.session_id,
                "session_file": session_file,
            }
            agent_info = session.payload.get("agentInfo")
            if isinstance(agent_info, Mapping):
                agent_name = agent_info.get("name")
                if isinstance(agent_name, str) and agent_name:
                    # 与事件载荷同一边界：内核自报的身份同样过 run 的脱敏并
                    # 限幅，落进 run 记录的永远是脱敏后的值。
                    changes["agent"] = self._secrets_of(execution).redact(agent_name)[:128]
            self.store.update(run_id, **changes)
        payload: dict[str, Any] = dict(session.payload)
        payload["sessionFile"] = session_file
        self.emit_open(execution, events.SESSION_READY, payload)
