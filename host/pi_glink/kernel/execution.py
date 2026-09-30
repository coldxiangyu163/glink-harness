"""Execution adapters: one harness driver over one sandbox, per run.

The runner owns the product lifecycle (run states, event persistence, artifact
policy, resource bookkeeping). What differs per run is composed from two
orthogonal dimensions (docs/architecture.md §4, ``docs/multi-harness-plan.md``):
the **harness** (pi, ACP — launch, protocol driver, stop vocabulary,
continuation, subagents, usage) and the **sandbox**
(:mod:`pi_glink.kernel.sandbox`: in-process, Docker, E2B — resource and
transport). The adapter is ``harness.driver(sandbox.spawn(harness.launch(spec)))``:
one driver class per harness family (:class:`PiExecutionAdapter` here,
:class:`~pi_glink.kernel.acp_execution.AcpExecutionAdapter` for ACP), each
running unchanged on every sandbox, so the runner never grows a second copy of
"wait for settle", "kill on cancel" or "pull the report back", and no code
branches on a ``(harness, executor)`` string pair.

The frozen input is :class:`ExecutionSpec`: the run id, the kernel prompt, the
system prompt, the per-run workspace, the per-run rendered configuration and
the run's timeout. An adapter never re-reads global environment for data the
spec already carries (image, template, credentials, paths), and never writes
to another run's workspace.

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
    (in-process, Docker) keep the ABC's default empty answer — there is
    nothing to pull back.
``child_progress_source()``
    Optional (R3a, docs/architecture.md §3.5「运行中进度（R3a）」): a polling
    source for *running* subagent progress, built while the face's transcripts
    are still readable. A face that cannot observe its children answers
    ``None`` and that face honestly has no progress; the runner owns the
    polling thread, the throttling and the ``subagent_updated`` emission.
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
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ..config import HostConfig
from .archive_channel import ArchiveEndpoint
from .child_progress import E2bChildProgressSource, PiChildProgressSource
from .e2b_env import (
    SANDBOX_SESSIONS,
    SUBAGENT_ARTIFACTS_DIRNAME,
    PiSandboxTransfer,
)
from .outcome import POLL_SECONDS, ExecutionOutcome
from .rpc import PiRpcClient, PiRpcError
from .sandbox import CleanupReport, Launch, Sandbox, SandboxResume

__all__ = [
    "ABORT_SETTLE_GRACE_SECONDS",
    "ArtifactRecovery",
    "CleanupReport",
    "ExecutionAdapter",
    "ExecutionSpec",
    "PiExecutionAdapter",
    "SessionInfo",
    "publishable_recovered_session",
]

logger = logging.getLogger("pi_glink.kernel.execution")

#: Deadline for pi to accept and answer a command frame.
REQUEST_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class ExecutionSpec:
    """Everything an adapter may use, frozen when the run was accepted."""

    config: HostConfig
    #: The per-run rendered configuration for this run's harness: pi's
    #: ``AgentRuntime`` for the pi family, the ACP state root (``AcpRuntime``)
    #: for the ACP family. Only the owning family's launch/driver reads it.
    runtime: Any
    run_id: str
    prompt: str
    system_prompt: str
    workspace: Path
    timeout_seconds: int
    extra_argv: tuple[str, ...] = ()
    resume_session: str | None = None
    expected_session_id: str | None = None
    #: The archive channel endpoint (docs/adr/0018), present only for a remote
    #: sandbox face whose conversation state travels over the channel; the
    #: in-process and Docker faces keep their state on the host directly and
    #: carry ``None`` here. ``conversation_dir`` is the host-side counterpart
    #: (where recoveries publish); always set when ``archive`` is.
    archive: ArchiveEndpoint | None = None
    #: Frozen decision: the admitted source is the conversation archive root.
    archive_backfill: bool = False
    conversation_dir: Path | None = None
    #: The paused sandbox a follow-up may reconnect to (docs/adr/0020's hot
    #: path), built from the conversation's lease; ``None`` everywhere else
    #: (a first turn, a missing/expired lease, a mismatched pack or template —
    #: those take the cold path of a fresh sandbox + backfill).
    sandbox_resume: SandboxResume | None = None


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
    def recover_artifacts(
        self, destination: Path, *, declared: tuple[str, ...] = ()
    ) -> ArtifactRecovery: ...

    @abstractmethod
    def close(self) -> CleanupReport: ...

    def session_info(self) -> SessionInfo | None:  # pragma: no cover - optional
        return None

    def recovered_session(self) -> SessionInfo | None:
        """Session facts learned through ``recover_artifacts`` worth publishing.

        ABC 默认空实现：only the sandbox faces learn session facts while
        pulling work products back; every other face knows its session before
        the turn, so the base answer is ``None``.
        """
        return None

    def recover_subagents(self, destination: Path) -> ArtifactRecovery:
        """Mirror subagent side-products out of the face before ``close()``.

        ABC 默认空实现：只有 pi 沙箱面有子代理副产物要镜像（branched
        sessions 与 subagent artifacts 随沙箱销毁，须在 ``close()`` 前镜像回
        宿主）；其余面的子代理文件本就在宿主，没有可回收之物，基类返回一
        份空的 :class:`ArtifactRecovery`。
        """
        return ArtifactRecovery()

    def child_progress_source(self) -> Any | None:
        """Optional R3a seam: a polling source for running subagent progress.

        与 ``recovered_session`` / ``recover_subagents`` 同为 ABC 默认空实
        现：a face that cannot observe its children's transcripts answers
        ``None`` and that face honestly has no progress
        (docs/architecture.md §3.5「运行中进度（R3a）」). The runner owns the
        polling thread, the throttling and the ``subagent_updated`` emission;
        the source only answers ``poll()``/``close()``.
        """
        return None

    def identity(self) -> Mapping[str, str]:
        return {}


class PiExecutionAdapter(ExecutionAdapter):
    """pi behind its bidirectional RPC protocol, on any :class:`Sandbox`.

    The pi family's driver (``harness.driver(sandbox.spawn(harness.launch(spec)))``,
    docs/architecture.md §4): one protocol implementation — ``get_state`` /
    ``prompt`` / ``abort``, frame dispatch, the wait loop — whatever carries the
    process. The sandbox owns the resource (create/spawn/identity/destroy); this
    adapter owns what is pi-specific about it:

    * ``start()`` 的顺序钉死：``sandbox.open`` → （远端面）身份已由沙箱在
      上传前落盘 → ``upload_config`` → ``upload_continuation`` →
      ``sandbox.spawn`` → 客户端启动；任一步失败即释放沙箱（身份不擦除）；
    * ``session_info`` 追加沙箱事实（E2B：``sandboxId``/``template``），会话
      文件经 ``sandbox.host_path`` 映回宿主（Docker 报容器路径），远端面在
      ``recover_session`` 落宿主路径前为 ``None``，回收后由
      ``recovered_session`` 补发；
    * 断流即失败（远端面）：沙箱流断开时 RPC 通道没有第二份事件副本，
      ``wait`` 把带 ``stream_error`` 的 ``kernel_exit`` 改写为
      ``execution_error``——不补发、不做 ``get_state`` 对账；
    * 取消是协议级 abort；远端面在 abort 后等收尾宽限，使被取消的 run 也能
      回收 ``report.md`` 与主会话（docs/adr/0004）。
    """

    def __init__(
        self,
        spec: ExecutionSpec,
        *,
        sandbox: Sandbox,
        launch: Launch,
        client_factory: Callable[..., PiRpcClient] | None = None,
        on_frame: FrameCallback,
    ) -> None:
        super().__init__(spec, on_frame=on_frame)
        self.kind = sandbox.kind
        self.sandbox = sandbox
        self.launch = launch
        self._client_factory = client_factory or PiRpcClient
        self._client: PiRpcClient | None = None
        self._transfer: PiSandboxTransfer | None = None
        self._process: Any | None = None
        self._release_failures: list[str] = []
        self._recovered_session_file: Path | None = None
        self._settled = threading.Event()
        self._outcome = ExecutionOutcome()

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        sandbox = self.sandbox
        try:
            sandbox.open(self.launch, timeout_seconds=self.spec.timeout_seconds + 300)
            if sandbox.remote:
                transfer = PiSandboxTransfer(sandbox.handle, self.spec.runtime)
                self._transfer = transfer
                transfer.upload_config()
                if self.spec.resume_session:
                    transfer.upload_continuation(self.spec.resume_session, self.spec.workspace)
            spawn = sandbox.spawn(self.launch)
            extra: dict[str, Any] = {}
            if spawn.process is not None:
                # 进程工厂注入点（远端面）：客户端用它替代 ``subprocess.Popen``
                # 取得一个已启动的进程门面。
                extra["popen"] = spawn.process
            client = self._client_factory(
                spawn.argv,
                cwd=spawn.cwd,
                env=spawn.env,
                on_event=self._handle_frame,
                **extra,
            )
            self._client = client
            client.start()
            # The carrier's process facade (remote faces): ``stream_error``
            # is read from it after the wait.
            self._process = spawn.process() if spawn.process is not None else None
        except Exception:
            # A partially created resource must still be released; the
            # identity the sandbox already persisted is not erased.
            self._release_failures.extend(sandbox.close().failures)
            self._transfer = None
            raise

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
        payload: dict[str, Any] = {
            "sessionId": session_id,
            "sessionFile": data.get("sessionFile"),
            "model": model.get("id") if isinstance(model, Mapping) else None,
            "thinkingLevel": data.get("thinkingLevel"),
        }
        payload.update(self.sandbox.session_facts())
        return SessionInfo(
            payload=payload,
            session_id=str(session_id) if session_id else None,
            session_file=self._session_file(data.get("sessionFile")),
        )

    def _session_file(self, reported: Any) -> str | None:
        """The host location of the session file pi reported.

        A remote sandbox's file only exists on the host after
        ``recover_artifacts``; a mounting sandbox reports its own path, mapped
        back through the session mount only (an unmappable path is kept as
        reported and never read back as a host file).
        """
        if self.sandbox.remote:
            return str(self._recovered_session_file) if self._recovered_session_file else None
        if not isinstance(reported, str) or not reported:
            return None
        mapped = self.sandbox.host_path(
            reported, role="sessions", host_root=self.spec.runtime.session_dir
        )
        if mapped is None:
            logger.warning(
                "container session path does not map back to the host mount: %s", reported
            )
            return reported
        return mapped

    def recovered_session(self) -> SessionInfo | None:
        if not self.sandbox.remote:
            return None
        # The remote face's session file travels back with
        # ``recover_artifacts``; a publishable answer exists from then on.
        return publishable_recovered_session(self.session_info())

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
        if self.sandbox.remote:
            if outcome.cancelled or outcome.timed_out:
                self._await_abort_settle()
            self._rewrite_stream_loss(outcome)
        return outcome

    def _await_abort_settle(self) -> None:
        """等 abort 后的收尾（``agent_settled`` 或进程退出），宽限到期照常返回。

        宽限只保护回收完整性：``recover_artifacts`` 不与仍在收尾落盘的 pi
        竞争。终态判定（cancelled/timeout）在此之前已作出，不受宽限影响。
        本地面的工作区即宿主目录，没有竞争可言，不等。
        """
        grace_deadline = time.monotonic() + ABORT_SETTLE_GRACE_SECONDS
        client = self._client
        while time.monotonic() < grace_deadline:
            if self._settled.is_set():
                return
            if client is None or not client.running:
                return
            time.sleep(min(POLL_SECONDS, max(grace_deadline - time.monotonic(), 0.0)))

    def _rewrite_stream_loss(self, outcome: ExecutionOutcome) -> None:
        """断流即失败：带 ``stream_error`` 的 ``kernel_exit`` 改写为 ``execution_error``。

        已交付的帧保留在事件日志（它们真实到达过），缺失的尾巴不存在于任何
        权威副本，run 只能是 failed——不补发、不伪造完整性。
        """
        if outcome.stop_reason != "kernel_exit":
            return
        stream_error = getattr(self._process, "stream_error", None)
        if not stream_error:
            return
        outcome.stop_reason = "execution_error"
        outcome.exit_code = None
        outcome.error = f"沙箱流断开（{stream_error}）；不补发、不伪造完整性"

    def cancel(self) -> None:
        self._abort()

    def recover_artifacts(
        self, destination: Path, *, declared: tuple[str, ...] = ()
    ) -> ArtifactRecovery:
        transfer = self._transfer
        if transfer is None or self.sandbox.handle is None:
            # 本地面：工作区即宿主目录；远端面未启动（start 失败即毁）：没有
            # 可回收之物，也不伪造失败。
            return ArtifactRecovery()
        files, failures = (
            self.sandbox.recover_workspace(destination, declared=declared)
            if declared else self.sandbox.recover_workspace(destination)
        )
        session_file, session_failure = transfer.recover_session()
        self._recovered_session_file = session_file
        if session_failure:
            failures.append(session_failure)
        return ArtifactRecovery(files=tuple(files), failures=tuple(failures))

    def recover_subagents(self, destination: Path) -> ArtifactRecovery:
        """Mirror subagent sessions/artifacts while the remote sandbox exists."""
        transfer = self._transfer
        if transfer is None or self.sandbox.handle is None:
            return ArtifactRecovery()
        files, failures = transfer.recover_subagent_files(destination)
        return ArtifactRecovery(files=tuple(files), failures=tuple(failures))

    def child_progress_source(self) -> Any | None:
        """Poll the session directory's ``subagent-artifacts`` (R3a).

        A mounting sandbox keeps the session directory on the host, so the
        transcript layout is directly pollable; a remote sandbox's transcripts
        live inside it, so a source can only be built while it is still alive.
        """
        if self.sandbox.remote:
            handle = self.sandbox.handle
            if handle is None:
                return None
            return E2bChildProgressSource(
                handle, f"{SANDBOX_SESSIONS}/{SUBAGENT_ARTIFACTS_DIRNAME}"
            )
        session_dir = getattr(self.spec.runtime, "session_dir", None)
        if session_dir is None:
            return None
        return PiChildProgressSource(Path(session_dir) / SUBAGENT_ARTIFACTS_DIRNAME)

    def identity(self) -> Mapping[str, str]:
        return dict(self.sandbox.identity())

    def close(self) -> CleanupReport:
        failures: list[str] = list(self._release_failures)
        self._release_failures = []
        client = self._client
        self._client = None
        if client is not None:
            try:
                exit_code = client.close()
                if exit_code is not None:
                    self._outcome.exit_code = exit_code
            except OSError as exc:
                if not self.sandbox.remote:
                    failures.append(f"关闭 pi 进程失败: {type(exc).__name__}: {exc}")
                else:
                    # 断流后 ``SandboxCommandProcess.wait()`` 拿不到退出码：沙箱
                    # ``kill()`` 才是释放权威，一次已判定 execution_error 的断流
                    # 不再被谎报为资源未释放——降级为日志，不进 failures。
                    logger.info(
                        "断流后的 RPC 进程关闭无退出码 run=%s: %s（沙箱销毁兜底）",
                        self.spec.run_id,
                        exc,
                    )
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                failures.append(f"关闭 pi 进程失败: {type(exc).__name__}: {exc}")
        self._process = None
        self._transfer = None
        failures.extend(self.sandbox.close().failures)
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


#: abort 被受理后等 pi 收尾落盘的宽限（秒，远端面）。宽限只保护回收完整性——
#: ``recover_artifacts`` 不与仍在写会话/报告的 pi 竞争；pi 不理会 abort 时
#: 宽限到期照常返回，``close()`` 的 terminate/kill 与沙箱销毁兜底。终态判定
#: （cancelled/timeout）在此之前已作出，不受宽限影响。
ABORT_SETTLE_GRACE_SECONDS = 10.0
