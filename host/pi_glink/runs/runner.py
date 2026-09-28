"""The run manager: the public API of the run lifecycle and its admission lock.

One run is one workspace plus one kernel process/sandbox plus one session file.
Its lifecycle is an explicit pipeline of stage objects, each reading the frozen
facts of the stages before it:

========== ================================================ =====================
stage      module                                           produces
========== ================================================ =====================
Admit      :mod:`pi_glink.runs.admission`                   ``AdmittedRun``
Prepare    :mod:`pi_glink.runs.stages`                      ``LaunchPlan``
Launch     :mod:`pi_glink.runs.stages`                      execution facts (open)
Observe    :mod:`pi_glink.runs.channel` + plugins           committed events
Recover    :mod:`pi_glink.runs.stages`                      ``RecoveryFacts``
Seal       :mod:`pi_glink.runs.stages` + secret boundary      ``ArtifactFacts``
Decide     :mod:`pi_glink.runs.stages`                      ``Verdict``
Commit     :mod:`pi_glink.runs.stages`                      committed or not
========== ================================================ =====================

The ordering after admission is :meth:`pi_glink.runs.pipeline.RunPipeline.close_out`;
the run's secrets (redactor, egress grant, sweep roots, credential copies) are
one :class:`~pi_glink.runs.secrets.RunSecretBoundary`; subagent documents,
running progress and enrichment are lifecycle plugins
(:mod:`pi_glink.runs.plugins`).

The manager keeps only the public surface — ``start``, ``stop``, ``wait``,
``shutdown``, the reconciliation/adjudication forwards to the resource ledger
(:mod:`pi_glink.runs.resource_ledger`) — plus the admission lock and the table
of runs this process executes. The verdict is committed at the run's
linearization point (the run lock); a stop accepted before it is the verdict, a
stop after it is refused with the committed status. Admission is derived from
durable facts: a new run is accepted only when the concurrency ceilings allow
it and no run holds unconfirmed resources (see :meth:`RunManager._check_admission`).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Sequence

from ..config import HostConfig
from ..egress import EgressProxy
from ..kernel.execution import ExecutionAdapter
from ..ownership import DataRootOwnership
from ..redaction import SecretRedactor, redactor_from_config
from ..store import REPORT_FILENAME, RUNNING, RunRecord, RunStore
from ..tenancy import TenantContext, TenantCredentialStore
from .admission import RunAdmission, parse_blueprint_command
from .errors import (
    BlueprintUnavailable,
    EventLogError,
    ResourcesUnreleased,
    RunQuotaExceeded,
    UnsupportedCommand,
)
from .limits import REPORT_MAX_BYTES
from .pipeline import RunPipeline

# ``RunBusy`` is defined next to the ledger because both the admission gate and
# the ledger's adjudication raise it.
from .resource_ledger import ResourceLedger, RunBusy
from .state import COMMITTED, LOGGER_NAME, OPEN, UNCOMMITTED, RunExecution, StopDecision

__all__ = [
    "REPORT_FILENAME",
    "REPORT_MAX_BYTES",
    "BlueprintUnavailable",
    "EventLogError",
    "ResourcesUnreleased",
    "RunBusy",
    "RunManager",
    "RunQuotaExceeded",
    "StopDecision",
    "UnsupportedCommand",
    "parse_blueprint_command",
]

logger = logging.getLogger(LOGGER_NAME)


class RunManager:
    """Own the lifecycle of local runs: public API and the admission lock."""

    def __init__(
        self,
        config: HostConfig,
        bus: Any,
        store: RunStore,
        *,
        client_factory: Callable[..., Any] | None = None,
        sandbox_factory: Callable[..., Any] | None = None,
        ownership: DataRootOwnership | None = None,
        credential_store: TenantCredentialStore | None = None,
        docker_probe: Callable[[str], tuple[bool, str]] | None = None,
        sandbox_probe: Callable[[str], tuple[bool, str]] | None = None,
    ) -> None:
        self.config = config
        self.bus = bus
        self.store = store
        #: The kernel client / sandbox factories (tests substitute them after
        #: construction; the stages read them at call time).
        self._client_factory = client_factory
        self._sandbox_factory = sandbox_factory
        # The host-level redaction boundary: it carries the host's own secrets
        # (global model/search keys, sandbox key). Each run's secret boundary
        # derives its own copy and adds only its own credentials and egress
        # token to it, so tenant secrets stay apart.
        self.redactor: SecretRedactor = redactor_from_config(config)
        # The credential egress proxy (docs/adr/0008): holds the real keys for
        # families that ``uses_egress``; each such run gets one token.
        self.egress = EgressProxy.from_config(config)
        #: The admission lock: the concurrency gate, the record creation and
        #: the in-flight table below move together under it.
        self._lock = threading.RLock()
        self._executions: dict[str, RunExecution] = {}
        self._admission = RunAdmission(
            config=config,
            store=store,
            credential_store=credential_store,
            host_redactor=self.redactor,
        )
        # Fail at startup (not at the first admission) on a family the frozen
        # configuration cannot provide.
        self._admission.family_for(config.harness)
        self._pipeline = RunPipeline(
            config=config,
            store=store,
            bus=bus,
            egress=self.egress,
            admission=self._admission,
            host_redactor=self.redactor,
            sandbox_factory=lambda: self._sandbox_factory,
            client_factory=lambda: self._client_factory,
            on_committed=self._forget,
        )
        #: The external-resource ledger: restart reconciliation, operator
        #: adjudication and the online identity-probe sweep. It shares this
        #: manager's admission lock and reads the in-flight table through the
        #: injected snapshots; the probes themselves (Docker CLI / E2B SDK,
        #: authenticated from the frozen config) are assembled inside it.
        self._ledger = ResourceLedger(
            store=store,
            config=config,
            ownership=ownership,
            redactor=self.redactor,
            redactor_for_record=self.redactor_for_record,
            admission_lock=self._lock,
            execution_ids=lambda: set(self._executions),
            executing_ids=lambda: {
                run_id
                for run_id, execution in self._executions.items()
                if execution.phase != COMMITTED
            },
            docker_probe=docker_probe,
            sandbox_probe=sandbox_probe,
        )

    @property
    def _docker_probe(self) -> Callable[[str], tuple[bool, str]]:
        """The ledger's Docker identity probe (tests substitute a fake here)."""
        return self._ledger._docker_probe

    @_docker_probe.setter
    def _docker_probe(self, probe: Callable[[str], tuple[bool, str]]) -> None:
        self._ledger._docker_probe = probe

    @property
    def _sandbox_probe(self) -> Callable[[str], tuple[bool, str]]:
        """The ledger's E2B identity probe (tests substitute a fake here)."""
        return self._ledger._sandbox_probe

    @_sandbox_probe.setter
    def _sandbox_probe(self, probe: Callable[[str], tuple[bool, str]]) -> None:
        self._ledger._sandbox_probe = probe

    # -- admission ------------------------------------------------------------

    @property
    def active_run_id(self) -> str | None:
        with self._lock:
            return next(
                (
                    run_id
                    for run_id, execution in self._executions.items()
                    if execution.phase != COMMITTED
                ),
                None,
            )

    def start(
        self,
        prompt: str,
        context: TenantContext | None = None,
        *,
        parent_run_id: str | None = None,
        blueprint: str | None = None,
    ) -> RunRecord:
        """Admit a run and execute it in the background.

        ``context`` is the verified tenant identity in multi-tenant mode and
        ``None`` in the single-user local mode. The run's owner labels come
        from that context alone — never from the request body, a query parameter
        or any other client-controlled input — and are written atomically with
        the admission record.

        ``parent_run_id`` continues one earlier research conversation. In
        multi-tenant mode the parent must belong to the caller's own tenant:
        an unknown, foreign or ownerless parent is refused as ``unknown_run``
        **before** its session, report, status or children are read.

        A prompt that opens with a ``/`` command other than ``/<blueprint>
        <topic>`` is refused with :class:`UnsupportedCommand` before anything
        is written or read (see :func:`parse_blueprint_command`).

        ``blueprint`` optionally names a session template from the configured
        blueprint directory (an exact file stem); an unknown or malformed
        template raises :class:`BlueprintUnavailable`. The rule is
        harness-independent.

        Admission is two halves around the admission lock
        (:mod:`pi_glink.runs.admission`): everything that reads files (the
        follow-up's session snapshot) happens before the lock, so one tenant's
        follow-up does not serialize every other tenant's admission; the
        parent's authoritative state is rechecked inside it.
        """
        request = self._admission.prepare(
            prompt, context, parent_run_id=parent_run_id, blueprint=blueprint
        )
        with self._lock:
            admitted = self._admission.admit(
                request, lambda holding: self._check_admission(context, holding)
            )
            execution = RunExecution.from_admitted(admitted)
            run_id = admitted.record.run_id
            self._executions[run_id] = execution
            # Register the run before the caller gets the id back: a browser
            # that opens the event stream immediately must see an open run, not
            # a 404 race with the execution thread's first line.
            self.bus.open_run(run_id)

        threading.Thread(
            target=self._pipeline.execute,
            args=(execution,),
            name=f"run-{run_id}",
            daemon=True,
        ).start()
        return admitted.record

    def redactor_for_record(self, record: RunRecord) -> SecretRedactor:
        """The redaction boundary appropriate for an already-persisted run."""
        return self._admission.redactor_for_record(record)

    def _check_admission(
        self, context: TenantContext | None, holding: Sequence[RunRecord]
    ) -> None:
        """Authorize concurrency (caller holds the lock; the store its transaction).

        Called by :meth:`RunStore.create` with ``holding`` — the records whose
        execution resources are not confirmed released, read inside the same
        ``BEGIN IMMEDIATE`` transaction that then inserts the new record. The
        quota count and the ledger it counts are therefore one transaction;
        raising refuses the admission and rolls the insert back. Two durable
        facts drive the decision — never a wall-clock guess: the runs executing
        in this process, and the holding records.

        In multi-tenant Docker/E2B mode the ceilings are global 4 / per-tenant 2
        by default and an over-limit request is refused immediately (no
        unbounded queue). An unreleased hold **consumes slots** instead of
        blocking everyone: it counts against its owner tenant's ceiling and
        against the global one (an ownerless single-user record has no tenant
        and therefore only consumes the global ceiling). A leak is never
        forgotten — it keeps eating capacity, and once the holds reach the global
        ceiling every new admission is refused — but one tenant's leftover no
        longer stops another tenant from making progress.

        Single-user local mode keeps its original behaviour: any hold refuses
        admission with ``resources_unreleased`` (carrying the run ids) and the
        single-run slot is unchanged.

        ``inproc`` is a single-user debug entry, including in multi-tenant
        deployments (the configuration layer refuses ``inproc`` entirely there).
        """
        active = [
            execution
            for execution in self._executions.values()
            if execution.phase != COMMITTED
        ]
        # Holds that are still in flight in this process already occupy a
        # concurrency slot below, so they are counted exactly once. (A run past
        # its terminal commit is no longer active: its unconfirmed release
        # already counts as a hold.)
        in_flight = {execution.record.run_id for execution in active}
        if not self.config.multi_tenant:
            holds = [record.run_id for record in holding if record.run_id not in in_flight]
            if holds:
                raise ResourcesUnreleased(holds)
            if active:
                raise RunBusy(active[0].record.run_id)
            return
        holds = [record for record in holding if record.run_id not in in_flight]
        global_total = len(active) + len(holds)
        # Refuse on the most specific ceiling first: a tenant already at its own
        # limit should be told about its own quota, not about the host total.
        tenant_id = context.tenant_id if context is not None else None
        tenant_total = sum(
            1 for execution in active if execution.record.tenant_id == tenant_id
        ) + sum(1 for record in holds if record.tenant_id == tenant_id)
        if tenant_total >= self.config.max_tenant_runs:
            raise RunQuotaExceeded("tenant", self.config.max_tenant_runs)
        if global_total >= self.config.max_global_runs:
            raise RunQuotaExceeded("global", self.config.max_global_runs)

    def _forget(self, run_id: str) -> None:
        """Drop a committed run from the in-flight table (the commit's callback)."""
        with self._lock:
            self._executions.pop(run_id, None)

    # -- cancellation ---------------------------------------------------------

    def stop(self, run_id: str, context: TenantContext | None = None) -> StopDecision:
        """Request cancellation, decided against the run's commit point.

        Accepting sets the flag the verdict reads under the same lock that
        commits it, so an accepted stop always ends as ``cancelled``. The
        face's own cancel only shortens the wait: a failed abort is logged and
        does not turn an accepted stop into a rejection.

        ``context`` is the verified identity in multi-tenant mode. The owner
        and role check runs under the run's own lock, so it cannot race the
        verdict commit: another tenant (or an ownerless single-user record) is
        answered like an unknown run, while a same-tenant member who did not
        create the run is refused with ``forbidden``.
        """
        with self._lock:
            execution = self._executions.get(run_id)
        if execution is None:
            record = self.store.get(run_id)
            if record is None:
                return StopDecision(False, None, "unknown")
            refused = self._stop_access(record, context)
            if refused is not None:
                return refused
            reason = "terminal" if record.is_terminal else "not_owned"
            return StopDecision(False, record.status, reason)
        with execution.lock:
            refused = self._stop_access(execution.record, context)
            if refused is not None:
                return refused
            if execution.phase == COMMITTED:
                assert execution.verdict is not None
                return StopDecision(False, execution.verdict.status, "terminal")
            if execution.phase == UNCOMMITTED:
                return StopDecision(False, RUNNING, "finalizing")
            execution.cancel_requested = True
            adapter = execution.adapter
        self._cancel_adapter(run_id, adapter)
        return StopDecision(True, RUNNING, "accepted")

    def _stop_owned(self, execution: RunExecution) -> None:
        """Cancel one run this process owns, bypassing the tenant role gate.

        Shutdown is host lifecycle, not an authenticated user action, so it
        must not depend on a request context. Ownership is already proven by
        the in-memory execution table.
        """
        with execution.lock:
            if execution.phase != OPEN:
                return
            execution.cancel_requested = True
            adapter = execution.adapter
        self._cancel_adapter(execution.record.run_id, adapter)

    @staticmethod
    def _cancel_adapter(run_id: str, adapter: ExecutionAdapter | None) -> None:
        if adapter is None:
            return
        try:
            adapter.cancel()
        except Exception:  # noqa: BLE001 - the flag is what ends the run
            logger.debug("adapter cancel failed run=%s", run_id, exc_info=True)

    def _stop_access(
        self, record: RunRecord, context: TenantContext | None
    ) -> StopDecision | None:
        """Owner/role gate for cancellation; ``None`` means allowed.

        Another tenant and an ownerless single-user record are answered exactly like
        an unknown run (404, no side effect). A same-tenant member who is not
        the creator is refused; the creator and tenant admins may stop it.
        Local mode has no identity and keeps its original behaviour.
        """
        if not self.config.multi_tenant:
            return None
        if context is None or record.tenant_id != context.tenant_id:
            return StopDecision(False, None, "unknown")
        if context.is_admin or record.creator_user_id == context.user_id:
            return None
        return StopDecision(False, record.status, "forbidden")

    def wait(self, run_id: str, timeout: float = 60.0) -> RunRecord | None:
        """Block until the run leaves ``running`` (used by tests and smoke)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self.store.get(run_id)
            if record is not None and record.is_terminal:
                return record
            time.sleep(0.1)
        return self.store.get(run_id)

    def shutdown(self, timeout: float = 15.0) -> list[str]:
        """Cancel and wait for every run owned by this process.

        Returns every run id that still needs an operator: runs whose verdict
        was not committed within the bound, and runs whose resources are not
        confirmed released (including those finished earlier). A thread that
        ended is not evidence that its resource is gone.
        """
        with self._lock:
            executions = list(self._executions.values())
        for execution in executions:
            self._stop_owned(execution)
        deadline = time.monotonic() + max(0.0, timeout)
        for execution in executions:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                execution.finalized.wait(remaining)
        with self._lock:
            pending = set(self._executions)
        unconfirmed = sorted(
            pending | set(self._ledger.resource_holds())
        )
        if unconfirmed:
            logger.error(
                "宿主退出时仍有 run 的终态或执行资源未确认: %s", ", ".join(unconfirmed)
            )
        # Tokens are memory-only; nothing outlives the host process.
        self.egress.close()
        return unconfirmed

    # -- restart reconciliation and resource adjudication ---------------------
    #
    # The implementations live in :class:`pi_glink.runs.resource_ledger.ResourceLedger`
    # (the external-resource ledger collaborator built in ``__init__``); these
    # forwards are the manager's surface for ``app.py`` and ``cli.py``.

    def reconcile(self) -> list[str]:
        """Close out runs a previous host process left ``running``."""
        return self._ledger.reconcile()

    def unreleased_resources(self) -> list[dict[str, Any]]:
        """Runs whose resources are not confirmed released, for operator review."""
        return self._ledger.unreleased_resources()

    def adjudicate_release(self, run_id: str, note: str) -> RunRecord:
        """Record an operator's decision that a run's resources are released."""
        return self._ledger.adjudicate_release(run_id, note)

    def reconcile_resources(self) -> list[str]:
        """Confirm persisted resources are gone and release the verified holds."""
        return self._ledger.reconcile_resources()

    def start_resource_watch(self) -> threading.Thread | None:
        """Start the periodic ledger sweep (once at startup, then every interval)."""
        return self._ledger.start_resource_watch()

    def stop_resource_watch(self, timeout: float = 5.0) -> None:
        """Stop the periodic sweep without touching the holds it did not verify."""
        self._ledger.stop_resource_watch(timeout)
