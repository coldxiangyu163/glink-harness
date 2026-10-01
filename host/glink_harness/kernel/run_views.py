"""会话来源窄协议与宿主侧 RunStore 适配器（R1-1，设计 §2.3）。

把 ``HarnessFamily.__init__(*, config, store)`` 里的 ``store`` 注入点收窄为
:class:`SessionSource` 协议——「会话来源 + 记录只读视图 + 子代理文档读写」。
族不再持有具体 :class:`~glink_harness.store.RunStore`；宿主侧由
:class:`RunStoreSessionSource` 适配器实现协议，构造族实例时（
``kernel.harness.select_family``）统一接线。

协议面对设计 §2.3 表格的覆盖（缺一即返工）：

=================== ================================================ ====
协议面              协议方法                                         现状位置
=================== ================================================ ====
父记录只读视图      :meth:`SessionSource.get`                        harness_acp.py 父链走查 / 状态根推导
父 run 事件扫描     :meth:`SessionSource.iter_events`                harness_acp.py 能力矩阵（session_ready 帧）
目录布局推导        ``run_dir`` / ``session_dir`` /                  harness_acp.py 状态根与会话目录、harness_pi.py 种子目录
                    ``conversations_root`` / ``conversation_dir``
父会话种子有界读    :meth:`SessionSource.read_session_seed`          harness_pi.py resume_seed 的读路径
子代理文档读写      ``read_children`` / ``read_child`` /             harness_pi.py enrich_children
                    ``write_child``
e2b 租约/泊车事实   ``get`` + ``conversations_root`` /               harness_acp.py sandbox_resume_target
                    ``conversation_dir``（租约文件读取本体在
                    ``kernel/conversation_lease.py``，不经 store）
=================== ================================================ ====

两条纪律：

* 协议方法不得引用 ``RunStore`` 具体类型（记录只读视图沿用共享的
  ``RunRecord`` 数据类型；进一步收窄为专用视图类型属 R1-2/R1-3），由
  ``tests/test_repository_boundaries.py`` 卡住；
* 目录布局方法只回答「宿主把事实放在哪」，不泄露 SQLite 布局假设——R3 之后
  路径语义由 ULegal 重定义，族不得在此之外自造路径规则。

pi/ACP 族内部改用协议访问属 R1-2/R1-3；R1-1 只换注入点，族模块经本适配层
拿到与现状等价的能力，不新增对 store 的直接引用面。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol

from ..artifacts import ArtifactTree
from ..store import ChildDocuments, RunRecord, RunStore


class SessionSource(Protocol):
    """族可见的会话来源窄协议（方法面即上表；实现由宿主侧适配器给出）。

    方法名与语义对齐 ``RunStore`` 的同名只读/文档方法，使族内访问面在
    R1-2/R1-3 迁移前后保持一致；协议本身不暴露 record 写入、事件提交、
    资源台账等 RunStore 的其余能力。
    """

    # -- 面 1：父记录只读视图（spec/session_id/status/executor） ---------------

    def get(self, run_id: str) -> RunRecord | None:
        """The committed run record, or ``None``（父链走查与状态根事实用）。"""
        ...

    # -- 面 2：父 run 事件扫描（能力矩阵只在事件流，不进 RunRecord） -----------

    def iter_events(self, run_id: str, after: int = 0) -> Iterator[dict[str, Any]]:
        """Yield committed events with ``seq > after``, in ``seq`` order."""
        ...

    # -- 面 3：目录布局推导 -----------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        """The run's own directory（状态根推导与子代理增强的落点）。"""
        ...

    def session_dir(self, run_id: str) -> Path:
        """The run's own pi session directory（面 4 种子读的目录事实）。"""
        ...

    def conversations_root(self) -> Path:
        """The conversations root（面 6 租约/状态发布事实的根）。"""
        ...

    def conversation_dir(self, root_run_id: str) -> Path:
        """The conversation-root directory of one chain（宿主拥有，0700）。"""
        ...

    # -- 面 4：父会话种子有界读（重脱敏留族侧，见 harness_pi.resume_seed） -----

    def read_session_seed(self, run_id: str, file_name: str, *, max_bytes: int) -> bytes:
        """Bounded read of one direct file in the run's session directory.

        ``file_name`` 必须是不含路径分隔符的裸文件名（嵌套路径是调用方
        缺陷，抛 ``ValueError``）；读取失败抛
        :class:`~glink_harness.artifacts.ArtifactError` / ``OSError``，由族
        包装为 ``session_unavailable``。重脱敏不在本层：种子的脱敏边界属于
        子 run，族拿到字节后用自己的 redactor 处理。
        """
        ...

    # -- 面 5：子代理文档双向读写 ----------------------------------------------

    def read_children(self, run_id: str) -> ChildDocuments:
        """All readable child-run documents of one run（含诚实跳过计数）。"""
        ...

    def read_child(self, run_id: str, n: int) -> dict[str, Any] | None:
        """One child-run document, or ``None`` when it is missing or unusable."""
        ...

    def write_child(self, run_id: str, document: Mapping[str, Any]) -> None:
        """Atomically rewrite one child-run document（``n`` 取自文档本身）。"""
        ...


class RunStoreSessionSource(SessionSource):
    """宿主侧适配器：``RunStore`` 上的 :class:`SessionSource` 生产实现。

    只委托协议方法，不新增能力；族经此适配器拿到的每一件事与 R1-1 之前
    直接持有 ``RunStore`` 时等价（纯重构，行为不变）。
    """

    def __init__(self, store: RunStore) -> None:
        self._store = store

    def get(self, run_id: str) -> RunRecord | None:
        return self._store.get(run_id)

    def iter_events(self, run_id: str, after: int = 0) -> Iterator[dict[str, Any]]:
        return self._store.iter_events(run_id, after)

    def run_dir(self, run_id: str) -> Path:
        return self._store.run_dir(run_id)

    def session_dir(self, run_id: str) -> Path:
        return self._store.session_dir(run_id)

    def conversations_root(self) -> Path:
        return self._store.conversations_root()

    def conversation_dir(self, root_run_id: str) -> Path:
        return self._store.conversation_dir(root_run_id)

    def read_session_seed(self, run_id: str, file_name: str, *, max_bytes: int) -> bytes:
        if Path(file_name).name != file_name:
            raise ValueError(f"会话种子必须是会话目录的直系文件: {file_name!r}")
        with ArtifactTree(self._store.session_dir(run_id)) as tree:
            return tree.read(file_name, max_bytes=max_bytes)

    def read_children(self, run_id: str) -> ChildDocuments:
        return self._store.read_children(run_id)

    def read_child(self, run_id: str, n: int) -> dict[str, Any] | None:
        return self._store.read_child(run_id, n)

    def write_child(self, run_id: str, document: Mapping[str, Any]) -> None:
        self._store.write_child(run_id, document)
