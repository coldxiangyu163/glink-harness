"""Execution adapters: the per-face mechanics behind one run lifecycle.

The runner owns the product lifecycle (run states, event persistence, artifact
policy, resource bookkeeping). Each execution face — in-process pi, a disposable
Docker container, an E2B sandbox — differs in how a kernel is launched, spoken
to, cancelled, recovered and released. Those differences live behind
:class:`ExecutionAdapter` so the runner never grows a second copy of "wait for
settle", "kill on cancel" or "pull the report back".

The frozen input is :class:`ExecutionSpec`: the run id, the kernel prompt, the
system prompt, the per-run workspace, the per-run rendered configuration and
the run's timeout. ``harness`` (the pi kernel or an ACP agent) and ``executor``
(the in-process, Docker or E2B transmission face) are orthogonal: the adapter
count is "harness families × transmission faces", not one adapter per pair (see
``docs/multi-harness-plan.md``). An adapter never re-reads global environment
for data the spec already carries (image, template, credentials, paths), and
never writes to another run's workspace.

The contract is:

``start()``
    Bring the kernel up. Raises if the face cannot be created; the run then
    fails with the adapter's error and ``close()`` still runs.
``session_info()``
    The kernel-reported session identity, for the host to publish. May return
    ``None`` when the face has no session concept.
``deliver_input()``
    Hand the frozen prompt to the kernel. Raises when the kernel rejects it.
``wait(cancel, deadline)``
    Drive/observe the execution until it settles, is cancelled, times out or
    the kernel exits. Never raises for an ordinary failure: the failure is
    reported in :class:`ExecutionOutcome`.
``cancel()``
    Request cancellation. Idempotent and safe to call from another thread.
``recover_artifacts(destination)``
    Ensure the face's work products are on the host (a no-op for faces whose
    workspace already is the host workspace). Bounded, validated, and honest
    about files it skipped.
``recovered_session()``
    Session identity that only became knowable through that recovery and is
    worth (re)publishing — the sandbox faces learn such facts while pulling
    the workspace back; every other face answers ``None``.
``recover_subagents(destination)``
    Optional, faces with subagent side-products only (the pi sandbox face):
    mirror branched sessions and subagent artifacts out of the dying sandbox
    before ``close()``. Faces whose subagent files already live on the host
    (in-process, Docker) do not implement it — there is nothing to pull back.
``close()``
    Release the process/container/sandbox and report whether that succeeded.
    A cleanup failure is reported, never silently swallowed, and never
    overwrites the run's result.
``identity()``
    Durable external identity (container name, sandbox id) the run record keeps
    so a restarted host can reconcile leftovers instead of guessing.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, TYPE_CHECKING

from ..config import ConfigError, HostConfig
from .acp_e2b_env import E2BAcpSandboxClient
from .acp_rpc import AcpRpcClient
from .agent_env import AgentRuntime
from .docker_env import CONTAINER_WORKSPACE
from .e2b_env import (
    E2BSandboxClient,
    SandboxError,
    parse_json_stream_line,
)
from .outcome import POLL_SECONDS, ExecutionOutcome, SealedOutcome
from .rpc import PiRpcClient, PiRpcError

if TYPE_CHECKING:  # pragma: no cover - annotation only, avoids an import cycle
    from .acp_env import AcpRuntime

logger = logging.getLogger("pi_glink.kernel.execution")

#: Deadline for pi to accept and answer a command frame.
REQUEST_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ExecutionSpec:
    """Everything an adapter may use, frozen when the run was accepted."""

    config: HostConfig
    #: The per-run rendered configuration for this run's harness: pi's
    #: :class:`AgentRuntime` for the pi family, the ACP state root for the ACP
    #: family. A harness-specific plan reads its own shape.
    runtime: AgentRuntime | AcpRuntime
    run_id: str
    prompt: str
    system_prompt: str
    workspace: Path
    timeout_seconds: int
    extra_argv: tuple[str, ...] = ()
    resume_session: str | None = None
    expected_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class CleanupReport:
    """Whether the face's resources are actually released."""

    released: bool
    failures: tuple[str, ...] = ()
    external: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ArtifactRecovery:
    """Files pulled into the host workspace, and those that were refused.

    ``state_recovered`` is the face's own verdict on an *agent state root* it
    had to fetch back before tearing the execution down (the E2B face: the
    sandbox destroys the state with it). ``None`` means the face has no such
    state to fetch — the in-process, Docker and pi faces keep it on the host.
    The verdict is durable because the face publishes the recovered root only
    when the whole bounded copy succeeded; a ``False`` (or a missing root)
    keeps every follow-up refused.

    ``secrets`` are credential-shaped values discovered in recovered material
    (never the values themselves in any log): the runner adds them to the run's
    redaction boundary so a value the host only learned from the recovered
    state is masked exactly like an injected credential.
    """

    files: tuple[Path, ...] = ()
    failures: tuple[str, ...] = ()
    state_recovered: bool | None = None
    secrets: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Kernel-reported session identity (fields the host may publish)."""

    payload: Mapping[str, Any]
    session_id: str | None = None
    session_file: str | None = None


FrameCallback = Callable[[Mapping[str, Any]], None]


def publishable_recovered_session(session: SessionInfo | None) -> SessionInfo | None:
    """The session a face may (re)publish after artifact recovery, or ``None``.

    Recovery teaches the host something new only when the pi sandbox face's
    session file travelled back with the workspace, or the ACP sandbox face
    decided its state-root recovery verdict. Anything else was already known
    (and published, when appropriate) before the turn started.
    """
    if session is None:
        return None
    if session.session_file or session.payload.get("stateRootRecovered") is not None:
        return session
    return None


class ExecutionAdapter(ABC):
    """One execution face for one run."""

    kind: str = "unknown"

    def __init__(self, spec: ExecutionSpec, *, on_frame: FrameCallback) -> None:
        self.spec = spec
        self._on_frame = on_frame

    # -- lifecycle ------------------------------------------------------------

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def deliver_input(self) -> None: ...

    @abstractmethod
    def wait(self, *, cancel: Callable[[], bool], deadline: float) -> ExecutionOutcome: ...

    @abstractmethod
    def cancel(self) -> None: ...

    @abstractmethod
    def recover_artifacts(self, destination: Path) -> ArtifactRecovery: ...

    @abstractmethod
    def close(self) -> CleanupReport: ...

    def session_info(self) -> SessionInfo | None:  # pragma: no cover - optional
        return None

    def recovered_session(self) -> SessionInfo | None:
        """Session facts learned through ``recover_artifacts`` worth publishing.

        Only the sandbox faces learn session facts while pulling work products
        back; every other face knows its session before the turn, so the base
        answer is ``None``.
        """
        return None

    def identity(self) -> Mapping[str, str]:
        return {}


class RpcExecutionAdapter(ExecutionAdapter):
    """pi behind the bidirectional RPC protocol (in-process or in a container).

    Both faces speak the same protocol over stdio; the difference is only the
    command line, the environment and how the process is torn down. ``argv``,
    ``env`` and ``cwd`` are prepared by the runner's face-specific plan and are
    frozen in the adapter.
    """

    def __init__(
        self,
        spec: ExecutionSpec,
        *,
        argv: list[str],
        env: Mapping[str, str],
        cwd: Path | None,
        client_factory: Callable[..., PiRpcClient],
        on_frame: FrameCallback,
        kind: str = "inproc",
        container_name: str | None = None,
        container_stop: Callable[[str], list[str]] | None = None,
    ) -> None:
        super().__init__(spec, on_frame=on_frame)
        self.kind = kind
        self._argv = list(argv)
        self._env = dict(env)
        self._cwd = cwd
        self._client_factory = client_factory
        self._client: PiRpcClient | None = None
        self._container_name = container_name
        self._container_stop = container_stop
        self._settled = threading.Event()
        self._outcome = ExecutionOutcome()

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        client = self._client_factory(
            self._argv,
            cwd=self._cwd,
            env=self._env,
            on_event=self._handle_frame,
        )
        self._client = client
        client.start()

    def _handle_frame(self, frame: Mapping[str, Any]) -> None:
        if frame.get("type") == "agent_settled":
            self._settled.set()
        self._on_frame(frame)

    def session_info(self) -> SessionInfo | None:
        client = self._require_client()
        try:
            state = client.request({"type": "get_state"}, timeout=30.0)
        except PiRpcError as exc:
            logger.warning("get_state failed run=%s: %s", self.spec.run_id, exc)
            return None
        if not state.success:
            logger.warning("get_state rejected run=%s: %s", self.spec.run_id, state.error)
            return None
        data = state.data
        model = data.get("model")
        session_id = data.get("sessionId")
        session_file = data.get("sessionFile")
        return SessionInfo(
            payload={
                "sessionId": session_id,
                "sessionFile": session_file,
                "model": model.get("id") if isinstance(model, Mapping) else None,
                "thinkingLevel": data.get("thinkingLevel"),
            },
            session_id=str(session_id) if session_id else None,
            session_file=str(session_file) if session_file else None,
        )

    def deliver_input(self) -> None:
        client = self._require_client()
        accepted = client.request(
            {"type": "prompt", "message": self.spec.prompt},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        if not accepted.success:
            raise PiRpcError(accepted.error or "pi rejected the prompt")

    def wait(self, *, cancel: Callable[[], bool], deadline: float) -> ExecutionOutcome:
        outcome = self._outcome
        client = self._require_client()
        while True:
            if cancel():
                outcome.cancelled = True
                outcome.stop_reason = "abort"
                self._abort()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome.timed_out = True
                outcome.stop_reason = "timeout"
                outcome.error = (
                    f"run did not settle within {self.spec.timeout_seconds}s"
                )
                self._abort()
                break
            if self._settled.wait(min(POLL_SECONDS, remaining)):
                outcome.settled = True
                break
            if not client.running:
                # The process is gone. Give the reader thread a moment to flush
                # a settle frame that raced the exit; if none arrives, the run
                # converges now instead of waiting for the whole task timeout.
                if self._settled.wait(0.5):
                    outcome.settled = True
                    break
                outcome.exit_code = client.exit_code
                outcome.stop_reason = "kernel_exit"
                outcome.error = (
                    "pi 进程在接受指令后退出，未报告 agent_settled"
                    + (
                        f"（exit code {outcome.exit_code}）"
                        if outcome.exit_code is not None
                        else ""
                    )
                )
                break
        return outcome

    def cancel(self) -> None:
        self._abort()

    def recover_artifacts(self, destination: Path) -> ArtifactRecovery:
        # The workspace already is the host directory for this face.
        return ArtifactRecovery()

    def identity(self) -> Mapping[str, str]:
        return {"container": self._container_name} if self._container_name else {}

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
                failures.append(f"关闭 pi 进程失败: {type(exc).__name__}: {exc}")
        if self._container_name and self._container_stop is not None:
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

    # -- internals ------------------------------------------------------------

    def _require_client(self) -> PiRpcClient:
        if self._client is None:
            raise PiRpcError("pi 客户端未启动")
        return self._client

    def _abort(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            client.request({"type": "abort"}, timeout=10.0)
        except (PiRpcError, OSError) as exc:
            # The cancel flag is what ends the run; a failed abort only makes the
            # wait longer. Log it instead of turning the request into a rejection.
            logger.warning(
                "abort request failed run=%s: %s: %s (cancellation still accepted)",
                self.spec.run_id,
                type(exc).__name__,
                exc,
            )


class E2BSandboxExecutionAdapter(SealedOutcome, ExecutionAdapter):
    """pi inside an E2B sandbox, streaming its single-shot JSON event stream.

    The sandbox kernel runs ``pi --mode json`` once; its stdout is teed inside
    the sandbox so the host can project complete LF-framed records while the
    command runs (the SDK's background ``CommandHandle`` is the streaming
    interface). The file is the completeness authority: a stream that drops
    before an end event is recovered from the file plus the sandbox's
    exit-code marker, so the run is not lost and no frame is delivered twice.
    """

    kind = "e2b"

    def __init__(
        self,
        spec: ExecutionSpec,
        *,
        on_frame: FrameCallback,
        sandbox_factory: Callable[..., Any] | None = None,
        on_identity: Callable[[Mapping[str, str]], None] | None = None,
    ) -> None:
        super().__init__(spec, on_frame=on_frame)
        self._sandbox_factory = sandbox_factory
        self._on_identity = on_identity
        self._client: E2BSandboxClient | None = None
        # The outcome, its lock and the seal protocol come from SealedOutcome;
        # the driver thread reports the sandbox command's result into it while
        # the run thread decides the verdict from it.
        self._done = threading.Event()
        self._cancelled = threading.Event()
        self._released = True
        self._release_failures: list[str] = []
        self._driver: threading.Thread | None = None
        self._session_file: Path | None = None
        self._session_id: str | None = None
        #: The sandbox id as soon as it exists, kept even after ``close()``: a
        #: failure later in ``start()`` must not erase the durable identity.
        self._sandbox_id: str = ""

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        client = E2BSandboxClient(
            self.spec.config,
            self.spec.runtime,
            run_id=self.spec.run_id,
            workspace=self.spec.workspace,
            sandbox_factory=self._sandbox_factory,
        )
        self._client = client
        try:
            client.start(timeout_seconds=self.spec.timeout_seconds + 300)
            # The sandbox exists from here on. Hand its identity to the host
            # before any step that can fail, so a partial creation still leaves
            # a named leftover the operator and the online sweep can act on.
            self._record_identity(client.sandbox_id)
            client.upload_config()
            if self.spec.resume_session:
                client.upload_continuation(self.spec.resume_session, self.spec.workspace)
        except Exception:
            # A partially created sandbox must still be destroyed.
            self._release_failures.extend(client.close())
            self._released = not self._release_failures
            self._client = None
            raise

    def _record_identity(self, sandbox_id: str) -> None:
        if not sandbox_id:
            return
        self._sandbox_id = sandbox_id
        if self._on_identity is not None:
            self._on_identity({"sandbox": sandbox_id})

    def session_info(self) -> SessionInfo | None:
        client = self._client
        if client is None:
            return None
        return SessionInfo(
            payload={
                "sandboxId": client.sandbox_id,
                "template": self.spec.config.e2b_template,
                "sessionId": self._session_id,
            },
            session_id=self._session_id,
            session_file=str(self._session_file) if self._session_file else None,
        )

    def recovered_session(self) -> SessionInfo | None:
        # This face discovers its session file only when the recovery pulls it
        # back from the sandbox, so a publishable answer exists from then on.
        return publishable_recovered_session(self.session_info())

    def deliver_input(self) -> None:
        if self._client is None:
            raise PiRpcError("sandbox 未启动")
        self._driver = threading.Thread(
            target=self._drive, name=f"e2b-{self.spec.run_id}", daemon=True
        )
        self._driver.start()

    def _drive(self) -> None:
        client = self._client
        assert client is not None
        try:
            result = client.run_prompt(
                self.spec.prompt,
                system_prompt=self.spec.system_prompt,
                resume_session=self.spec.resume_session,
                on_line=self._deliver_line,
                is_cancelled=self._cancelled.is_set,
            )
            if self._cancelled.is_set():
                return
            if result.exit_code is None:
                # The stream was lost and the sandbox never wrote its exit-code
                # marker: there is no honest way to call this settled.
                raise SandboxError(
                    "sandbox command ended without a recoverable exit code: "
                    f"{result.stream_error or 'unknown'}"
                )
            if self.spec.expected_session_id and self._session_id != self.spec.expected_session_id:
                raise PiRpcError("continued session identity did not match the parent")
            self._record_result(
                exit_code=int(result.exit_code), stderr_tail=result.stderr_tail
            )
        except Exception as exc:  # noqa: BLE001 - reported in the outcome
            if self._cancelled.is_set():
                return
            self._record_failure(f"{type(exc).__name__}: {exc}")
        finally:
            self._done.set()

    def _deliver_line(self, line: str) -> None:
        """Project one LF-framed record — streamed live or caught up from file.

        The session id is captured here, so it is set whether the record
        arrived on the stream or from the file after a dropped connection.
        """
        frame = parse_json_stream_line(line)
        if frame is None:
            return
        if frame.get("type") == "session" and isinstance(frame.get("id"), str):
            self._session_id = frame["id"]
        self._on_frame(frame)

    # -- the outcome's writers ------------------------------------------------
    #
    # The lock, the seal flag and the settle/seal decisions are the shared
    # :class:`SealedOutcome` protocol; the two writers below stay here because
    # their error text is this face's own. Unlike the ACP face there is no
    # controlled late write here: ``close`` does not complete this outcome.

    def _record_result(self, *, exit_code: int, stderr_tail: str) -> None:
        """Record the sandbox command's result unless the verdict is decided."""
        with self._outcome_lock:
            if self._outcome_sealed:
                logger.debug(
                    "late sandbox result ignored run=%s: the verdict is decided",
                    self.spec.run_id,
                )
                return
            self._outcome.exit_code = exit_code
            if exit_code != 0:
                self._outcome.stop_reason = "kernel_exit"
                self._outcome.error = (
                    f"sandbox pi exited with {exit_code}: {stderr_tail[-300:]}"
                )
            else:
                # ``--mode json`` exits after the supplied prompt finishes: a
                # zero exit is this face's settle signal.
                self._outcome.settled = True

    def _record_failure(self, message: str) -> None:
        """Record the driver's error unless the verdict is decided."""
        with self._outcome_lock:
            if self._outcome_sealed:
                logger.debug(
                    "late failure report ignored run=%s: the verdict is decided",
                    self.spec.run_id,
                )
                return
            self._outcome.stop_reason = "execution_error"
            self._outcome.error = message

    def wait(self, *, cancel: Callable[[], bool], deadline: float) -> ExecutionOutcome:
        while True:
            if cancel() and not self._done.is_set():
                self.cancel()
            remaining = deadline - time.monotonic()
            if self._done.wait(min(POLL_SECONDS, max(remaining, 0.0))):
                # The driver's report is complete the moment ``_done`` is set
                # (its outcome writes precede the ``finally``): seal it before
                # the verdict reads it.
                self._seal_outcome()
                break
            if remaining <= 0:
                # The decision and its freeze are one critical section: a
                # driver write racing this point lands either before it (where
                # this decision overrides it) or after the seal (where it is
                # refused), never between the two.
                self._settle_timeout()
                self.cancel()
                self._done.wait(30.0)
                break
        if cancel():
            self._settle_cancelled()
        return self._outcome

    def cancel(self) -> None:
        """Kill the sandbox; the blocked command handle then returns or raises."""
        self._cancelled.set()
        client = self._client
        if client is None:
            return
        failures = client.close()
        if failures:
            self._release_failures.extend(failures)
            self._released = False

    def recover_artifacts(self, destination: Path) -> ArtifactRecovery:
        client = self._client
        if client is None:
            if self._cancelled.is_set():
                # A cancelled sandbox is destroyed by design; no work exists to
                # recover, which is not a failure.
                return ArtifactRecovery()
            return ArtifactRecovery(failures=("sandbox 已关闭，产物不可回收",))
        files, failures = client.recover_artifacts(destination)
        session_file, session_failure = client.recover_session()
        self._session_file = session_file
        if session_failure:
            failures.append(session_failure)
        return ArtifactRecovery(files=tuple(files), failures=tuple(failures))

    def recover_subagents(self, destination: Path) -> ArtifactRecovery:
        """Mirror subagent sessions/artifacts while the sandbox still exists."""
        client = self._client
        if client is None:
            if self._cancelled.is_set():
                return ArtifactRecovery()
            return ArtifactRecovery(failures=("sandbox 已关闭，子代理产物不可回收",))
        files, failures = client.recover_subagent_files(destination)
        return ArtifactRecovery(files=tuple(files), failures=tuple(failures))

    def close(self) -> CleanupReport:
        client = self._client
        self._client = None
        self._driver = None
        if client is not None:
            self._release_failures.extend(client.close())
        failures = tuple(self._release_failures)
        return CleanupReport(
            released=not failures, failures=failures, external=dict(self.identity())
        )

    def identity(self) -> Mapping[str, str]:
        return {"sandbox": self._sandbox_id} if self._sandbox_id else {}


def build_adapter(
    spec: ExecutionSpec,
    *,
    harness: str = "pi",
    executor: str,
    client_factory: Callable[..., PiRpcClient],
    on_frame: FrameCallback,
    sandbox_factory: Callable[..., Any] | None = None,
    on_identity: Callable[[Mapping[str, str]], None] | None = None,
    rpc_plan: Callable[[ExecutionSpec], tuple[list[str], Mapping[str, str], Path | None]],
    docker_plan: Callable[[ExecutionSpec], tuple[list[str], Mapping[str, str], str]],
    docker_stop: Callable[[str], list[str]],
    acp_plan: Callable[[ExecutionSpec], tuple[list[str], Mapping[str, str], Path | None]]
    | None = None,
    acp_docker_plan: Callable[[ExecutionSpec], tuple[list[str], Mapping[str, str], str]]
    | None = None,
    acp_e2b_plan: Callable[[ExecutionSpec], "E2BAcpSandboxClient"] | None = None,
    acp_client_factory: Callable[..., AcpRpcClient] | None = None,
) -> ExecutionAdapter:
    """Compose the adapter for one run's frozen harness and execution face.

    ``harness`` selects the adapter family (``pi`` or ``acp``), ``executor`` the
    transmission face within it. ``rpc_plan``/``docker_plan``/``acp_plan`` are
    the harness-and-face-specific input builders kept in the runner (they touch
    the run's rendered configuration); the adapter owns everything after that.
    ``on_identity`` is called as soon as a face knows the durable identity of
    the resource it created, before any later step can fail.

    The ACP family shares one adapter across faces: ``inproc`` spawns the agent
    directly; ``docker`` spawns ``docker run -i`` and hands the container's
    stdio to the same :class:`AcpRpcClient`; ``e2b`` creates the run's sandbox
    and hands a sandbox background command's handle to the same client through
    :class:`~pi_glink.kernel.acp_e2b_env.SandboxCommandProcess`. There is no second
    protocol implementation on any face.
    """
    if harness == "acp":
        # Imported here, not at module import: the ACP adapter depends on the
        # contract defined in this module.
        from .acp_execution import AcpExecutionAdapter, AcpSandboxExecutionAdapter

        if executor == "e2b":
            if acp_e2b_plan is None:
                raise ConfigError("ACP e2b 适配器需要 acp_e2b_plan")
            sandbox = acp_e2b_plan(spec)
            return AcpSandboxExecutionAdapter(
                spec,
                sandbox=sandbox,
                client_factory=acp_client_factory or AcpRpcClient,
                on_frame=on_frame,
                on_identity=on_identity,
            )
        if executor == "docker":
            if acp_docker_plan is None:
                raise ConfigError("ACP docker 适配器需要 acp_docker_plan")
            argv, env, container_name = acp_docker_plan(spec)
            return AcpExecutionAdapter(
                spec,
                argv=argv,
                env=env,
                cwd=None,
                # The agent-visible workspace is the container mount point; the
                # host workspace path does not exist inside the container.
                session_cwd=CONTAINER_WORKSPACE,
                client_factory=acp_client_factory or AcpRpcClient,
                on_frame=on_frame,
                container_name=container_name,
                container_stop=docker_stop,
            )
        if acp_plan is None:
            raise ConfigError("ACP adapter requires an acp_plan")
        argv, env, cwd = acp_plan(spec)
        return AcpExecutionAdapter(
            spec,
            argv=argv,
            env=env,
            cwd=cwd,
            client_factory=acp_client_factory or AcpRpcClient,
            on_frame=on_frame,
        )
    if executor == "e2b":
        return E2BSandboxExecutionAdapter(
            spec, on_frame=on_frame, sandbox_factory=sandbox_factory, on_identity=on_identity
        )
    if executor == "docker":
        argv, env, container_name = docker_plan(spec)
        return RpcExecutionAdapter(
            spec,
            argv=argv,
            env=env,
            cwd=None,
            client_factory=client_factory,
            on_frame=on_frame,
            kind="docker",
            container_name=container_name,
            container_stop=docker_stop,
        )
    argv, env, cwd = rpc_plan(spec)
    return RpcExecutionAdapter(
        spec,
        argv=argv,
        env=env,
        cwd=cwd,
        client_factory=client_factory,
        on_frame=on_frame,
        kind="inproc",
    )
