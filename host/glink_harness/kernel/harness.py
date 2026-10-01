"""Harness families: the per-harness policy behind one run lifecycle.

The runner owns locks, phases, finalize ordering and commits. Everything that
reads as "the pi family does X, the ACP family does Y" lives behind
:class:`HarnessFamily`: admission allowlists, follow-up gates and resume
seeds, runtime rendering and projector construction, the launch description
and protocol driver, stop-reason normalization, and subagent enrichment.

The execution face is the orthogonal :class:`~glink_harness.kernel.sandbox.Sandbox`
dimension (in-process / Docker / E2B: resource, spawn, path mapping, identity,
teardown). A run's adapter is ``harness.driver(sandbox.spawn(harness.launch(spec)))``
(:meth:`HarnessFamily.build_adapter`); a family never branches on the
sandbox's name — it asks the sandbox facts (``isolated``, ``remote``,
``place``). The behavioural invariants of this interface are listed in
docs/architecture.md §4「族接口」.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, TYPE_CHECKING

from ..config import ConfigError, HostConfig
from ..contracts import Verdict
from ..store import RunRecord, RunStore
from .agent_env import EgressEndpoint
from .execution import ExecutionAdapter, ExecutionSpec
from .archive_channel import ArchiveEndpoint
from .run_views import RunStoreSessionSource, SessionSource
from .sandbox import Launch, Sandbox, SandboxResume

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from ..redaction import SecretRedactor
    from .agent_env import RunCredentials
    from .sweep import SessionSweep


class HarnessRuntime(Protocol):
    """A family's rendered per-run runtime, as the runner consumes it.

    The concrete type is the family's own (pi ``AgentRuntime``, ACP
    ``AcpRuntime``); the runner only asks for the paths it must sweep and
    otherwise hands the object back to the family and its driver.
    """

    def session_sweeps(self) -> "tuple[SessionSweep, ...]": ...


class FrameProjector(Protocol):
    """A family's per-run frame projector, as the runner consumes it.

    pi ``Projector`` / ACP ``AcpProjector``: one wire frame in, the product
    events it projects to out; ``flush`` releases the withheld stream tail
    before the verdict. The runner never names the concrete class.
    """

    def project(self, frame: Mapping[str, Any]) -> list[tuple[str, dict[str, Any]]]: ...

    def flush(self) -> list[tuple[str, dict[str, Any]]]: ...


@dataclass(frozen=True, slots=True)
class PreparedRuntime:
    """What a family's runtime render produced（runner 按原序消费）。"""

    runtime: HarnessRuntime
    #: 渲染中新知的凭据形值：并入本 run 的脱敏边界（ACP 的 runtime.secrets）。
    secret_values: tuple[str, ...] = ()
    #: pi 族消费了 session_seed（写入 continued.jsonl）；runner 据此清空。
    seed_consumed: bool = False


@dataclass(frozen=True, slots=True)
class StateTransfer:
    """Family-owned state transfer policy; the host only issues its capability."""

    conversation_dir: Path
    credential_dests: tuple[str, ...]
    backfill: bool
    resume_target: SandboxResume | None = None
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class OwnerRef:
    """kernel 层的抽象属主身份（R1-4，设计 §2.3 末段）。

    只承载 owner 标识与必要的准入事实：``owner_id`` 是 run 记录的属主标签
    （多租户模式为租户 id，本地模式为 ``None``）。kernel 的族接口与协议面
    只引用本类型，不导入 ``tenancy`` 或任何产品账号类型；宿主租户身份 →
    ``OwnerRef`` 的映射是 runs 编排层的职责（
    :func:`glink_harness.runs.admission.owner_ref_of`），身份校验语义
    （owner 匹配、异租户拒绝）不变。
    """

    owner_id: str | None = None


class HarnessFamily(ABC):
    """One harness family's policy surface. One instance per family name.

    ``RunManager`` 按 run 的 ``RunSpec.family`` 经 ``select_family`` 取得并
    缓存实例（每个族名一份），构造时注入冻结的 ``config`` 与会话来源窄协议
    （:class:`~glink_harness.kernel.run_views.SessionSource`，宿主侧以
    RunStore 适配器实现，设计 §2.3）；准入时族名写入 run 的
    ``RunSpec.family``，此后 runner 与族方法只读 ``RunSpec``，不再读
    ``config.harness``/``config.executor`` 做分支。

    R1-4：族不持有全局 ``HostConfig``——构造点把注入的冻结 config 收敛为
    族段冻结配置（各族 ``__init__`` 里的 ``*FamilyConfig.from_host``），
    此后族的构造/运行期只读族段的显式字段，不回读全局配置或环境。
    """

    name: str = "unknown"  # "pi" | "acp"，与 RunSpec.family 的取值同表
    #: 本族的模型/检索调用是否经宿主凭据出口代理（docs/adr/0008）。True 时
    #: runner 为 run 签发短期 token，并以 ``EgressEndpoint`` 交给
    #: ``prepare_runtime``；真实 key 不进执行环境。ACP 族为 False（agent
    #: 自带模型配置，出口代理未覆盖）。
    uses_egress: bool = False

    def __init__(self, *, config: HostConfig, store: SessionSource) -> None:
        # R1-4：``config`` 是构造点的一次性输入（族段由子类 ``__init__``
        # 从它冻结提取），基类不留存全局配置句柄。
        del config
        # R1-3：过渡兼容退场——注入点只收 SessionSource 窄协议对象，不再把传入
        # 的具体 RunStore 兜底包成适配器。kernel 内唯一接线点是 select_family
        # （统一注入 RunStoreSessionSource），因此族实例不可能持有具体 RunStore；
        # 仓外调用点直接构造时须自己给出协议对象。
        self._store: SessionSource = store

    # -- (a) 准入：凭据要求 -----------------------------------------------------

    #: ``/`` 命令门禁是宿主级、harness 无关（`/<蓝图名> <题目>`），不由族提供
    #: 白名单；族只负责它自己的凭据前提。

    @abstractmethod
    def validate_credentials(self, credentials: "RunCredentials") -> None:
        """Refuse admission early when the family lacks what it needs.

        pi：缺模型 key 抛 ``ConfigError``，消息逐字为
        "OPENAI_API_KEY is not configured; set it in .env before starting
        a run"；ACP：空实现（agent 自带模型配置）。
        """

    # -- (b) 追问门禁与续接种子 ------------------------------------------------

    def check_continuation(self, parent: RunRecord) -> Path | None:
        """Family-specific follow-up gate, after the runner's shared gate
        （owner→harness 字段→terminal→stale，runner 保留）。

        返回权威准入认可的状态根（pi 无状态根、返回 None），供后续准备/传输
        沿用同一事实。ACP 族覆写，追加事实：能力矩阵
        （父 run ``session_ready`` 帧，族已由父记录 ``spec.family`` 证明）、
        session_id、``acp_resume_method``、状态根目录存在性；拒绝一律
        ``glink_harness.contracts.FollowupUnavailable("session_unavailable")``。
        """
        # Hook 默认空实现：pi 族没有额外门禁（显式 return None，区别于被
        # 遗忘的空 body；B027 语义：要么覆写，要么明确什么都不做）。
        return None

    def continuation_guard(self, parent: RunRecord) -> AbstractContextManager[None]:
        """Serialize admission with conversation state/resource destruction.

        The pi seed is a per-run snapshot; stateful families override this for
        their shared conversation. Acquire before the global admission lock.
        """
        return nullcontext()

    def continuation_workspace(self, parent: RunRecord) -> Path | None:
        """The workspace a follow-up reuses.

        基类返回 None（每 run 独立工作区）；ACP 返回
        ``Path(parent.workspace)``（会话 cwd 记在 agent 自己的状态里）。
        """
        return None

    @abstractmethod
    def resume_seed(
        self, parent: RunRecord, *, redactor: "SecretRedactor"
    ) -> bytes | None:
        """The sanitized session snapshot handed to the child kernel.

        pi：经 ArtifactTree 有界读父会话文件、重脱敏，各失败点抛
        ``session_unavailable``（路径校验、读失败、空种子、无 session_id、
        解码失败——拒绝文本与次序原样）；ACP：返回 None（续接住在父
        状态根，prepare_runtime 继承）。
        """

    @abstractmethod
    def resume_value(self, parent: RunRecord | None) -> str | None:
        """The ``ExecutionSpec.resume_session`` value.

        pi：有父即 ``"continued.jsonl"``；ACP：父记录的 ``session_id``。
        无父两族皆 None。
        """

    @abstractmethod
    def verifies_continuation_before_turn(self) -> bool:
        """Whether the child's session identity is proven before the turn.

        pi：恒 True——每个 sandbox 上都走 RPC 通道，``get_state`` 在
        ``session_ready`` 即知会话身份（docs/adr/0004）；ACP：恒 True
        （session/resume 之后即知）。与 sandbox 无关，故无参数。
        """

    # -- (c) runtime 渲染与 projector 工厂 -------------------------------------

    def state_transfer(
        self, run: RunRecord, *, parent: RunRecord | None,
        source: Path | None, sandbox: Sandbox,
    ) -> StateTransfer | None:
        """Stateful families select transfer/resume policy from admitted facts.

        pi's per-run session seed needs no conversation archive or hot cache.
        """
        return None

    @abstractmethod
    def prepare_runtime(
        self,
        run_id: str,
        run_dir: Path,
        *,
        parent: RunRecord | None,
        session_seed: bytes | None,
        egress: EgressEndpoint | None,
        sandbox: Sandbox,
        archive: ArchiveEndpoint | None = None,
        resume_target: "SandboxResume | None" = None,
        continuation_source: Path | None = None,
    ) -> PreparedRuntime:
        """Render the run's per-harness configuration.

        ``egress`` is the run's proxy endpoint (base URLs as the sandbox's
        execution face reaches them plus the run token) when the family
        ``uses_egress``; otherwise ``None``. No real credential is passed.

        ``archive`` is the run's archive-channel endpoint (docs/adr/0018) when
        the face is remote and the channel can serve it; otherwise ``None``.
        The ACP family uses it to move the follow-up's state inheritance off
        the host-side copy (the sandbox pulls the conversation state itself).

        ``sandbox`` is asked facts only: an ``isolated`` sandbox runs the
        kernel from its image, so pi's ``settings.json`` names the image-local
        prebaked packages instead of host paths.

        调用前 runner 已做好父记录二次授权（``_authorized_parent`` 重查，
        失败即 ``session_unavailable``）。pi：prepare_agent_dir + 种子写入
        continued.jsonl（凭据位只写 egress token）。ACP：
        prepare_acp_runtime（inproc/docker 含 inherit_from=self.state_home(parent)、
        AcpStateInheritError 原文包装；e2b 走归档通道时状态不在宿主复制）；
        session_seed 非 None 抛 ``session_unavailable``。
        """

    @abstractmethod
    def make_projector(
        self, redactor: "SecretRedactor", *, agent_workspace: str | None = None
    ) -> FrameProjector:
        """The frame projector（pi：``Projector``；ACP：``AcpProjector``）。
        runner 在 PreparedRuntime 的 secret 全部并入边界之后调用。
        ``agent_workspace`` 是本 run 工作区在 agent 侧的路径
        （``sandbox.place("workspace", …)``，与 ACP 适配器交给 session/new 的
        cwd 同一值）；pi 投影不需要它（pi 面交付物本就按工作区相对路径），
        ACP 投影用它把 agent 交付的绝对路径归一为契约要求的相对路径。"""

    # -- (d) launch 描述与协议驱动 -------------------------------------------

    @abstractmethod
    def launch(self, spec: ExecutionSpec, sandbox: Sandbox) -> Launch:
        """Describe the kernel process independent of the carrier.

        argv、非密环境、密值环境（``secret_env``，Docker 只按名转发）、需挂载
        的宿主路径、包守卫、镜像与模板名。族自己的路径一律经
        ``sandbox.place(role, host)`` 得到进程视角的位置，不按 sandbox 名分支。
        """

    @abstractmethod
    def driver(
        self,
        spec: ExecutionSpec,
        *,
        sandbox: Sandbox,
        launch: Launch,
        on_frame: Callable[[Mapping[str, Any]], None],
        client_factory: Callable[..., Any] | None,
    ) -> ExecutionAdapter:
        """The family's protocol driver over ``sandbox`` (pi：
        ``PiExecutionAdapter``；ACP：``AcpExecutionAdapter``)。driver 在
        ``start()`` 里调 ``sandbox.open``/``sandbox.spawn(launch)``。"""

    def build_adapter(
        self,
        spec: ExecutionSpec,
        sandbox: Sandbox,
        *,
        on_frame: Callable[[Mapping[str, Any]], None],
        client_factory: Callable[..., Any] | None = None,
    ) -> ExecutionAdapter:
        """``driver(sandbox.spawn(launch(spec)))`` — the only composition path.

        没有 ``(harness, executor)`` 字符串分派：sandbox 由 runner 按
        ``RunSpec.executor`` 选出（:func:`~glink_harness.kernel.sandbox.open_sandbox`），
        族只提供 launch 与 driver。
        """
        return self.driver(
            spec,
            sandbox=sandbox,
            launch=self.launch(spec, sandbox),
            on_frame=on_frame,
            client_factory=client_factory,
        )

    # -- (e) 终态词汇归一化 ------------------------------------------------------

    @abstractmethod
    def model_stop_verdict(
        self, stop: str | None, *, model_error: str | None
    ) -> Verdict | None:
        """Translate the harness's stop vocabulary into the product verdict
        table；``None`` 表示落入 runner 的交付策略（报告检查）。映射表见
        docs/architecture.md §4，是本方法的验收 oracle。"""

    # -- (f) 子代理增强与运行中事实 ---------------------------------------------

    def enrich_children(
        self,
        run_id: str,
        *,
        runtime: "HarnessRuntime | None",
        artifact_failures: list[str],
        sweep: Callable[[Path], bool],
        warn: Callable[[str], None],
    ) -> bool:
        """Associate subagent artifacts with child documents（pi 族实现；
        基类空实现即 ACP 的 early return）。

        ``sweep``/``warn`` 是 runner 注入的回调（脱敏清扫与
        child_doc_warning），事件发射权留 runner；返回末段 children 目录
        清扫是否改写了字节（runner 据此置 redacted）。界限 ``_ENRICH_*`` 与
        ``meta``/``unmatched``/``none`` 词汇属于 pi 族实现。
        """
        return False

    @abstractmethod
    def match_progress_n(
        self, fact: Any, progress_calls: Mapping[str, list[dict[str, Any]]]
    ) -> int | None:
        """Pair one polled progress fact with its child ``n``。pi：前台守卫 + transcript index + agent 同名；
        ACP：call_id 精确命中。通用字段校验（steps/bytes/name/target）留
        runner。对不上返回 None，不伪造进度。"""

    @abstractmethod
    def accumulate_usage(
        self,
        frame: Mapping[str, Any],
        payload: Mapping[str, Any],
        total: dict[str, Any],
        seen_keys: set[str],
    ) -> dict[str, Any] | None:
        """Fold one usage snapshot into the run total。pi：按响应去重键累加 + requests 计数；ACP：累计上下文
        快照整体替换（诚实转换，不伪造增长）。返回事件载荷或 None（重复
        帧）。"""


def select_family(
    harness: str, *, config: HostConfig, store: RunStore
) -> HarnessFamily:
    """The family instance for one ``RunSpec.family`` name.

    ``RunManager`` caches one instance per name and looks it up from each
    run's frozen spec, so nothing assumes one harness per host. ``harness`` is
    a pack manifest ``family`` (``pi``/``acp``), derived and validated by
    :func:`glink_harness.config.build_config`; the pack itself travels on the
    frozen config as the parsed manifest object (``config.pack``: entry argv,
    capability matrix, prompt, HOME overlay, image requirement).

    这里同时是窄协议的唯一接线点（R1-1）：宿主的 ``RunStore`` 在此包成
    :class:`RunStoreSessionSource` 适配器后注入族构造，族只见
    ``SessionSource`` 协议面。
    """
    from .harness_acp import AcpHarnessFamily
    from .harness_pi import PiHarnessFamily

    families: dict[str, type[HarnessFamily]] = {
        "pi": PiHarnessFamily,
        "acp": AcpHarnessFamily,
    }
    family = families.get(harness)
    if family is None:  # config 层已拒绝；这里是防御性兜底
        raise ConfigError(f"未知 harness: {harness!r}")
    return family(config=config, store=RunStoreSessionSource(store))
