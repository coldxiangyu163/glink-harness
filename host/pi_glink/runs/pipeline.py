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
from .state import LOGGER_NAME, RunExecution

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
                logger.exception("run failed run=%s", run_id)
            facts.failure = f"{type(exc).__name__}: {exc}"
        finally:
            self.close_out(execution)

    def close_out(self, execution: RunExecution) -> None:
        """Observe's finish → Recover → Seal → Decide/Commit, in contract order."""
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
        execution.finalized.set()
