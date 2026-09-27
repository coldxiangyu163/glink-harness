"""Harness families: the per-harness policy behind one run lifecycle.

The runner owns locks, phases, finalize ordering and commits. Everything that
reads as "the pi family does X, the ACP family does Y" lives behind
:class:`HarnessFamily`: admission allowlists, follow-up gates and resume
seeds, runtime rendering and projector construction, adapter assembly,
stop-reason normalization, and subagent enrichment. ``executor`` (the
in-process / Docker / E2B transmission face) stays orthogonal: face concerns
(container identity persistence, container-path mapping) remain in the runner
or the face plans.

本模块承载的原义务出处：实现体逐字搬迁自 ``runner.py``（FollowupUnavailable
原 L191-200、_Verdict 原 L277-281 及各 harness 分支段落），runner 已重接线
到本接口并删除原文；行为的逐字节不变式见 docs/architecture.md §4「族接口」。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, TYPE_CHECKING

from ..config import ConfigError, HostConfig
from ..store import RunRecord, RunStore
from .execution import ExecutionAdapter, ExecutionSpec

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from ..redaction import SecretRedactor
    from .agent_env import AgentRuntime, RunCredentials
    from .acp_env import AcpRuntime
    from .projection import Projector


class FollowupUnavailable(RuntimeError):
    """A parent run cannot safely supply a resumable session.

    ``code`` is the refusal an HTTP caller sees (``unknown_run``, ``stale_run``,
    ``run_busy``, ``session_unavailable``, ``report_unavailable``).
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Verdict:
    """The run's terminal verdict（自 runner 的 ``_Verdict`` 原样搬迁）。

    产品终态词汇的定义点仍在 runner ``_decide``：status 取 store 的
    FINISHED/FAILED/CANCELLED，stop_reason 取 abort/event_log_failed/
    execution_error/timeout/model_error/aborted/length/max_turn_requests/
    report_rejected/no_report。族方法只把 harness 自己的 stop 词汇译成这
    张表，不新增词汇。
    """

    status: str
    stop_reason: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class PreparedRuntime:
    """What a family's runtime render produced（runner 按原序消费）。"""

    runtime: "AgentRuntime | AcpRuntime"
    #: 渲染中新知的凭据形值：并入本 run 的脱敏边界（ACP 的 runtime.secrets）。
    secret_values: tuple[str, ...] = ()
    #: 需 add_from_file 并入边界的文件（pi 的 web-search.json）。
    secret_files: tuple[Path, ...] = ()
    #: pi 族消费了 session_seed（写入 continued.jsonl）；runner 据此清空。
    seed_consumed: bool = False


class HarnessFamily(ABC):
    """One harness family's policy surface. Selected once per process.

    实例在 ``RunManager.__init__`` 由 ``select_family`` 唯一选出，构造时
    注入冻结的 ``config`` 与 ``store``；此后 runner 不再读
    ``config.harness`` 做族分支（准入冻结写入与记录字段比较除外）。
    """

    name: str = "unknown"  # "pi" | "acp"，与 record.harness 的取值同表

    def __init__(self, *, config: HostConfig, store: RunStore) -> None:
        self._config = config
        self._store = store

    # -- (a) 准入：允许的 / 命令与凭据要求 ------------------------------------

    @abstractmethod
    def allowed_prompt_commands(self) -> tuple[str, ...]:
        """The ``/`` commands admission accepts（自 runner L612-624 搬迁）。

        pi：``harnesses/pi/prompts/`` 渲染出的模板名；ACP：空表（其斜杠命令是
        agent 自己的面，宿主不路由，plan §4.3）。
        """

    @abstractmethod
    def validate_credentials(self, credentials: "RunCredentials") -> None:
        """Refuse admission early when the family lacks what it needs
        （自 runner L523-528 搬迁）。

        pi：缺模型 key 抛 ``ConfigError``，消息逐字为
        "OPENAI_API_KEY is not configured; set it in .env before starting
        a run"；ACP：空实现（agent 自带模型配置）。
        """

    # -- (b) 追问门禁与续接种子 ------------------------------------------------

    def check_continuation(self, parent: RunRecord) -> None:
        """Family-specific follow-up gate, after the runner's shared gate
        （owner→harness 字段→terminal→stale，runner 保留）。

        基类空实现（pi 无额外门禁）。ACP 族覆写，承载现
        ``_acp_resumable_parent`` L799-818 的全部追加事实：能力矩阵
        （``_recorded_acp_capabilities`` L820-855 整体搬入，含族证明的
        记录字段优先 / legacy 帧扫描双路语义与中文注释块）、session_id、
        ``acp_resume_method``、状态根目录存在性；拒绝一律
        ``FollowupUnavailable("session_unavailable")``。
        """

    def continuation_workspace(self, parent: RunRecord) -> Path | None:
        """The workspace a follow-up reuses（自 runner L547-555 搬迁）。

        基类返回 None（每 run 独立工作区）；ACP 返回
        ``Path(parent.workspace)``（会话 cwd 记在 agent 自己的状态里）。
        """
        return None

    @abstractmethod
    def resume_seed(
        self, parent: RunRecord, *, redactor: "SecretRedactor"
    ) -> bytes | None:
        """The sanitized session snapshot handed to the child kernel
        （自 runner L880-926 搬迁）。

        pi：经 ArtifactTree 有界读父会话文件、重脱敏，各失败点抛
        ``session_unavailable``（路径校验、读失败、空种子、无 session_id、
        解码失败——拒绝文本与次序原样）；ACP：返回 None（续接住在父
        状态根，prepare_runtime 继承）。
        """

    @abstractmethod
    def resume_value(self, parent: RunRecord | None) -> str | None:
        """The ``ExecutionSpec.resume_session`` value（自 runner L1633-1637）。

        pi：有父即 ``"continued.jsonl"``；ACP：父记录的 ``session_id``。
        无父两族皆 None。
        """

    @abstractmethod
    def verifies_continuation_before_turn(self, executor: str) -> bool:
        """Whether the child's session identity is proven before the turn
        （自 runner L1202-1214 的条件搬迁；GLINK-ARCH-3 收敛后两族皆恒 True）。

        pi：恒 True——e2b 面自 RPC 通道收敛起与 inproc/docker 同规则，
        ``get_state`` 在 ``session_ready`` 即知会话身份（原义务：沙箱面流
        中才发现 session id，只能回合后校验，已随旧 ``--mode json`` 适配器
        删除）；ACP：恒 True（session/resume 之后即知）。``executor`` 参数
        保留以稳定签名。
        """

    # -- (c) runtime 渲染与 projector 工厂 -------------------------------------

    @abstractmethod
    def prepare_runtime(
        self,
        run_id: str,
        run_dir: Path,
        *,
        parent: RunRecord | None,
        session_seed: bytes | None,
        credentials: "RunCredentials | None",
    ) -> PreparedRuntime:
        """Render the run's per-harness configuration（自 L1509-1596 搬迁）。

        调用前 runner 已做好父记录二次授权（原 L1534-1539 的
        ``_authorized_parent`` 重查与 ``session_unavailable`` 拒绝，时序
        等价地平移到调用点）。pi：prepare_agent_dir + 种子写入
        continued.jsonl + web-search.json 入 secret_files。ACP：
        prepare_acp_runtime（含 inherit_from=self.state_home(parent)、
        AcpStateInheritError 原文包装）；session_seed 非 None 抛
        ``session_unavailable``（原 L1555-1558 语义与文本）。
        """

    @abstractmethod
    def make_projector(self, redactor: "SecretRedactor") -> "Projector":
        """The frame projector（pi：``Projector``；ACP：``AcpProjector``，
        自 L1570/L1595 搬迁）。runner 在 PreparedRuntime 的 secret 全部
        并入边界之后调用，保持原构造次序。"""

    # -- (d) adapter 组装 -------------------------------------------------------

    @abstractmethod
    def build_adapter(
        self,
        spec: ExecutionSpec,
        *,
        executor: str,
        on_frame: Callable[[Mapping[str, Any]], None],
        on_identity: Callable[[Mapping[str, str]], None],
        client_factory: Callable[..., Any],
        sandbox_factory: Callable[..., Any] | None,
    ) -> ExecutionAdapter:
        """Compose the face adapter（委托 ``execution.build_adapter``，签名
        不变）。族内私有 plan 方法自 runner 搬迁：pi 族 ``_rpc_plan``
        （L1719-1729）/``_docker_plan``（L1731-1746）；ACP 族
        ``_acp_plan``/``_acp_docker_plan``/``_acp_e2b_plan``
        （L1656-1717，含 AcpRuntime 类型守卫与 daemon 检查原文）。docker
        容器名预持久化不在此——runner 保留（资源台账身份，面关切）。
        """

    # -- (e) 终态词汇归一化 ------------------------------------------------------

    @abstractmethod
    def model_stop_verdict(
        self, stop: str | None, *, model_error: str | None
    ) -> Verdict | None:
        """Translate the harness's stop vocabulary into the product verdict
        table（自 runner L1411-1441 两族分支搬迁）；``None`` 表示落入
        runner 的交付策略（报告检查）。返回值逐字等于现分支字面量，映射
        表见契约 §4，是本方法的验收 oracle。"""

    # -- (f) 子代理增强与运行中事实 ---------------------------------------------

    def enrich_children(
        self,
        run_id: str,
        *,
        runtime: "AgentRuntime | AcpRuntime | None",
        artifact_failures: list[str],
        sweep: Callable[[Path], bool],
        warn: Callable[[str], None],
    ) -> bool:
        """Associate subagent artifacts with child documents（pi 族承载现
        L2553-2642 全量逻辑；基类空实现即 ACP 的 early return）。

        ``sweep``/``warn`` 是 runner 注入的回调（脱敏清扫与
        child_doc_warning），事件发射权留 runner；返回末段 children 目录
        清扫是否改写了字节（对应现 L2638-2642 的 redacted 置位）。界限
        ``_ENRICH_*`` 与 ``meta``/``unmatched``/``none`` 词汇随实现搬迁。
        """
        return False

    @abstractmethod
    def match_progress_n(
        self, fact: Any, progress_calls: Mapping[str, list[dict[str, Any]]]
    ) -> int | None:
        """Pair one polled progress fact with its child ``n``（自
        L2384-2404 搬迁）。pi：前台守卫 + transcript index + agent 同名；
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
        """Fold one usage snapshot into the run total（自 L1882-1911 两套
        算法搬迁）。pi：按响应去重键累加 + requests 计数；ACP：累计上下文
        快照整体替换（诚实转换，不伪造增长）。返回事件载荷或 None（重复
        帧）。"""


def select_family(
    harness: str, *, config: HostConfig, store: RunStore
) -> HarnessFamily:
    """The process-wide family, chosen once in ``RunManager.__init__``.

    ``harness`` is the selected pack's manifest ``family`` (``pi``/``acp``),
    derived and validated by :func:`pi_glink.config.build_config`; the pack
    itself travels on the frozen config (``harness_pack``,
    ``pack_capabilities``, ``pack_home``, the ACP argv/options).
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
    return family(config=config, store=store)
