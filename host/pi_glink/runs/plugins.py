"""Run lifecycle plugins: what a harness adds around the run's own facts.

A plugin is an enhancement over the committed event log, never a fact source
the verdict reads. The pipeline calls every plugin of a run at fixed hooks:

* :meth:`RunPlugin.on_committed` — after one non-terminal frame is committed,
  on the same critical section (the run lock is held);
* :meth:`RunPlugin.close` — the first step of Observe's finish, before any
  artifact fact is collected (no plugin-owned frame may race the verdict);
* :meth:`RunPlugin.seal` — inside Seal, after the credential sweeps and before
  the report is recovered; it may rewrite bytes (through the run's own secret
  boundary) and answers whether it did;
* :meth:`RunPlugin.settle` — in the verdict's critical section, after the
  verdict is decided and before the terminal frame is committed.

The subagent plugins below are the ones every harness family currently uses:
child documents (§3.5), R3a running progress and the family's artifact
enrichment. A family that has none of these facts simply answers nothing from
its seams (``child_progress_source() -> None``, the empty ``enrich_children``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Mapping

from .. import events
from ..artifacts import ArtifactError
from ..contracts import Verdict
from ..kernel.harness import HarnessFamily
from ..store import CANCELLED, FAILED, FINISHED, RunStore
from .child_progress_observer import ChildProgressObserver
from .state import LOGGER_NAME, OPEN, RunExecution

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .channel import RunChannel
    from .secrets import RunSecretBoundary

logger = logging.getLogger(LOGGER_NAME)


class RunPlugin:
    """Base plugin: every hook is a no-op."""

    def on_committed(
        self, execution: RunExecution, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        """One non-terminal frame was committed (caller holds the run lock)."""

    def close(self, execution: RunExecution) -> None:
        """Stop producing frames (first step of Observe's finish)."""

    def seal(
        self, execution: RunExecution, secrets: "RunSecretBoundary", failures: list[str]
    ) -> bool:
        """Seal-time work; returns whether artifact bytes were redacted."""
        return False

    def settle(self, execution: RunExecution, verdict: Verdict) -> None:
        """The verdict is decided (caller holds the run lock)."""


class ChildDocumentsPlugin(RunPlugin):
    """``children/<n>/child.json``: maintained on commit, settled at the verdict."""

    def __init__(self, store: RunStore, channel: "RunChannel") -> None:
        self.store = store
        self.channel = channel

    def _warn(self, execution: RunExecution, detail: str) -> None:
        self.channel.warn(execution, "child_doc_failed", detail, label="child document")

    def on_committed(
        self, execution: RunExecution, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        """Maintain the child documents after a committed typed event.

        The payload is already redacted (projection + the commit path), so the
        document is built from exactly the values the event log holds. The
        write rides the same critical section as the event commit: no reader
        of the documents observes a state the committed events have already
        passed. A failure is reported as a ``kernel_status`` warning and
        nothing more — the document is an enhancement over the committed event,
        not a racing fact source.
        """
        if event_type not in (events.SUBAGENT_STARTED, events.SUBAGENT_FINISHED):
            return
        run_id = execution.record.run_id
        try:
            if event_type == events.SUBAGENT_STARTED:
                self.store.write_child(
                    run_id,
                    {
                        "n": payload.get("n"),
                        "toolCallId": payload.get("toolCallId") or "",
                        "agent": payload.get("agent") or "",
                        "task": payload.get("task") or "",
                        "status": "running",
                        "resultPreview": None,
                        "enrichment": "none",
                    },
                )
                return
            ns = payload.get("ns")
            if not isinstance(ns, list):
                raise ValueError("subagent_finished 载荷缺少 ns 列表")
            status = child_terminal_status(payload)
            for n in ns:
                if not isinstance(n, int) or isinstance(n, bool) or n < 0:
                    raise ValueError(f"subagent_finished 的 ns 含非法序号: {n!r}")
                document = self.store.read_child(run_id, n)
                if document is None:
                    # The started document never landed (its warning was
                    # emitted then): record what this event proves, nothing
                    # more — agent/task are not fabricated.
                    document = {
                        "n": n,
                        "toolCallId": payload.get("toolCallId") or "",
                        "agent": "",
                        "task": "",
                        "enrichment": "none",
                    }
                document["status"] = status
                document["resultPreview"] = payload.get("resultPreview")
                self.store.write_child(run_id, document)
        except (ArtifactError, OSError, ValueError) as exc:
            self._warn(
                execution, f"子代理文档写入失败（{event_type}）: {type(exc).__name__}: {exc}"
            )

    def settle(self, execution: RunExecution, verdict: Verdict) -> None:
        """Close out children whose own finished event never arrived.

        The same rule as tools (§3.4「工具终态跟随 run」): a child still
        ``running`` at the verdict follows the run — ``finished`` on a clean
        finish, ``interrupted`` on failure, ``cancelled`` on cancellation. The
        documents are authoritative, so the sweep reads and rewrites them on
        disk (shared with the restart ledger via ``store.settle_children``);
        a write failure is a warning, never a blocker for the verdict.
        """
        followed = {
            FINISHED: "finished",
            FAILED: "interrupted",
            CANCELLED: "cancelled",
        }[verdict.status]
        failures = self.store.settle_children(execution.record.run_id, followed)
        if failures < 0:
            self._warn(execution, "子代理文档读取失败，未收口的按文档现状保留")
        elif failures:
            self._warn(
                execution,
                f"{failures} 个子代理文档未能落终态（run 终态 {verdict.status}）",
            )


def child_terminal_status(payload: Mapping[str, Any]) -> str:
    """The child status a ``subagent_finished`` payload asserts.

    The projection's own vocabulary wins; a payload without one degrades to the
    ``isError`` fact instead of inventing a success.
    """
    status = payload.get("status")
    if status in ("finished", "failed", "cancelled", "interrupted"):
        return status
    return "failed" if payload.get("isError") else "finished"


class ChildProgressPlugin(RunPlugin):
    """R3a running child progress (§3.5「运行中进度（R3a）」).

    The observer (:mod:`pi_glink.runs.child_progress_observer`) is a pure add-on
    over the committed typed events: throttled ``subagent_updated`` frames
    (latest value wins), never a verdict input, never written to child.json,
    never inside the admission lock. It is created on the first committed
    dispatch, or at :meth:`close` when no dispatch happened — creating it there
    is what keeps the closed flag authoritative: a late ``subagent_started``
    between the stop and the verdict finds a closed observer and starts no
    thread.
    """

    def __init__(self, family: HarnessFamily, channel: "RunChannel") -> None:
        self.family = family
        self.channel = channel
        self.observer: ChildProgressObserver | None = None

    def _observer(self, execution: RunExecution) -> ChildProgressObserver:
        """The run's observer, created once under the run lock."""
        with execution.lock:
            observer = self.observer
            if observer is None:
                observer = ChildProgressObserver(
                    run_id=execution.record.run_id,
                    lock=execution.lock,
                    is_open=lambda: execution.phase == OPEN,
                    match_n=self.family.match_progress_n,
                    source_factory=lambda: _child_progress_source(execution),
                    emit_frame=lambda event_type, payload: self.channel.emit_frame(
                        execution, event_type, payload
                    ),
                    # The observer's one allowed warning (at most once); it
                    # logs its own failure, so no extra warning line here.
                    emit_warning=lambda detail: self.channel.warn(
                        execution,
                        "child_progress_failed",
                        detail,
                        label="child progress",
                        log=False,
                    ),
                )
                self.observer = observer
            return observer

    def on_committed(
        self, execution: RunExecution, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        if event_type in (events.SUBAGENT_STARTED, events.SUBAGENT_FINISHED):
            self._observer(execution).track(event_type, payload)

    def close(self, execution: RunExecution) -> None:
        self._observer(execution).stop()


def _child_progress_source(execution: RunExecution) -> Any | None:
    """The adapter's ``child_progress_source()``, or ``None`` for no seam.

    The seam is an ABC 默认空实现 like ``recovered_session`` /
    ``recover_subagents``; a face whose answer is ``None`` honestly has no
    progress and gets no thread. The factory is called at first committed
    dispatch.
    """
    adapter = execution.adapter
    factory = adapter.child_progress_source if adapter is not None else None
    return factory() if factory is not None else None


class ChildEnrichmentPlugin(RunPlugin):
    """Associate subagent artifacts with the child documents — the family's job.

    The pi family copies matched pi-subagents artifact groups into
    ``children/<n>/`` and records the outcome honestly
    (``meta``/``unmatched``/``none``); the ACP family has no such layout and
    does nothing. The plugin hands the family the run boundary's sweep and the
    child-doc warning; the family's answer for the closing children-dir sweep
    flips the run's ``redacted`` fact exactly like the other sweeps.
    Enrichment is best-effort: an error is logged, never a verdict input.
    """

    def __init__(self, family: HarnessFamily, channel: "RunChannel") -> None:
        self.family = family
        self.channel = channel

    def seal(
        self, execution: RunExecution, secrets: "RunSecretBoundary", failures: list[str]
    ) -> bool:
        try:
            return bool(
                self.family.enrich_children(
                    execution.record.run_id,
                    runtime=execution.runtime,
                    artifact_failures=failures,
                    sweep=secrets.sweep_tree,
                    warn=lambda detail: self.channel.warn(
                        execution, "child_doc_failed", detail, label="child document"
                    ),
                )
            )
        except Exception:  # noqa: BLE001 - enrichment is best-effort, never a verdict input
            logger.exception("children enrichment failed run=%s", execution.record.run_id)
            return False


def plugins_for(
    family: HarnessFamily, *, store: RunStore, channel: "RunChannel"
) -> tuple[RunPlugin, ...]:
    """The lifecycle plugins one run of ``family`` carries, in hook order."""
    return (
        ChildDocumentsPlugin(store, channel),
        ChildProgressPlugin(family, channel),
        ChildEnrichmentPlugin(family, channel),
    )
