"""Per-run state and the facts the lifecycle stages hand to each other.

The run pipeline (:mod:`pi_glink.runs.pipeline`) moves one admitted run through
fixed stages. Each stage reads the frozen output of the stages before it and
produces its own frozen fact; the only mutable pieces are the ones that must
stay open while the kernel runs:

* :class:`LiveExecutionFacts` — what the adapter and the model report. Frames
  keep arriving until the verdict's critical section, so the collector stays
  open until :class:`~pi_glink.runs.stages.DecideStage` freezes it into
  :class:`ExecutionFacts` at the linearization point.
* :class:`RunExecution` — the run's holder (lock, phase, cancellation flag,
  adapter, runtime, projector, plugins). It is the handle ``stop`` and the
  frame path share with the executor thread.

Everything else — :class:`RecoveryFacts` (Recover), :class:`ArtifactFacts`
(Seal), :class:`ResourceFacts` (Recover's teardown) and the
:class:`~pi_glink.contracts.Verdict` (Decide) — is immutable once built.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from ..blueprints import Blueprint
from ..contracts import Verdict
from ..kernel.agent_env import RunCredentials
from ..kernel.execution import CleanupReport, ExecutionAdapter, ExecutionOutcome
from ..kernel.harness import FrameProjector, HarnessRuntime
from ..store import RunRecord
from ..tenancy import TenantContext

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .plugins import RunPlugin
    from .secrets import RunSecretBoundary

#: One log channel for the whole run lifecycle: operators (and the tests that
#: capture it) filter on this name whichever stage module wrote the line.
LOGGER_NAME = "pi_glink.runs.runner"

#: Run phases. ``open`` accepts frames and cancellation; the verdict commit
#: moves it to ``committed``, or to ``uncommitted`` when the durable terminal
#: append failed (the run then keeps its slot until a restart reconciles it).
OPEN = "open"
COMMITTED = "committed"
UNCOMMITTED = "uncommitted"


@dataclass(frozen=True, slots=True)
class StopDecision:
    """The answer to a stop request, decided at the run's linearization point.

    ``accepted`` means the cancellation will be the run's verdict. Otherwise
    ``reason`` says why not: ``terminal`` (a verdict is committed; ``status``
    is it), ``finalizing`` (the verdict is decided but its commit failed),
    ``not_owned`` (the record is not executed by this process) or ``unknown``.
    """

    accepted: bool
    status: str | None
    reason: str


@dataclass(frozen=True, slots=True)
class ExecutionFacts:
    """Execution facts, frozen at the linearization point (Decide's input)."""

    outcome: ExecutionOutcome | None = None
    failure: str | None = None
    event_log_error: str | None = None
    model_stop_reason: str | None = None
    model_error: str | None = None
    usage_total: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class LiveExecutionFacts:
    """Execution facts while the run is *open*: what the adapter and model report.

    Written by the executor thread (outcome/failure) and by the frame path under
    the run lock (model stop reason, usage, event-log health). A fact never
    overwrites an artifact or resource fact; :meth:`freeze` is called once, in
    the verdict's critical section.
    """

    outcome: ExecutionOutcome | None = None
    failure: str | None = None
    event_log_error: str | None = None
    model_stop_reason: str | None = None
    model_error: str | None = None
    usage_total: dict[str, Any] = field(default_factory=dict)
    usage_keys: set[str] = field(default_factory=set)

    def freeze(self) -> ExecutionFacts:
        return ExecutionFacts(
            outcome=self.outcome,
            failure=self.failure,
            event_log_error=self.event_log_error,
            model_stop_reason=self.model_stop_reason,
            model_error=self.model_error,
            usage_total=dict(self.usage_total),
        )


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    """Prepare's output: the face, the rendered runtime and the built adapter."""

    sandbox: Any
    runtime: HarnessRuntime
    adapter: ExecutionAdapter


@dataclass(frozen=True, slots=True)
class ResourceFacts:
    """Resource facts: the adapter's ``CleanupReport`` (released or unknown)."""

    cleanup: CleanupReport


@dataclass(frozen=True, slots=True)
class RecoveryFacts:
    """Recover's output: what the face gave back before and during teardown."""

    artifact_failures: tuple[str, ...]
    resources: ResourceFacts


@dataclass(frozen=True, slots=True)
class ReportFacts:
    """The report recovery's answer (``none``/``ok``/``missing``/``rejected``)."""

    status: str = "none"
    path: Path | None = None
    redacted: bool = False


@dataclass(frozen=True, slots=True)
class ArtifactFacts:
    """Seal's output: the recovered, validated, redacted artifacts.

    ``report_status`` (``none``/``ok``/``missing``/``rejected``) is the single
    reason vocabulary the verdict reads; the path is host-owned.
    """

    redacted: bool = False
    report_path: Path | None = None
    report_status: str = "none"
    artifact_failures: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AdmittedRun:
    """Admit's output: everything decided before the execution thread starts."""

    record: RunRecord
    workspace: Path
    #: The instruction as the user wrote it. ``record.prompt`` is the redacted
    #: copy that may be persisted and shown; the kernel receives this one.
    kernel_prompt: str
    context: TenantContext | None
    credentials: RunCredentials
    #: The run's secret boundary (host secrets + this run's credentials).
    secrets: "RunSecretBoundary"
    #: Snapshot of the sanitized previous session. Never a mutable session file.
    session_seed: bytes | None
    parent_run_id: str | None
    blueprint: Blueprint | None
    blueprint_prompt: str


@dataclass
class RunExecution:
    """Mutable per-run holder from admission until the verdict is committed."""

    record: RunRecord
    workspace: Path
    kernel_prompt: str = ""
    #: Verified tenant identity (multi-tenant mode) or ``None`` (local mode).
    context: TenantContext | None = None
    #: The credentials this run may render — its own tenant's in multi-tenant
    #: mode, the host's frozen configuration in local mode.
    credentials: RunCredentials | None = None
    #: This run's secret boundary (:class:`~pi_glink.runs.secrets.RunSecretBoundary`):
    #: its redactor, egress token and sweep roots. One tenant's secret is never
    #: used to redact (or leaked into) another tenant's events, logs or files.
    secrets: "RunSecretBoundary | None" = None
    session_seed: bytes | None = None
    parent_run_id: str | None = None
    #: The parsed session blueprint (``None`` for a plain run) and its rendered
    #: system-prompt text. Both are decided at admission; the host never
    #: advances a stage.
    blueprint: Blueprint | None = None
    blueprint_prompt: str = ""
    seeded_report_digest: str | None = None
    #: Guards the phase, the cancellation flag and every frame commit.
    lock: threading.RLock = field(default_factory=threading.RLock)
    phase: str = OPEN
    cancel_requested: bool = False
    adapter: ExecutionAdapter | None = None
    runtime: HarnessRuntime | None = None
    projector: FrameProjector | None = None
    #: The harness-provided lifecycle plugins (built once, under the run lock).
    plugins: "tuple[RunPlugin, ...] | None" = None
    #: Set once the pipeline finished (committed or not).
    finalized: threading.Event = field(default_factory=threading.Event)
    verdict: Verdict | None = None
    #: Execution facts stay open until Decide freezes them; artifact and
    #: resource facts are the frozen outputs of Seal and Recover.
    execution_facts: LiveExecutionFacts = field(default_factory=LiveExecutionFacts)
    artifact_facts: ArtifactFacts | None = None
    resource_facts: ResourceFacts | None = None

    @classmethod
    def from_admitted(cls, admitted: AdmittedRun) -> "RunExecution":
        return cls(
            record=admitted.record,
            workspace=admitted.workspace,
            kernel_prompt=admitted.kernel_prompt,
            context=admitted.context,
            credentials=admitted.credentials,
            secrets=admitted.secrets,
            session_seed=admitted.session_seed,
            parent_run_id=admitted.parent_run_id,
            blueprint=admitted.blueprint,
            blueprint_prompt=admitted.blueprint_prompt,
        )
