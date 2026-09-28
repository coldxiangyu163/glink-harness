"""运行时声明的收尾扫描契约（pi 与 ACP 各自声明，runner 只消费）。

收尾脱敏要扫哪些「内核写出的会话/状态路径」是每个 harness 运行时自己的
事实：pi 把会话日志写进本 run 的专属 ``sessions/`` 目录，ACP agent 把会话
写进自己的状态根 ``acp-agent/home``（E2B 面回收后是
``acp-agent/recovered``）。两个运行时形状不同，契约因此是运行时的属性而不是
runner 里的类型分支。

两种处置在契约里显式区分：

* ``rewrite=True``：整棵树由宿主改写（经 :class:`~pi_glink.artifacts.
  ArtifactTree` 的目录句柄，逐级不跟随链接），含凭据的文本文件被原子替换。
  pi 的会话目录适用——它是宿主为 run 渲染的目录，重写幂等，子代理分支会话
  与 ``subagent-artifacts/`` 同树同边界（架构 §6.1）。
* ``rewrite=False``：只读扫描，把文本里的凭据形状值并入本 run 的脱敏边界，
  文件字节不动。ACP 状态根是 **agent 自己的会话存储**：改写会破坏追问要
  续接的会话，因此不重写，边界靠 redactor 知道这些值保持
  （``docs/acp-integration-notes.md`` §10.2、§14.1）。
* ``credential_copies``：宿主注入的凭据副本在该根下的**已知相对路径**，只读
  扫描完成之后逐个删除（docs/adr/0003、``docs/acp-integration-notes.md``
  §18.4）。只删
  列出的路径本身（不按模式匹配、不跟随链接、路径必须在根内）；删除失败记入
  收尾事实并告警，不改变 run 终态。
* pi 的运行期配置目录不声明扫描根：宿主渲染的 ``auth.json`` /
  ``web-search.json`` 只含本 run 的 egress token，终帧提交时已吊销
  （docs/adr/0008-egress-proxy.md），没有需要删除的真实凭据副本。

runner 只按契约遍历：主会话文件是否属于某个根、根能否重写都由运行时声明，
runner 不询问具体 runtime 类型的属性。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SessionSweep:
    """One path a harness runtime declares for the run's收尾 sweep."""

    #: 该根目录的路径；不存在时扫描按「无可扫之物」处理（不报错）。
    root: Path
    #: 是否允许改写含凭据的文本文件（见模块文档的两种处置）。
    rewrite: bool = True
    #: ``RunRecord.session_file`` 记录的主会话文件是否属于这个根（pi）。
    carries_session_file: bool = False
    #: 宿主注入的凭据副本在该根下的已知相对路径；扫描之后收尾删除，
    #: 每个路径只删其本身（不做模式匹配、不跟随链接、必须在根内）。
    credential_copies: tuple[str, ...] = ()
