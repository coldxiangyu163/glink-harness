"""One run driven over the ACP client.

The ACP family shares this adapter across transmission faces: ``inproc`` (the
agent process shares the host machine), ``docker`` (the agent runs in a
container, ``AcpRpcClient`` speaking to ``docker run -i``'s stdio) and ``e2b``
(the agent runs as a sandbox background command driven by ``send_stdin``, wired
through :class:`AcpSandboxExecutionAdapter` below). Only the process launch, the
agent-visible workspace and the teardown differ; the protocol work — and
therefore this module — is the same.

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
    No-op: the workspace already is the host directory (mounted into the
    container on the Docker face).
``close()``
    Stop the agent process, force-remove the container by its persisted name
    when there is one, and report whether that succeeded.

``session/request_permission`` is answered fail-closed (no human is watching):
the first ``reject_once``/``reject_always`` option is selected, or — when the
agent offers no rejection — the request is answered ``cancelled``. Either way
the refusal is emitted as a ``kernel_status`` frame, never silently swallowed.
A run explicitly given ``PI_GLINK_ACP_PERMISSION_ALLOW`` (option kinds and
optionally tool kinds) may instead select the narrowest matching ``allow_*``
option; the decision is still recorded, with the policy that produced it. When
the request omits ``toolCall.kind`` (the field is optional; claude-code-acp
0.16.2 omits it), the kind the agent published for the same ``toolCallId`` in
its ``tool_call`` notification is used, so a tool-kind allowlist keeps working
without matching on titles or raw input.

The host-side system prompt is delivered as the leading, fenced block of the
``session/prompt`` message (:func:`compose_prompt`): ACP v1 has no system-role
channel, and the baseline text block is the one channel every agent accepts.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import __version__ as HOST_VERSION
from ..config import (
    ACP_PERMISSION_OPTION_KINDS,
    ACP_PERMISSION_TOOL_KINDS,
)
from .acp_rpc import (
    REQUEST_TIMEOUT_SECONDS,
    AcpClosed,
    AcpError,
    AcpMethodNotFound,
    AcpProtocolError,
    AcpRequestError,
    AcpRpcClient,
)
from .e2b_env import SANDBOX_WORKSPACE, E2BAcpSandboxClient
from .execution import (
    ArtifactRecovery,
    CleanupReport,
    ExecutionAdapter,
    ExecutionOutcome,
    ExecutionSpec,
    FrameCallback,
    SessionInfo,
    _POLL_SECONDS,
)

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

#: Bound on the session config matrix carried into the ``session_ready`` frame.
_CONFIG_OPTIONS_LIMIT = 20
_CONFIG_VALUES_LIMIT = 100
_CONFIG_TEXT_LIMIT = 300

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


class AcpExecutionAdapter(ExecutionAdapter):
    """The ACP face: one agent process, one session, one prompt turn."""

    kind = "acp"

    def __init__(
        self,
        spec: ExecutionSpec,
        *,
        argv: list[str],
        env: Mapping[str, str],
        cwd: Path | None,
        client_factory: Callable[..., AcpRpcClient] = AcpRpcClient,
        on_frame: FrameCallback,
        session_cwd: str | None = None,
        container_name: str | None = None,
        container_stop: Callable[[str], list[str]] | None = None,
        spawn_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(spec, on_frame=on_frame)
        self._argv = list(argv)
        self._env = dict(env)
        self._cwd = cwd
        self._client_factory = client_factory
        #: How the client starts the agent: ``None`` spawns the argv on this
        #: host; the sandbox face injects a factory for a sandbox background
        #: command. Only the carrier differs — the protocol work below is the
        #: same code for every face.
        self._spawn_factory = spawn_factory
        #: The ``cwd`` handed to ``session/new``: the workspace as the *agent*
        #: sees it. In-process that is the host workspace; in a container it is
        #: the mount point (:data:`~pi_glink.kernel.docker_env.CONTAINER_WORKSPACE`),
        #: because the host path does not exist inside the container.
        self._session_cwd = session_cwd or str(spec.workspace)
        #: Durable container identity, for the run record and reconciliation.
        self._container_name = container_name
        self._container_stop = container_stop
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
        #: ``session/request_permission`` (claude-code-acp 0.16.2 does, measured
        #: in M4) while having published it for the same ``toolCallId``; the
        #: allowlist resolves the request against the agent's own classification
        #: rather than guessing from a human-readable title. A kind never
        #: published stays unknown and the allowlist keeps refusing it.
        self._tool_kinds: dict[str, str] = {}
        self._outcome = ExecutionOutcome()
        #: The outcome is shared: the driver thread reports the turn's end
        #: into it while the run thread decides the verdict from it. ``wait``
        #: freezes it in the same critical section that writes the final
        #: decision, so every driver write lands either before that decision
        #: (which overrides it) or after the freeze (which rejects it) -- the
        #: late-write race can no longer rewrite a decided error text.
        self._outcome_lock = threading.Lock()
        self._outcome_sealed = False
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

    def start(self) -> None:
        client_kwargs: dict[str, Any] = {
            "cwd": self._cwd,
            "env": self._env,
            "on_notification": self._on_notification,
            "on_request": self._on_request,
        }
        if self._spawn_factory is not None:
            client_kwargs["spawn"] = self._spawn_factory
        client = self._client_factory(self._argv, **client_kwargs)
        self._client = client
        try:
            client.start()
            negotiated = client.request(
                "initialize",
                {
                    "protocolVersion": ACP_PROTOCOL_VERSION,
                    "clientCapabilities": CLIENT_CAPABILITIES,
                    # ACP's ``Implementation`` carries both ``name`` and
                    # ``version``; claude-code-acp 0.16.2 validates the
                    # initialize params strictly and rejects a clientInfo
                    # without ``version`` with -32602 before any session
                    # exists (measured, M4). The host's own version is the
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
        self._config_options = _project_config_options(created.get("configOptions"))
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
            answer = client.request(method, params, timeout=REQUEST_TIMEOUT_SECONDS)
        finally:
            replayed = self._end_replay_suppression()
        self._session_id = session_id
        self._modes = (
            dict(answer["modes"]) if isinstance(answer.get("modes"), Mapping) else {}
        )
        self._config_options = _project_config_options(answer.get("configOptions"))
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

    def _begin_replay_suppression(self) -> None:
        with self._replay_lock:
            self._suppressing_replay = True
            self._replayed_updates = 0

    def _end_replay_suppression(self) -> int:
        with self._replay_lock:
            self._suppressing_replay = False
            return self._replayed_updates

    def _apply_session_config(self, client: AcpRpcClient, session_id: str) -> None:
        """Apply ``PI_GLINK_ACP_SESSION_CONFIG`` right after ``session/new``.

        Modes, model and reasoning options are advertised by the agent as
        ``configOptions``; the values differ per agent, so nothing is hardcoded.
        An explicitly configured option or value the agent does not advertise
        fails the run: silently dropping it would run a policy the operator did
        not choose (fail closed). When the advertised current value already
        matches, no request is sent.
        """
        for config_id, value in self.spec.config.acp_session_config:
            option = next(
                (
                    entry
                    for entry in self._config_options
                    if str(entry.get("id") or "") == config_id
                ),
                None,
            )
            if option is None:
                raise AcpProtocolError(
                    f"ACP agent 未提供配置选项 {config_id!r}；"
                    "PI_GLINK_ACP_SESSION_CONFIG 不能被静默忽略"
                )
            if str(option.get("type") or "select") == "select":
                values = _select_values(option)
                if value not in values:
                    raise AcpProtocolError(
                        f"ACP 配置选项 {config_id!r} 不接受取值 {value!r}；"
                        f"可选：{', '.join(values)}（协议要求先按 agent 声明取值）"
                    )
            if option.get("currentValue") == value:
                continue
            updated = client.request(
                "session/set_config_option",
                {"sessionId": session_id, "configId": config_id, "value": value},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            options = updated.get("configOptions") if isinstance(updated, Mapping) else None
            if isinstance(options, list):
                self._config_options = _project_config_options(options)
            else:
                for entry in self._config_options:
                    if str(entry.get("id") or "") == config_id:
                        entry["currentValue"] = value
            # codex-acp models its approval presets both as a ``mode`` config
            # option and as ``modes``; keep the run record's mode state truthful
            # when the value really is one of the advertised mode ids.
            mode_ids = {
                str(mode.get("id"))
                for mode in self._modes.get("availableModes", [])
                if isinstance(mode, Mapping)
            }
            if config_id == "mode" and value in mode_ids:
                self._modes["currentModeId"] = value

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
            # face. This is the ACP fact, not a host path.
            "cwd": self._session_cwd,
        }
        if self._resume_method_used is not None:
            # A continuation's session id is the parent's own id, so the run
            # record can prove the agent reattached to exactly that session
            # rather than opening a new one.
            payload["resumed"] = True
            payload["resumeMethod"] = self._resume_method_used
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
    # Every write below is guarded by ``_outcome_lock`` and refused once the
    # outcome is sealed. Only ``wait`` seals it, and it does so in the same
    # critical section that writes the decision, so a driver write either
    # precedes that decision (and is overridden by it) or follows the seal
    # (and is dropped). The one write the seal deliberately leaves open is
    # ``close``'s exit-status completion, and that one is conditional on the
    # sealed decision actually being the ``kernel_exit`` report.

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

    def _settle_cancelled(self) -> None:
        """Decide cancellation and freeze the outcome in one critical section."""
        with self._outcome_lock:
            self._outcome.cancelled = True
            self._outcome.stop_reason = "abort"
            self._outcome_sealed = True

    def _settle_timeout(self) -> None:
        """Decide the deadline and freeze the outcome in one critical section."""
        with self._outcome_lock:
            self._outcome.timed_out = True
            self._outcome.stop_reason = "timeout"
            self._outcome.error = (
                f"run did not settle within {self.spec.timeout_seconds}s"
            )
            self._outcome_sealed = True

    def _seal_outcome(self) -> None:
        """Freeze the driver-written decision before the verdict is read."""
        with self._outcome_lock:
            self._outcome_sealed = True

    def _kernel_exit_message(self, exit_code: int | None) -> str:
        """The ``kernel_exit`` report, with the agent's status once it is known."""
        suffix = f"（exit code {exit_code}）" if exit_code is not None else ""
        cause = self._kernel_exit_cause or ""
        return f"ACP agent 进程在应答 session/prompt 前退出/失联{suffix}: {cause}"

    def _complete_kernel_exit(
        self, client: AcpRpcClient, exit_code: int | None
    ) -> None:
        """Attach the agent's own status to a pending ``kernel_exit`` report.

        The stream ending is not the process exiting. ``close()`` has just
        reaped the child, so its exit status is available now; it is attributed
        to the agent only when the teardown did not have to stop it -- a status
        produced by terminate/kill is the host's doing, not the agent's own.

        The completion serves a decision that *is* the ``kernel_exit`` report:
        when the driver's stream loss only surfaced during the teardown of an
        already-decided timeout (or cancellation), the report is late and the
        decided error text stays untouched.
        """
        if exit_code is None:
            return
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
            if client.stopped_process:
                return
            self._outcome.error = self._kernel_exit_message(exit_code)

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
            if self._done.wait(min(_POLL_SECONDS, remaining)):
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

    def recover_artifacts(self, destination: Path) -> ArtifactRecovery:
        # The workspace already is the host directory for this face.
        return ArtifactRecovery()

    def identity(self) -> Mapping[str, str]:
        # An in-process agent has no durable external identity (no container, no
        # sandbox), exactly like the pi inproc face. A containerised ACP agent
        # has one: the frozen container name.
        return {"container": self._container_name} if self._container_name else {}

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
        if self._container_name and self._container_stop is not None:
            # Closing stdin ends the agent; with ``--rm`` the container is then
            # removed by the daemon. The force-remove is the belt-and-braces
            # confirmation that nothing keeps running, addressed by the one
            # identity this run persisted — never a name pattern.
            try:
                result = subprocess.run(
                    self._container_stop(self._container_name),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                stderr = (result.stderr or "").strip()
                if result.returncode != 0 and "No such container" not in stderr:
                    failures.append(
                        f"删除容器失败: exit {result.returncode}: {stderr[-200:]}"
                    )
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                failures.append(f"删除容器失败: {type(exc).__name__}: {exc}")
        return CleanupReport(
            released=not failures, failures=tuple(failures), external=dict(self.identity())
        )

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
        # agent do) or omit it (claude-code-acp 0.16.2, measured in M4); in the
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
        allowlist = self.spec.config.acp_permission_allow
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


class AcpSandboxExecutionAdapter(AcpExecutionAdapter):
    """The ACP family inside an E2B sandbox (M3b).

    Composition, not a second protocol implementation: the sandbox's background
    command is wrapped as a ``subprocess``-shaped process
    (:class:`~pi_glink.kernel.e2b_env.SandboxCommandProcess`) and handed to the
    *same* :class:`AcpRpcClient` the in-process and Docker faces use, so
    ``initialize``/``session/new``/``session/prompt`` handling, permission
    answers, turn boundaries and the verdict inputs are the same code.

    Face-specific work only:

    * the sandbox is created first — its identity is persisted immediately, like
      the pi face — and the run's rendered ACP ``HOME`` (credentials, 0600
      inside the sandbox) is uploaded before the agent starts;
    * the agent-visible workspace is the sandbox path, so ``session/new``'s
      ``cwd`` is that path and never a host path the sandbox cannot see;
    * the workspace is recovered from the sandbox before it is destroyed (the pi
      face's rule), and ``close()`` kills the sandbox after the client stopped
      the agent.
    """

    kind = "acp-e2b"

    def __init__(
        self,
        spec: ExecutionSpec,
        *,
        sandbox: E2BAcpSandboxClient,
        client_factory: Callable[..., AcpRpcClient] = AcpRpcClient,
        on_frame: FrameCallback,
        on_identity: Callable[[Mapping[str, str]], None] | None = None,
    ) -> None:
        super().__init__(
            spec,
            argv=sandbox.argv,
            # The agent's environment is per command (per-run HOME plus the
            # configured entries); the client's ``env`` is only used when it
            # spawns a host process, which this face never does.
            env={},
            cwd=None,
            client_factory=client_factory,
            on_frame=on_frame,
            session_cwd=SANDBOX_WORKSPACE,
            spawn_factory=sandbox.spawn,
        )
        self._sandbox = sandbox
        self._on_identity = on_identity

    def start(self) -> None:
        self._sandbox.start(timeout_seconds=self.spec.timeout_seconds + 300)
        # The sandbox exists from here on: hand its identity to the host before
        # any step that can fail, so a partial start still leaves a named
        # leftover the operator and the online sweep can act on.
        if self._sandbox.sandbox_id and self._on_identity is not None:
            self._on_identity({"sandbox": self._sandbox.sandbox_id})
        self._sandbox.upload_home()
        super().start()

    def session_info(self) -> SessionInfo | None:
        session = super().session_info()
        if session is None:
            return None
        payload = dict(session.payload)
        # Same facts the pi E2B face records: which sandbox carried the run and
        # which template it came from.
        payload["sandboxId"] = self._sandbox.sandbox_id
        payload["template"] = self._sandbox.template
        return SessionInfo(
            payload=payload, session_id=session.session_id, session_file=None
        )

    def recover_artifacts(self, destination: Path) -> ArtifactRecovery:
        files, failures = self._sandbox.recover_artifacts(destination)
        return ArtifactRecovery(files=tuple(files), failures=tuple(failures))

    def close(self) -> CleanupReport:
        # Client first (stop the agent), then the sandbox. The base report
        # carries the client/container failures; this face adds the sandbox
        # destruction. Either failure is reported, never swallowed.
        report = super().close()
        failures = list(report.failures)
        failures.extend(self._sandbox.close())
        return CleanupReport(
            released=not failures,
            failures=tuple(failures),
            external=dict(self.identity()),
        )

    def identity(self) -> Mapping[str, str]:
        return {"sandbox": self._sandbox.sandbox_id} if self._sandbox.sandbox_id else {}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _project_config_options(options: Any) -> list[dict[str, Any]]:
    """A bounded, JSON-safe copy of the agent's advertised session options."""
    if not isinstance(options, list):
        return []
    projected: list[dict[str, Any]] = []
    for option in options[:_CONFIG_OPTIONS_LIMIT]:
        if not isinstance(option, Mapping):
            continue
        entry: dict[str, Any] = {
            key: option[key]
            for key in ("id", "name", "type", "category", "currentValue")
            if key in option
        }
        if isinstance(option.get("description"), str):
            entry["description"] = _clip(option["description"], _CONFIG_TEXT_LIMIT)
        values = option.get("options")
        if isinstance(values, list):
            entry["options"] = [
                value
                for value in values[:_CONFIG_VALUES_LIMIT]
                if isinstance(value, Mapping)
            ]
        projected.append(entry)
    return projected


def _select_values(option: Mapping[str, Any]) -> tuple[str, ...]:
    """Every selectable value id of one option, grouped or flat.

    ACP's ``SessionConfigSelect`` allows either a flat option list or grouped
    entries (dsh groups the model choices by provider); both shapes resolve to
    the same value ids.
    """
    values: list[str] = []
    entries = option.get("options")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            grouped = entry.get("options")
            if isinstance(grouped, list):
                for inner in grouped:
                    if isinstance(inner, Mapping) and inner.get("value") is not None:
                        values.append(str(inner["value"]))
            elif entry.get("value") is not None:
                values.append(str(entry["value"]))
    return tuple(values)
