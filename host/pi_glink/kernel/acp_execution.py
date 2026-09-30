"""One run driven over the ACP client.

This is the ACP family's driver in ``harness.driver(sandbox.spawn(harness.launch(spec)))``
(docs/architecture.md §4): one adapter class for every
:class:`~pi_glink.kernel.sandbox.Sandbox` — in-process (the agent shares the
host machine), Docker (``AcpRpcClient`` speaking to ``docker run -i``'s stdio)
and E2B (a sandbox background command driven by ``send_stdin``). The sandbox
owns the resource, the process spawn, the agent-visible workspace path and the
teardown; this module owns the protocol work, the same code on every sandbox.
On a remote sandbox the driver additionally delivers the rendered ``HOME``
before the agent starts and recovers the workspace and state root before the
sandbox dies (:class:`~pi_glink.kernel.acp_e2b_env.AcpSandboxTransfer`).

``start()``
    Spawn the agent, ``initialize`` (protocol version + capability negotiation)
    and either ``session/new`` (a first turn) or the continuation of the parent
    session: ``session/resume`` when the agent declares
    ``sessionCapabilities.resume``, otherwise ``session/load`` when the older
    top-level ``loadSession`` capability is declared. A continuation request
    never falls back to ``session/new``: with no declared continuation channel,
    or when the agent refuses the resume, the run fails instead of presenting a
    fresh session as the parent's next turn. ``session/load`` replays the
    conversation as ``session/update`` notifications before it answers; those
    frames are the parent's already-recorded history, so they are counted and
    dropped rather than projected twice into this run's stream.
``session_info()``
    The ACP session id plus the negotiated capability matrix, for the run
    record's ``session_ready`` frame.
``deliver_input()``
    Send ``session/prompt`` and observe it on a driver thread: the request is
    answered only when the turn ends, so waiting for it is ``wait()``'s job.
``wait()``
    Observe the driver until the prompt response, cancellation or the run
    deadline. A cancellation returns immediately — the verdict is the host's
    flag, and ``close()`` ends the process.
``cancel()``
    ``session/cancel`` notification. Idempotent, safe from another thread.
``recover_artifacts()``
    No-op where the workspace already is the host directory (in-process, the
    Docker mount); on a remote sandbox, pull the workspace and the agent state
    root back before teardown.
``close()``
    Stop the agent process, then close the sandbox (force-remove the container
    by its persisted name, kill the E2B sandbox), and report whether that
    succeeded.

``session/request_permission`` is answered fail-closed (no human is watching):
the first ``reject_once``/``reject_always`` option is selected, or — when the
agent offers no rejection — the request is answered ``cancelled``. Either way
the refusal is emitted as a ``kernel_status`` frame, never silently swallowed.
A pack that declares ``capabilities.permission_allow`` (option kinds and
optionally tool kinds) may instead select the narrowest matching ``allow_*``
option; the decision is still recorded, with the policy that produced it. When
the request omits ``toolCall.kind`` (the field is optional and some agents omit
it), the kind the agent published for the same ``toolCallId`` in
its ``tool_call`` notification is used, so a tool-kind allowlist keeps working
without matching on titles or raw input.

The host-side system prompt is delivered as the leading, fenced block of the
``session/prompt`` message (:func:`compose_prompt`): ACP v1 has no system-role
channel, and the baseline text block is the one channel every agent accepts.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import __version__ as HOST_VERSION
from ..config import (
    ACP_PERMISSION_OPTION_KINDS,
    ACP_PERMISSION_TOOL_KINDS,
)
from .acp_child_progress import FORMAT as DSH_SESSION_PROGRESS
from .acp_child_progress import SESSIONS_RELATIVE as DSH_SESSIONS_RELATIVE
from .acp_child_progress import AcpChildProgressSource
from .acp_child_progress import available as acp_progress_available
from .acp_e2b_env import AcpSandboxTransfer
from .acp_env import (
    INHERIT_MAX_FILES,
    state_threshold_warning,
)
from .archive_channel import read_state_manifest
from .acp_rpc import (
    REQUEST_TIMEOUT_SECONDS,
    AcpClosed,
    AcpError,
    AcpMethodNotFound,
    AcpProtocolError,
    AcpRequestError,
    AcpRpcClient,
)
from .acp_session_config import apply_session_config, project_config_options
from .acp_sandbox_park import (
    park_sandbox,
    read_generation_marker,
)
from .conversation_lease import (
    LeaseCorruptError,
    expire_lease_now,
    settle_lease_after_destroy,
)
from .e2b_env import SandboxError, kill_sandbox_by_id
from .execution import (
    ArtifactRecovery,
    CleanupReport,
    ExecutionAdapter,
    ExecutionSpec,
    FrameCallback,
    SessionInfo,
    publishable_recovered_session,
)
from .outcome import POLL_SECONDS, ExecutionOutcome, SealedOutcome
from .sandbox import Launch, Sandbox

logger = logging.getLogger("pi_glink.kernel.acp_execution")

#: The protocol MAJOR version this client implements (ACP v1 schema).
ACP_PROTOCOL_VERSION = 1

#: The continuation method that reattaches to an existing session without
#: replaying it (capability ``sessionCapabilities.resume``).
RESUME_METHOD = "session/resume"
#: The older top-level ``loadSession`` continuation: it replays the conversation
#: through ``session/update`` notifications before answering.
LOAD_METHOD = "session/load"

#: Capabilities this client advertises. Deliberately empty: the agent operates
#: on its own filesystem, so the host offers no ``fs/*`` and no ``terminal/*``
#: methods, and it cannot reproduce the agent invocation in an interactive
#: terminal (``auth.terminal``). Omission means "unsupported" in ACP; the object
#: is explicit so the negotiation is visible.
CLIENT_CAPABILITIES: dict[str, Any] = {
    "fs": {"readTextFile": False, "writeTextFile": False},
    "terminal": False,
    "auth": {"terminal": False},
}

#: Rejection preference: a one-shot rejection is the narrowest fail-closed
#: answer; a durable one would let the agent remember it.
_REJECT_KINDS = ("reject_once", "reject_always")

#: Bound on the initialize response's auth method list carried into the record.
_AUTH_METHODS_LIMIT = 20

#: Bound on the remembered tool-call kinds used to resolve a permission request
#: that omits ``toolCall.kind`` (see
#: :meth:`AcpExecutionAdapter._remember_tool_kind`). One run's tool calls are
#: bounded by the turn budget; the cap only keeps a misbehaving agent from
#: growing the map without limit.
_TOOL_KIND_MEMORY_LIMIT = 512

#: Bounds on the agent's stderr tail attached to a ``kernel_exit`` report. The
#: tail is the only place an agent's own crash message (a stack trace, a missing
#: credential, a protocol mismatch) appears: the ACP wire carries JSON-RPC only.
#: Three ceilings keep the report bounded — lines kept, characters per line, and
#: the whole block — and the run's redactor is applied to the resulting text
#: before it reaches a log or an event.
ACP_STDERR_TAIL_LINES = 20
ACP_STDERR_TAIL_LINE_CHARS = 500
ACP_STDERR_TAIL_CHARS = 4000

#: The host-side system prompt is delivered as a leading text block of the
#: ``session/prompt`` message. ACP v1 has no system-role channel: ``session/new``
#: carries no prompt field, ``_meta`` is explicitly "implementations MUST NOT
#: make assumptions about values at these keys", and the session modes and
#: configOptions are approval/model selectors, not prompts. The baseline text
#: block is the one channel every ACP agent MUST accept, so the working
#: agreement travels there, fenced and labelled so the model can tell it apart
#: from the user's request. Measured on dsh and codex-acp in M2; see
#: ``docs/acp-integration-notes.md``.
_SYSTEM_PROMPT_HEADER = (
    "以下是宿主工作台注入的 system prompt（本次会话的工作约定）；"
    "请把它当作系统级工作准则执行，而不是用户请求。"
)

#: The resume RPC's deadline grows with the state root it has to reparse
#（阶段 1 收尾，docs/adr/0019）. ``session/resume`` (and the replaying
# ``session/load``) makes the agent read and decode the whole session store —
# dsh's zstd frame chain — so a fixed 60s budget times out on a large root
# before the parse completes. The linear term assumes a pessimistic 4 MiB/s
# (an order below raw zstd decompression, to cover indexing/replay work), the
# base stays :data:`REQUEST_TIMEOUT_SECONDS` (the protocol's own overhead),
# and the hard ceiling aligns with the archive command budget
# (:data:`~pi_glink.kernel.acp_e2b_env.ARCHIVE_COMMAND_TIMEOUT_SECONDS`): one
# direction's transport gets 600s, so a reparse must not outlive it.
RESUME_TIMEOUT_BASE_SECONDS = REQUEST_TIMEOUT_SECONDS
RESUME_TIMEOUT_BYTES_PER_SECOND = 4 * 1024 * 1024
RESUME_TIMEOUT_MAX_SECONDS = 600.0


def resume_timeout_seconds(state_bytes: int) -> float:
    """The resume RPC deadline for a state root of ``state_bytes`` bytes.

    Base + linear growth, clamped to the hard ceiling; a non-positive input is
    the base (the protocol overhead alone).
    """
    if state_bytes <= 0:
        return RESUME_TIMEOUT_BASE_SECONDS
    grown = RESUME_TIMEOUT_BASE_SECONDS + state_bytes / RESUME_TIMEOUT_BYTES_PER_SECOND
    return min(grown, RESUME_TIMEOUT_MAX_SECONDS)


def _tree_bytes(root: Path, *, max_files: int = INHERIT_MAX_FILES) -> int:
    """Sum a state root's regular-file sizes without reading any content.

    A metadata-only walk (``scandir``, symlinks never followed) for the resume
    deadline's input; the bound keeps a malformed tree from making the walk
    itself unbounded — anything past it is the warning tier's business anyway.
    """
    total = 0
    files = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += info.st_size
                    files += 1
                    if files >= max_files:
                        return total
        except OSError:
            continue
    return total


def compose_prompt(system_prompt: str, prompt: str) -> str:
    """The text block one ``session/prompt`` carries, with the working agreement.

    An empty system prompt leaves the user's text untouched (the M1 shape); a
    non-empty one is prefixed as an explicitly fenced block so a model cannot
    mistake the agreement for the request.
    """
    instructions = (system_prompt or "").strip()
    request_text = prompt or ""
    if not instructions:
        return request_text
    return (
        f"{_SYSTEM_PROMPT_HEADER}\n\n"
        f"<system-prompt>\n{instructions}\n</system-prompt>\n\n"
        f"--- 用户请求 ---\n\n{request_text}"
    )


def acp_resume_method(capabilities: Mapping[str, Any]) -> str | None:
    """The continuation channel an agent's capability matrix declares.

    ACP carries two. ``sessionCapabilities.resume`` is the no-replay method and
    is preferred: the host already owns the parent run's event log, so replaying
    the conversation into a new run would duplicate it. The older top-level
    ``loadSession`` boolean is the replaying method and is used only when
    ``resume`` is absent. A missing bit (or a false one) means the agent has no
    continuation at all — the caller then fails closed instead of starting a
    fresh session as the parent's next turn.

    One function serves both callers: the adapter checks the matrix it just
    negotiated, and the runner's admission gate checks the matrix recorded in
    the parent run's ``session_ready`` frame (the only capability source that
    exists before the child's agent process is started).
    """
    session_capabilities = capabilities.get("sessionCapabilities")
    if isinstance(session_capabilities, Mapping):
        resume = session_capabilities.get("resume")
        if resume is not None and resume is not False:
            return RESUME_METHOD
    if capabilities.get("loadSession") is True:
        return LOAD_METHOD
    return None


class AcpExecutionAdapter(SealedOutcome, ExecutionAdapter):
    """The ACP driver: one agent process, one session, one prompt turn."""

    def __init__(
        self,
        spec: ExecutionSpec,
        *,
        sandbox: Sandbox,
        launch: Launch,
        client_factory: Callable[..., AcpRpcClient] | None = None,
        on_frame: FrameCallback,
    ) -> None:
        super().__init__(spec, on_frame=on_frame)
        self.kind = sandbox.kind
        self.sandbox = sandbox
        self.launch = launch
        self._client_factory = client_factory or AcpRpcClient
        #: The ``cwd`` handed to ``session/new``: the workspace as the *agent*
        #: sees it — the host workspace in-process, the mount point in a
        #: container, the sandbox path on E2B. A host path the carrier cannot
        #: see is never sent.
        self._session_cwd = sandbox.place("workspace", spec.workspace)
        #: Remote-sandbox delivery/recovery of the rendered ``HOME``.
        self._transfer: AcpSandboxTransfer | None = None
        #: The sandbox state-root recovery verdict (``None`` until a remote
        #: sandbox's ``recover_artifacts`` ran). It is published in the
        #: post-recovery ``session_ready`` frame so a reader can see whether
        #: this run's state root is available on the host for a follow-up; the
        #: admission gate reads the recovered directory itself, never this
        #: frame alone.
        self._state_recovered: bool | None = None
        #: The last state recovery's full facts (mode/volume/duration), kept
        #: for the post-recovery ``session_ready`` payload.
        self._state_recovery: Any | None = None
        #: Whether the close-out may park the sandbox as the conversation's
        #: hot cache (docs/adr/0020): set once this run's recovery **succeeded
        #: and published** (the manifest's ``sourceRunId`` is this run). A
        #: failed recovery parks nothing — the sandbox is destroyed, exactly as
        #: before 阶段 2.
        self._park_ready = False
        #: The sandbox id the hot path reconnected to (``None`` on a cold
        #: path); a session-ready fact and the close-out's retire check.
        self._hot_path_sandbox: str | None = None
        self._client: AcpRpcClient | None = None
        self._driver: threading.Thread | None = None
        self._done = threading.Event()
        #: The **host** asked for cancellation (``RunManager.stop``/shutdown).
        #: A timeout-driven ``session/cancel`` is teardown, not a user stop, so
        #: it must not turn the verdict into ``cancelled``.
        self._cancelled = threading.Event()
        self._cancel_lock = threading.Lock()
        self._cancel_sent = False
        #: Tool-call id -> ACP tool kind as last announced by a ``tool_call``
        #: notification. An agent may omit ``kind`` from
        #: ``session/request_permission`` (measured in M4) while having published
        #: it for the same ``toolCallId``; the
        #: allowlist resolves the request against the agent's own classification
        #: rather than guessing from a human-readable title. A kind never
        #: published stays unknown and the allowlist keeps refusing it.
        self._tool_kinds: dict[str, str] = {}
        # The outcome, its lock and the seal protocol come from SealedOutcome;
        # the driver thread reports the turn's end into it while the run
        # thread decides the verdict from it.
        #: The cause of a pending ``kernel_exit`` report: the agent's stream
        #: ended before it answered ``session/prompt``. The exit status is
        #: deliberately not part of it -- the stream ending and the process
        #: exiting are two events, and a status exists only once the child has
        #: been reaped. :meth:`close` completes the report from that status,
        #: and only while the frozen decision *is* that report.
        self._kernel_exit_cause: str | None = None
        self._session_id: str | None = None
        #: Which continuation method this run used (``session/resume`` or
        #: ``session/load``); ``None`` for a first turn.
        self._resume_method_used: str | None = None
        #: The wall-clock time the continuation RPC took (阶段 1 的观测义务:
        #: resume 耗时 = agent 端重解析重建；回灌/继承的传输耗时另由
        #: ``stateBackfill``/``stateInherit`` 携带)。 ``None`` for a first turn.
        self._resume_duration_ms: float | None = None
        #: ``session/load`` replays the parent's conversation as ordinary
        #: ``session/update`` notifications before it answers. Those frames are
        #: history this host already recorded for the parent run, so they must
        #: not be projected a second time into this run's stream; they are
        #: counted while the load is in flight and reported once it returns.
        self._replay_lock = threading.Lock()
        self._suppressing_replay = False
        self._replayed_updates = 0
        self._protocol_version: int | None = None
        self._agent_info: dict[str, Any] = {}
        self._capabilities: dict[str, Any] = {}
        self._auth_methods: list[Any] = []
        self._modes: dict[str, Any] = {}
        self._config_options: list[dict[str, Any]] = []

    # -- lifecycle ------------------------------------------------------------

    def _open_sandbox(self, sandbox: Sandbox) -> bool:
        """Open the face; ``True`` when the hot path (docs/adr/0020) held.

        A follow-up with a lease candidate reconnects to the paused parent
        sandbox first (``on_resume="reboot"``: the agent process was stopped
        before the pause, so a disk boot is the only resume that cannot
        resurrect a second writer) and verifies the sandbox-side generation
        marker against the lease. Any failure — the reconnect, a missing or
        foreign marker, a sandbox E2B already reaped — **degrades to the cold
        path** (a fresh sandbox + the 阶段 1 backfill): the follow-up is served,
        the degradation is visible as a ``kernel_status`` warning, and the
        abandoned sandbox is destroyed by id (the lease expires immediately
        when even that fails, so the reconciliation sweep retries it).
        """
        target = self.spec.sandbox_resume
        if target is None:
            sandbox.open(self.launch, timeout_seconds=self.spec.timeout_seconds + 300)
            return False
        # Authoritative admission holds this conversation through its running
        # run record. Cleanup checks that ownership under the same guard used
        # by admission; a reconnect TTL extension is neither needed nor safe.
        try:
            sandbox.open(
                self.launch,
                timeout_seconds=self.spec.timeout_seconds + 300,
                resume=target,
            )
            marker = read_generation_marker(sandbox.handle)
            expected = (target.conversation_run, target.generation)
            if marker != expected:
                raise SandboxError(
                    "沙箱状态代数与租约不一致"
                    f"（sandbox={marker!r} != lease={expected!r}）"
                )
        except SandboxError as exc:
            reason = str(exc)
            self._degrade_from_hot_path(sandbox, target, reason)
            sandbox.open(self.launch, timeout_seconds=self.spec.timeout_seconds + 300)
            return False
        self._on_frame(
            {
                "type": "acp_sandbox_resume",
                "path": "hot",
                "sandboxId": target.sandbox_id,
                "generation": target.generation,
            }
        )
        self._hot_path_sandbox = target.sandbox_id
        return True

    def _degrade_from_hot_path(
        self, sandbox: Sandbox, target: Any, reason: str
    ) -> None:
        """Abandon the lease sandbox and record the degradation (cold path next).

        The reconnected handle (when the reconnect got that far) is killed
        first; without a handle — the reconnect itself failed — the sandbox is
        destroyed by its persisted id (the SDK's not-found contract counts as
        "already gone"). The lease follows the destroy's truth: gone means the
        record goes, an unconfirmed destroy keeps the record but expires it, so
        the very next reconciliation tick retries.
        """
        destroyed = bool(sandbox.discard())
        if not destroyed:
            destroyed, _detail = kill_sandbox_by_id(
                target.sandbox_id, config=self.spec.config
            )
        conversation_dir = self.spec.conversation_dir
        if conversation_dir is None:  # pragma: no cover - candidates always carry one
            return
        if destroyed:
            try:
                # 销毁的只是本沙箱：租约里挂账的 retired 还在账上，只有清空了
                # 才删整个记录（否则改写为立即过期，下轮对账收尾）。
                settle_lease_after_destroy(conversation_dir)
            except LeaseCorruptError as exc:
                logger.warning(
                    "热路径降级销毁后租约不可读，保留待人工处置 run=%s: %s",
                    self.spec.run_id,
                    exc,
                )
        else:
            # The sandbox may still be alive and billing: keep the record and
            # expire it now, so the very next reconciliation tick destroys it.
            expire_lease_now(conversation_dir)
        self._on_frame(
            {
                "type": "acp_sandbox_resume",
                "path": "cold",
                "sandboxId": target.sandbox_id,
                "reason": reason,
            }
        )

    def start(self) -> None:
        sandbox = self.sandbox
        hot = self._open_sandbox(sandbox)
        # From here on the sandbox has persisted any identity it created, so a
        # partial start still leaves a named leftover; ``close()`` releases it.
        if sandbox.remote:
            transfer = AcpSandboxTransfer(
                sandbox.handle,
                self.spec.runtime,
                workspace=self.spec.workspace,
                archive=self.spec.archive,
                conversation_dir=self.spec.conversation_dir,
                run_id=self.spec.run_id,
            )
            self._transfer = transfer
            if (
                self.spec.resume_session
                and self.spec.archive is not None
                and self.spec.archive_backfill
                and not hot
            ):
                # The follow-up's state half, over the archive channel: the
                # sandbox pulls the conversation's authoritative root itself
                # (docs/adr/0018), before the credential upload so a credential
                # destination always wins over anything with the same path. A
                # face without the channel keeps the host-side inheritance
                # copy, which ``upload_home`` delivers file by file below. The
                # **hot** path skips this (docs/adr/0020): the reconnected
                # sandbox already *is* the conversation's state at the lease's
                # generation — re-pulling it would only re-transmit what the
                # sandbox never lost.
                transfer.backfill_state()
            transfer.upload_home()
            if self.spec.resume_session:
                # The continuation's workspace half: the resumed agent works in
                # the conversation's workspace (M7), so the deliverable it
                # produced there travels into the new sandbox first. On the hot
                # path the workspace never left — the upload re-delivers the
                # same bytes and stays the one code path for both.
                transfer.upload_continuation()
        self._emit_state_threshold_warnings()
        spawn = sandbox.spawn(self.launch)
        client_kwargs: dict[str, Any] = {
            "cwd": spawn.cwd,
            "env": spawn.env,
            "on_notification": self._on_notification,
            "on_request": self._on_request,
        }
        if spawn.process is not None:
            # How the client starts the agent: a remote sandbox hands over an
            # already-started background command; only the carrier differs.
            client_kwargs["spawn"] = spawn.process
        client = self._client_factory(spawn.argv, **client_kwargs)
        self._client = client
        try:
            client.start()
            negotiated = client.request(
                "initialize",
                {
                    "protocolVersion": ACP_PROTOCOL_VERSION,
                    "clientCapabilities": CLIENT_CAPABILITIES,
                    # ACP's ``Implementation`` carries both ``name`` and
                    # ``version``; an ACP agent validated the initialize params
                    # strictly in M4 and rejected a clientInfo without
                    # ``version`` with -32602 before any session
                    # exists. The host's own version is the
                    # truthful value for what is speaking.
                    "clientInfo": {"name": "pi-glink", "version": HOST_VERSION},
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            self._accept_initialize(negotiated)
            if self.spec.resume_session:
                self._resume_session(client, self.spec.resume_session)
            else:
                self._new_session(client)
        except Exception:
            # Nothing may outlive a failed start: the caller still runs
            # ``close()``, but leaving the child up when start() itself failed
            # would make that a silent dependency.
            self._client = None
            try:
                client.close()
            except Exception:  # noqa: BLE001 - the start failure is the report
                logger.exception("closing ACP agent after a failed start failed")
            raise

    def _new_session(self, client: AcpRpcClient) -> None:
        """A first turn: create the session this run's prompt works in."""
        created = client.request(
            "session/new",
            {"cwd": self._session_cwd, "mcpServers": []},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        session_id = created.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise AcpProtocolError("session/new 没有返回 sessionId")
        self._session_id = session_id
        self._modes = (
            dict(created["modes"]) if isinstance(created.get("modes"), Mapping) else {}
        )
        self._config_options = project_config_options(created.get("configOptions"))
        self._apply_session_config(client, session_id)

    def _resume_session(self, client: AcpRpcClient, session_id: str) -> None:
        """Continue the parent's session the way this agent's matrix allows.

        The method is chosen from the negotiated capability matrix, never guessed:
        ``session/resume`` when declared, ``session/load`` when only the older
        top-level ``loadSession`` bit is. Anything else fails closed — this run
        is not allowed to answer a follow-up with a brand-new session.
        """
        method = self._resume_method()
        if method is None:
            raise AcpError(
                "ACP agent 未声明续接能力（sessionCapabilities.resume 或 loadSession）；"
                "拒绝以新会话冒充追问"
            )
        if self.spec.expected_session_id and self.spec.expected_session_id != session_id:
            # The admission layer verified the parent; a value that changed
            # between admission and start is never used as a resume target.
            raise AcpProtocolError(
                "续接目标与准入时校验的父 sessionId 不一致；拒绝续接"
            )
        self._resume_method_used = method
        params: dict[str, Any] = {
            "sessionId": session_id,
            "cwd": self._session_cwd,
            # The host declares no MCP server for any run; both methods take the
            # same empty list (``session/load`` requires the field).
            "mcpServers": [],
        }
        if method == LOAD_METHOD:
            # ``session/load`` replays the parent's conversation before it
            # answers: those frames belong to the parent run's record, not to
            # this turn's stream.
            self._begin_replay_suppression()
        try:
            # The deadline grows with the state root the agent has to reparse
            # (阶段 1 收尾，docs/adr/0019): a fixed budget times out on a large
            # root before the parse completes, which would read as a refusal.
            started = time.monotonic()
            answer = client.request(
                method,
                params,
                timeout=resume_timeout_seconds(self._resume_state_bytes()),
            )
            self._resume_duration_ms = round((time.monotonic() - started) * 1000, 1)
        finally:
            replayed = self._end_replay_suppression()
        self._session_id = session_id
        self._modes = (
            dict(answer["modes"]) if isinstance(answer.get("modes"), Mapping) else {}
        )
        self._config_options = project_config_options(answer.get("configOptions"))
        self._on_frame(
            {
                "type": "acp_session_resumed",
                "method": method,
                "sessionId": session_id,
                # How many replayed frames were dropped (0 for resume). The
                # projection records this; nothing is silently swallowed.
                "replayedUpdates": replayed,
            }
        )
        self._apply_session_config(client, session_id)

    def _resume_method(self) -> str | None:
        """The continuation channel this agent's capability matrix declares.

        ACP carries two (see :func:`acp_resume_method`), and the admission gate
        reads the parent run's recorded matrix through the same function, so a
        run is only admitted when the agent this host is about to start would
        also accept the resume.
        """
        return acp_resume_method(self._capabilities)

    def _resume_state_bytes(self) -> int:
        """The bytes the agent will reparse on a continuation (three faces).

        In-process and Docker: the run's own HOME after the inheritance copy —
        that tree *is* the session store. Remote with the archive channel: the
        backfilled conversation state plus the freshly rendered HOME (the
        sandbox's state root is both). The **hot** path (docs/adr/0020) has no
        backfill facts — the sandbox already carries the state — so the
        conversation's published manifest supplies the volume the agent is
        about to reparse. Symlinks are never followed and the walk stops at
        the inheritance file bound: the tree that exceeds it is the anomaly
        the warning tier already reports, not a reason to walk forever.
        """
        total = 0
        home = getattr(self.spec.runtime, "home", None)
        if home is not None:
            total += _tree_bytes(Path(home))
        backfill = getattr(self._transfer, "backfill_facts", None) if self._transfer else None
        if backfill:
            total += int(backfill.get("bytes") or 0)
        elif self._hot_path_sandbox is not None and self.spec.conversation_dir is not None:
            manifest = read_state_manifest(self.spec.conversation_dir)
            if manifest is not None:
                total += int(manifest.get("bytes") or 0)
        return total

    def _emit_state_threshold_warnings(self) -> None:
        """Emit the warning-tier state notices the run opened with (ADR 0019).

        Two sources exist by the time the agent starts: the inheritance copy
        (in-process/Docker, ``runtime.inherit_facts``) and the archive-channel
        backfill (remote, ``transfer.backfill_facts``). A warning never refuses
        anything — the follow-up is served — but it must be visible in the
        event stream instead of silently crossing a bound that size says is
        anomalous.
        """
        inherit = getattr(self.spec.runtime, "inherit_facts", None)
        if isinstance(inherit, Mapping) and inherit.get("warning"):
            self._on_frame(
                {
                    "type": "acp_state_threshold_warning",
                    "source": "inherit",
                    "detail": str(inherit["warning"]),
                }
            )
        backfill = getattr(self._transfer, "backfill_facts", None) if self._transfer else None
        if isinstance(backfill, Mapping):
            warning = state_threshold_warning(
                files=int(backfill.get("files") or 0),
                total_bytes=int(backfill.get("bytes") or 0),
            )
            if warning:
                self._on_frame(
                    {
                        "type": "acp_state_threshold_warning",
                        "source": "backfill",
                        "detail": warning,
                    }
                )

    def _begin_replay_suppression(self) -> None:
        with self._replay_lock:
            self._suppressing_replay = True
            self._replayed_updates = 0

    def _end_replay_suppression(self) -> int:
        with self._replay_lock:
            self._suppressing_replay = False
            return self._replayed_updates

    def _apply_session_config(self, client: AcpRpcClient, session_id: str) -> None:
        """Apply and verify declared options after both new and resume."""
        self._config_options = apply_session_config(
            client, session_id, self.spec.config.session_config,
            self._config_options,
        )
        # Only mirror verified approval state, never the requested value alone.
        mode_ids = {
            str(mode.get("id"))
            for mode in self._modes.get("availableModes", [])
            if isinstance(mode, Mapping)
        }
        for option in self._config_options:
            if option.get("id") == "mode" and option.get("currentValue") in mode_ids:
                self._modes["currentModeId"] = option["currentValue"]

    def _accept_initialize(self, negotiated: Mapping[str, Any]) -> None:
        version = negotiated.get("protocolVersion")
        if version != ACP_PROTOCOL_VERSION:
            # The negotiation rule says the client should close the connection
            # when it does not support the agent's version. A mismatch must not
            # be papered over: the wire shape could differ.
            raise AcpProtocolError(
                f"ACP agent 协商到协议版本 {version!r}；本客户端只支持 v{ACP_PROTOCOL_VERSION}"
            )
        self._protocol_version = ACP_PROTOCOL_VERSION
        capabilities = negotiated.get("agentCapabilities")
        self._capabilities = dict(capabilities) if isinstance(capabilities, Mapping) else {}
        info = negotiated.get("agentInfo")
        self._agent_info = dict(info) if isinstance(info, Mapping) else {}
        methods = negotiated.get("authMethods")
        self._auth_methods = list(methods)[:_AUTH_METHODS_LIMIT] if isinstance(methods, list) else []

    def session_info(self) -> SessionInfo | None:
        payload: dict[str, Any] = {
            "harness": "acp",
            "protocolVersion": self._protocol_version,
            "sessionId": self._session_id,
            "agentInfo": dict(self._agent_info),
            # The negotiated capability matrix goes into the run record's
            # session_ready frame (plan §4.1), so a later reader can tell what
            # this agent actually supported.
            "agentCapabilities": dict(self._capabilities),
            "authMethods": list(self._auth_methods),
            # The session's own option state (model, reasoning, approval mode,
            # …): the ACP channel that carries what a pi run would read from
            # its rendered settings.
            "modes": dict(self._modes),
            "configOptions": [dict(option) for option in self._config_options],
            # The ACP session's cwd (the agent-visible workspace): the host
            # workspace in-process, the container mount point on the Docker
            # face, the sandbox path on E2B. This is the ACP fact, not a host
            # path.
            "cwd": self._session_cwd,
        }
        # Which carrier ran the agent (E2B: sandbox id and template).
        payload.update(self.sandbox.session_facts())
        if self._state_recovered is not None:
            # Set once the state root has been recovered to the host (or the
            # recovery failed): the durable published state (the conversation
            # root since 阶段 1, the legacy ``acp-agent/recovered`` mirror
            # before it) is what a follow-up inherits.
            payload["stateRootRecovered"] = self._state_recovered
        transfer = self._transfer
        backfill = getattr(transfer, "backfill_facts", None) if transfer else None
        if backfill:
            # The archive-channel backfill's facts (files/bytes/duration), for
            # the run's own event stream — 阶段 1 的观测义务：回灌耗时进事件流。
            payload["stateBackfill"] = dict(backfill)
        inherit = getattr(self.spec.runtime, "inherit_facts", None)
        if isinstance(inherit, Mapping):
            # The host-side inheritance copy's facts (in-process/Docker — 阶段 1
            # 的观测义务：继承即本族在那些面上的传输，耗时与体量进事件流;
            # remote faces with the channel answer ``stateBackfill`` instead).
            payload["stateInherit"] = dict(inherit)
        if self._resume_duration_ms is not None:
            # The continuation RPC's own wall clock (dsh's reparse/rebuild),
            # separate from the transports above — 阶段 1 的观测义务：resume
            # 耗时区分传输与解析。
            payload["resumeDurationMs"] = self._resume_duration_ms
        if self._state_recovery is not None and self._state_recovery.ok:
            # The recovery's own facts: which path produced it (archive
            # incremental/full or the files fallback), its volume and duration.
            payload["stateRecovery"] = {
                "mode": self._state_recovery.mode,
                "files": len(self._state_recovery.files),
                "bytes": self._state_recovery.bytes,
                "durationMs": self._state_recovery.duration_ms,
            }
        if self._resume_method_used is not None:
            # A continuation's session id is the parent's own id, so the run
            # record can prove the agent reattached to exactly that session
            # rather than opening a new one.
            payload["resumed"] = True
            payload["resumeMethod"] = self._resume_method_used
        if self._hot_path_sandbox is not None:
            # 阶段 2 的观测义务：本轮追问走的是热路径（重连泊车沙箱，未回灌）。
            # The ``cold`` degradations are visible as their own
            # ``kernel_status`` warning (``acp_sandbox_resume`` frame).
            payload["sandboxResume"] = {
                "path": "hot",
                "sandboxId": self._hot_path_sandbox,
                "backfilled": False,
            }
        return SessionInfo(payload=payload, session_id=self._session_id, session_file=None)

    def deliver_input(self) -> None:
        if self._client is None:
            raise AcpError("ACP agent 未启动")
        if self._cancelled.is_set():
            # Cancelled while the kernel was starting: never start the turn.
            return
        self._driver = threading.Thread(
            target=self._drive, name=f"acp-{self.spec.run_id}", daemon=True
        )
        self._driver.start()

    def _drive(self) -> None:
        client = self._client
        assert client is not None
        try:
            if self._cancelled.is_set():
                # The flag may have been set between deliver_input's check and
                # this thread starting: no turn exists to cancel.
                self._record_cancel()
                return
            # The turn boundary the product's streaming model needs: streamed
            # text/thinking blocks are only closed (and rendered as Markdown)
            # at ``turn_finished``. ACP has no such update of its own, but the
            # host knows exactly when the prompt turn starts and ends.
            self._on_frame({"type": "acp_turn_started"})
            response = client.request(
                "session/prompt",
                {
                    "sessionId": self._session_id,
                    "prompt": [
                        {
                            "type": "text",
                            "text": compose_prompt(
                                self.spec.system_prompt, self.spec.prompt
                            ),
                        }
                    ],
                },
                timeout=None,
            )
        except AcpRequestError as exc:
            self._fail("execution_error", f"session/prompt 被拒绝: {exc}")
        except (AcpClosed, AcpProtocolError) as exc:
            if self._cancelled.is_set():
                self._record_cancel()
            else:
                # The stream ended before the answer. Whether the agent itself
                # exited, and with what status, is a fact about the reaped
                # process: the client can observe the stream's end (EOF) while
                # the child is not yet reapable, so no status is sampled here.
                # close() reaps the child before the verdict is decided and
                # completes the report from that status.
                self._record_kernel_exit(str(exc))
        except Exception as exc:  # noqa: BLE001 - reported in the outcome
            self._fail("execution_error", f"{type(exc).__name__}: {exc}")
        else:
            stop = response.get("stopReason")
            # The runner records this verbatim as the run's model stop reason
            # and decides the verdict from it (the ACP values are mapped there).
            self._on_frame(
                {"type": "acp_prompt_result", "stopReason": stop if isinstance(stop, str) else None}
            )
            if self._cancelled.is_set():
                self._record_cancel()
            elif stop == "cancelled" and not self._timed_out():
                # The agent stopped the turn itself. A ``cancelled`` answer to
                # this adapter's own timeout teardown is not that case: the
                # deadline verdict was already decided and must stay ``timeout``.
                self._record_cancel()
            else:
                self._record_settled()
        finally:
            # The turn ended (answered, rejected or the process died): close the
            # streamed blocks and release any withheld delta tail. Emitted from
            # this thread before ``_done``, so the normal path always precedes
            # the terminal verdict.
            try:
                self._on_frame({"type": "acp_turn_finished"})
            except Exception:  # noqa: BLE001 - the outcome is already recorded
                logger.exception(
                    "emitting the ACP turn end failed run=%s", self.spec.run_id
                )
            self._done.set()

    # -- the outcome's writers ------------------------------------------------
    #
    # The lock, the seal flag and the settle/seal decisions are the shared
    # :class:`SealedOutcome` protocol; the writers below stay here because
    # their error text is this family's own. The one write the seal
    # deliberately leaves open is ``close``'s exit-status completion, and that
    # one is conditional on the sealed decision actually being the
    # ``kernel_exit`` report.

    def _timed_out(self) -> bool:
        with self._outcome_lock:
            return self._outcome.timed_out

    def _fail(self, stop_reason: str, error: str) -> None:
        """Record the driver's failure unless the decision is already final."""
        if self._cancelled.is_set():
            self._record_cancel()
            return
        with self._outcome_lock:
            if self._outcome_sealed:
                logger.debug(
                    "late %s report ignored run=%s: the verdict is decided",
                    stop_reason,
                    self.spec.run_id,
                )
                return
            self._outcome.stop_reason = stop_reason
            self._outcome.error = error

    def _record_cancel(self) -> None:
        """Record a host cancellation unless the decision is already final."""
        with self._outcome_lock:
            if self._outcome_sealed:
                logger.debug(
                    "late cancellation report ignored run=%s: the verdict is decided",
                    self.spec.run_id,
                )
                return
            self._outcome.cancelled = True
            self._outcome.stop_reason = "abort"

    def _record_settled(self) -> None:
        """Record a settled turn unless the decision is already final."""
        with self._outcome_lock:
            if self._outcome_sealed:
                logger.debug(
                    "late settle report ignored run=%s: the verdict is decided",
                    self.spec.run_id,
                )
                return
            self._outcome.settled = True

    def _record_kernel_exit(self, cause: str) -> None:
        """Record the stream loss; :meth:`close` completes it from the reap."""
        with self._outcome_lock:
            if self._outcome_sealed:
                logger.debug(
                    "late kernel_exit report ignored run=%s: the verdict is decided",
                    self.spec.run_id,
                )
                return
            self._kernel_exit_cause = cause
            self._outcome.stop_reason = "kernel_exit"
            self._outcome.error = self._kernel_exit_message(None)

    def _kernel_exit_message(self, exit_code: int | None, *, stderr_tail: str = "") -> str:
        """The ``kernel_exit`` report: status, cause and the agent's stderr tail.

        The stderr tail is the agent's own explanation of its death (ACP itself
        carries JSON-RPC only, so nothing else on the wire has it). It is
        already bounded by :func:`_bounded_stderr_tail`; the caller's redaction
        boundary is applied when this text is logged or committed (the runner
        redacts both the terminal frame's ``error`` and its failure log).
        """
        suffix = f"（exit code {exit_code}）" if exit_code is not None else ""
        cause = self._kernel_exit_cause or ""
        message = f"ACP agent 进程在应答 session/prompt 前退出/失联{suffix}: {cause}"
        if stderr_tail:
            message += f"\nagent stderr 尾部（有界 {ACP_STDERR_TAIL_LINES} 行）:\n{stderr_tail}"
        return message

    def _complete_kernel_exit(
        self, client: AcpRpcClient, exit_code: int | None
    ) -> None:
        """Attach the agent's own status and stderr to a pending ``kernel_exit``.

        The stream ending is not the process exiting. ``close()`` has just
        reaped the child, so its exit status is available now; it is attributed
        to the agent only when the teardown did not have to stop it -- a status
        produced by terminate/kill is the host's doing, not the agent's own.
        The same window is the only one where the agent's stderr has drained
        (the reader finishes when the pipe closes), so the bounded tail is
        collected here too, status or no status: a crash that cannot be reaped
        still deserves its message in the report.

        The completion serves a decision that *is* the ``kernel_exit`` report:
        when the driver's stream loss only surfaced during the teardown of an
        already-decided timeout (or cancellation), the report is late and the
        decided error text stays untouched.
        """
        with self._outcome_lock:
            if self._kernel_exit_cause is None:
                return
            if (
                self._outcome.timed_out
                or self._outcome.cancelled
                or self._outcome.stop_reason != "kernel_exit"
            ):
                logger.debug(
                    "kernel_exit status not attributed run=%s: the verdict is %s",
                    self.spec.run_id,
                    self._outcome.stop_reason,
                )
                return
            protocol_error = getattr(client, "protocol_error", None)
            if protocol_error:
                # The host killed the agent for breaking the framing bound: the
                # status is the host's doing, the violation is the cause.
                self._outcome.error = f"ACP agent 协议违规，宿主已终止进程：{protocol_error}"
                return
            if client.stopped_process:
                return
            stderr_tail = _bounded_stderr_tail(client.stderr_tail(ACP_STDERR_TAIL_LINES))
            self._outcome.error = self._kernel_exit_message(exit_code, stderr_tail=stderr_tail)

    def wait(self, *, cancel: Callable[[], bool], deadline: float) -> ExecutionOutcome:
        while True:
            if cancel():
                # Each decision below and its freeze are one critical section:
                # a driver write racing this point lands either before it (where
                # this decision overrides it) or after it (where the seal
                # refuses it), never between the two.
                self._settle_cancelled()
                self.cancel()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._settle_timeout()
                # Teardown only: the deadline verdict is ``timeout``, so this
                # does not set the host-cancellation flag.
                self._request_cancel()
                break
            if self._done.wait(min(POLL_SECONDS, remaining)):
                self._seal_outcome()
                break
        return self._outcome

    def cancel(self) -> None:
        """Request cancellation (host-initiated); idempotent and thread-safe."""
        self._cancelled.set()
        self._request_cancel()

    def _request_cancel(self) -> None:
        """Send ``session/cancel`` once, if a session exists.

        Safe to call before ``start()`` finished: with no session there is no
        turn to cancel, and ``deliver_input()``/the driver observe the flag and
        never start one.
        """
        client = self._client
        if client is None or self._session_id is None:
            return
        with self._cancel_lock:
            if self._cancel_sent:
                return
            self._cancel_sent = True
        try:
            client.notify("session/cancel", {"sessionId": self._session_id})
        except AcpError as exc:
            logger.debug("session/cancel could not be sent: %s", exc)

    def recover_artifacts(
        self, destination: Path, *, declared: tuple[str, ...] = ()
    ) -> ArtifactRecovery:
        transfer = self._transfer
        if transfer is None or self.sandbox.handle is None:
            # The workspace already is the host directory (in-process, the
            # Docker mount), or the remote sandbox never came up.
            return ArtifactRecovery()
        files, failures = (
            self.sandbox.recover_workspace(destination, declared=declared)
            if declared else self.sandbox.recover_workspace(destination)
        )
        # The sandbox destroys the agent state with it, so the state root is
        # recovered in the same pre-teardown window as the workspace. A failed
        # recovery publishes nothing, which is exactly the fail-closed refusal
        # a follow-up then earns.
        state = transfer.recover_state()
        self._state_recovered = state.ok
        self._state_recovery = state
        if state.ok:
            # 阶段 2（docs/adr/0020）的泊车前提：本轮回收不仅成功，而且由本 run
            # 自己发布（manifest 的 sourceRunId）——租约的代数必须描述权威状态，
            # 而不是某个回收失败后留在沙箱里的旧状态。files 降级路径的发布同样
            # 带本 run 的 manifest（发布即真相），因此也可泊车。
            self._park_ready = self._published_this_run()
        if state.ok and state.mode == "files-fallback" and self.spec.archive is not None:
            # 阶段 1 的降级事实必须可见（裁决）：通道在而回收走了 files 逐文件，
            # 说明通道失败——观测上等价于一次退化，发 kernel_status 而不是
            # 静默吞掉。追问不受影响（发布仍然成功）。
            self._on_frame(
                {
                    "type": "acp_state_channel_degraded",
                    "detail": (
                        "归档通道回收失败，本轮已降级为 files 逐文件回收"
                        f"（{state.files and len(state.files) or 0} 个文件，"
                        f"{state.bytes} 字节）；追问仍可续接"
                    ),
                }
            )
        if state.ok:
            # The recovery's own warning-tier check (ADR 0019): a state that
            # crossed the warning line but still published is served to the
            # next follow-up — visibly, not silently.
            warning = state_threshold_warning(
                files=len(state.files), total_bytes=state.bytes
            )
            if warning:
                self._on_frame(
                    {
                        "type": "acp_state_threshold_warning",
                        "source": "recovery",
                        "detail": warning,
                    }
                )
        return ArtifactRecovery(
            files=tuple(files),
            failures=tuple(failures) + state.failures,
            state_recovered=state.ok,
            secrets=state.secrets,
        )

    def _published_this_run(self) -> bool:
        """Whether the conversation's published manifest names this run.

        The parking precondition (docs/adr/0020): the sandbox's state is only
        a hot cache for authoritative state that **this run** published. A
        recovery that succeeded while the manifest still names an earlier run
        (the legacy layout, a hand-placed state) parks a sandbox the follow-up
        gate would refuse anyway.
        """
        conversation_dir = self.spec.conversation_dir
        if conversation_dir is None:
            return False
        manifest = read_state_manifest(conversation_dir)
        if manifest is None:
            # The legacy per-run layout (pre-阶段 1): nothing conversation-level
            # was published, so there is no authoritative state to cache.
            return False
        return manifest.get("sourceRunId") == self.spec.run_id

    def recovered_session(self) -> SessionInfo | None:
        # The state-root recovery verdict exists only once a remote sandbox's
        # ``recover_artifacts`` ran, so a publishable answer exists from then
        # on (``session_file`` stays None on this family).
        if not self.sandbox.remote:
            return None
        return publishable_recovered_session(self.session_info())

    def child_progress_source(self) -> AcpChildProgressSource | None:
        """Poll the state root's session files for child progress (R3a).

        The source is chosen by the pack's declared ``child_progress``
        capability, never by the pack name: a pack that declares none honestly
        has no running progress on any face. For ``dsh-session-zstd``, a mounting sandbox shares the host state root (the container mounts
        this run's ``HOME``), so one source serves in-process and Docker. The
        sessions are dsh's private zstd format, so the source exists only
        while ``zstandard`` is installed (``pi-glink[acp-progress]``); without
        it this face honestly has no progress. A remote sandbox stays
        **coarse** (``None``): its sessions die with it and no live mirror of
        them is implemented.
        """
        if self.spec.config.pack.capabilities.child_progress != DSH_SESSION_PROGRESS:
            return None
        if self.sandbox.remote or not acp_progress_available():
            return None
        home = getattr(self.spec.runtime, "home", None)
        if home is None:
            return None
        return AcpChildProgressSource(Path(home).joinpath(*DSH_SESSIONS_RELATIVE))

    def identity(self) -> Mapping[str, str]:
        # The carrier's durable identity (container name, sandbox id); none
        # in-process, exactly like the pi family on the same sandbox.
        return dict(self.sandbox.identity())

    def close(self) -> CleanupReport:
        failures: list[str] = []
        client = self._client
        self._client = None
        if client is not None:
            try:
                exit_code = client.close()
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                failures.append(f"关闭 ACP agent 进程失败: {type(exc).__name__}: {exc}")
            else:
                if exit_code is not None:
                    # The reaped status is a fact about the process, not a
                    # verdict: it is recorded even after the decision froze.
                    with self._outcome_lock:
                        self._outcome.exit_code = exit_code
                # The client has reaped the child: a pending stream-loss report
                # gets its exit status here, where that status really exists.
                self._complete_kernel_exit(client, exit_code)
        lease_payload = self._park_sandbox()
        # Client first (stop the agent), then the carrier: the sandbox
        # force-removes the container by its persisted name or hands the E2B
        # sandbox to the conversation lease (paused, docs/adr/0020) instead of
        # killing it. Either failure is reported, never swallowed.
        self._transfer = None
        failures.extend(self.sandbox.close().failures)
        return CleanupReport(
            released=not failures,
            failures=tuple(failures),
            external=dict(self.identity()),
            lease=lease_payload,
        )

    def _park_sandbox(self) -> Mapping[str, Any] | None:
        """Park this run's sandbox as the conversation's hot cache (ADR 0020).

        Runs inside ``close()`` after the agent process is reaped, before the
        carrier teardown. Eligibility: the E2B face, a conversation directory
        (the lease's home), and a recovery this run itself published. A parking
        failure degrades to the destroy (the cold-path semantics) and the fact
        rides on the cleanup report — never as a failure that would keep the
        run holding a slot for a sandbox that is already gone.
        """
        if self.sandbox.kind != "e2b" or self.spec.conversation_dir is None:
            return None
        if not self._park_ready or not self._sandbox_parkable():
            return None
        handle = self.sandbox.handle
        if handle is None:
            return None
        reclaim_failure = self._reclaim_after_agent_stop()
        if reclaim_failure is not None:
            # 停机后增量发布失败 = 沙箱可能持有宿主不知道的状态：不泊车，
            # 沙箱按冷路径销毁，宿主权威状态保持唯一（cc 终审 B1）。
            return {
                "parked": False,
                "reason": f"post-close reclaim failed: {reclaim_failure}",
            }
        outcome = park_sandbox(
            handle,
            config=self.spec.config,
            conversation_dir=self.spec.conversation_dir,
            conversation_run=self._conversation_run(),
            sandbox_id=self.sandbox.session_facts().get("sandboxId", ""),
            template=str(self.sandbox.session_facts().get("template") or ""),
            pack=self.spec.config.harness_pack,
            run_id=self.spec.run_id,
            credential_dests=tuple(
                getattr(self.spec.runtime, "credential_dests", ()) or ()
            ),
            kill_by_id=lambda sandbox_id: kill_sandbox_by_id(
                sandbox_id, config=self.spec.config
            ),
        )
        # 泊车的每一步失败都由 ``park_sandbox`` 内部处理为销毁降级（已含写盘
        # 与 pause 失败）；这里不另设防御分支——若未来出现新的异常面，让它
        # 冒泡进 close 的护栏（released=False 保持持有）比吞成静默降级更诚实。
        if outcome.ok:
            assert outcome.lease is not None
            # The lease owns the paused sandbox from here: the carrier's close
            # hands it over instead of killing it.
            self.sandbox.defer_destroy()
            return outcome.lease.payload()
        payload: dict[str, Any] = {"parked": False, "reason": outcome.reason}
        if not outcome.destroyed:
            payload["destroyFailed"] = True
        return payload

    def _reclaim_after_agent_stop(self) -> str | None:
        """Re-reclaim after the agent stopped; ``None`` = OK, else the reason.

        The first recovery (RecoverStage) runs while the agent is still alive;
        an exit-time flush (dsh writing its session log or index on shutdown)
        between that recovery and the process exit would leave the parked
        sandbox holding state the host manifest does not know — the sandbox
        would silently be the newer authority and the hot/cold paths would
        diverge. Parking therefore re-runs the incremental reclaim **after**
        the client reaped the agent and **before** pausing: the delta is
        whatever the agent wrote on exit; a failed or non-publishing reclaim
        refuses to park (cold-path semantics keep the host authoritative).
        """
        transfer = self._transfer
        if transfer is None:
            return "no transfer"
        try:
            recovery = transfer.recover_state()
        except Exception as exc:  # noqa: BLE001 - reported, drives the park refusal
            return f"{type(exc).__name__}: {exc}"
        if not recovery.ok:
            return "; ".join(recovery.failures) or "recovery failed"
        return None

    def _sandbox_parkable(self) -> bool:
        """Only an ACP-family E2B run parks (the lease is an ACP shape)."""
        return self.spec.config.harness == "acp" and self.sandbox.remote

    def _conversation_run(self) -> str:
        """The conversation's root run id (the lease's owner key)."""
        conversation_dir = self.spec.conversation_dir
        assert conversation_dir is not None
        return conversation_dir.name

    # -- agent -> client ------------------------------------------------------

    def _on_notification(self, frame: Mapping[str, Any]) -> None:
        """Hand one wire notification to the run's projection."""
        if self._suppressing_replay:
            # ``session/load`` is replaying the parent's conversation: these
            # frames are already in the parent run's record. Counting them (the
            # adapter reports the count when the load answers) keeps the drop
            # visible without duplicating an entire transcript into this run.
            with self._replay_lock:
                self._replayed_updates += 1
            return
        self._remember_tool_kind(frame)
        self._on_frame(frame)

    def _remember_tool_kind(self, frame: Mapping[str, Any]) -> None:
        """Remember the kind a ``tool_call``/``tool_call_update`` announced.

        ACP carries the tool category on the tool-call update itself; a later
        permission request for the same ``toolCallId`` may legitimately omit it
        (the field is optional). Remembering the agent's own classification lets
        a tool-kind allowlist keep working for such agents without matching on
        titles or raw input. Only the first non-empty kind per id is kept: the
        initial ``tool_call`` is the classification, later updates must not be
        able to re-label the same tool.
        """
        if frame.get("method") != "session/update":
            return
        params = frame.get("params")
        update = params.get("update") if isinstance(params, Mapping) else None
        if not isinstance(update, Mapping):
            return
        if update.get("sessionUpdate") not in ("tool_call", "tool_call_update"):
            return
        tool_call_id = update.get("toolCallId")
        kind = update.get("kind")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return
        if not isinstance(kind, str) or not kind:
            return
        if tool_call_id in self._tool_kinds:
            return
        if len(self._tool_kinds) >= _TOOL_KIND_MEMORY_LIMIT:
            oldest = next(iter(self._tool_kinds), None)
            if oldest is not None:
                del self._tool_kinds[oldest]
        self._tool_kinds[tool_call_id] = kind

    def _on_request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if method == "session/request_permission":
            return self._answer_permission(params)
        # fs/*, terminal/*, elicitation/* and anything unknown: the host
        # advertised no such capability, so the fail-closed answer is an
        # explicit JSON-RPC refusal rather than an unanswered request.
        raise AcpMethodNotFound(method)

    def _answer_permission(self, params: Mapping[str, Any]) -> Mapping[str, Any]:
        options = params.get("options")
        tool_call = params.get("toolCall")
        tool_call = tool_call if isinstance(tool_call, Mapping) else {}
        tool_call_id = str(tool_call.get("toolCallId") or "")
        # The request may carry the kind itself (dsh, codex-acp and the fake
        # agent do) or omit it (measured in M4); in the
        # latter case the agent already published the same tool's kind in its
        # ``tool_call`` notification, so resolve it by ``toolCallId``. Neither
        # source giving a kind leaves the request unclassified and the
        # configured tool kinds (if any) refuse it.
        request_kind = str(tool_call.get("kind") or "")
        remembered_kind = self._tool_kinds.get(tool_call_id, "")
        tool_kind = request_kind or remembered_kind
        tool_kind_source = "request" if request_kind else ("tool_call" if remembered_kind else "")
        allow = self._allowed_permission_option(options, tool_kind)
        reject: Mapping[str, Any] | None = None
        if isinstance(options, (list, tuple)):
            for kind in _REJECT_KINDS:
                for option in options:
                    if isinstance(option, Mapping) and option.get("kind") == kind:
                        reject = option
                        break
                if reject is not None:
                    break
        if allow is not None:
            option_id = str(allow.get("optionId") or "")
            option_kind = str(allow.get("kind") or "")
            decision = "allowed"
            policy = "allowlist"
            outcome: dict[str, Any] = {"outcome": "selected", "optionId": option_id}
        else:
            option_id = ""
            option_kind = ""
            if reject is not None:
                option_id = str(reject.get("optionId") or "")
                option_kind = str(reject.get("kind") or "")
            if self._cancelled.is_set() or not option_id:
                # Either the turn was already cancelled (the spec requires the
                # ``cancelled`` outcome then) or the agent offered no usable
                # rejection: an unattended host must not allow the tool.
                decision = "cancelled"
                outcome = {"outcome": "cancelled"}
            else:
                decision = "rejected"
                outcome = {"outcome": "selected", "optionId": option_id}
            policy = "fail_closed"
        self._on_frame(
            {
                "type": "acp_permission",
                "decision": decision,
                "policy": policy,
                "optionId": option_id,
                "optionKind": option_kind,
                "toolKind": tool_kind,
                "toolKindSource": tool_kind_source,
                "toolCall": dict(tool_call),
            }
        )
        return {"outcome": outcome}

    def _allowed_permission_option(
        self, options: Any, tool_kind: str
    ) -> Mapping[str, Any] | None:
        """The allow option this run's explicit allowlist selects, if any.

        The default host policy is fail-closed, so an empty allowlist always
        returns ``None``. An allowlist needs a configured **option kind**
        (:data:`~pi_glink.config.ACP_PERMISSION_OPTION_KINDS`); when tool kinds
        are configured too, the request's ``toolCall.kind`` must match them, and
        a request without a tool kind is refused rather than trusted. The
        narrowest option (``allow_once``) is preferred over ``allow_always``.
        """
        allowlist = self.spec.config.pack.capabilities.permission_allow
        if not allowlist:
            return None
        allow_option_kinds = tuple(
            kind for kind in ACP_PERMISSION_OPTION_KINDS if kind in allowlist
        )
        allow_tool_kinds = frozenset(
            token for token in allowlist if token in ACP_PERMISSION_TOOL_KINDS
        )
        if not allow_option_kinds or not isinstance(options, (list, tuple)):
            return None
        if allow_tool_kinds and tool_kind not in allow_tool_kinds:
            return None
        for kind in allow_option_kinds:
            for option in options:
                if (
                    isinstance(option, Mapping)
                    and option.get("kind") == kind
                    and str(option.get("optionId") or "")
                ):
                    return option
        return None


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _bounded_stderr_tail(lines: list[str]) -> str:
    """The tail of an agent's stderr, bounded for one failure report.

    Empty lines are dropped (a crash often prints a run of them), the block is
    cut to the last :data:`ACP_STDERR_TAIL_LINES` lines, each line is clipped,
    and the whole block keeps its **last** characters: the end of a crash output
    is what names the failure. The bounds hold here even though the caller's
    ``stderr_tail(limit)`` already asks for a bounded history.
    """
    if not lines:
        return ""
    clipped = [
        _clip(line, ACP_STDERR_TAIL_LINE_CHARS)
        for line in lines[-ACP_STDERR_TAIL_LINES:]
        if line.strip()
    ]
    if not clipped:
        return ""
    block = "\n".join(clipped)
    return block[-ACP_STDERR_TAIL_CHARS:]
