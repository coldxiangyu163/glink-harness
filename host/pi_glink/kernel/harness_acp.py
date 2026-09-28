"""The ACP harness family.

ACP 族的策略面：空命令白名单、凭据准入空实现、追问追加门禁（能力矩阵、
session id、续接通道、状态根）、状态根继承与 ``AcpProjector``、
与执行面无关的 launch 描述和 ACP 驱动、ACP stopReason 词汇归一化，以及
call_id 进度配对与累计快照替换的 usage 语义。
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Any, Callable, Mapping, TYPE_CHECKING

from .. import events
from ..config import ConfigError
from ..store import CANCELLED, FAILED, FINISHED, RunRecord
from .acp_env import (
    ACP_HOME_DIRNAME,
    ACP_RECOVERED_DIRNAME,
    ACP_RUNTIME_DIRNAME,
    AcpRuntime,
    AcpStateInheritError,
    build_acp_argv,
    prepare_acp_runtime,
)
from .acp_execution import AcpExecutionAdapter, acp_resume_method
from .acp_projection import ACP_STOP_REASONS, AcpProjector
from .docker_env import acp_worker_image
from .execution import ExecutionAdapter, ExecutionSpec
from .harness import (
    FollowupUnavailable,
    HarnessFamily,
    PreparedRuntime,
    Verdict,
)
from .sandbox import Launch, Mount, Sandbox

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from ..redaction import SecretRedactor
    from .agent_env import EgressEndpoint, RunCredentials

logger = logging.getLogger("pi_glink.kernel.harness_acp")

#: Bound on how many committed frames of a parent's log the ACP admission gate
#: scans for the recorded ``session_ready`` capability matrix. The frame is
#: published right after a run opens, so the scan is independent of how long the
#: parent's transcript grew afterwards.
_ACP_CAPABILITY_SCAN_EVENTS = 50


class AcpHarnessFamily(HarnessFamily):
    """ACP 族：agent 自带配置，续接住在父 run 的 agent 状态根。"""

    name = "acp"

    # -- (a) 准入 --------------------------------------------------------------

    def validate_credentials(self, credentials: "RunCredentials") -> None:
        """空实现：agent 自带模型配置，宿主凭据不是准入条件。"""
        # The ACP family does not use the host's model credential: the agent
        # brings its own model configuration (injection points are M2 work).
        return None

    # -- (b) 追问门禁与续接种子 -------------------------------------------------

    def check_continuation(self, parent: RunRecord) -> None:
        """The ACP follow-up gate, on top of the runner's shared parent gate.

        An ACP continuation needs three more facts, all of which are read
        from the host's own record of the parent rather than from the caller:

        * the parent ran the **same harness** — proven by the runner's shared
          gate from the parent record's own ``spec.family`` (the RunSpec
          frozen at admission), never from the current
          configuration alone (a host switched from ``pi`` to ``acp`` must not
          present a pi run as resumable);
        * the parent recorded a **session id** (an ACP run without one has
          nothing to resume);
        * the capability matrix recorded in that same frame declares a
          continuation channel (:func:`~pi_glink.kernel.acp_execution.
          acp_resume_method`). The matrix is a negotiated snapshot and stays
          in the event stream (it is not a RunRecord field), so the frame
          scan is its only source. No capability means the follow-up is refused
          here, before a child run exists — it is never downgraded to a new
          session.

        The E2B face needs one more fact and is admitted by it:

        * 沙箱销毁前把该 run 的状态根有界回收到宿主；追问在「父状态根回收
          成功」时与 inproc/docker 面同样受理（docs/adr/0005）。回收到
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
        admission lock in :meth:`prepare_runtime` and fails the run closed if
        it cannot complete.
        """
        # 族已由 runner 共享门禁按父记录 ``spec.family`` 证明；能力矩阵（协商
        # 快照，不进 RunRecord）从 session_ready 帧取得，扫不到即无续接通道。
        capabilities = self.recorded_capabilities(parent)
        if capabilities is None:
            raise FollowupUnavailable("session_unavailable")
        if not parent.session_id:
            raise FollowupUnavailable("session_unavailable")
        if acp_resume_method(capabilities) is None:
            raise FollowupUnavailable("session_unavailable")
        state_home = self.state_home(parent)
        try:
            info = os.stat(state_home, follow_symlinks=False)
        except OSError as exc:
            raise FollowupUnavailable("session_unavailable") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise FollowupUnavailable("session_unavailable")

    def recorded_capabilities(
        self, parent: RunRecord
    ) -> Mapping[str, Any] | None:
        """The capability matrix a parent ACP run recorded, or ``None``.

        The family is a record fact (``parent.spec.family``): a parent that is not
        an ACP run has no capability matrix to offer and answers ``None``
        without a scan. The parent's ``session_ready`` frame is the only
        capability source that exists before the child's agent process starts;
        ``session_ready`` is committed right after the run opens, so the scan is
        bounded and does not grow with the parent's transcript. A parent
        without such a frame is refused as a run without a continuation
        channel.
        """
        if parent.spec.family != self.name:
            return None
        for index, event in enumerate(self._store.iter_events(parent.run_id)):
            if index >= _ACP_CAPABILITY_SCAN_EVENTS:
                break
            if event.get("type") != events.SESSION_READY:
                continue
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                continue
            capabilities = payload.get("agentCapabilities")
            return dict(capabilities) if isinstance(capabilities, Mapping) else {}
        return None

    def state_home(self, run: RunRecord) -> Path:
        """The ACP agent state root of one run a follow-up could inherit.

        In-process and Docker runs keep the state root the run rendered and the
        agent wrote (``acp-agent/home``). An E2B run's agent state only ever
        existed inside the sandbox, so the face recovers it to
        ``acp-agent/recovered`` before destroying the sandbox; that path is
        derived from the run record like the other one — never from caller
        input — and its existence is the recovered-state fact itself.

        Which of the two applies follows the run's own ``spec.executor``
        (frozen at admission), never the host's current face: a host that
        switched faces still resumes its earlier runs (docs/adr/0002).
        """
        base = self._store.run_dir(run.run_id) / ACP_RUNTIME_DIRNAME
        if run.spec.executor == "e2b":
            return base / ACP_RECOVERED_DIRNAME
        return base / ACP_HOME_DIRNAME

    def continuation_workspace(self, parent: RunRecord) -> Path | None:
        # An ACP continuation keeps the conversation's working directory: an
        # agent session records its ``cwd``, and the resumed agent (and the
        # model's remembered paths) stay in it regardless of what a request
        # claims. Making the child run use that same workspace is what keeps
        # the agent's tools and the host's report contract pointed at one
        # place.
        return Path(parent.workspace)

    def resume_seed(
        self, parent: RunRecord, *, redactor: "SecretRedactor"
    ) -> bytes | None:
        # The ACP family has no host session file: its continuation lives in
        # the parent's agent state root, which ``prepare_runtime`` inherits
        # after admission.
        return None

    def resume_value(self, parent: RunRecord | None) -> str | None:
        # The ACP family resumes the **session id** the parent recorded,
        # against the state root inherited in ``prepare_runtime``.
        return parent.session_id if parent else None

    def verifies_continuation_before_turn(self) -> bool:
        # The ACP faces — including the sandbox one — know the resumed session
        # id right after ``session/resume``, so the child's session identity
        # is proven against the parent's before the turn starts.
        return True

    # -- (c) runtime 渲染与 projector 工厂 --------------------------------------

    def prepare_runtime(
        self,
        run_id: str,
        run_dir: Path,
        *,
        parent: RunRecord | None,
        session_seed: bytes | None,
        egress: "EgressEndpoint | None",
        sandbox: Sandbox,
    ) -> PreparedRuntime:
        # A follow-up inherits the authorized parent's agent state root into
        # this run's own HOME. The parent gate ran at admission and the runner
        # re-authorized the record before calling; the credential destinations
        # are re-rendered fresh and excluded from the copy (see
        # :func:`prepare_acp_runtime`).
        inherit_from = self.state_home(parent) if parent is not None else None
        try:
            runtime = prepare_acp_runtime(
                self._config,
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
        if session_seed is not None:
            # The pi session seed has no ACP meaning; reaching it would mean
            # a pi-shaped continuation was routed to the ACP adapter.
            raise FollowupUnavailable("session_unavailable")
        # Credential material copied into this run's HOME — and any
        # credential-shaped value found in the inherited parent state — joins
        # this run's redaction boundary (and only this run's): a value the
        # agent echoes into a stream, tool result or report is masked like
        # every other secret the host knows. The values never reach a log.
        return PreparedRuntime(runtime=runtime, secret_values=tuple(runtime.secrets))

    def make_projector(self, redactor: "SecretRedactor") -> AcpProjector:
        return AcpProjector(
            redactor, subagent_dispatch=self._config.pack.capabilities.subagent_dispatch
        )

    # -- (d) launch 与驱动 ------------------------------------------------------

    def launch(self, spec: ExecutionSpec, sandbox: Sandbox) -> Launch:
        """What the ACP agent process needs, independent of where it runs.

        The argv is the selected pack's ``entry.command`` verbatim and is never
        shell-expanded; on an isolated sandbox it names an image-local command.
        ``HOME`` is this run's own agent state root as the sandbox places it
        (host path in-process, :data:`CONTAINER_HOME` in Docker, the uploaded
        state root on E2B), so ``~/.codex`` / ``~/.dsh`` live in the run
        directory and never in the operator's home. ``PI_GLINK_ACP_ENV``
        entries are the face's explicit process-environment channel and travel
        as ``secret_env`` (by name on the Docker CLI, never in argv); the
        configuration refuses ``HOME`` among them, and the credential material
        itself stays in files. A mounting sandbox mounts the state root
        writable and each injected credential ``:ro`` on top of it.
        """
        runtime = spec.runtime
        if not isinstance(runtime, AcpRuntime):
            raise ConfigError("ACP harness 的运行期配置没有按 AcpRuntime 渲染")
        config = spec.config
        home = sandbox.place("home", runtime.home)
        mounts = (
            Mount(runtime.home, home),
            *(
                Mount(runtime.home / dest, f"{home}/{dest}", read_only=True)
                for dest in runtime.credential_dests
            ),
        )
        return Launch(
            argv=tuple(build_acp_argv(config)),
            env={"HOME": home},
            secret_env={name: value for name, value in config.acp_env},
            mounts=mounts,
            image=acp_worker_image(config),
            # The pi template has no ACP agent installed.
            template=config.acp_e2b_template,
            create_env=None,
        )

    def driver(
        self,
        spec: ExecutionSpec,
        *,
        sandbox: Sandbox,
        launch: Launch,
        on_frame: Callable[[Mapping[str, Any]], None],
        client_factory: Callable[..., Any] | None,
    ) -> ExecutionAdapter:
        return AcpExecutionAdapter(
            spec,
            sandbox=sandbox,
            launch=launch,
            client_factory=client_factory,
            on_frame=on_frame,
        )

    # -- (e) 终态词汇归一化 ------------------------------------------------------

    def model_stop_verdict(
        self, stop: str | None, *, model_error: str | None
    ) -> Verdict | None:
        # ACP's own ``StopReason`` vocabulary, recorded verbatim in
        # ``modelStopReason``. The verdict is still decided only here:
        # ``max_tokens`` becomes the product's ``length`` (finished, but the
        # delivery is incomplete), ``refusal`` fails as a model error,
        # ``cancelled`` takes the cancellation path; an unknown or missing
        # value fails closed instead of claiming a normal finish. Only
        # ``end_turn`` falls through to the delivery policy below.
        if stop is None:
            return Verdict(
                FAILED, "execution_error", "ACP session/prompt 没有返回 stopReason"
            )
        if stop not in ACP_STOP_REASONS:
            return Verdict(FAILED, "execution_error", f"未知的 ACP stopReason: {stop!r}")
        if stop == "refusal":
            return Verdict(FAILED, "model_error", "ACP agent 拒绝继续（stopReason=refusal）")
        if stop == "cancelled":
            return Verdict(CANCELLED, "abort", None)
        if stop == "max_tokens":
            return Verdict(FINISHED, "length", None)
        if stop == "max_turn_requests":
            return Verdict(FINISHED, "max_turn_requests", None)
        return None

    # -- (f) 子代理增强与运行中事实 ----------------------------------------------

    def match_progress_n(
        self, fact: Any, progress_calls: Mapping[str, list[dict[str, Any]]]
    ) -> int | None:
        # ACP 族：fact 的 ``call_id`` 精确命中 started 事件记录的 toolCallId。
        call_id = getattr(fact, "call_id", None)
        entries = (
            progress_calls.get(call_id)
            if isinstance(call_id, str)
            else None
        )
        if entries:
            return entries[0]["n"]
        return None

    def accumulate_usage(
        self,
        frame: Mapping[str, Any],
        payload: Mapping[str, Any],
        total: dict[str, Any],
        seen_keys: set[str],
    ) -> dict[str, Any] | None:
        # ACP's usage_update is a cumulative context snapshot (``used`` of
        # ``size`` plus a session cost), not pi's per-response usage: the
        # honest conversion keeps that view and replaces the total instead
        # of summing snapshots (which would fabricate a growing number).
        # The payload's ``basis`` marker carries the distinction into the
        # run record.
        snapshot = payload.get("usage")
        if not isinstance(snapshot, Mapping):
            return None
        total.clear()
        total.update(dict(snapshot))
        return dict(payload)
