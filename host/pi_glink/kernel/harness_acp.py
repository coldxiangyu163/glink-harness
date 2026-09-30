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
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable, Mapping, TYPE_CHECKING

from .. import events
from ..config import ConfigError
from ..contracts import FollowupUnavailable, Verdict, conversation_guard
from ..store import CANCELLED, FAILED, FINISHED, RunRecord
from .acp_env import (
    ACP_HOME_DIRNAME,
    ACP_RECOVERED_DIRNAME,
    ACP_RUNTIME_DIRNAME,
    AcpRuntime,
    AcpStateInheritError,
    build_acp_argv,
    credential_destinations,
    discard_rendered_credential_copies,
    learn_conversation_state_secrets,
    prepare_acp_runtime,
)
from .archive_channel import ArchiveEndpoint, read_state_manifest
from .acp_execution import AcpExecutionAdapter, acp_resume_method
from .acp_projection import ACP_STOP_REASONS, AcpProjector
from .conversation_lease import LeaseCorruptError, read_lease
from .docker_env import acp_worker_image
from .execution import ExecutionAdapter, ExecutionSpec
from .harness import HarnessFamily, PreparedRuntime, StateTransfer
from .sandbox import Launch, Mount, Sandbox, SandboxResume

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from ..redaction import SecretRedactor
    from .agent_env import EgressEndpoint, RunCredentials

logger = logging.getLogger("pi_glink.kernel.harness_acp")

#: Bound on how many committed frames of a parent's log the ACP admission gate
#: scans for the recorded ``session_ready`` capability matrix. The frame is
#: published right after a run opens, so the scan is independent of how long the
#: parent's transcript grew afterwards.
_ACP_CAPABILITY_SCAN_EVENTS = 50
#: Bound on the parent-chain walk that finds a conversation's root run. Every
#: follow-up has exactly one parent (the stale-run gate), so the chain is the
#: conversation's turn count; the bound only refuses a (corrupt) cycle.
_CONVERSATION_CHAIN_LIMIT = 128


class AcpHarnessFamily(HarnessFamily):
    """ACP 族：agent 自带配置，续接住在父 run 的 agent 状态根。"""

    name = "acp"

    # -- (a) 准入 --------------------------------------------------------------

    def validate_credentials(self, credentials: "RunCredentials") -> None:
        """按模型凭据政策分流（docs/adr/0015）。

        单用户与显式 ``shared``：agent 用运营方配置的 ACP 凭据通道
        （``PI_GLINK_ACP_ENV`` / ``PI_GLINK_ACP_CREDENTIAL_FILES``，宿主显式
        声明的共享凭据），宿主模型凭据不是准入条件。多租户 ``tenant`` 政策：
        每租户应自备凭据，而 ACP 的租户级注入点未实现（M2），受理 run 只会
        静默落到运营方通道，所以在准入就拒绝，并指出显式出路；启动期的
        通道配置矛盾由 ``_acp_credentials_follow_policy`` 拒绝，这里拒绝的是
        通道未配置但政策不可满足的组合，两层互为纵深。
        """
        if self._config.multi_tenant and not self._config.shared_model_credentials:
            raise ConfigError(
                "多租户 tenant 政策下 ACP 族不可用：租户级凭据注入未实现（M2）；"
                "SaaS 统一供模型请显式配置 PI_GLINK_MODEL_CREDENTIALS=shared"
                "（ADR 0015）"
            )

    # -- (b) 追问门禁与续接种子 -------------------------------------------------

    def check_continuation(self, parent: RunRecord) -> Path:
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

        * 沙箱销毁前把该 run 的状态根回收到宿主并**由该 run 自己发布**；追问
          在「父状态根回收成功且是父 run 自己的版本」时与 inproc/docker 面同样
          受理（docs/adr/0005 的受理语义、0018 的落点）。发布是整份成功才可见
          （staging + rename），manifest 的 ``sourceRunId`` 必须等于父 run——
          失败（或未尝试）的回收什么都不留下、陈旧的发布对不上父 run，追问都
          维持拒绝，而不是先花一个注定 resume 失败的云端沙箱。

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
        # E2B freshness follows the parent's own recovery. A files-only turn
        # owns recovered/ even if an older channel turn left a valid archive.
        # Without that mirror, the archive must have been published by this
        # parent. A corrupt publication never becomes a legacy fallback.
        state_home = self.state_home(parent)
        if parent.spec.executor == "e2b":
            conversation = self._store.conversations_root() / self.conversation_root(parent)
            manifest = read_state_manifest(conversation)
            # A known corrupt publication is not a missing archive. Even a
            # per-run mirror must not silently hide that uncertainty.
            try:
                published = os.stat(conversation / "state", follow_symlinks=False)
            except FileNotFoundError:
                published = None
            except OSError as exc:
                raise FollowupUnavailable("session_unavailable") from exc
            if published is not None and (
                not stat.S_ISDIR(published.st_mode) or manifest is None
                or not isinstance(manifest.get("sourceRunId"), str)
                or not manifest["sourceRunId"]
            ):
                raise FollowupUnavailable("session_unavailable")
            state_home = self.state_home(parent)  # a swap-window read may have restored state/
            if (state_home == conversation / "state"
                    and (manifest is None or manifest.get("sourceRunId") != parent.run_id)):
                raise FollowupUnavailable("session_unavailable")
        try:
            info = os.stat(state_home, follow_symlinks=False)
        except OSError as exc:
            raise FollowupUnavailable("session_unavailable") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise FollowupUnavailable("session_unavailable")
        return state_home

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

    def conversation_root(self, run: RunRecord) -> str:
        """The root run id of the conversation ``run`` belongs to.

        Every follow-up has exactly one parent (the stale-run gate), so the
        parent chain is the conversation; the root is its first turn. The walk
        is bounded and a (corrupt) cycle fails closed instead of looping.
        """
        current = run
        seen = {run.run_id}
        while current.parent_run_id is not None:
            if len(seen) > _CONVERSATION_CHAIN_LIMIT:
                raise FollowupUnavailable("session_unavailable")
            parent = self._store.get(current.parent_run_id)
            if parent is None:
                raise FollowupUnavailable("session_unavailable")
            if parent.run_id in seen:
                raise FollowupUnavailable("session_unavailable")
            seen.add(parent.run_id)
            current = parent
        return current.run_id

    def continuation_guard(self, parent: RunRecord) -> AbstractContextManager[None]:
        return conversation_guard(self._store.conversations_root() / self.conversation_root(parent))

    def state_home(self, run: RunRecord) -> Path:
        """The ACP agent state root of one run a follow-up could inherit.

        In-process and Docker runs keep the state root the run rendered and the
        agent wrote (``acp-agent/home``) — on those faces the host *is* the
        durable owner, so the latest turn's own home is the conversation's
        authoritative state (docs/adr/0018's three-face mapping: the common
        layer only abstracts "where the host holds the authoritative state
        root"; it does not flatten what each face can do). An E2B run's agent
        state only ever existed inside the sandbox, so the face publishes every
        turn's recovery to the conversation state root
        (``<data_root>/conversations/<root run>/state``). A files-only turn
        (including a turn after the archive channel was disabled) keeps its
        own ``acp-agent/recovered`` mirror. When present that mirror is the
        parent's authoritative source, ahead of any earlier archive; the
        continuation gate still rejects a known corrupt archive manifest.

        Which of the paths applies follows the run's own ``spec.executor``
        (frozen at admission), never the host's current face: a host that
        switched faces still resumes its earlier runs (docs/adr/0002).
        """
        base = self._store.run_dir(run.run_id) / ACP_RUNTIME_DIRNAME
        if run.spec.executor == "e2b":
            recovered = base / ACP_RECOVERED_DIRNAME
            # A files-only turn publishes its own recovered/ mirror even when
            # an earlier channel turn left a conversation archive behind.
            if recovered.is_dir():
                return recovered
            conversation = self._store.conversations_root() / self.conversation_root(run)
            if (conversation / "state").is_dir():
                return conversation / "state"
            return base / ACP_RECOVERED_DIRNAME
        return base / ACP_HOME_DIRNAME

    def sandbox_resume_target(self, parent: RunRecord) -> SandboxResume | None:
        """The paused parent sandbox a follow-up may hot-path onto (ADR 0020).

        The candidate exists only when every fact lines up:

        * the parent ran the **E2B face** (a paused sandbox is an E2B shape);
        * the conversation's lease record exists and is **fresh** — written by
          the parent run itself (``run_id`` equals the parent), so the sandbox
          carries exactly the state the follow-up gate already admitted against
          (the manifest's ``sourceRunId`` freshness check in
          :meth:`check_continuation` is the same fact's state side);
        * the lease's pack and template match what this host would run now —
          a different agent binary or image layout makes the sandbox's state
          root a foreign environment, and the follow-up takes the cold path;
        * the lease has not expired (the idle TTL) and TTL is enabled.

        The answer is a candidate, not a decision: the adapter still verifies
        the sandbox-side generation marker after reconnecting, and any failure
        there degrades to the cold path (a fresh sandbox + the 阶段 1 backfill).
        """
        if parent.spec.executor != "e2b":
            return None
        if int(self._config.conversation_lease_ttl_seconds) <= 0:
            return None
        conversation_dir = self._store.conversations_root() / self.conversation_root(parent)
        try:
            lease = read_lease(conversation_dir)
        except LeaseCorruptError:
            # 不可读租约不呈现为候选：异常上抛给 Prepare 阶段，由它发
            # kernel_status 警告（降级对操作员可见，cc 复审 SHOULD）后走冷路径。
            raise
        if lease is None:
            return None
        if lease.run_id != parent.run_id:
            # A stale lease (an earlier turn parked a sandbox, but the parent
            # itself did not — its close-out destroyed or replaced it) must not
            # present an older sandbox as the parent's hot cache.
            return None
        if lease.pack != parent.spec.pack:
            return None
        if lease.template != self._config.acp_e2b_template:
            return None
        if lease.is_expired(time.time()):
            return None
        return SandboxResume(
            sandbox_id=lease.sandbox_id,
            generation=lease.generation,
            conversation_run=lease.conversation_run,
            template=lease.template,
        )

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

    def state_transfer(
        self, run: RunRecord, *, parent: RunRecord | None,
        source: Path | None, sandbox: Sandbox,
    ) -> StateTransfer | None:
        if not sandbox.remote:
            return None
        directory = self._store.conversation_dir(self.conversation_root(run))
        backfill = parent is not None and source == directory / "state"
        target = None
        warning = None
        if parent is not None and backfill and run.spec.executor == "e2b":
            try:
                target = self.sandbox_resume_target(parent)
            except LeaseCorruptError as exc:
                warning = str(exc)
        return StateTransfer(directory, credential_destinations(self._config),
                             backfill, target, warning)

    def prepare_runtime(
        self,
        run_id: str,
        run_dir: Path,
        *,
        parent: RunRecord | None,
        session_seed: bytes | None,
        egress: "EgressEndpoint | None",
        sandbox: Sandbox,
        archive: "ArchiveEndpoint | None" = None,
        resume_target: SandboxResume | None = None,
        continuation_source: Path | None = None,
    ) -> PreparedRuntime:
        # A follow-up inherits the authorized parent's agent state root into
        # this run's own HOME — except on a remote face the archive channel
        # can serve: there the state travels sandbox-side (``backfill_state``
        # pulls the conversation root), so this run's HOME stays credentials
        # only and nothing copies the state twice (docs/adr/0018). Without a
        # channel (no public URL) the remote face degrades to the host-side
        # copy exactly like before. 阶段 2 (docs/adr/0020) adds one more
        # skip: a hot-path candidate (``resume_target``) leaves the state in
        # the parked sandbox — the follow-up reconnects to it, and only a
        # degradation falls back to the channel backfill.
        # The parent gate ran at admission and the runner re-authorized the
        # record before calling; the credential destinations are re-rendered
        # fresh and excluded from the copy (see
        # :func:`prepare_acp_runtime`).
        source = (continuation_source if continuation_source is not None else
                  self.state_home(parent) if parent is not None else None)
        directory = (self._store.conversations_root() / self.conversation_root(parent)
                     if parent is not None else None)
        channel_backfill = (sandbox is not None and sandbox.remote and archive is not None
                            and directory is not None and source == directory / "state")
        inherit_from = None if channel_backfill or resume_target is not None else source
        # 跳过本地继承的两条 E2B 路径（归档回灌/热路径）仍然要认识历史状态里
        # 的凭据形值：宿主本地的对话权威状态就是那份历史，扫描它的学习半部
        # 与继承路径同一套预算与判定（cc 终审 B3；inproc/docker 走继承扫描，
        # 对话目录不存在时这里空转，三面口径统一）。
        learned_secrets: tuple[str, ...] = ()
        if parent is not None and inherit_from is None:
            assert source is not None
            learned_secrets = learn_conversation_state_secrets(source)
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
            # a pi-shaped continuation was routed to the ACP adapter. The run
            # stops before it ever has a close-out sweep, so the credentials
            # just rendered for it are deleted here (docs/adr/0016).
            discard_rendered_credential_copies(runtime.home, runtime.credential_dests)
            raise FollowupUnavailable("session_unavailable")
        # Credential material copied into this run's HOME — and any
        # credential-shaped value found in the inherited parent state — joins
        # this run's redaction boundary (and only this run's): a value the
        # agent echoes into a stream, tool result or report is masked like
        # every other secret the host knows. The values never reach a log.
        return PreparedRuntime(
            runtime=runtime, secret_values=tuple(runtime.secrets) + learned_secrets
        )

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
