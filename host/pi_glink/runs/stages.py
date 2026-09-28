"""The run lifecycle stages after admission.

Prepare → Launch → Observe → Recover → Seal → Decide → Commit. Each stage is an
object with one entry point; it reads the facts the earlier stages produced
and returns its own frozen fact:

========== ======================================= ==========================
stage      does                                    produces
========== ======================================= ==========================
Prepare    workspace, seeded report, face, runtime :class:`LaunchPlan`
           render (egress grant), adapter
Launch     start, identity, session, deliver, wait execution facts (open)
Observe    frames → events (:mod:`.channel`);      —
           ``finish`` stops plugins, first flush
Recover    face artifacts, face teardown           :class:`RecoveryFacts`
Seal       credential sweeps, plugin seal          :class:`ArtifactFacts`
           (subagent enrichment), report
Decide     freeze execution facts, pick verdict    :class:`Verdict`
Commit     critical section + bookkeeping          committed or not
========== ======================================= ==========================

Decide runs inside Commit's critical section (the linearization point): the
second projector flush, the frozen execution facts, the verdict, the plugins'
settle and the durable terminal frame all happen under the run lock, together
with the cancellation flag. The ordering of :meth:`RunPipeline.close_out` is the
contract; every step before the commit is guarded on its own.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Mapping

from .. import events
from ..artifacts import ArtifactError, ArtifactMissing, ArtifactTree
from ..config import ConfigError, HostConfig
from ..egress import MODEL_ROUTE, SEARCH_ROUTE, EgressProxy
from ..kernel.agent_env import RunCredentials
from ..kernel.execution import CleanupReport, ExecutionAdapter, ExecutionSpec
from ..kernel.harness import (
    EgressEndpoint,
    FollowupUnavailable,
    HarnessFamily,
    HarnessRuntime,
    Verdict,
)
from ..kernel.sandbox import Sandbox, open_sandbox
from ..store import (
    ARTIFACTS_DIRNAME,
    CANCELLED,
    FAILED,
    FINISHED,
    REPORT_FILENAME,
    SUBAGENTS_DIRNAME,
    RunRecord,
    RunStore,
)
from . import limits
from .errors import EventLogError
from .state import (
    COMMITTED,
    LOGGER_NAME,
    UNCOMMITTED,
    ArtifactFacts,
    LaunchPlan,
    RecoveryFacts,
    ReportFacts,
    ResourceFacts,
    RunExecution,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .admission import RunAdmission
    from .channel import RunChannel
    from .plugins import RunPlugin
    from .secrets import RunSecretBoundary

logger = logging.getLogger(LOGGER_NAME)


@dataclass(frozen=True)
class StageServices:
    """What the stages share: host collaborators, never per-run state.

    The sandbox and client factories are read lazily (tests substitute the
    manager's ``_sandbox_factory``/``_client_factory`` after construction).
    """

    config: HostConfig
    store: RunStore
    bus: Any
    egress: EgressProxy
    admission: "RunAdmission"
    channel: "RunChannel"
    secrets: Callable[[RunExecution], "RunSecretBoundary"]
    plugins: Callable[[RunExecution], "tuple[RunPlugin, ...]"]
    sandbox_factory: Callable[[], Any]
    client_factory: Callable[[], Any]

    def family(self, execution: RunExecution) -> HarnessFamily:
        return self.admission.family_of(execution.record)

    def authorized_parent(self, execution: RunExecution) -> RunRecord | None:
        if not execution.parent_run_id:
            return None
        return self.admission.authorized_parent(execution.context, execution.parent_run_id)


def store_identity(
    store: RunStore, execution: RunExecution, identity: Mapping[str, str]
) -> None:
    """Write one face's durable identity into the run record.

    Called from inside the adapter as soon as the resource exists, so a failure
    in the very next step (uploading the configuration, for instance) cannot
    produce a run that holds an unreleased resource without saying which one
    it is.
    """
    changes: dict[str, Any] = {}
    if identity.get("container"):
        changes["container_name"] = identity["container"]
    if identity.get("sandbox"):
        changes["sandbox_id"] = identity["sandbox"]
    if not changes:
        return
    if all(getattr(execution.record, key) == value for key, value in changes.items()):
        return
    execution.record = store.update(execution.record.run_id, **changes)


# -- Prepare -------------------------------------------------------------------


class PrepareStage:
    """Workspace, seeded report, face, rendered runtime and the built adapter."""

    def __init__(self, services: StageServices) -> None:
        self.s = services

    def run(self, execution: RunExecution) -> LaunchPlan:
        config = self.s.config
        config.workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        execution.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.seed_report(execution)
        self.s.channel.emit_open(
            execution, events.RUN_STARTED, {"prompt": execution.record.prompt}
        )
        # The sandbox face is the run's frozen ``spec.executor``, not the host's
        # current configuration. Constructing it is side-effect free; the
        # resource is created inside ``adapter.start()``.
        sandbox = open_sandbox(
            execution.record.spec.executor,
            config=config,
            run_id=execution.record.run_id,
            workspace=execution.workspace,
            sandbox_factory=self.s.sandbox_factory(),
            on_identity=lambda identity: store_identity(self.s.store, execution, identity),
        )
        runtime = self.prepare_runtime(execution, sandbox)
        adapter = self.build_adapter(execution, runtime, sandbox)
        execution.adapter = adapter
        return LaunchPlan(sandbox=sandbox, runtime=runtime, adapter=adapter)

    def seed_report(self, execution: RunExecution) -> None:
        """Put the previous deliverable in this run's private workspace for revisions.

        The parent is owner-checked again here (it was authorized at admission):
        a report is copied only from a run this run's tenant may read, never
        from another tenant or an ownerless single-user record.
        """
        if not execution.parent_run_id:
            return
        parent = self.s.authorized_parent(execution)
        location = self.s.store.report_location(parent) if parent else None
        if location is None:
            return
        try:
            with ArtifactTree(location.root) as source:
                content = source.read(location.relative, max_bytes=limits.REPORT_MAX_BYTES)
            with ArtifactTree(execution.workspace) as destination:
                destination.replace(REPORT_FILENAME, content, max_bytes=limits.REPORT_MAX_BYTES)
            execution.seeded_report_digest = hashlib.sha256(content).hexdigest()
        except (ArtifactError, OSError) as exc:
            raise FollowupUnavailable("report_unavailable") from exc

    def prepare_runtime(self, execution: RunExecution, sandbox: Sandbox) -> HarnessRuntime:
        """Render the run's own per-harness configuration and session directory.

        The render itself is the family's (:meth:`HarnessFamily.prepare_runtime`):
        pi gets one runtime directory per run plus its session subdirectory, ACP
        gets its own agent state root (the agent's ``HOME``) with the parent's
        state inherited on a follow-up. The stage keeps the orchestration: the
        parent's re-authorization (a parent that disappeared or became
        unreadable to this tenant between admission and here is never used as a
        resume source), merging the render's newly learned secrets into the
        run's boundary, binding the runtime's declared session/state roots to
        the boundary for Seal, and only then the projector — so every secret
        (ACP ``runtime.secrets``, the pi run's egress token) is in the boundary
        before the projector is built.

        A family that ``uses_egress`` never receives the run's real
        credentials: the stage issues the run's egress grant (the proxy keeps
        the keys), the boundary holds it (its token joins the redactor), and
        the family gets only the proxy URLs as the sandbox reaches them plus
        that token (docs/adr/0008). The grant is held first, so a refusal below
        (E2B without a public URL) still revokes it at the verdict commit.
        """
        run_id = execution.record.run_id
        run_dir = self.s.store.run_dir(run_id)
        parent: RunRecord | None = None
        if execution.parent_run_id is not None:
            parent = self.s.authorized_parent(execution)
            if parent is None:
                raise FollowupUnavailable("session_unavailable")
        family = self.s.family(execution)
        credentials = execution.credentials or RunCredentials.from_host(self.s.config)
        secrets = self.s.secrets(execution)
        egress = (
            self.egress_endpoint(execution, credentials, sandbox, secrets)
            if family.uses_egress
            else None
        )
        prepared = family.prepare_runtime(
            run_id,
            run_dir,
            parent=parent,
            session_seed=execution.session_seed,
            egress=egress,
            sandbox=sandbox,
        )
        execution.runtime = prepared.runtime
        secrets.bind_runtime(prepared.runtime)
        if prepared.seed_consumed:
            execution.session_seed = None
        for value in prepared.secret_values:
            secrets.learn(value)
        execution.projector = family.make_projector(secrets.redactor)
        return prepared.runtime

    def egress_endpoint(
        self,
        execution: RunExecution,
        credentials: RunCredentials,
        sandbox: Sandbox,
        secrets: "RunSecretBoundary",
    ) -> EgressEndpoint:
        """Issue the run's egress grant and describe it as the sandbox sees it."""
        context = execution.context
        egress = self.s.egress
        grant = egress.issue(
            run_id=execution.record.run_id,
            tenant=context.tenant_id if context is not None else None,
            model_key=credentials.model_api_key,
            search_key=credentials.web_search_key,
        )
        secrets.hold_grant(grant)
        port = egress.port
        if port is None:  # pragma: no cover - issue() binds the listener
            raise ConfigError("credential egress proxy is not listening")
        origin = sandbox.egress_origin(port).rstrip("/")
        return EgressEndpoint(
            model_base_url=f"{origin}/{MODEL_ROUTE}",
            token=grant.token,
            search_base_url=f"{origin}/{SEARCH_ROUTE}" if grant.has_search else None,
        )

    def build_adapter(
        self, execution: RunExecution, runtime: HarnessRuntime, sandbox: Sandbox
    ) -> ExecutionAdapter:
        config = self.s.config
        parent = self.s.authorized_parent(execution)
        if execution.parent_run_id and parent is None:
            # The parent disappeared or is no longer readable by this run's
            # tenant: never start a kernel with an unverified resume target.
            raise FollowupUnavailable("session_unavailable")
        planned = sandbox.planned_identity()
        if planned:
            # An identity fixed before the resource exists (the Docker
            # container name derives from the run id): persist it now so a
            # failure inside the adapter's start() cannot leave an unnamed
            # resource behind. 两族同行为，是执行面事实，由 sandbox 给出。
            store_identity(self.s.store, execution, planned)
        family = self.s.family(execution)
        system_prompt = self.system_prompt_text(execution)
        spec = ExecutionSpec(
            config=config,
            runtime=runtime,
            run_id=execution.record.run_id,
            prompt=execution.kernel_prompt or execution.record.prompt,
            system_prompt=system_prompt,
            workspace=execution.workspace,
            timeout_seconds=config.task_timeout_seconds,
            extra_argv=tuple(self.extra_argv(execution, system_prompt)),
            # The continuation value is harness-shaped: pi resumes from the
            # sanitized session file copied into the run's session directory,
            # while the ACP family resumes the **session id** the parent
            # recorded, against the state root inherited in prepare_runtime.
            resume_session=family.resume_value(parent),
            expected_session_id=parent.session_id if parent else None,
        )
        return family.build_adapter(
            spec,
            sandbox,
            on_frame=lambda frame: self.s.channel.on_frame(execution, frame),
            client_factory=self.s.client_factory(),
        )

    @staticmethod
    def extra_argv(execution: RunExecution, system_prompt: str) -> list[str]:
        extra = ["--name", f"pi-glink-{execution.record.run_id}"]
        if system_prompt:
            extra.extend(["--append-system-prompt", system_prompt])
        return extra

    def system_prompt_text(self, execution: RunExecution) -> str:
        """The system prompt one run's kernel receives, on every face.

        All three layers, in one fixed order: the working agreement
        (``methodology/work-agreement.md``), the selected pack's capability
        prompt (``harnesses/<pack>/capabilities.md``, when it has one) and, when
        the run was admitted with one, the rendered session blueprint. pi gets
        the same text through ``--append-system-prompt``; the ACP family gets it
        as the leading text block of ``session/prompt``. The user's own prompt is
        a different channel and is never part of this text.
        """
        parts = [
            self._research_prompt_text(),
            self.s.config.pack.prompt,
            execution.blueprint_prompt,
        ]
        return "\n\n".join(part for part in parts if part)

    def _research_prompt_text(self) -> str:
        path = self.s.config.work_agreement_path
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()


# -- Launch --------------------------------------------------------------------


class LaunchStage:
    """Start the face, verify the continuation, deliver the input, wait."""

    def __init__(self, services: StageServices) -> None:
        self.s = services

    def run(self, execution: RunExecution, plan: LaunchPlan) -> None:
        adapter = plan.adapter
        adapter.start()
        # Persist the container/sandbox identity as soon as it exists: without
        # it a host restart could not tell an operator which leftover resource
        # belongs to which run.
        store_identity(self.s.store, execution, adapter.identity())

        session = adapter.session_info()
        if (
            execution.parent_run_id
            and self.s.family(execution).verifies_continuation_before_turn()
        ):
            # 两族三面同规则：RPC ``get_state`` 在 session_ready 即知会话身份，
            # 续接身份不符在回合前、第一次模型调用前拒 session_unavailable
            # （docs/adr/0004）。
            parent = self.s.authorized_parent(execution)
            if not session or not parent or session.session_id != parent.session_id:
                raise FollowupUnavailable("session_unavailable")
        if session is not None:
            self.s.channel.publish_session_ready(execution, session)

        # Cancelled while the kernel was starting: never start the work.
        if not execution.cancel_requested:
            adapter.deliver_input()
            deadline = time.monotonic() + self.s.config.task_timeout_seconds
            execution.execution_facts.outcome = adapter.wait(
                cancel=lambda: execution.cancel_requested, deadline=deadline
            )


# -- Observe (finish) ----------------------------------------------------------


class ObserveStage:
    """End of observation: plugins stop, the projector's tail is released."""

    def __init__(self, services: StageServices) -> None:
        self.s = services

    def finish(self, execution: RunExecution) -> None:
        # R3a: plugin-owned frame producers die first — no transient frame may
        # race the artifact facts and the verdict, and no thread may leak into
        # the next run.
        for plugin in self.s.plugins(execution):
            plugin.close(execution)
        if execution.projector is not None:
            # Release the answer's withheld tail now, so it stays attached to
            # its text block ahead of any host-owned notice emitted below.
            with execution.lock:
                self.s.channel.flush(execution)


# -- Recover -------------------------------------------------------------------


class RecoverStage:
    """Pull work products off the face, then tear the face down."""

    def __init__(self, services: StageServices) -> None:
        self.s = services

    def run(self, execution: RunExecution, adapter: ExecutionAdapter | None) -> RecoveryFacts:
        failures = self.face_artifacts(execution, adapter)
        cleanup = self.close_face(execution, adapter)
        return RecoveryFacts(
            artifact_failures=tuple(failures), resources=ResourceFacts(cleanup=cleanup)
        )

    def face_artifacts(
        self, execution: RunExecution, adapter: ExecutionAdapter | None
    ) -> tuple[str, ...]:
        """Recover work products (and what they teach) before teardown."""
        if adapter is None:
            return ()
        failures: list[str] = []
        # Recover work products before the face is torn down: the sandbox
        # cannot be read after it is destroyed.
        try:
            recovery = adapter.recover_artifacts(execution.workspace)
            failures.extend(recovery.failures)
            # Values the host only learned from recovered material (the E2B
            # face's state root) join this run's boundary exactly like an
            # injected credential: a later echo is masked, and the values are
            # never written anywhere else.
            secrets = self.s.secrets(execution)
            for value in recovery.secrets:
                secrets.learn(value)
            # A sandbox face can learn session facts only while pulling the
            # workspace back (the pi face's session file travels with the
            # recovery; the ACP face's state-root verdict is decided there), so
            # the adapter answers what is worth republishing; every other face
            # answers ``None`` (ABC 默认空实现).
            recovered_session = adapter.recovered_session()
            if recovered_session is not None:
                self.s.channel.publish_session_ready(execution, recovered_session)
            # Subagent side-products die with the sandbox: the face that has
            # any (pi E2B) mirrors them under the run directory while the
            # sandbox still exists; faces whose subagent files already live on
            # the host recover nothing (ABC 默认空实现).
            subagent_recovery = adapter.recover_subagents(
                self.s.store.run_dir(execution.record.run_id) / SUBAGENTS_DIRNAME
            )
            failures.extend(subagent_recovery.failures)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            failures.append(f"产物回收失败: {type(exc).__name__}: {exc}")
        return tuple(failures)

    def close_face(
        self, execution: RunExecution, adapter: ExecutionAdapter | None
    ) -> CleanupReport:
        """Tear the face down into a CleanupReport (synthesized on failure)."""
        if adapter is None:
            # Nothing was acquired: no process, container or sandbox exists.
            return CleanupReport(released=True)
        try:
            return adapter.close()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            return CleanupReport(
                released=False,
                failures=(f"关闭执行面失败: {type(exc).__name__}: {exc}",),
                external=dict(adapter.identity()),
            )


# -- Seal ----------------------------------------------------------------------


class SealStage:
    """Credential sweeps, plugin seal (subagent enrichment), report recovery.

    The only stage that uses the boundary's seal (rewriting or deleting files
    the run left behind); it runs after the face is closed and before the
    verdict, so the report the verdict reads is already the redacted copy.
    """

    def __init__(self, services: StageServices) -> None:
        self.s = services

    def run(self, execution: RunExecution, recovery: RecoveryFacts) -> ArtifactFacts:
        failures = list(recovery.artifact_failures)
        redacted = self.sweep(execution, failures)
        if self.enrich(execution, failures):
            redacted = True
        try:
            report = self.report(execution)
        except Exception:  # noqa: BLE001 - reported as a rejected artifact
            logger.exception("report recovery failed run=%s", execution.record.run_id)
            report = ReportFacts(status="rejected", path=None)
        return ArtifactFacts(
            redacted=redacted or report.redacted,
            report_path=report.path,
            report_status=report.status,
            artifact_failures=tuple(failures),
        )

    def sweep(self, execution: RunExecution, failures: list[str]) -> bool:
        """The three sweeps: workspace, subagent mirror, session/state roots."""
        secrets = self.s.secrets(execution)
        redacted = secrets.seal_trees().redacted
        try:
            record = self.s.store.get(execution.record.run_id)
            session_file = record.session_file if record is not None else None
            sealed = secrets.seal_sessions(session_file)
            failures.extend(sealed.failures)
            if sealed.redacted:
                redacted = True
        except Exception:  # noqa: BLE001
            logger.exception("session credential sweep failed run=%s", execution.record.run_id)
        return redacted

    def enrich(self, execution: RunExecution, failures: list[str]) -> bool:
        """Each plugin's seal hook (the subagent enrichment); best-effort."""
        secrets = self.s.secrets(execution)
        redacted = False
        for plugin in self.s.plugins(execution):
            if plugin.seal(execution, secrets, failures):
                redacted = True
        return redacted

    def report(self, execution: RunExecution) -> ReportFacts:
        """Validate the report and copy it to a host-owned, redacted artifact.

        The kernel-produced file is never served directly and never written
        through: it is read through the run workspace's directory handle (no
        link anywhere below the workspace root, regular file only, bounded),
        redacted, and published under ``<run_dir>/artifacts/report.md``. A file
        that cannot be validated is a visible ``report_rejected`` with a warning
        event — not a fake report.
        """
        run_id = execution.record.run_id
        try:
            with ArtifactTree(execution.workspace) as workspace:
                data = workspace.read(REPORT_FILENAME, max_bytes=limits.REPORT_MAX_BYTES)
        except ArtifactMissing:
            return ReportFacts(status="missing")
        except ArtifactError as exc:
            self._warn(execution, f"report.md 未通过安全校验，已拒绝：{exc}")
            return ReportFacts(status="rejected")
        if (
            execution.seeded_report_digest is not None
            and execution.seeded_report_digest == hashlib.sha256(data).hexdigest()
        ):
            # The previous deliverable is context, not a newly generated file.
            return ReportFacts(status="missing")
        original = data.decode("utf-8", errors="replace")
        redacted = self.s.secrets(execution).redact(original)
        changed = redacted != original
        try:
            with ArtifactTree(self.s.store.run_dir(run_id)) as run_tree:
                run_tree.replace(
                    f"{ARTIFACTS_DIRNAME}/{REPORT_FILENAME}",
                    redacted.encode("utf-8"),
                    max_bytes=limits.REPORT_MAX_BYTES,
                    create_parents=True,
                )
        except (ArtifactError, OSError) as exc:
            self._warn(execution, f"report.md 无法写入宿主产物目录，已拒绝：{exc}")
            return ReportFacts(status="rejected", redacted=changed)
        return ReportFacts(
            status="ok", path=self.s.store.report_artifact_path(run_id), redacted=changed
        )

    def _warn(self, execution: RunExecution, detail: str) -> None:
        self.s.channel.warn(execution, "report_rejected", detail, label="report")


# -- Decide --------------------------------------------------------------------


class DecideStage:
    """Combine every fact into one verdict (called under the run lock)."""

    def __init__(self, services: StageServices) -> None:
        self.s = services

    def run(self, execution: RunExecution) -> Verdict:
        """Freeze the execution facts and decide.

        An accepted cancellation wins: it was accepted while the run was open,
        so it must be the verdict. ``agent_settled`` is deliberately not the
        success signal: pi can settle after an error stop reason or a truncated
        (``length``) response. The product terminal vocabulary (status from the
        store's FINISHED/FAILED/CANCELLED, the ``stop_reason`` string set) is
        defined here; the family only translates its own stop vocabulary into
        it (:meth:`HarnessFamily.model_stop_verdict`).
        """
        facts = execution.execution_facts.freeze()
        artifacts = execution.artifact_facts or ArtifactFacts()
        log_error = facts.event_log_error
        if execution.cancel_requested:
            return Verdict(CANCELLED, "abort", log_error)
        if log_error:
            return Verdict(FAILED, "event_log_failed", log_error)
        if facts.failure:
            return Verdict(FAILED, "execution_error", facts.failure)
        outcome = facts.outcome
        if outcome is None:
            return Verdict(FAILED, "execution_error", "执行面没有给出结果")
        if outcome.cancelled:
            return Verdict(CANCELLED, "abort", None)
        if outcome.timed_out:
            return Verdict(FAILED, "timeout", outcome.error or "run timed out")
        if outcome.error:
            return Verdict(FAILED, outcome.stop_reason or "execution_error", outcome.error)
        # 族把 harness 自己的 stop 词汇（pi 的 error/aborted/length，ACP 的
        # StopReason 集合）译成产品终态词汇；``None`` 落入下方的交付策略
        # （报告检查）。映射逐字节不变式见契约 §4 与族实现。
        verdict = self.s.family(execution).model_stop_verdict(
            facts.model_stop_reason, model_error=facts.model_error
        )
        if verdict is not None:
            return verdict
        if artifacts.report_path is None:
            reason = "report_rejected" if artifacts.report_status == "rejected" else "no_report"
            return Verdict(FINISHED, reason, None)
        return Verdict(FINISHED, None, None)


# -- Commit --------------------------------------------------------------------


class CommitStage:
    """The linearization point and the bookkeeping after it."""

    def __init__(
        self,
        services: StageServices,
        decide: DecideStage,
        *,
        on_committed: Callable[[str], None],
    ) -> None:
        self.s = services
        self.decide = decide
        self._on_committed = on_committed

    def run(self, execution: RunExecution) -> bool:
        """Decide and durably commit the verdict, then the bookkeeping."""
        run_id = execution.record.run_id
        committed = False
        with execution.lock:
            # Frames may have arrived while artifacts were recovered; their
            # withheld tail still belongs to the open run, before the verdict.
            self.s.channel.flush(execution)
            verdict = self.decide.run(execution)
            execution.verdict = verdict
            # Plugins settle on the decided verdict — children that never
            # closed themselves follow the run's terminal state in their
            # documents (§3.4/§3.5) — before the terminal frame summarizes them.
            for plugin in self.s.plugins(execution):
                plugin.settle(execution, verdict)
            try:
                committed = self.commit_terminal(execution, verdict)
            except Exception:  # noqa: BLE001 - an unexpected error is an uncommitted verdict
                logger.exception("terminal commit failed run=%s", run_id)
            execution.phase = COMMITTED if committed else UNCOMMITTED

        try:
            if committed:
                try:
                    self.s.store.settle(run_id)
                except Exception:  # noqa: BLE001 - readers project the committed frame
                    logger.exception("settling the run record failed run=%s", run_id)
                self.s.bus.close(run_id)
                self._on_committed(run_id)
            else:
                # Every reader still sees ``running`` (nothing is committed), so
                # the run keeps its slot and its live stream; a restart
                # reconciles it.
                logger.critical(
                    "run 终态未能提交，保持占用直至重启对账 run=%s status=%s",
                    run_id,
                    verdict.status,
                )
        finally:
            # The kernel is gone by now: the run's token stops working whether
            # or not the verdict was committed (an uncommitted run never runs
            # again; a restart reconciles it without a kernel). It runs after
            # the record is settled because stopping the last grant's listener
            # is synchronous (up to one serve_forever poll) and must not delay
            # the settled record.
            self.s.secrets(execution).release_grant(self.s.egress)
        return committed

    def commit_terminal(self, execution: RunExecution, verdict: Verdict) -> bool:
        """Durably append the terminal frame; True once it is committed."""
        run_id = execution.record.run_id
        current = self.s.store.get(run_id) or execution.record
        secrets = self.s.secrets(execution)
        facts = execution.execution_facts
        artifacts = execution.artifact_facts or ArtifactFacts()
        resources = execution.resource_facts
        cleanup = resources.cleanup if resources is not None else CleanupReport(released=True)
        grant = secrets.egress_grant
        terminal_type = {
            FAILED: events.RUN_FAILED,
            CANCELLED: events.RUN_CANCELLED,
        }.get(verdict.status, events.RUN_FINISHED)
        payload = {
            "status": verdict.status,
            "stopReason": verdict.stop_reason,
            "error": secrets.redact(verdict.error) if verdict.error else None,
            "reportPath": str(artifacts.report_path) if artifacts.report_path else None,
            "reportStatus": artifacts.report_status,
            "modelStopReason": facts.model_stop_reason,
            "sessionId": current.session_id,
            "sessionFile": current.session_file,
            # The egress proxy's metered total is the authoritative usage of a
            # run whose calls went through it (docs/adr/0008); the kernel's own
            # reports stay as a separate, non-authoritative field.
            "usage": grant.usage() if grant is not None else facts.usage_total or None,
            "kernelUsage": (facts.usage_total or None) if grant is not None else None,
            "redacted": artifacts.redacted,
            "durationSeconds": round(time.time() - current.created_at, 3),
            "cleanup": {
                "released": cleanup.released,
                "failures": list(cleanup.failures),
                "external": dict(cleanup.external),
            },
            "artifactFailures": list(artifacts.artifact_failures),
            # The committed documents are the fact source for replay rendering
            # (§3.5); a read failure degrades to an empty summary (with a log
            # line), never to a blocked verdict.
            "children": self.s.store.children_summary(run_id),
        }
        try:
            self.s.channel.emit(execution, terminal_type, payload, terminal=True)
        except EventLogError as exc:
            logger.error("terminal frame could not be committed run=%s: %s", run_id, exc)
            return False
        return True
