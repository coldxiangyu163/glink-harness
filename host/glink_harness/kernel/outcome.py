"""The execution outcome type and the seal protocol for driver-thread adapters.

:class:`ExecutionOutcome` is the execution-level verdict every adapter's
``wait`` returns (the adapter contract lives in
:mod:`glink_harness.kernel.execution`). Adapters whose turn is observed on a driver
thread — the pi E2B face and the whole ACP family — share one sealing
mechanism, owned by :class:`SealedOutcome`:

* every driver-thread write to the outcome is guarded by ``_outcome_lock`` and
  refused once ``_outcome_sealed`` is set;
* ``wait`` seals the outcome in the same critical section that writes the
  final decision (``_settle_timeout`` / ``_settle_cancelled``), or right after
  the driver finished (``_seal_outcome``), so a driver write lands either
  before the decision — where the decision overrides it — or after the seal,
  where it is dropped. A late report can never rewrite a decided verdict.

What is deliberately **not** shared: the ``wait`` loop itself. The pi E2B face
keeps polling after a cancellation until the killed sandbox's driver returns
and only then seals the cancelled verdict; the ACP face decides and breaks
immediately, and its timeout teardown is a ``session/cancel`` request rather
than a sandbox kill. A parametrized skeleton would hide exactly the semantics
that differ, so each adapter keeps its own loop over these shared primitives.
The ACP family's one controlled late write — ``close()`` completing a pending
``kernel_exit`` report with the reaped exit status — likewise stays in
:mod:`glink_harness.kernel.acp_execution`, guarded by the same lock.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - annotation only, avoids an import cycle
    from .execution import ExecutionSpec

#: How often a wait loop re-checks cancellation and process liveness.
POLL_SECONDS = 0.25


@dataclass(slots=True)
class ExecutionOutcome:
    """Execution-level facts, before the model result and artifact policy."""

    settled: bool = False
    exit_code: int | None = None
    cancelled: bool = False
    timed_out: bool = False
    error: str | None = None
    stop_reason: str | None = None


class SealedOutcome:
    """Base for adapters whose outcome is written from a driver thread.

    Mixed in ahead of :class:`~glink_harness.kernel.execution.ExecutionAdapter`:
    the cooperative ``__init__`` chains to the adapter's and then installs the
    outcome, its lock and the seal flag. Subclasses keep their face-specific
    writers (the error text is the face's own) and their own ``wait`` loop;
    the pieces below are the ones that were identical on every face.
    """

    #: Set by ``ExecutionAdapter.__init__``; declared for the timeout text.
    spec: ExecutionSpec

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._outcome = ExecutionOutcome()
        self._outcome_lock = threading.Lock()
        self._outcome_sealed = False

    def _settle_timeout(self) -> None:
        """Decide the deadline and freeze the outcome in one critical section."""
        with self._outcome_lock:
            self._outcome.timed_out = True
            self._outcome.stop_reason = "timeout"
            self._outcome.error = (
                f"run did not settle within {self.spec.timeout_seconds}s"
            )
            self._outcome_sealed = True

    def _settle_cancelled(self) -> None:
        """Decide cancellation and freeze the outcome in one critical section.

        An accepted cancellation stays the verdict even when the deadline (or
        a settled turn) was decided first: the flag is written and the outcome
        resealed rather than refused.
        """
        with self._outcome_lock:
            self._outcome.cancelled = True
            self._outcome.stop_reason = "abort"
            self._outcome_sealed = True

    def _seal_outcome(self) -> None:
        """Freeze the driver-written decision before the verdict is read."""
        with self._outcome_lock:
            self._outcome_sealed = True
