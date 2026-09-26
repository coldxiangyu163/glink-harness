"""Run lifecycle for the local execution faces.

One run is one workspace plus one kernel process/sandbox plus one session file.
The manager keeps four facts apart, each with one owner and one commit point:

* **Execution facts** — what the adapter and the model reported (outcome,
  model stop reason, event-log health). Collected while the run is *open*.
* **Artifact facts** — the recovered, validated, redacted report copy
  (``report_status``/``report_path``). Written before the verdict.
* **The verdict** — ``finished``/``failed``/``cancelled`` with a reason. It is
  decided and committed in one critical section under the run lock: the
  verdict is computed from all facts *and* the cancellation flag at that
  moment, then committed as the durable terminal frame (see
  :mod:`pi_glink.store`). This is the run's linearization point. A stop
  accepted before it is reflected in the verdict; a stop after it is refused
  and reports the committed status. Frames that arrive after it are dropped.
* **Resource release** — the adapter's ``CleanupReport``. A run whose cleanup
  did not confirm ``released: true`` keeps holding capacity after its verdict
  (durably, across restarts) until an operator adjudicates it; finishing the
  execution thread never implies the resource is gone.

Admission is derived from those facts: a new run is accepted only when no run
is executing in this process and no run holds unconfirmed resources.

Restart reconciliation rewrites runs left ``running`` only after the host proved
exclusive ownership of the data root (:mod:`pi_glink.ownership`); a committed
terminal frame whose settled copy is missing is settled, never overwritten.
Restart reconciliation, operator adjudication and the online identity-probe
sweep are implemented by the resource-ledger collaborator
(:mod:`pi_glink.kernel.resource_ledger`); the manager keeps thin forwards under
its existing names.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .. import events
from ..artifacts import ArtifactError, ArtifactMissing, ArtifactTree
from ..blueprints import (
    Blueprint,
    BlueprintError,
    blueprint_names,
    load_blueprint,
    render_blueprint,
)
from ..config import ConfigError, HostConfig
from ..ownership import DataRootOwnership
from ..redaction import SecretRedactor, redactor_from_config
from ..store import (
    ARTIFACTS_DIRNAME,
    CANCELLED,
    FAILED,
    FINISHED,
    REPORT_FILENAME,
    RUNNING,
    RunRecord,
    RunStore,
)
from .acp_e2b_env import E2BAcpSandboxClient
from .acp_execution import acp_resume_method
from .acp_env import (
    ACP_HOME_DIRNAME,
    ACP_RECOVERED_DIRNAME,
    ACP_RUNTIME_DIRNAME,
    AcpRuntime,
    AcpStateInheritError,
    build_acp_argv,
    build_acp_env,
    prepare_acp_runtime,
)
from .acp_projection import ACP_STOP_REASONS, AcpProjector
from .agent_env import (
    AgentRuntime,
    RunCredentials,
    build_pi_argv,
    build_pi_env,
    prepare_agent_dir,
    prompt_template_names,
)
from .docker_env import (
    build_acp_plan as build_acp_docker_plan,
    build_plan as build_docker_plan,
    container_name_for as docker_container_name,
    host_session_path as docker_host_session_path,
    is_available as docker_available,
    stop_argv as docker_stop_argv,
)
from .execution import (
    CleanupReport,
    ExecutionAdapter,
    ExecutionOutcome,
    ExecutionSpec,
    SessionInfo,
    build_adapter,
)
from .projection import Projector
# ``RunBusy`` is defined next to the ledger because both the admission gate and
# the ledger's adjudication raise it; re-exported here for existing importers.
from .resource_ledger import ResourceLedger, RunBusy
from .rpc import PiRpcClient
from ..tenancy import (
    TenantContext,
    TenantCredentialStore,
    TenantNotConfigured,
)

logger = logging.getLogger("pi_glink.kernel.runner")

#: Per-run pi configuration directory inside the run directory.
RUNTIME_DIRNAME = "pi-agent"
#: System prompt appended to every run (pi's own prompt stays untouched).
RESEARCH_PROMPT_FILENAME = "research-prompt.md"
#: Ceiling for artifacts the host reads back and serves.
REPORT_MAX_BYTES = 32 * 1024 * 1024
#: Ceiling for one file touched by the workspace credential sweep.
SWEEP_MAX_BYTES = 2 * 1024 * 1024
#: Bound on how many files one sweep may visit (a runaway workspace must not
#: turn run teardown into an unbounded walk).
SWEEP_MAX_FILES = 2000
#: Bound on how many committed frames of a parent's log the ACP admission gate
#: scans for the recorded ``session_ready`` capability matrix. The frame is
#: published right after a run opens, so the scan is independent of how long the
#: parent's transcript grew afterwards.
_ACP_CAPABILITY_SCAN_EVENTS = 50
_USAGE_FIELDS = ("input", "output", "cacheRead", "cacheWrite", "totalTokens")

#: Run phases. ``open`` accepts frames and cancellation; the verdict commit
#: moves it to ``committed``, or to ``uncommitted`` when the durable terminal
#: append failed (the run then keeps its slot until a restart reconciles it).
_OPEN = "open"
_COMMITTED = "committed"
_UNCOMMITTED = "uncommitted"


class ResourcesUnreleased(RuntimeError):
    """Raised when earlier runs still hold resources that are not confirmed released."""

    def __init__(self, run_ids: list[str]) -> None:
        super().__init__("执行资源尚未确认释放: " + ", ".join(run_ids))
        self.run_ids = list(run_ids)


class RunQuotaExceeded(RuntimeError):
    """Raised when admitting a run would exceed a configured concurrency ceiling.

    ``scope`` names which ceiling was hit (``global`` or ``tenant``). The HTTP
    layer answers a 429 with that scope and limit, never with another tenant's
    run id: an over-quota refusal must not disclose who else is running.
    """

    def __init__(self, scope: str, limit: int) -> None:
        super().__init__(f"{scope} 并发上限 {limit} 已达")
        self.scope = scope
        self.limit = limit


class FollowupUnavailable(RuntimeError):
    """A parent run cannot safely supply a resumable session.

    ``code`` is the refusal an HTTP caller sees (``unknown_run``, ``stale_run``,
    ``run_busy``, ``session_unavailable``, ``report_unavailable``).
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class UnsupportedCommand(RuntimeError):
    """The prompt names a ``/`` command outside the workbench's allowlist.

    pi dispatches extension commands and expands prompt templates for a message
    that starts with ``/`` (docs/rpc-commands.md). Once pi-subagents is loaded,
    commands such as ``/run --bg`` or ``/subagents-detach`` would start a
    detached run that no longer follows this run's cancel/timeout/reclaim path,
    and the entry is client-controlled input. Admission therefore accepts only
    the prompt-template names rendered from ``agent/prompts/``.

    ``allowed`` carries only the accepted names; the refused command itself is
    never echoed back to the caller (the HTTP layer reports the allowlist at
    most), so the refusal does not turn the endpoint into a command oracle.
    """

    def __init__(self, allowed: Sequence[str]) -> None:
        self.allowed = tuple(sorted(allowed))
        super().__init__("prompt names an unsupported / command")


def prompt_names_unsupported_command(text: str, allowed: Sequence[str]) -> bool:
    """Whether ``text`` opens with a ``/`` command outside ``allowed``.

    The prompt is checked the way pi reads it: leading whitespace does not make
    ``/run`` less of a command, the name is the first whitespace-delimited token
    (a bare ``/`` has no name and is refused), and the comparison is exact
    because pi's template match is case-sensitive. Text that does not start with
    ``/`` is never a command — a ``/run`` in the middle of a question is ordinary
    prose and stays acceptable.
    """
    stripped = text.lstrip()
    if not stripped.startswith("/"):
        return False
    match = re.match(r"^/(\S+)", stripped)
    name = match.group(1) if match is not None else ""
    return name not in allowed


class BlueprintUnavailable(RuntimeError):
    """The requested session blueprint cannot be used for this run.

    ``code`` is ``unknown_blueprint`` (no such template in the blueprint
    directory) or ``invalid_blueprint`` (present but not a usable document).
    ``available`` is the derived name list, which the HTTP layer may report back
    — it is reviewed repository content, not client input. The refused value
    itself is never echoed, so the endpoint is not an oracle for arbitrary
    strings.
    """

    def __init__(self, code: str, available: Sequence[str]) -> None:
        self.code = code
        self.available = tuple(sorted(available))
        super().__init__("session blueprint is unavailable")


class EventLogError(RuntimeError):
    """The durable event log could not be written; the frame must stay hidden."""


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
class _Verdict:
    status: str
    stop_reason: str | None
    error: str | None


@dataclass
class _Execution:
    """Mutable per-run state held from admission until the verdict is committed."""

    record: RunRecord
    workspace: Path
    #: The instruction as the user wrote it. ``record.prompt`` is the redacted
    #: copy that may be persisted and shown; the kernel receives this one.
    kernel_prompt: str = ""
    #: Verified tenant identity (multi-tenant mode) or ``None`` (local mode).
    context: TenantContext | None = None
    #: The credentials this run may render — its own tenant's in multi-tenant
    #: mode, the host's frozen configuration in local mode.
    credentials: RunCredentials | None = None
    #: This run's redaction boundary. It contains the host's known secrets plus
    #: this run's credentials only, so one tenant's secret is never used to
    #: redact (or leaked into) another tenant's events, logs or artifacts.
    redactor: SecretRedactor | None = None
    #: Snapshot of the sanitized previous session. Never share a mutable session file.
    session_seed: bytes | None = None
    parent_run_id: str | None = None
    #: The parsed session blueprint this run was admitted with (``None`` for a
    #: plain run) and its rendered system-prompt text. Both are decided at
    #: admission; the host never advances a stage.
    blueprint: Blueprint | None = None
    blueprint_prompt: str = ""
    seeded_report_digest: str | None = None
    #: Guards the phase, the cancellation flag and every frame commit.
    lock: threading.RLock = field(default_factory=threading.RLock)
    phase: str = _OPEN
    cancel_requested: bool = False
    adapter: ExecutionAdapter | None = None
    runtime: AgentRuntime | AcpRuntime | None = None
    projector: Projector | None = None
    #: Set once the finalizer finished (committed or not).
    finalized: threading.Event = field(default_factory=threading.Event)
    verdict: _Verdict | None = None
    # execution facts
    outcome: ExecutionOutcome | None = None
    failure: str | None = None
    event_log_error: str | None = None
    model_stop_reason: str | None = None
    model_error: str | None = None
    usage_total: dict[str, Any] = field(default_factory=dict)
    usage_keys: set[str] = field(default_factory=set)
    # artifact facts
    redacted: bool = False
    report_path: Path | None = None
    report_status: str = "none"
    artifact_failures: list[str] = field(default_factory=list)
    # resource facts
    cleanup: CleanupReport | None = None


ClientFactory = Callable[..., PiRpcClient]


class RunManager:
    """Own the lifecycle of local runs."""

    def __init__(
        self,
        config: HostConfig,
        bus: Any,
        store: RunStore,
        *,
        client_factory: ClientFactory = PiRpcClient,
        sandbox_factory: Callable[..., Any] | None = None,
        ownership: DataRootOwnership | None = None,
        credential_store: TenantCredentialStore | None = None,
        docker_probe: Callable[[str], tuple[bool, str]] | None = None,
        sandbox_probe: Callable[[str], tuple[bool, str]] | None = None,
    ) -> None:
        self.config = config
        self.bus = bus
        self.store = store
        self._client_factory = client_factory
        self._sandbox_factory = sandbox_factory
        self._credential_store = credential_store
        # The host-level redaction boundary: it carries the host's own secrets
        # (global model/search keys, sandbox key, the operator's own search
        # file). Each run derives its own copy and adds only its own credentials
        # to it, so tenant secrets stay apart. Reading the operator file here —
        # rather than while rendering a run — also keeps the download boundary
        # able to mask old reports after a restart.
        self.redactor: SecretRedactor = redactor_from_config(config)
        if config.web_search_config_path is not None:
            self.redactor.add_from_file(config.web_search_config_path)
        self._lock = threading.RLock()
        self._executions: dict[str, _Execution] = {}
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
                if execution.phase != _COMMITTED
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
                    if execution.phase != _COMMITTED
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

        A prompt that opens with a ``/`` command other than the rendered prompt
        templates is refused with :class:`UnsupportedCommand` before anything is
        written or read (see :func:`prompt_names_unsupported_command`).

        ``blueprint`` optionally names a session template from the configured
        blueprint directory (an exact file stem). It is resolved and rendered
        here, before any credential is resolved or a run directory exists; an
        unknown or malformed template raises :class:`BlueprintUnavailable`. The
        rule is harness-independent — the pi and ACP faces admit the same names.
        A run without a blueprint is byte-for-byte the run it was before
        blueprints existed.

        The follow-up's session snapshot (a bounded file read plus redaction)
        is taken before the admission lock is entered, so one tenant's
        follow-up does not serialize every other tenant's admission behind its
        disk IO; the parent's authoritative state is rechecked inside the lock.
        """
        text = (prompt or "").strip()
        if not text:
            raise ValueError("prompt must not be empty")
        # Slash-command gate: a message starting with ``/`` reaches pi's
        # extension-command dispatch before anything else, and the loaded
        # pi-subagents package registers detaching commands (``/run --bg``,
        # ``/subagents-detach``, …) that no run lifecycle here owns. Only the
        # prompt templates rendered from ``agent/prompts/`` are accepted; the
        # refusal happens before a credential is resolved, before the parent's
        # session is read and before any run directory or execution face exists,
        # and the same call serves first turns and follow-ups on every face.
        allowed_commands = self._allowed_prompt_commands()
        if prompt_names_unsupported_command(text, allowed_commands):
            raise UnsupportedCommand(allowed_commands)
        # The session template is resolved and rendered before any credential is
        # read, any parent is inspected or a run directory exists: an unknown or
        # malformed blueprint refuses the whole admission, on every harness.
        blueprint_template = (
            self._resolve_blueprint(blueprint) if blueprint is not None else None
        )
        blueprint_prompt = (
            render_blueprint(blueprint_template) if blueprint_template is not None else ""
        )
        if self.config.harness == "acp" and parent_run_id is not None:
            # Original obligation (M1): ACP had no ``session/resume``, so every
            # ACP follow-up was refused with ``session_unavailable``. New
            # obligation (M7): the follow-up is admitted when the parent really
            # can be continued — same harness, terminal, latest turn, a recorded
            # ACP session id, and an agent capability matrix that declares a
            # continuation channel — and refused otherwise. The failure stays
            # fail-closed: the refusal happens before a credential is resolved or
            # a run directory exists, and the child adapter re-checks the
            # capability it negotiates itself before any resume request.
            self._acp_resumable_parent(context, parent_run_id)
        credentials = self._credentials_for_context(context)
        if self.config.harness == "pi" and not credentials.model_api_key:
            # The ACP family does not use the host's model credential: the agent
            # brings its own model configuration (injection points are M2 work).
            raise ConfigError(
                "OPENAI_API_KEY is not configured; set it in .env before starting a run"
            )
        # Build the run's redaction boundary before anything derived from the
        # prompt can be stored: a credential pasted into the prompt is masked
        # with the same knowledge the run's own output will be.
        redactor = self._redactor_for(credentials)
        # Caller holds no lock: the parent gate, the refusal checks and the
        # session snapshot's file IO happen before admission. The refusals are
        # re-evaluated inside the lock below, where the seed is consumed, so a
        # state that changed while the file was being read is still enforced.
        seed = self._resume_seed(context, parent_run_id, redactor)

        with self._lock:
            self._check_admission(context)
            continuation_workspace: Path | None = None
            if parent_run_id is not None:
                # Authoritative recheck (the snapshot above may have raced a
                # concurrent follow-up of the same parent): a refusal here
                # discards the seed, nothing has been written yet.
                self._resumable_parent(context, parent_run_id)
                if self.config.harness == "acp":
                    acp_parent = self._acp_resumable_parent(context, parent_run_id)
                    # An ACP continuation keeps the conversation's working
                    # directory: an agent session records its ``cwd``, and the
                    # resumed agent (and the model's remembered paths) stay in
                    # it regardless of what a request claims. Making the child
                    # run use that same workspace is what keeps the agent's
                    # tools and the host's report contract pointed at one place.
                    continuation_workspace = Path(acp_parent.workspace)
            # The store allocates the id and creates the run directory
            # exclusively (a generated collision is retried there): a new run can
            # never adopt or overwrite an existing record. An ACP continuation
            # records the conversation's workspace; every other run gets its own
            # freshly allocated one.
            record = self.store.create(
                redactor.redact(text),
                workspace=continuation_workspace,
                workspace_root=(
                    None if continuation_workspace is not None else self.config.workspace_root
                ),
                tenant_id=context.tenant_id if context is not None else None,
                creator_user_id=context.user_id if context is not None else None,
                parent_run_id=parent_run_id,
                blueprint=(
                    blueprint_template.name if blueprint_template is not None else None
                ),
                blueprint_stages=(
                    tuple(stage.as_record() for stage in blueprint_template.stages)
                    if blueprint_template is not None
                    else ()
                ),
            )
            run_id = record.run_id
            workspace = Path(record.workspace)
            execution = _Execution(
                record=record,
                workspace=workspace,
                kernel_prompt=text,
                context=context,
                credentials=credentials,
                redactor=redactor,
                session_seed=seed,
                parent_run_id=parent_run_id,
                blueprint=blueprint_template,
                blueprint_prompt=blueprint_prompt,
            )
            self._executions[run_id] = execution
            # Register the run before the caller gets the id back: a browser
            # that opens the event stream immediately must see an open run, not
            # a 404 race with the execution thread's first line.
            self.bus.open_run(run_id)

        threading.Thread(
            target=self._execute,
            args=(execution,),
            name=f"run-{run_id}",
            daemon=True,
        ).start()
        return record

    def _allowed_prompt_commands(self) -> tuple[str, ...]:
        """The ``/`` commands this host admits for the configured harness.

        pi dispatches extension commands and expands prompt templates, so the
        allowlist is the rendered prompt-template names. An ACP agent's slash
        commands (advertised through ``available_commands_update``) are its own
        surface and none of them are routed by this host: that family's
        allowlist is empty by construction, derived from the harness instead of
        a template-directory lookup (plan §4.3).
        """
        if self.config.harness == "acp":
            return ()
        return prompt_template_names(self.config.agent_config_dir)

    def _resolve_blueprint(self, blueprint: str) -> Blueprint:
        """Resolve one session template name against the configured directory.

        The accepted set is derived from the directory on every admission (a
        blueprint added or removed on disk is in force immediately), and the
        name is matched exactly — a client cannot address a file outside it. A
        malformed document is refused too: a partially rendered contract would
        be worse than no contract. The refusal carries only the derived name
        list, never the submitted value.
        """
        try:
            return load_blueprint(self.config.blueprints_dir, blueprint)
        except BlueprintError as exc:
            available = blueprint_names(self.config.blueprints_dir)
            code = "unknown_blueprint" if exc.reason == "unknown" else "invalid_blueprint"
            # ``repr`` escapes control characters, so a hostile name cannot
            # inject lines into the host log; the value is truncated, and the
            # refusals the client sees carry none of it.
            logger.warning(
                "blueprint refused name=%r reason=%s", (blueprint or "")[:64], exc.reason
            )
            raise BlueprintUnavailable(code, available) from exc

    def _credentials_for_context(self, context: TenantContext | None) -> RunCredentials:
        """Select the credentials one run may render (caller holds no lock).

        Multi-tenant mode requires a verified context. Under the default
        ``tenant`` policy the run needs the tenant's own model key; a missing
        entry is a refusal, never a fallback to the host's global ``.env`` key
        or to another tenant's credential file. Under the explicit ``shared``
        policy every tenant renders the one host-frozen credential instead.
        """
        if self.config.multi_tenant:
            if context is None:
                raise TenantNotConfigured("多租户模式需要已验证的租户身份")
            if self.config.shared_model_credentials:
                shared = RunCredentials.from_shared_host(self.config)
                if not shared.model_api_key:
                    # Defensive: the configuration refuses this at startup.
                    raise TenantNotConfigured("宿主未冻结共享模型凭据")
                return shared
            if self._credential_store is None:
                raise TenantNotConfigured("宿主未加载租户凭据配置")
            tenant = self._credential_store.require(context.tenant_id)
            return RunCredentials(
                model_api_key=tenant.model_api_key,
                web_search_key=tenant.web_search_key,
                web_search_config_path=None,
                origin="tenant",
            )
        return RunCredentials.from_host(self.config)

    # -- follow-up parents ----------------------------------------------------

    def _authorized_parent(
        self, context: TenantContext | None, parent_run_id: str
    ) -> RunRecord | None:
        """The parent this run may resume, or ``None`` (same as an unknown run).

        The tenant gate runs before any other field of the parent is read. In
        multi-tenant mode an unknown, foreign or ownerless (legacy) parent is
        therefore invisible: the caller cannot learn its status, session,
        report, children or existence. Local mode has no tenant identity and
        keeps the original single-user behaviour.
        """
        parent = self.store.get(parent_run_id)
        if parent is None:
            return None
        if self.config.multi_tenant and (
            context is None or parent.tenant_id != context.tenant_id
        ):
            return None
        return parent

    def _visible_records(self, context: TenantContext | None) -> list[RunRecord]:
        """Every run the caller may see (all runs in local mode)."""
        if not self.config.multi_tenant:
            return self.store.list(limit=None)
        return self.store.list_for(context.tenant_id, limit=None)

    def _resumable_parent(
        self, context: TenantContext | None, parent_run_id: str
    ) -> RunRecord:
        """The parent a follow-up may resume *now*, or the refusal it has earned.

        The owner gate comes first (``unknown_run``): an unknown, foreign or
        ownerless parent is refused before its status, session, report or
        children are read. The state refusals then follow in their fixed order:
        a parent that is not terminal is ``run_busy``, one that already has a
        follow-up turn is ``stale_run``.

        One admission calls this twice: outside the lock, to fail fast before
        the parent's session file is read, and inside it, where the decision is
        authoritative — between the two calls the parent may have gained a
        follow-up turn.
        """
        parent = self._authorized_parent(context, parent_run_id)
        if parent is None:
            raise FollowupUnavailable("unknown_run")
        if not parent.is_terminal:
            raise FollowupUnavailable("run_busy")
        if any(
            item.parent_run_id == parent_run_id
            for item in self._visible_records(context)
        ):
            raise FollowupUnavailable("stale_run")
        return parent

    def _acp_resumable_parent(
        self, context: TenantContext | None, parent_run_id: str
    ) -> RunRecord:
        """The ACP parent this follow-up may resume, or the refusal it has earned.

        On top of the shared parent gate (:meth:`_resumable_parent`: owner,
        terminal, latest turn), an ACP continuation needs three more facts, all
        of which are read from the host's own record of the parent rather than
        from the caller:

        * the parent ran the **same harness** — proven by the ``harness: "acp"``
          marker its ``session_ready`` frame carries, not by the current
          configuration alone (a host switched from ``pi`` to ``acp`` must not
          present a pi run as resumable);
        * the parent recorded a **session id** (an ACP run without one has
          nothing to resume);
        * the capability matrix recorded in that same frame declares a
          continuation channel (:func:`~pi_glink.kernel.acp_execution.
          acp_resume_method`). No capability means the follow-up is refused
          here, before a child run exists — it is never downgraded to a new
          session.

        The E2B face needs one more fact and is admitted by it:

        * **原义务（M7）**：e2b 面一律拒绝（``session_unavailable``）。ACP 沙箱的
          agent 状态随沙箱销毁，宿主没有可继承的状态根，起沙箱也必然无法 resume。
        * **新义务（M8）**：沙箱销毁前把该 run 的状态根有界回收到宿主；追问在
          「父状态根回收成功」时与 inproc/docker 面同样受理。回收到
          ``acp-agent/recovered`` 是整份成功的才发布，所以下面这个目录检查就是
          “回收成功”这个事实本身：失败（或未尝试）的回收什么都不留下，追问维持
          拒绝，而不是先花一个注定 resume 失败的云端沙箱。

        The admitted continuation also reuses the parent run's workspace (the
        caller reads :attr:`RunRecord.workspace`): the ACP session's ``cwd`` is
        recorded in the agent's own state and the agent keeps working there.
        Giving the child a fresh directory would leave the agent's writes (and
        the model's remembered absolute paths) outside the run whose report the
        host recovers. The parent's **record and published artifacts** stay
        immutable; the conversation's workspace is the one piece the turns
        share.

        The inheritance source is checked to exist as a directory so the common
        failure is an admission refusal; the copy itself happens off the
        admission lock in :meth:`_prepare_runtime` and fails the run closed if
        it cannot complete.
        """
        parent = self._resumable_parent(context, parent_run_id)
        capabilities = self._recorded_acp_capabilities(parent)
        if capabilities is None:
            raise FollowupUnavailable("session_unavailable")
        if not parent.session_id:
            raise FollowupUnavailable("session_unavailable")
        if acp_resume_method(capabilities) is None:
            raise FollowupUnavailable("session_unavailable")
        state_home = self._acp_state_home(parent)
        try:
            info = os.stat(state_home, follow_symlinks=False)
        except OSError as exc:
            raise FollowupUnavailable("session_unavailable") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise FollowupUnavailable("session_unavailable")
        return parent

    def _recorded_acp_capabilities(
        self, parent: RunRecord
    ) -> Mapping[str, Any] | None:
        """The capability matrix a parent ACP run recorded, or ``None``.

        The parent's ``session_ready`` frame is the only capability source that
        exists before the child's agent process starts; ``session_ready`` is
        committed right after the run opens, so the scan is bounded and does not
        grow with the parent's transcript. A parent with no such frame is not an
        ACP run (or predates it), which is exactly the ``session_unavailable``
        refusal the ACP gate owes it.
        """
        for index, event in enumerate(self.store.iter_events(parent.run_id)):
            if index >= _ACP_CAPABILITY_SCAN_EVENTS:
                break
            if event.get("type") != events.SESSION_READY:
                continue
            payload = event.get("payload")
            if not isinstance(payload, Mapping) or payload.get("harness") != "acp":
                continue
            capabilities = payload.get("agentCapabilities")
            return dict(capabilities) if isinstance(capabilities, Mapping) else {}
        return None

    def _acp_state_home(self, run: RunRecord) -> Path:
        """The ACP agent state root of one run a follow-up could inherit.

        In-process and Docker runs keep the state root the run rendered and the
        agent wrote (``acp-agent/home``). An E2B run's agent state only ever
        existed inside the sandbox, so the face recovers it to
        ``acp-agent/recovered`` before destroying the sandbox; that path is
        derived from the run record like the other one — never from caller
        input — and its existence is the recovered-state fact itself.
        """
        base = self.store.run_dir(run.run_id) / ACP_RUNTIME_DIRNAME
        if self.config.executor == "e2b":
            return base / ACP_RECOVERED_DIRNAME
        return base / ACP_HOME_DIRNAME

    def _resume_seed(
        self,
        context: TenantContext | None,
        parent_run_id: str | None,
        redactor: SecretRedactor,
    ) -> bytes | None:
        """Snapshot the sanitized session of an authorized parent run.

        Caller holds no lock. This is the follow-up's file IO — up to
        ``REPORT_MAX_BYTES`` read, decoded and redacted — which therefore never
        blocks another admission. The owner gate and the state refusals still
        run first (:meth:`_resumable_parent`) and are rechecked under the
        admission lock before the run is created, so the seed handed to the
        kernel comes from a parent that was authorized when it was read *and*
        was still resumable when the child was admitted.

        The pi family seeds the child's session directory with a sanitized copy
        of the parent's session file. The ACP family has no host session file:
        its continuation lives in the parent's agent state root, which
        :meth:`_prepare_runtime` inherits after admission (the gate already ran
        in :meth:`_acp_resumable_parent`).
        """
        if parent_run_id is None:
            return None
        if self.config.harness == "acp":
            return None
        parent = self._resumable_parent(context, parent_run_id)
        # The recorded path is not a capability. It must be a direct file in
        # that run's own session directory; ArtifactTree refuses links.
        session_dir = Path(self.config.session_dir) / parent_run_id
        path = Path(parent.session_file or "")
        if path.parent != session_dir or path.suffix != ".jsonl":
            raise FollowupUnavailable("session_unavailable")
        try:
            with ArtifactTree(session_dir) as tree:
                seed = tree.read(path.name, max_bytes=REPORT_MAX_BYTES)
        except (ArtifactError, OSError) as exc:
            raise FollowupUnavailable("session_unavailable") from exc
        if not seed.strip() or not parent.session_id:
            raise FollowupUnavailable("session_unavailable")
        try:
            # Historical files may predate the latest redaction rules. Never
            # hand an unchecked copy to the next kernel.
            seed = redactor.redact(seed.decode("utf-8")).encode("utf-8")
        except UnicodeError as exc:
            raise FollowupUnavailable("session_unavailable") from exc
        return seed

    def _redactor_for(self, credentials: RunCredentials) -> SecretRedactor:
        """A per-run redaction boundary: host secrets + this run's credentials."""
        redactor = SecretRedactor(self.redactor.known_values)
        redactor.add(credentials.model_api_key)
        redactor.add(credentials.web_search_key)
        return redactor

    def _run_redactor(self, execution: _Execution) -> SecretRedactor:
        """The run's own boundary, falling back to the host-level one.

        Production always sets the run's boundary at admission; the fallback
        keeps an execution object constructed directly (unit tests, future
        callers) from emitting through no boundary at all.
        """
        return execution.redactor or self.redactor

    def redactor_for_record(self, record: RunRecord) -> SecretRedactor:
        """The redaction boundary appropriate for an already-persisted run.

        Used by the report download: a multi-tenant report is masked against
        the record's own tenant credentials, never another tenant's. Under the
        explicit ``shared`` policy the credential is the host-level one, which
        is already part of ``self.redactor`` (and is never a tenant secret), so
        no tenant entry is consulted — the shared key must mask even when the
        tenant entry has no key at all.
        """
        if (
            not self.config.multi_tenant
            or self.config.shared_model_credentials
            or self._credential_store is None
        ):
            return self.redactor
        tenant = self._credential_store.get(record.tenant_id)
        if tenant is None:
            return self.redactor
        redactor = SecretRedactor(self.redactor.known_values)
        redactor.add(tenant.model_api_key)
        redactor.add(tenant.web_search_key)
        return redactor

    def _check_admission(self, context: TenantContext | None) -> None:
        """Authorize concurrency and reserve the slot (caller holds the lock).

        Two durable facts drive the decision — never a wall-clock guess: the
        runs executing in this process, and the runs whose execution resources
        are not confirmed released.

        In multi-tenant Docker/E2B mode the ceilings are global 4 / per-tenant 2
        by default and an over-limit request is refused immediately (no
        unbounded queue). An unreleased hold **consumes slots** instead of
        blocking everyone: it counts against its owner tenant's ceiling and
        against the global one (an ownerless legacy or unreadable record has no
        tenant and therefore only consumes the global ceiling). A leak is never
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
            if execution.phase != _COMMITTED
        ]
        if not self.config.multi_tenant:
            holds = [*self._resource_holds(), *self.store.unreadable_holds()]
            if holds:
                raise ResourcesUnreleased(holds)
            if active:
                raise RunBusy(active[0].record.run_id)
            return
        # Holds that are still in flight in this process already occupy a
        # concurrency slot below, so they are counted exactly once.
        in_flight = {execution.record.run_id for execution in active}
        holds = [
            record
            for record in self.store.holding_records()
            if record.run_id not in in_flight
        ]
        # An unreadable record has no owner: it only consumes global capacity.
        global_total = len(active) + len(holds) + len(self.store.unreadable_holds())
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

    def _resource_holds(self) -> list[str]:
        """Runs whose resources are not confirmed released (global fail-closed)."""
        return self._ledger.resource_holds()

    # -- cancellation ---------------------------------------------------------

    def stop(self, run_id: str, context: TenantContext | None = None) -> StopDecision:
        """Request cancellation, decided against the run's commit point.

        Accepting sets the flag the verdict reads under the same lock that
        commits it, so an accepted stop always ends as ``cancelled``. The
        face's own cancel only shortens the wait: a failed abort is logged and
        does not turn an accepted stop into a rejection.

        ``context`` is the verified identity in multi-tenant mode. The owner
        and role check runs under the run's own lock, so it cannot race the
        verdict commit: another tenant (or a legacy ownerless record) is
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
            if execution.phase == _COMMITTED:
                assert execution.verdict is not None
                return StopDecision(False, execution.verdict.status, "terminal")
            if execution.phase == _UNCOMMITTED:
                return StopDecision(False, RUNNING, "finalizing")
            execution.cancel_requested = True
            adapter = execution.adapter
        self._cancel_adapter(run_id, adapter)
        return StopDecision(True, RUNNING, "accepted")

    def _stop_owned(self, execution: _Execution) -> None:
        """Cancel one run this process owns, bypassing the tenant role gate.

        Shutdown is host lifecycle, not an authenticated user action, so it
        must not depend on a request context. Ownership is already proven by
        the in-memory execution table.
        """
        with execution.lock:
            if execution.phase != _OPEN:
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

        Another tenant and an ownerless legacy record are answered exactly like
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
            pending | set(self._resource_holds()) | set(self.store.unreadable_holds())
        )
        if unconfirmed:
            logger.error(
                "宿主退出时仍有 run 的终态或执行资源未确认: %s", ", ".join(unconfirmed)
            )
        return unconfirmed

    # -- restart reconciliation and resource adjudication ---------------------
    #
    # The implementations live in :class:`pi_glink.kernel.resource_ledger.ResourceLedger`
    # (the external-resource ledger collaborator built in ``__init__``); these
    # forwards keep the manager's existing surface for ``app.py``, ``cli.py``
    # and callers that predate the split.

    def reconcile(self) -> list[str]:
        """Close out runs a previous host process left ``running``."""
        return self._ledger.reconcile()

    def _commit_interrupted(self, run_id: str) -> bool:
        return self._ledger._commit_interrupted(run_id)

    def unreleased_resources(self) -> list[dict[str, Any]]:
        """Runs whose resources are not confirmed released, for operator review."""
        return self._ledger.unreleased_resources()

    def adjudicate_release(self, run_id: str, note: str) -> RunRecord | None:
        """Record an operator's decision that a run's resources are released."""
        return self._ledger.adjudicate_release(run_id, note)

    # -- online resource reconciliation ---------------------------------------

    def reconcile_resources(self) -> list[str]:
        """Confirm persisted resources are gone and release the verified holds."""
        return self._ledger.reconcile_resources()

    def start_resource_watch(self) -> threading.Thread | None:
        """Start the periodic ledger sweep (once at startup, then every interval)."""
        return self._ledger.start_resource_watch()

    def stop_resource_watch(self, timeout: float = 5.0) -> None:
        """Stop the periodic sweep without touching the holds it did not verify."""
        self._ledger.stop_resource_watch(timeout)

    # -- execution ------------------------------------------------------------

    def _execute(self, execution: _Execution) -> None:
        run_id = execution.record.run_id
        try:
            self.config.workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            execution.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._seed_report(execution)
            self._emit_open(execution, events.RUN_STARTED, {"prompt": execution.record.prompt})

            runtime = self._prepare_runtime(execution)
            adapter = self._build_adapter(execution, runtime)
            execution.adapter = adapter
            adapter.start()
            self._store_external_identity(execution, adapter)

            session = adapter.session_info()
            if execution.parent_run_id and (
                self.config.executor != "e2b" or self.config.harness == "acp"
            ):
                # The pi E2B face discovers its session id mid-stream, so it can
                # only verify the continuation after the turn; the ACP faces —
                # including the sandbox one — know the resumed session id right
                # after ``session/resume``, so the child's session identity is
                # proven against the parent's before the turn starts.
                parent = self._authorized_parent(
                    execution.context, execution.parent_run_id
                )
                if not session or not parent or session.session_id != parent.session_id:
                    raise FollowupUnavailable("session_unavailable")
            if session is not None:
                self._publish_session_ready(execution, session)

            # Cancelled while the kernel was starting: never start the work.
            if not execution.cancel_requested:
                adapter.deliver_input()
                deadline = time.monotonic() + self.config.task_timeout_seconds
                execution.outcome = adapter.wait(
                    cancel=lambda: execution.cancel_requested, deadline=deadline
                )
        except EventLogError as exc:
            logger.error("run failed run=%s: %s", run_id, exc)
            execution.event_log_error = execution.event_log_error or str(exc)
        except Exception as exc:  # noqa: BLE001 - every run must end with a reason
            if execution.cancel_requested:
                # Stopping a run closes the sandbox (or aborts pi); the error
                # that surfaces is the cancellation, not a failure.
                logger.info("run cancelled run=%s (%s)", run_id, type(exc).__name__)
            else:
                logger.exception("run failed run=%s", run_id)
            execution.failure = f"{type(exc).__name__}: {exc}"
        finally:
            self._finalize(execution)

    def _finalize(self, execution: _Execution) -> None:
        """Collect artifact and resource facts, then commit the verdict.

        Every step before the commit is guarded on its own, so a failing sweep
        cannot prevent the verdict. The verdict and its durable commit happen
        in one critical section with the cancellation flag.
        """
        run_id = execution.record.run_id
        projector = execution.projector
        if projector is not None:
            # Release the answer's withheld tail now, so it stays attached to
            # its text block ahead of any host-owned notice emitted below.
            with execution.lock:
                for kind, payload in projector.flush():
                    if not self._emit_frame(execution, kind, payload):
                        break

        adapter = execution.adapter
        if adapter is not None:
            # Recover work products before the face is torn down: the sandbox
            # cannot be read after it is destroyed.
            try:
                recovery = adapter.recover_artifacts(execution.workspace)
                execution.artifact_failures.extend(recovery.failures)
                # Values the host only learned from recovered material (the E2B
                # face's state root) join this run's boundary exactly like an
                # injected credential: a later echo is masked, and the values
                # are never written anywhere else.
                for value in recovery.secrets:
                    self._run_redactor(execution).add(value)
                # A sandbox face can learn session facts only while pulling the
                # workspace back (the pi face's session file travels with the
                # recovery; the ACP face's state-root verdict is decided there),
                # so the adapter answers what is worth republishing; every other
                # face answers ``None``. Duck-typed test doubles predate this
                # optional method, so it is looked up rather than assumed.
                recovered = getattr(adapter, "recovered_session", None)
                if recovered is not None:
                    recovered_session = recovered()
                    if recovered_session is not None:
                        self._publish_session_ready(execution, recovered_session)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                execution.artifact_failures.append(
                    f"产物回收失败: {type(exc).__name__}: {exc}"
                )
            try:
                execution.cleanup = adapter.close()
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                execution.cleanup = CleanupReport(
                    released=False,
                    failures=(f"关闭执行面失败: {type(exc).__name__}: {exc}",),
                    external=dict(adapter.identity()),
                )
        else:
            # Nothing was acquired: no process, container or sandbox exists.
            execution.cleanup = CleanupReport(released=True)

        try:
            if self._sanitize_workspace(execution.workspace, self._run_redactor(execution)):
                execution.redacted = True
        except Exception:  # noqa: BLE001 - sweep is defence in depth
            logger.exception("workspace credential sweep failed run=%s", run_id)
        try:
            if self._sanitize_session(execution):
                execution.redacted = True
        except Exception:  # noqa: BLE001
            logger.exception("session credential sweep failed run=%s", run_id)
        try:
            self._recover_report(execution)
        except Exception:  # noqa: BLE001 - reported as a rejected artifact
            logger.exception("report recovery failed run=%s", run_id)
            execution.report_status = "rejected"
            execution.report_path = None

        committed = False
        with execution.lock:
            # Frames may have arrived while artifacts were recovered; their
            # withheld tail still belongs to the open run, before the verdict.
            if projector is not None:
                for kind, payload in projector.flush():
                    if not self._emit_frame(execution, kind, payload):
                        break
            verdict = self._decide(execution)
            execution.verdict = verdict
            try:
                committed = self._commit_terminal(execution, verdict)
            except Exception:  # noqa: BLE001 - an unexpected error is an uncommitted verdict
                logger.exception("terminal commit failed run=%s", run_id)
            execution.phase = _COMMITTED if committed else _UNCOMMITTED

        if committed:
            try:
                self.store.settle(run_id)
            except Exception:  # noqa: BLE001 - readers project the committed frame
                logger.exception("settling the run record failed run=%s", run_id)
            self.bus.close(run_id)
            with self._lock:
                self._executions.pop(run_id, None)
        else:
            # Every reader still sees ``running`` (nothing is committed), so the
            # run keeps its slot and its live stream; a restart reconciles it.
            logger.critical(
                "run 终态未能提交，保持占用直至重启对账 run=%s status=%s", run_id, verdict.status
            )
        cleanup = execution.cleanup
        logger.info(
            "run finalized run=%s status=%s stop=%s committed=%s report=%s released=%s",
            run_id,
            verdict.status,
            verdict.stop_reason,
            committed,
            execution.report_path or "<none>",
            cleanup.released if cleanup is not None else None,
        )
        execution.finalized.set()

    # -- the verdict ----------------------------------------------------------

    def _decide(self, execution: _Execution) -> _Verdict:
        """Combine every fact into one verdict (call under the run lock).

        An accepted cancellation wins: it was accepted while the run was open,
        so it must be the verdict. ``agent_settled`` is deliberately not the
        success signal: pi can settle after an error stop reason or a truncated
        (``length``) response.
        """
        log_error = execution.event_log_error
        if execution.cancel_requested:
            return _Verdict(CANCELLED, "abort", log_error)
        if log_error:
            return _Verdict(FAILED, "event_log_failed", log_error)
        if execution.failure:
            return _Verdict(FAILED, "execution_error", execution.failure)
        outcome = execution.outcome
        if outcome is None:
            return _Verdict(FAILED, "execution_error", "执行面没有给出结果")
        if outcome.cancelled:
            return _Verdict(CANCELLED, "abort", None)
        if outcome.timed_out:
            return _Verdict(FAILED, "timeout", outcome.error or "run timed out")
        if outcome.error:
            return _Verdict(FAILED, outcome.stop_reason or "execution_error", outcome.error)
        stop = execution.model_stop_reason
        if self.config.harness == "acp":
            # ACP's own ``StopReason`` vocabulary, recorded verbatim in
            # ``modelStopReason``. The verdict is still decided only here:
            # ``max_tokens`` becomes the product's ``length`` (finished, but the
            # delivery is incomplete), ``refusal`` fails as a model error,
            # ``cancelled`` takes the cancellation path; an unknown or missing
            # value fails closed instead of claiming a normal finish. Only
            # ``end_turn`` falls through to the delivery policy below.
            if stop is None:
                return _Verdict(
                    FAILED, "execution_error", "ACP session/prompt 没有返回 stopReason"
                )
            if stop not in ACP_STOP_REASONS:
                return _Verdict(FAILED, "execution_error", f"未知的 ACP stopReason: {stop!r}")
            if stop == "refusal":
                return _Verdict(FAILED, "model_error", "ACP agent 拒绝继续（stopReason=refusal）")
            if stop == "cancelled":
                return _Verdict(CANCELLED, "abort", None)
            if stop == "max_tokens":
                return _Verdict(FINISHED, "length", None)
            if stop == "max_turn_requests":
                return _Verdict(FINISHED, "max_turn_requests", None)
        elif stop == "error":
            return _Verdict(FAILED, "model_error", execution.model_error or "模型返回了错误结果")
        elif stop == "aborted":
            return _Verdict(FAILED, "aborted", execution.model_error or "模型输出被中断")
        elif stop == "length":
            # The execution succeeded, but the answer was cut off at the output
            # limit. That is a delivery fact the record must carry.
            return _Verdict(FINISHED, "length", None)
        if execution.report_path is None:
            reason = "report_rejected" if execution.report_status == "rejected" else "no_report"
            return _Verdict(FINISHED, reason, None)
        return _Verdict(FINISHED, None, None)

    def _commit_terminal(self, execution: _Execution, verdict: _Verdict) -> bool:
        """Durably append the terminal frame; True once it is committed."""
        run_id = execution.record.run_id
        current = self.store.get(run_id) or execution.record
        cleanup = execution.cleanup or CleanupReport(released=True)
        terminal_type = {
            FAILED: events.RUN_FAILED,
            CANCELLED: events.RUN_CANCELLED,
        }.get(verdict.status, events.RUN_FINISHED)
        payload = {
            "status": verdict.status,
            "stopReason": verdict.stop_reason,
            "error": self._run_redactor(execution).redact(verdict.error)
            if verdict.error
            else None,
            "reportPath": str(execution.report_path) if execution.report_path else None,
            "reportStatus": execution.report_status,
            "modelStopReason": execution.model_stop_reason,
            "sessionId": current.session_id,
            "sessionFile": current.session_file,
            "usage": execution.usage_total or None,
            "redacted": execution.redacted,
            "durationSeconds": round(time.time() - current.created_at, 3),
            "cleanup": {
                "released": cleanup.released,
                "failures": list(cleanup.failures),
                "external": dict(cleanup.external),
            },
            "artifactFailures": list(execution.artifact_failures),
        }
        try:
            self._emit(execution, terminal_type, payload, terminal=True)
        except EventLogError as exc:
            logger.error("terminal frame could not be committed run=%s: %s", run_id, exc)
            return False
        return True

    # -- pi configuration -----------------------------------------------------

    def _seed_report(self, execution: _Execution) -> None:
        """Put the previous deliverable in this run's private workspace for revisions.

        The parent is owner-checked again here (it was authorized at admission):
        a report is copied only from a run this run's tenant may read, never
        from another tenant or an ownerless legacy record.
        """
        if not execution.parent_run_id:
            return
        parent = self._authorized_parent(execution.context, execution.parent_run_id)
        location = self.store.report_location(parent) if parent else None
        if location is None:
            return
        try:
            with ArtifactTree(location.root) as source:
                content = source.read(location.relative, max_bytes=REPORT_MAX_BYTES)
            with ArtifactTree(execution.workspace) as destination:
                destination.replace(REPORT_FILENAME, content, max_bytes=REPORT_MAX_BYTES)
            execution.seeded_report_digest = hashlib.sha256(content).hexdigest()
        except (ArtifactError, OSError) as exc:
            raise FollowupUnavailable("report_unavailable") from exc

    def _prepare_runtime(self, execution: _Execution) -> AgentRuntime | AcpRuntime:
        """Render the run's own per-harness configuration and session directory.

        pi: one run, one runtime directory — nothing in ``data_root/pi-agent`` is
        shared or mutated by a run any more, so two runs cannot overwrite each
        other's credentials, extensions or pi session state. The session
        directory is the run's subdirectory of the configured session root
        (``PI_CODING_AGENT_SESSION_DIR``), which keeps that setting meaningful
        while keeping two runs apart.

        ACP: the run gets its own agent state root (:mod:`acp_env`), which the
        agent process sees as ``HOME`` — the in-process child directly, the
        container through the ``HOME`` mount. Credentials are injected into it
        by read-only copy (:data:`PI_GLINK_ACP_CREDENTIAL_FILES`).
        """
        run_id = execution.record.run_id
        run_dir = self.store.run_dir(run_id)
        if self.config.harness == "acp":
            # A follow-up inherits the authorized parent's agent state root into
            # this run's own HOME. The parent gate ran at admission; this second
            # authorization keeps a parent that disappeared (or became
            # unreadable to this tenant) between admission and here from being
            # used as a copy source. The credential destinations are re-rendered
            # fresh and excluded from the copy (see :func:`prepare_acp_runtime`).
            inherit_from: Path | None = None
            if execution.parent_run_id is not None:
                parent = self._authorized_parent(
                    execution.context, execution.parent_run_id
                )
                if parent is None:
                    raise FollowupUnavailable("session_unavailable")
                inherit_from = self._acp_state_home(parent)
            try:
                runtime = prepare_acp_runtime(
                    self.config,
                    runtime_dir=run_dir / ACP_RUNTIME_DIRNAME,
                    inherit_from=inherit_from,
                )
            except AcpStateInheritError as exc:
                # The state could not be inherited safely; the run stops here,
                # before any agent process exists, instead of resuming from a
                # partial copy or from nothing.
                raise FollowupUnavailable(
                    f"session_unavailable: 继承父 run 的 ACP 状态失败（{exc}）"
                ) from exc
            execution.runtime = runtime
            if execution.session_seed is not None:
                # The pi session seed has no ACP meaning; reaching it would mean
                # a pi-shaped continuation was routed to the ACP adapter.
                raise FollowupUnavailable("session_unavailable")
            if execution.redactor is None:
                execution.redactor = self._redactor_for(
                    execution.credentials or RunCredentials.from_host(self.config)
                )
            # Credential material copied into this run's HOME — and any
            # credential-shaped value found in the inherited parent state — joins
            # this run's redaction boundary (and only this run's): a value the
            # agent echoes into a stream, tool result or report is masked like
            # every other secret the host knows. The values never reach a log.
            for value in runtime.secrets:
                execution.redactor.add(value)
            execution.projector = AcpProjector(execution.redactor)
            return runtime
        runtime = prepare_agent_dir(
            self.config,
            runtime_dir=run_dir / RUNTIME_DIRNAME,
            session_dir=Path(self.config.session_dir) / run_id,
            credentials=execution.credentials,
        )
        execution.runtime = runtime
        if execution.session_seed is not None:
            with ArtifactTree(runtime.session_dir) as tree:
                tree.replace(
                    "continued.jsonl", execution.session_seed, max_bytes=REPORT_MAX_BYTES
                )
            execution.session_seed = None
        # This run's own redaction boundary: the host's secrets plus the
        # credentials this run was actually given. The rendered search
        # credential only exists in the file, so it is learned here (never
        # logged, never returned). Adding it to the run's boundary — not to the
        # host-level one — keeps two tenants' secrets from mixing.
        if execution.redactor is None:
            execution.redactor = self._redactor_for(
                execution.credentials or RunCredentials.from_host(self.config)
            )
        execution.redactor.add_from_file(runtime.directory / "web-search.json")
        execution.projector = Projector(execution.redactor)
        return runtime

    # -- adapters -------------------------------------------------------------

    def _build_adapter(
        self, execution: _Execution, runtime: AgentRuntime | AcpRuntime
    ) -> ExecutionAdapter:
        run_id = execution.record.run_id
        parent = (
            self._authorized_parent(execution.context, execution.parent_run_id)
            if execution.parent_run_id
            else None
        )
        if execution.parent_run_id and parent is None:
            # The parent disappeared or is no longer readable by this run's
            # tenant: never start a kernel with an unverified resume target.
            raise FollowupUnavailable("session_unavailable")
        if self.config.executor == "docker":
            # The container name is fixed by the run id before ``docker run``
            # executes: persist it now so a failure inside the adapter's start()
            # cannot leave an unnamed container behind.
            self._store_identity(
                execution, {"container": docker_container_name(run_id)}
            )
        spec = ExecutionSpec(
            config=self.config,
            runtime=runtime,
            run_id=run_id,
            prompt=execution.kernel_prompt or execution.record.prompt,
            system_prompt=self._system_prompt_text(execution),
            workspace=execution.workspace,
            timeout_seconds=self.config.task_timeout_seconds,
            extra_argv=tuple(self._extra_argv(execution)),
            # The continuation value is harness-shaped: pi resumes from the
            # sanitized session file copied into the run's session directory,
            # while the ACP family resumes the **session id** the parent
            # recorded, against the state root inherited in _prepare_runtime.
            resume_session=(
                parent.session_id
                if parent and self.config.harness == "acp"
                else ("continued.jsonl" if parent else None)
            ),
            expected_session_id=parent.session_id if parent else None,
        )
        return build_adapter(
            spec,
            harness=self.config.harness,
            executor=self.config.executor,
            client_factory=self._client_factory,
            on_frame=lambda frame: self._on_frame(execution, frame),
            sandbox_factory=self._sandbox_factory,
            on_identity=lambda identity: self._store_identity(execution, identity),
            rpc_plan=self._rpc_plan,
            docker_plan=self._docker_plan,
            docker_stop=docker_stop_argv,
            acp_plan=self._acp_plan,
            acp_docker_plan=self._acp_docker_plan,
            acp_e2b_plan=self._acp_e2b_plan,
        )

    def _acp_plan(
        self, spec: ExecutionSpec
    ) -> tuple[list[str], dict[str, str], Path | None]:
        """Launch inputs for the in-process ACP agent: argv, filtered env, cwd.

        The argv comes from the frozen ``PI_GLINK_ACP_COMMAND`` and is never
        shell-expanded; the environment is the pi face's minimal passthrough
        list plus this run's own ``HOME`` (plan §4.5).
        """
        if not isinstance(spec.runtime, AcpRuntime):
            raise ConfigError("ACP harness 的运行期配置没有按 AcpRuntime 渲染")
        return (
            build_acp_argv(spec.config),
            build_acp_env(spec.config, spec.runtime),
            spec.workspace,
        )

    def _acp_docker_plan(
        self, spec: ExecutionSpec
    ) -> tuple[list[str], dict[str, str], str]:
        """Launch inputs for the containerised ACP agent (M3a).

        Same composition rules as :meth:`_docker_plan` (reachable daemon, frozen
        image, persisted container name, resource ceilings), but the container's
        stdio carries ACP JSON-RPC and its ``HOME`` is the run's own agent state
        root with the injected credentials mounted read-only inside it. The
        command is the frozen ``PI_GLINK_ACP_COMMAND`` executed *inside* the
        image, so it must name an image-local executable.
        """
        if not isinstance(spec.runtime, AcpRuntime):
            raise ConfigError("ACP harness 的运行期配置没有按 AcpRuntime 渲染")
        if not docker_available():
            raise ConfigError(
                "LOCAL_WEB_EXECUTOR=docker requires a reachable Docker daemon"
            )
        plan = build_acp_docker_plan(
            spec.config,
            spec.runtime,
            run_id=spec.run_id,
            workspace=spec.workspace,
        )
        return plan.argv, plan.env, plan.container_name

    def _acp_e2b_plan(self, spec: ExecutionSpec) -> E2BAcpSandboxClient:
        """The sandbox carrier for one ACP run (M3b).

        The ACP family's E2B face reuses the pi face's sandbox lifecycle
        (:class:`~pi_glink.kernel.acp_e2b_env.E2BAcpSandboxClient`: same frozen
        credential, bounded lifetime, artifact recovery and identity), and the
        separate ``LOCAL_WEB_E2B_ACP_TEMPLATE`` because the pi template has no
        ACP agent installed. The sandbox id is persisted by the adapter as soon
        as it exists.
        """
        if not isinstance(spec.runtime, AcpRuntime):
            raise ConfigError("ACP harness 的运行期配置没有按 AcpRuntime 渲染")
        return E2BAcpSandboxClient(
            spec.config,
            spec.runtime,
            run_id=spec.run_id,
            workspace=spec.workspace,
            sandbox_factory=self._sandbox_factory,
        )

    def _rpc_plan(
        self, spec: ExecutionSpec
    ) -> tuple[list[str], dict[str, str], Path | None]:
        return (
            build_pi_argv(spec.config, spec.runtime, extra=[
                *spec.extra_argv,
                *(["--session", str(spec.runtime.session_dir / spec.resume_session)] if spec.resume_session else []),
            ]),
            build_pi_env(spec.config, spec.runtime),
            spec.workspace,
        )

    def _docker_plan(
        self, spec: ExecutionSpec
    ) -> tuple[list[str], dict[str, str], str]:
        if not docker_available():
            raise ConfigError(
                "LOCAL_WEB_EXECUTOR=docker requires a reachable Docker daemon"
            )
        plan = build_docker_plan(
            spec.config,
            spec.runtime,
            run_id=spec.run_id,
            workspace=spec.workspace,
            session_dir=spec.runtime.session_dir,
            extra=[*spec.extra_argv, *(["--session", f"/sessions/{spec.resume_session}"] if spec.resume_session else [])],
        )
        return plan.argv, plan.env, plan.container_name

    def _store_external_identity(
        self, execution: _Execution, adapter: ExecutionAdapter
    ) -> None:
        """Persist the container/sandbox identity as soon as it exists.

        Without it, a host restart could not tell an operator which leftover
        resource belongs to which run; with it, reconciliation is bounded to the
        named resources instead of guessing.
        """
        self._store_identity(execution, adapter.identity())

    def _store_identity(self, execution: _Execution, identity: Mapping[str, str]) -> None:
        """Write one face's durable identity into the run record.

        Called from inside the adapter as soon as the resource exists, so a
        failure in the very next step (uploading the configuration, for
        instance) cannot produce a run that holds an unreleased resource
        without saying which one it is.
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
        execution.record = self.store.update(execution.record.run_id, **changes)

    def _extra_argv(self, execution: _Execution) -> list[str]:
        extra = ["--name", f"pi-glink-{execution.record.run_id}"]
        text = self._system_prompt_text(execution)
        if text:
            extra.extend(["--append-system-prompt", text])
        return extra

    def _system_prompt_text(self, execution: _Execution) -> str:
        """The system prompt one run's kernel receives, on every face.

        The working agreement (``agent/research-prompt.md``) plus, when the run
        was admitted with one, the rendered session blueprint. pi gets the same
        text through ``--append-system-prompt``; the ACP family gets it as the
        leading text block of ``session/prompt``. The user's own prompt is a
        different channel and is never part of this text.
        """
        parts = [self._research_prompt_text(), execution.blueprint_prompt]
        return "\n\n".join(part for part in parts if part)

    def _research_prompt_text(self) -> str:
        path = self.config.agent_config_dir / RESEARCH_PROMPT_FILENAME
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()

    # -- event projection -----------------------------------------------------

    def _on_frame(self, execution: _Execution, frame: Mapping[str, Any]) -> None:
        with execution.lock:
            if execution.phase != _OPEN or execution.projector is None:
                # After the verdict commit nothing may follow the terminal frame.
                logger.debug(
                    "frame after commit dropped run=%s type=%s",
                    execution.record.run_id,
                    frame.get("type"),
                )
                return
            for event_type, payload in execution.projector.project(frame):
                if event_type == events.USAGE_REPORTED:
                    payload = self._accumulate_usage(execution, frame, payload)
                    if payload is None:
                        continue
                if not self._emit_frame(execution, event_type, payload):
                    # The durable log is the fact source: a frame that cannot be
                    # committed is not shown either.
                    return
            self._record_model_result(execution, frame)

    def _emit_frame(
        self, execution: _Execution, event_type: str, payload: Mapping[str, Any]
    ) -> bool:
        """Commit one frame of an open run (caller holds the run lock)."""
        try:
            self._emit(execution, event_type, payload)
        except EventLogError as exc:
            execution.event_log_error = execution.event_log_error or str(exc)
            logger.error("event log failed run=%s: %s", execution.record.run_id, exc)
            return False
        return True

    def _emit_open(
        self, execution: _Execution, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        """Commit a host-owned frame while the run is open (raises on log failure)."""
        with execution.lock:
            if execution.phase != _OPEN:
                return
            self._emit(execution, event_type, payload)

    def _record_model_result(
        self, execution: _Execution, frame: Mapping[str, Any]
    ) -> None:
        frame_type = frame.get("type")
        if frame_type == "message_end":
            message = frame.get("message")
            if isinstance(message, Mapping) and message.get("role") == "assistant":
                stop = message.get("stopReason") or message.get("stop_reason")
                if isinstance(stop, str) and stop:
                    execution.model_stop_reason = stop
                error = message.get("errorMessage")
                execution.model_error = (
                    error if isinstance(error, str) and error.strip() else None
                )
        elif frame_type == "auto_retry_end" and not frame.get("success"):
            final = frame.get("finalError")
            if isinstance(final, str) and final.strip():
                execution.model_error = final
        elif frame_type == "acp_prompt_result":
            # ``session/prompt``'s answer: the ACP adapter translates it into
            # this frame, and the stop reason is recorded verbatim (the ACP
            # vocabulary is mapped to the product verdict in ``_decide``).
            stop = frame.get("stopReason")
            execution.model_stop_reason = stop if isinstance(stop, str) and stop else None

    def _accumulate_usage(
        self, execution: _Execution, frame: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Turn one response's usage snapshot into a run cumulative total.

        pi reports the usage of *one assistant response* (its own fields are
        already cumulative for that request). The run total is the sum across
        responses; a frame carrying the same message identity is not counted
        twice. No price is invented: ``cost`` is summed only when pi reported it.
        """
        snapshot = payload.get("usage")
        if not isinstance(snapshot, Mapping):
            return None
        if self.config.harness == "acp":
            # ACP's usage_update is a cumulative context snapshot (``used`` of
            # ``size`` plus a session cost), not pi's per-response usage: the
            # honest conversion keeps that view and replaces the total instead
            # of summing snapshots (which would fabricate a growing number).
            # The payload's ``basis`` marker carries the distinction into the
            # run record.
            execution.usage_total = dict(snapshot)
            return dict(payload)
        key = self._usage_key(frame)
        if key is not None:
            if key in execution.usage_keys:
                return None
            execution.usage_keys.add(key)
        total = execution.usage_total
        for name in _USAGE_FIELDS:
            value = snapshot.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total[name] = total.get(name, 0) + value
        cost = snapshot.get("cost")
        if isinstance(cost, Mapping):
            cost_total = total.setdefault("cost", {})
            for name, value in cost.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    cost_total[name] = cost_total.get(name, 0) + value
        total["requests"] = total.get("requests", 0) + 1
        return {"usage": dict(total), "requestUsage": dict(snapshot)}

    @staticmethod
    def _usage_key(frame: Mapping[str, Any]) -> str | None:
        message = frame.get("message")
        if not isinstance(message, Mapping):
            return None
        for name in ("timestamp", "id"):
            value = message.get(name)
            if isinstance(value, str) and value:
                return f"{name}:{value}"
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return f"{name}:{value}"
        return None

    def _publish_session_ready(
        self, execution: _Execution, session: SessionInfo
    ) -> None:
        run_id = execution.record.run_id
        session_file = session.session_file
        if isinstance(session_file, str) and session_file and self.config.executor == "docker":
            assert execution.runtime is not None
            mapped = self._host_session_file(session_file, execution.runtime)
            if mapped is None:
                logger.warning(
                    "container session path does not map back to the host mount: %s",
                    session_file,
                )
            else:
                session_file = mapped
        if session.session_id or session_file:
            self.store.update(
                run_id,
                session_id=session.session_id,
                session_file=session_file,
            )
        payload: dict[str, Any] = dict(session.payload)
        payload["sessionFile"] = session_file
        self._emit_open(execution, events.SESSION_READY, payload)

    def _host_session_file(self, reported: str, runtime: AgentRuntime) -> str | None:
        """Translate a pi-reported container path into its host location.

        The in-process face reports host paths directly. The Docker face
        reports container paths (``/sessions/…``) while the file itself lives in
        the run's mounted session directory; only the host path can be read back
        for sanitising. Only the session mount is translated: an unmappable path
        is refused instead of being trusted.
        """
        mapped = docker_host_session_path(reported, runtime.session_dir)
        return str(mapped) if mapped is not None else None

    # -- artifacts ------------------------------------------------------------

    def _recover_report(self, execution: _Execution) -> None:
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
                data = workspace.read(REPORT_FILENAME, max_bytes=REPORT_MAX_BYTES)
        except ArtifactMissing:
            execution.report_status = "missing"
            return
        except ArtifactError as exc:
            execution.report_status = "rejected"
            execution.report_path = None
            self._report_warning(execution, f"report.md 未通过安全校验，已拒绝：{exc}")
            return
        if execution.seeded_report_digest is not None and execution.seeded_report_digest == hashlib.sha256(data).hexdigest():
            # The previous deliverable is context, not a newly generated file.
            execution.report_status = "missing"
            return
        original = data.decode("utf-8", errors="replace")
        redacted = self._run_redactor(execution).redact(original)
        if redacted != original:
            execution.redacted = True
        try:
            with ArtifactTree(self.store.run_dir(run_id)) as run_tree:
                run_tree.replace(
                    f"{ARTIFACTS_DIRNAME}/{REPORT_FILENAME}",
                    redacted.encode("utf-8"),
                    max_bytes=REPORT_MAX_BYTES,
                    create_parents=True,
                )
        except (ArtifactError, OSError) as exc:
            execution.report_status = "rejected"
            execution.report_path = None
            self._report_warning(execution, f"report.md 无法写入宿主产物目录，已拒绝：{exc}")
            return
        execution.report_path = self.store.report_artifact_path(run_id)
        execution.report_status = "ok"

    def _report_warning(self, execution: _Execution, detail: str) -> None:
        run_id = execution.record.run_id
        logger.warning("run=%s %s", run_id, detail)
        try:
            self._emit_open(
                execution,
                events.KERNEL_STATUS,
                {"level": "warning", "message": "report_rejected", "detail": detail},
            )
        except EventLogError as exc:
            logger.error("report warning could not be persisted run=%s: %s", run_id, exc)

    def _sanitize_session(self, execution: _Execution) -> bool:
        """Mask credentials pi wrote into its own session log.

        The kernel owns that file, so redaction cannot happen on the way in; it
        happens here on the way out. The path is only trusted inside the run's
        own session directory — a kernel-reported path elsewhere on the host is
        refused rather than rewritten.
        """
        record = self.store.get(execution.record.run_id)
        session_file = record.session_file if record is not None else None
        if not session_file or execution.runtime is None:
            return False
        return self._sanitize_session_file(
            session_file,
            root=execution.runtime.session_dir,
            redactor=self._run_redactor(execution),
        )

    def _sanitize_session_file(
        self,
        session_file: str,
        *,
        root: Path,
        redactor: SecretRedactor | None = None,
    ) -> bool:
        """Redact one session file; returns whether its bytes changed.

        ``redactor`` is the run's own boundary; the default keeps the host-level
        one for callers that have no run (unit tests, future maintenance).
        """
        boundary = redactor or self.redactor
        try:
            with ArtifactTree(root) as tree:
                original = tree.read(session_file, max_bytes=REPORT_MAX_BYTES).decode(
                    "utf-8", errors="replace"
                )
                redacted = boundary.redact(original)
                if redacted == original:
                    return False
                tree.replace(session_file, redacted.encode("utf-8"), max_bytes=REPORT_MAX_BYTES)
        except (ArtifactError, OSError) as exc:
            logger.warning("会话文件未通过安全校验或无法改写，已跳过: %s (%s)", session_file, exc)
            return False
        logger.warning("session file contained credentials and was redacted: %s", session_file)
        return True

    def _sanitize_workspace(
        self, workspace: Path, redactor: SecretRedactor | None = None
    ) -> bool:
        """Redact credentials in every text artifact the run left behind.

        The report is not the only file a tool echo can reach; a ``write`` call
        can leak the same value into any artifact the operator later opens. The
        sweep is bounded (file count, size cap, binary skip) and walks the
        workspace through directory handles: links are never followed for
        reading, and every rewrite is published inside the verified directory.

        ``redactor`` is the run's own boundary; the default is the host-level
        one for callers that have no run.
        """
        boundary = redactor or self.redactor
        try:
            tree = ArtifactTree(workspace)
        except ArtifactError:
            return False
        changed = False
        with tree:
            for relative in tree.files(max_files=SWEEP_MAX_FILES):
                try:
                    raw = tree.read(relative, max_bytes=SWEEP_MAX_BYTES)
                except ArtifactError:
                    continue
                if b"\x00" in raw[:1024]:  # binary-ish
                    continue
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                redacted = boundary.redact(text)
                if redacted == text:
                    continue
                try:
                    tree.replace(relative, redacted.encode("utf-8"), max_bytes=SWEEP_MAX_BYTES)
                except (ArtifactError, OSError) as exc:
                    logger.error("failed to redact artifact: %s (%s)", relative, exc)
                    continue
                changed = True
                logger.warning("artifact contained credentials and was redacted: %s", relative)
        return changed

    # -- persistence ----------------------------------------------------------

    def _emit(
        self,
        execution: _Execution,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        terminal: bool = False,
    ) -> events.RunEvent:
        # Belt-and-braces: projection already redacts, but host-owned events
        # (run_finished, kernel_status, …) are built here, so the run's own
        # boundary runs again on the way out. The frame is committed before it
        # is shown; the terminal frame is committed durably (see
        # RunStore.commit_terminal).
        safe_payload = (
            self._run_redactor(execution).redact_payload(dict(payload))
            if payload is not None
            else None
        )
        run_id = execution.record.run_id
        write = self.store.commit_terminal if terminal else self.store.append_event
        try:
            return self.bus.commit(
                run_id,
                event_type,
                safe_payload,
                persist=lambda event: write(run_id, event.as_dict()),
            )
        except OSError as exc:
            raise EventLogError(
                f"事件日志写入失败: {type(exc).__name__}: {exc}"
            ) from exc
