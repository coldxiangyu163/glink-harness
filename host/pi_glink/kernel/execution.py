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
from .child_progress import E2bChildProgressSource, PiChildProgressSource
from .docker_env import CONTAINER_WORKSPACE
from .e2b_env import (
    SANDBOX_SESSIONS,
    SUBAGENT_ARTIFACTS_DIRNAME,
    E2BSandboxClient,
)
from .outcome import POLL_SECONDS, ExecutionOutcome
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
        popen: Callable[..., Any] | None = None,
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
        #: 进程工厂注入点（E2B 面）：非 None 时透传给 ``PiRpcClient``，后者
        #: 用它替代 ``subprocess.Popen`` 取得一个已启动的进程门面。
        self._popen = popen
        self._settled = threading.Event()
        self._outcome = ExecutionOutcome()

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        extra: dict[str, Any] = {}
        if self._popen is not None:
            extra["popen"] = self._popen
        client = self._client_factory(
            self._argv,
            cwd=self._cwd,
            env=self._env,
            on_event=self._handle_frame,
            **extra,
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

    def child_progress_source(self) -> PiChildProgressSource | None:
        """Poll the session directory's ``subagent-artifacts`` (R3a).

        Both host-side pi faces keep the session directory on the host (the
        Docker face mounts it), so the transcript layout is directly pollable
        for the in-process and the container face alike.
        """
        session_dir = getattr(self.spec.runtime, "session_dir", None)
        if session_dir is None:
            return None
        return PiChildProgressSource(Path(session_dir) / SUBAGENT_ARTIFACTS_DIRNAME)

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


#: abort 被受理后等 pi 收尾落盘的宽限（秒）。宽限只保护回收完整性——
#: ``recover_artifacts`` 不与仍在写会话/报告的 pi 竞争；pi 不理会 abort 时
#: 宽限到期照常返回，``close()`` 的 terminate/kill 与沙箱销毁兜底。终态判定
#: （cancelled/timeout）在此之前已作出，不受宽限影响。
ABORT_SETTLE_GRACE_SECONDS = 10.0


class E2BSandboxRpcExecutionAdapter(RpcExecutionAdapter):
    """pi inside an E2B sandbox, over the same bidirectional RPC channel.

    与 inproc/docker 面只有载体之差（ACP 面
    :class:`~pi_glink.kernel.acp_execution.AcpSandboxExecutionAdapter` 的同构
    先例）：沙箱的 background 命令（``background=True, stdin=True``）经
    :class:`~pi_glink.kernel.acp_e2b_env.SandboxCommandProcess` 包装后以
    ``popen=`` 注入共享的 ``PiRpcClient``，协议驱动（``get_state``/
    ``prompt``/``abort``、帧分发、wait 循环）全部继承，不复制。本类只持有
    沙箱生命周期与本面事实：

    * ``start()`` 的顺序钉死：沙箱 → 身份落盘（上传之前，部分失败也留可
      点名资源）→ ``upload_config`` → ``upload_continuation`` →
      ``spawn_rpc`` → ``super().start()``；
    * ``session_info`` 在继承的 ``get_state`` 载荷上追加 ``sandboxId`` 与
      ``template``；会话身份在回合前即可知（``session_file`` 在
      ``recover_session`` 成功落宿主路径前为 ``None``，回收后由
      ``recovered_session`` 补发一帧）；
    * 断流即失败：沙箱流断开时 RPC 通道没有第二份事件副本（旧 tee 路径的
      ``events.jsonl`` 权威已删），``wait`` 把带 ``stream_error`` 的
      ``kernel_exit`` 改写为 ``execution_error``——不补发、不做
      ``get_state`` 对账（其响应帧走同一条已断的 stdout；SDK 重连是否重放
      已消费输出未规定，重复投递比失败更糟）；
    * 取消是协议级 abort（原义务：旧 e2b 路径运行中不能 abort，cancel 只能
      直接销毁沙箱；新义务：abort 由运行中的 pi 受理，沙箱存活到收尾），
      因此被取消的 run 现在也能回收 ``report.md`` 与主会话。
    """

    kind = "e2b"

    def __init__(
        self,
        spec: ExecutionSpec,
        *,
        argv: list[str],
        client_factory: Callable[..., PiRpcClient],
        on_frame: FrameCallback,
        sandbox_factory: Callable[..., Any] | None = None,
        on_identity: Callable[[Mapping[str, str]], None] | None = None,
    ) -> None:
        super().__init__(
            spec,
            argv=argv,
            # 沙箱命令的环境与工作目录在 ``spawn_rpc`` 时给定；客户端的
            # env/cwd 只在它自己 spawn 宿主进程时使用，本面从不如此。
            env={},
            cwd=None,
            client_factory=client_factory,
            on_frame=on_frame,
            kind="e2b",
            popen=lambda *a, **k: self._process,
        )
        self._sandbox_factory = sandbox_factory
        self._on_identity = on_identity
        self._sandbox: E2BSandboxClient | None = None
        self._process: Any | None = None
        self._release_failures: list[str] = []
        self._session_file: Path | None = None
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
        self._sandbox = client
        try:
            client.start(timeout_seconds=self.spec.timeout_seconds + 300)
            # The sandbox exists from here on. Hand its identity to the host
            # before any step that can fail, so a partial creation still leaves
            # a named leftover the operator and the online sweep can act on.
            self._record_identity(client.sandbox_id)
            client.upload_config()
            if self.spec.resume_session:
                client.upload_continuation(self.spec.resume_session, self.spec.workspace)
            self._process = client.spawn_rpc(self._argv)
            super().start()
        except Exception:
            # A partially created sandbox must still be destroyed; the sandbox
            # identity already persisted above is not erased.
            self._release_failures.extend(client.close())
            self._sandbox = None
            raise

    def _record_identity(self, sandbox_id: str) -> None:
        if not sandbox_id:
            return
        self._sandbox_id = sandbox_id
        if self._on_identity is not None:
            self._on_identity({"sandbox": sandbox_id})

    def session_info(self) -> SessionInfo | None:
        session = super().session_info()
        if session is None:
            return None
        payload = dict(session.payload)
        # Same facts the ACP sandbox face records: which sandbox carried the
        # run and which template it came from.
        payload["sandboxId"] = self._sandbox_id
        payload["template"] = self.spec.config.e2b_template
        return SessionInfo(
            payload=payload,
            session_id=session.session_id,
            session_file=str(self._session_file) if self._session_file else None,
        )

    def recovered_session(self) -> SessionInfo | None:
        # This face's session file travels back with ``recover_artifacts``; a
        # publishable answer exists from then on.
        return publishable_recovered_session(self.session_info())

    def wait(self, *, cancel: Callable[[], bool], deadline: float) -> ExecutionOutcome:
        outcome = super().wait(cancel=cancel, deadline=deadline)
        if outcome.cancelled or outcome.timed_out:
            self._await_abort_settle()
        self._rewrite_stream_loss(outcome)
        return outcome

    def _await_abort_settle(self) -> None:
        """等 abort 后的收尾（``agent_settled`` 或进程退出），宽限到期照常返回。

        宽限只保护回收完整性：``recover_artifacts`` 不与仍在收尾落盘的 pi
        竞争。终态判定（cancelled/timeout）在此之前已作出，不受宽限影响。
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

    def recover_artifacts(self, destination: Path) -> ArtifactRecovery:
        client = self._sandbox
        if client is None:
            # 沙箱未启动（start 失败即毁）：没有可回收之物，也不伪造失败。
            return ArtifactRecovery()
        files, failures = client.recover_artifacts(destination)
        session_file, session_failure = client.recover_session()
        self._session_file = session_file
        if session_failure:
            failures.append(session_failure)
        return ArtifactRecovery(files=tuple(files), failures=tuple(failures))

    def recover_subagents(self, destination: Path) -> ArtifactRecovery:
        """Mirror subagent sessions/artifacts while the sandbox still exists."""
        client = self._sandbox
        if client is None:
            return ArtifactRecovery()
        files, failures = client.recover_subagent_files(destination)
        return ArtifactRecovery(files=tuple(files), failures=tuple(failures))

    def child_progress_source(self) -> E2bChildProgressSource | None:
        """Poll the sandbox's ``subagent-artifacts`` listing (R3a).

        The transcripts live inside the sandbox, so a source can only be built
        while the client — and its sandbox — is still alive; after ``close()``
        there is honestly nothing to poll and the answer is ``None``.
        """
        client = self._sandbox
        if client is None:
            return None
        sandbox = client.sandbox
        if sandbox is None:
            return None
        return E2bChildProgressSource(
            sandbox, f"{SANDBOX_SESSIONS}/{SUBAGENT_ARTIFACTS_DIRNAME}"
        )

    def close(self) -> CleanupReport:
        failures: list[str] = list(self._release_failures)
        client = self._client
        self._client = None
        if client is not None:
            try:
                exit_code = client.close()
                if exit_code is not None:
                    self._outcome.exit_code = exit_code
            except OSError as exc:
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
        sandbox = self._sandbox
        self._sandbox = None
        if sandbox is not None:
            failures.extend(sandbox.close())
        return CleanupReport(
            released=not failures, failures=tuple(failures), external=dict(self.identity())
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
    e2b_rpc_plan: Callable[[ExecutionSpec], list[str]] | None = None,
    acp_plan: Callable[[ExecutionSpec], tuple[list[str], Mapping[str, str], Path | None]]
    | None = None,
    acp_docker_plan: Callable[[ExecutionSpec], tuple[list[str], Mapping[str, str], str]]
    | None = None,
    acp_e2b_plan: Callable[[ExecutionSpec], "E2BAcpSandboxClient"] | None = None,
    acp_client_factory: Callable[..., AcpRpcClient] | None = None,
) -> ExecutionAdapter:
    """Compose the adapter for one run's frozen harness and execution face.

    ``harness`` selects the adapter family (``pi`` or ``acp``), ``executor`` the
    transmission face within it. ``rpc_plan``/``docker_plan``/``e2b_rpc_plan``/
    ``acp_plan`` are the harness-and-face-specific input builders kept in the
    runner (they touch the run's rendered configuration); the adapter owns
    everything after that. ``on_identity`` is called as soon as a face knows the
    durable identity of the resource it created, before any later step can fail.

    Every face speaks its family's protocol over one client class: for pi,
    ``inproc``/``docker`` spawn the process locally or in a container, and
    ``e2b`` hands a sandbox background command's handle
    (:class:`~pi_glink.kernel.acp_e2b_env.SandboxCommandProcess`) to the same
    :class:`PiRpcClient` through :class:`E2BSandboxRpcExecutionAdapter`; the ACP
    family does the same with :class:`AcpRpcClient`. There is no second protocol
    implementation on any face.
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
        if e2b_rpc_plan is None:
            raise ConfigError("pi e2b 适配器需要 e2b_rpc_plan")
        return E2BSandboxRpcExecutionAdapter(
            spec,
            argv=e2b_rpc_plan(spec),
            client_factory=client_factory,
            on_frame=on_frame,
            sandbox_factory=sandbox_factory,
            on_identity=on_identity,
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
