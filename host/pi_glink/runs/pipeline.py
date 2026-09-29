"""The run pipeline: one admitted run through Prepare → … → Commit.

:class:`RunPipeline` wires the stages of :mod:`pi_glink.runs.stages` to the
frame channel (:mod:`pi_glink.runs.channel`), the run's secret boundary
(:mod:`pi_glink.runs.secrets`) and the harness-provided lifecycle plugins
(:mod:`pi_glink.runs.plugins`). It runs on the run's own execution thread; the
manager (:mod:`pi_glink.runs.runner`) keeps only the public API and the
admission lock.

:meth:`RunPipeline.close_out` is the contract ordering — plugins stop → first
projector flush → face artifacts → face close → sweeps (workspace, subagent
mirror, session/state roots) → plugin seal (subagent enrichment) → report
recovery → verdict critical section → post-commit bookkeeping. Every step
before the commit is guarded on its own, so a failing sweep cannot prevent the
verdict.
"""

from __future__ import annotations

import logging
import traceback
from typing import TYPE_CHECKING, Any, Callable

from ..config import HostConfig
from ..egress import EgressProxy
from ..kernel.agent_env import RunCredentials
from ..kernel.harness import HarnessFamily
from ..redaction import SecretRedactor
from ..store import SUBAGENTS_DIRNAME, RunStore
from . import limits
from .channel import RunChannel
from .errors import EventLogError
from .plugins import RunPlugin, plugins_for
from .secrets import RunSecretBoundary
from .stages import (
    CommitStage,
    DecideStage,
    LaunchStage,
    ObserveStage,
    PrepareStage,
    RecoverStage,
    SealStage,
    StageServices,
)
from .state import COMMITTED, LOGGER_NAME, RunExecution

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .admission import RunAdmission

logger = logging.getLogger(LOGGER_NAME)


class RunPipeline:
    """Execute admitted runs; one instance serves every run of a manager."""

    def __init__(
        self,
        *,
        config: HostConfig,
        store: RunStore,
        bus: Any,
        egress: EgressProxy,
        admission: "RunAdmission",
        host_redactor: SecretRedactor,
        sandbox_factory: Callable[[], Any],
        client_factory: Callable[[], Any],
        on_committed: Callable[[str], None],
    ) -> None:
        self.config = config
        self.store = store
        self.admission = admission
        self._host_redactor = host_redactor
        self.channel = RunChannel(
            store=store,
            bus=bus,
            secrets_of=self.secrets,
            family_of=self.family_of,
            plugins_of=self.plugins,
        )
        services = StageServices(
            config=config,
            store=store,
            bus=bus,
            egress=egress,
            admission=admission,
            channel=self.channel,
            secrets=self.secrets,
            plugins=self.plugins,
            sandbox_factory=sandbox_factory,
            client_factory=client_factory,
        )
        self.prepare = PrepareStage(services)
        self.launch = LaunchStage(services)
        self.observe = ObserveStage(services)
        self.recover = RecoverStage(services)
        self.seal = SealStage(services)
        self.decide = DecideStage(services)
        self.commit = CommitStage(services, self.decide, on_committed=on_committed)

    # -- per-run collaborators -------------------------------------------------

    def family_of(self, execution: RunExecution) -> HarnessFamily:
        return self.admission.family_of(execution.record)

    def secrets(self, execution: RunExecution) -> RunSecretBoundary:
        """The run's secret boundary, with its rewritable trees bound.

        Production always builds the boundary at admission; an execution
        object constructed directly (unit tests, future callers) gets its own
        boundary from the host secrets plus its credentials, so nothing is
        ever emitted through no boundary at all.
        """
        with execution.lock:
            secrets = execution.secrets
            if secrets is None:
                credentials = execution.credentials or RunCredentials.from_host(self.config)
                secrets = RunSecretBoundary.for_credentials(
                    self._host_redactor, credentials, run_id=execution.record.run_id
                )
                execution.secrets = secrets
            if not secrets.has_trees:
                secrets.bind_trees(
                    ("workspace", execution.workspace),
                    (
                        "subagent artifact",
                        self.store.run_dir(execution.record.run_id) / SUBAGENTS_DIRNAME,
                    ),
                )
            return secrets

    def plugins(self, execution: RunExecution) -> tuple[RunPlugin, ...]:
        """The run's lifecycle plugins, built once from its family."""
        with execution.lock:
            plugins = execution.plugins
            if plugins is None:
                plugins = plugins_for(
                    self.family_of(execution), store=self.store, channel=self.channel
                )
                execution.plugins = plugins
            return plugins

    # -- the run ---------------------------------------------------------------

    def execute(self, execution: RunExecution) -> None:
        """Prepare and Launch; whatever happens, :meth:`close_out` follows."""
        run_id = execution.record.run_id
        facts = execution.execution_facts
        try:
            plan = self.prepare.run(execution)
            self.launch.run(execution, plan)
        except EventLogError as exc:
            logger.error("run failed run=%s: %s", run_id, exc)
            facts.event_log_error = facts.event_log_error or str(exc)
        except Exception as exc:  # noqa: BLE001 - every run must end with a reason
            if execution.cancel_requested:
                # Stopping a run closes the sandbox (or aborts pi); the error
                # that surfaces is the cancellation, not a failure.
                logger.info("run cancelled run=%s (%s)", run_id, type(exc).__name__)
            else:
                # The exception text can carry agent- or provider-controlled
                # content (an ACP ``error.data`` detail, a kernel message) that
                # has not been through the run's redactor yet. Log the type and
                # the stack frames only; the redacted, bounded text follows in
                # the close-out's "run failed ... error=" line.
                logger.error(
                    "run failed run=%s: %s\n%s",
                    run_id,
                    type(exc).__name__,
                    "".join(traceback.format_tb(exc.__traceback__)).rstrip(),
                )
            facts.failure = f"{type(exc).__name__}: {exc}"
        finally:
            self.close_out(execution)

    def close_out(self, execution: RunExecution) -> None:
        """Observe's finish → Recover → Seal → Decide/Commit, in contract order.

        The whole body sits behind one guard: one stage raising an *unexpected*
        exception must not leave the run ``running`` with ``finalized`` unset.
        The failure is folded into the run's own facts
        (``execution_facts.failure``) and the normal Decide/Commit path still
        decides a terminal state; only when even that commit fails does the run
        stay for restart reconciliation (the existing uncommitted-verdict
        contract) — always loudly recorded, never silently swallowed.
        """
        run_id = execution.record.run_id
        try:
            self._finalize(execution)
        except Exception as exc:  # noqa: BLE001 - every run must end with a reason
            logger.exception("close-out 未预期失败 run=%s", run_id)
            self._commit_close_out_failure(execution, exc)
        finally:
            execution.finalized.set()

    def _finalize(self, execution: RunExecution) -> None:
        """The contract-ordered close-out body (see :meth:`close_out`)."""
        run_id = execution.record.run_id
        self.observe.finish(execution)
        recovery = self.recover.run(execution, execution.adapter)
        execution.resource_facts = recovery.resources
        execution.artifact_facts = self.seal.run(execution, recovery)

        committed = self.commit.run(execution)
        verdict = execution.verdict
        assert verdict is not None  # the commit stage always decides one
        if verdict.error:
            # 失败详情进宿主日志：终帧的 ``error`` 已带 run 边界（ACP 的
            # ``kernel_exit`` 文本含经有界的 agent stderr 尾部），日志这里是
            # 同一份文本、同一边界，只另有界截断（不把未脱敏文本写出去）。
            logger.warning(
                "run failed run=%s status=%s stop=%s error=%s",
                run_id,
                verdict.status,
                verdict.stop_reason,
                self.secrets(execution).redact(verdict.error)[: limits.LOG_ERROR_MAX_CHARS],
            )
        logger.info(
            "run finalized run=%s status=%s stop=%s committed=%s report=%s released=%s",
            run_id,
            verdict.status,
            verdict.stop_reason,
            committed,
            execution.artifact_facts.report_path or "<none>",
            recovery.resources.cleanup.released,
        )

    def _commit_close_out_failure(self, execution: RunExecution, exc: Exception) -> None:
        """Fold a close-out crash into the run's facts and still commit a verdict.

        The already-decided/committed case only lost the tail: the durable
        terminal state stands. Otherwise the failure becomes
        ``execution_facts.failure`` (the same field the execution face uses) and
        the verdict is committed through the normal Decide path — a raise here
        can only come from the commit itself, and the run then stays for restart
        reconciliation with the reason recorded in ``event_log_error``.
        """
        run_id = execution.record.run_id
        detail = f"close-out 未预期失败: {type(exc).__name__}: {exc}"
        facts = execution.execution_facts
        if not facts.failure:
            facts.failure = detail
        if execution.phase == COMMITTED:
            return
        try:
            self.commit.run(execution)
        except Exception:  # noqa: BLE001 - the run stays for restart reconciliation
            logger.exception("close-out 护栏未能提交终态 run=%s", run_id)
            facts.event_log_error = facts.event_log_error or detail
