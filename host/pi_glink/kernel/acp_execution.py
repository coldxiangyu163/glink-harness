"""One run driven over the ACP client.

The face is ``inproc`` in M1: the agent process shares the host machine, the
workspace is the host workspace, and no artifact has to be pulled back. The
protocol work is identical for the container/sandbox faces (M3); only the
process launch and teardown differ.

The :class:`~pi_glink.kernel.execution.ExecutionAdapter` contract is unchanged;
this module maps it onto ACP's method set:

``start()``
    Spawn the agent, ``initialize`` (protocol version + capability negotiation)
    and ``session/new``. A continuation request is refused outright: M1 has no
    ``session/resume`` implementation, and starting a fresh session instead
    would fake a continuation.
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
    No-op: the workspace already is the host directory.
``close()``
    Stop the agent process and report whether that succeeded.

``session/request_permission`` is answered fail-closed (no human is watching):
the first ``reject_once``/``reject_always`` option is selected, or — when the
agent offers no rejection — the request is answered ``cancelled``. Either way
the refusal is emitted as a ``kernel_status`` frame, never silently swallowed.
A run explicitly given ``PI_GLINK_ACP_PERMISSION_ALLOW`` (option kinds and
optionally tool kinds) may instead select the narrowest matching ``allow_*``
option; the decision is still recorded, with the policy that produced it.

The host-side system prompt is delivered as the leading, fenced block of the
``session/prompt`` message (:func:`compose_prompt`): ACP v1 has no system-role
channel, and the baseline text block is the one channel every agent accepts.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

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
    ) -> None:
        super().__init__(spec, on_frame=on_frame)
        self._argv = list(argv)
        self._env = dict(env)
        self._cwd = cwd
        self._client_factory = client_factory
        self._client: AcpRpcClient | None = None
        self._driver: threading.Thread | None = None
        self._done = threading.Event()
        #: The **host** asked for cancellation (``RunManager.stop``/shutdown).
        #: A timeout-driven ``session/cancel`` is teardown, not a user stop, so
        #: it must not turn the verdict into ``cancelled``.
        self._cancelled = threading.Event()
        self._cancel_lock = threading.Lock()
        self._cancel_sent = False
        self._outcome = ExecutionOutcome()
        self._session_id: str | None = None
        self._protocol_version: int | None = None
        self._agent_info: dict[str, Any] = {}
        self._capabilities: dict[str, Any] = {}
        self._auth_methods: list[Any] = []
        self._modes: dict[str, Any] = {}
        self._config_options: list[dict[str, Any]] = []

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self.spec.resume_session:
            # A follow-up carries a parent session. ACP can only continue it via
            # ``session/resume`` (capability-gated, M2 work after real-agent
            # measurement); starting a fresh session here would misrepresent
            # the run as a continuation.
            raise AcpError(
                "ACP 适配器尚不支持续接（session/resume 未实现）；拒绝以新会话冒充追问"
            )
        client = self._client_factory(
            self._argv,
            cwd=self._cwd,
            env=self._env,
            on_notification=self._on_notification,
            on_request=self._on_request,
        )
        self._client = client
        try:
            client.start()
            negotiated = client.request(
                "initialize",
                {
                    "protocolVersion": ACP_PROTOCOL_VERSION,
                    "clientCapabilities": CLIENT_CAPABILITIES,
                    "clientInfo": {"name": "pi-glink"},
                },
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            self._accept_initialize(negotiated)
            created = client.request(
                "session/new",
                {"cwd": str(self.spec.workspace), "mcpServers": []},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
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
        session_id = created.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise AcpProtocolError("session/new 没有返回 sessionId")
        self._session_id = session_id
        self._modes = (
            dict(created["modes"]) if isinstance(created.get("modes"), Mapping) else {}
        )
        self._config_options = _project_config_options(created.get("configOptions"))
        self._apply_session_config(client, session_id)

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
            "cwd": str(self.spec.workspace),
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
                self._outcome.cancelled = True
                self._outcome.stop_reason = "abort"
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
                self._outcome.cancelled = True
                self._outcome.stop_reason = "abort"
            else:
                exit_code = client.exit_code
                suffix = f"（exit code {exit_code}）" if exit_code is not None else ""
                self._fail(
                    "kernel_exit",
                    f"ACP agent 进程在应答 session/prompt 前退出/失联{suffix}: {exc}",
                )
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
                self._outcome.cancelled = True
                self._outcome.stop_reason = "abort"
            elif stop == "cancelled" and not self._outcome.timed_out:
                # The agent stopped the turn itself. A ``cancelled`` answer to
                # this adapter's own timeout teardown is not that case: the
                # deadline verdict was already decided and must stay ``timeout``.
                self._outcome.cancelled = True
                self._outcome.stop_reason = "abort"
            else:
                self._outcome.settled = True
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

    def _fail(self, stop_reason: str, error: str) -> None:
        if self._cancelled.is_set():
            self._outcome.cancelled = True
            self._outcome.stop_reason = "abort"
            return
        self._outcome.stop_reason = stop_reason
        self._outcome.error = error

    def wait(self, *, cancel: Callable[[], bool], deadline: float) -> ExecutionOutcome:
        while True:
            if cancel():
                self._outcome.cancelled = True
                self._outcome.stop_reason = "abort"
                self.cancel()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._outcome.timed_out = True
                self._outcome.stop_reason = "timeout"
                self._outcome.error = (
                    f"run did not settle within {self.spec.timeout_seconds}s"
                )
                # Teardown only: the deadline verdict is ``timeout``, so this
                # does not set the host-cancellation flag.
                self._request_cancel()
                break
            if self._done.wait(min(_POLL_SECONDS, remaining)):
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
        # sandbox), exactly like the pi inproc face.
        return {}

    def close(self) -> CleanupReport:
        failures: list[str] = []
        client = self._client
        self._client = None
        if client is not None:
            try:
                exit_code = client.close()
                if exit_code is not None:
                    self._outcome.exit_code = exit_code
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                failures.append(f"关闭 ACP agent 进程失败: {type(exc).__name__}: {exc}")
        return CleanupReport(
            released=not failures, failures=tuple(failures), external=dict(self.identity())
        )

    # -- agent -> client ------------------------------------------------------

    def _on_notification(self, frame: Mapping[str, Any]) -> None:
        """Hand one wire notification to the run's projection."""
        self._on_frame(frame)

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
        tool_kind = str(tool_call.get("kind") or "")
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
