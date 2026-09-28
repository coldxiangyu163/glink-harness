"""pi 族子代理的运行中进度源（R3a）。

契约见 docs/architecture.md §3.5「运行中进度（R3a）」。本模块只提供纯
函数/类，**不接线 runner**——``ExecutionAdapter.child_progress_source()``
seam、轮询线程、节流与 ``subagent_updated`` 事件发射都由接线层完成；
（index → child n）的关联同样不在这里。

进度语义（逐字遵守 §3.5）：

* 载荷只含 facts——步骤数（工具调用计数）、最近工具名与目标（限幅、可空）、
  transcript 当前字节数的**累计快照**（与 ``tool_updated`` 同一语义，不是增量）；
* 是「最新值取胜」的瞬时事实：``poll()`` 返回每个已发现 transcript 的当前
  快照，丢中间帧不改变最终呈现；永不参与终态判定；
* 不伪造：读不出内容、被限幅冻结、链接/特殊文件、数量超限的 transcript
  不产出进度（对应 child 在前端显示为普通「进行中」）。

transcript 格式钉住 pi-subagents 0.40.0 ``ChildTranscriptWriter``
（``src/shared/child-transcript.ts``）：JSONL，每行一条带 ``recordType``
的记录；``tool_start`` 携带 ``toolName`` 与 ``argsPreview``（writer 已把
payload 限幅在 32KB 内）。布局命名复用
:mod:`pi_glink.kernel.subagent_artifacts` 钉住的
``<runId>_<agent>_<index>_transcript.jsonl`` 解析。
"""

from __future__ import annotations

import errno
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .e2b_env import (
    E2B_REQUEST_TIMEOUT_SECONDS,
    SUBAGENT_RECOVER_MAX_DEPTH,
    SUBAGENT_RECOVER_MAX_FILE_BYTES,
    SUBAGENT_RECOVER_MAX_FILES,
)
from .subagent_artifacts import parse_artifact_filename

#: 进度载荷的限幅（字符）。目标与名字只用于展示，超限截断不报错。
TOOL_NAME_MAX_CHARS = 64
TOOL_TARGET_MAX_CHARS = 120

#: 单次 poll 跟踪的 transcript 上限——与 R2 回收同一值；超出属 runaway，
#: 一律不读取不伪造（与 ``CHILD_READ_LIMIT`` 的诚实口径一致）。
PROGRESS_MAX_FILES = SUBAGENT_RECOVER_MAX_FILES

#: 单份 transcript 的读取上限——与 R2 回收同一值。越界后内容冻结在已读
#: 部分（步骤不再追加），``bytes`` 仍如实报文件当前大小。
TRANSCRIPT_MAX_BYTES = SUBAGENT_RECOVER_MAX_FILE_BYTES

#: writer 单条记录的 payload 上限是 32KB；半行超过此值即畸形，丢弃这段
#: 不可判定的内容，不拿半截 JSON 猜进度。
_PENDING_LINE_MAX_BYTES = 512 * 1024

#: 每份文件每次 refresh 最多读取的新增字节数（增量读取的有界性）。
_READ_CHUNK_BYTES = 1024 * 1024

#: E2B 面单次 ``files.list`` / ``files.read`` 的请求超时（秒）。SDK 默认 60s
#: 会把一次卡住的调用变成整整一分钟的停顿，且真实故障（沙箱繁忙）时每次
#: poll 都撞满这个值；显式收窄后失败更快、退避后重试更密（GLINK-OBS-01
#: 第 4 项：r2 里 5 次 60s 超时击穿了当时的重试预算）。值来自
#: :mod:`pi_glink.kernel.e2b_env`：回收与进度共用同一条有界口径，单一真源。
E2B_PROGRESS_REQUEST_TIMEOUT_SECONDS = E2B_REQUEST_TIMEOUT_SECONDS

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)


@dataclass(frozen=True)
class ProgressFact:
    """一份 transcript 的当前进度快照（瞬时事实，最新值取胜）。

    ``last_tool_name`` / ``last_tool_target`` 对应事件载荷的
    ``lastToolName`` / ``lastToolTarget``（命名映射在接线层完成）；
    ``bytes`` 是 transcript 当前字节数的累计快照。
    """

    agent: str
    index: int
    steps: int
    last_tool_name: str | None
    last_tool_target: str | None
    bytes: int


class _TranscriptParser:
    """单份 transcript 内容的增量 JSONL 解析器。

    只统计 ``recordType == "tool_start"`` 的记录；半行（未遇换行）留待
    下次，畸形行跳过。无名 ``tool_start`` 计数但不覆盖已知的最近工具——
    不拿不可判定的记录抹掉已知值；有名字但缺 ``argsPreview`` 的目标如实
    为空（不留旧目标造成错配）。
    """

    def __init__(self) -> None:
        self._pending = b""
        self.steps = 0
        self.last_tool_name: str | None = None
        self.last_tool_target: str | None = None

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        data = self._pending + chunk
        *complete, self._pending = data.split(b"\n")
        if len(self._pending) > _PENDING_LINE_MAX_BYTES:
            self._pending = b""
        for line in complete:
            self._consume(line)

    def _consume(self, line: bytes) -> None:
        line = line.strip()
        if not line:
            return
        try:
            record = json.loads(line)
        except ValueError:
            return
        if not isinstance(record, dict):
            return
        if record.get("recordType") != "tool_start":
            return
        self.steps += 1
        name = record.get("toolName")
        if not isinstance(name, str) or not name.strip():
            return
        self.last_tool_name = name[:TOOL_NAME_MAX_CHARS]
        target = record.get("argsPreview")
        if isinstance(target, str) and target.strip():
            self.last_tool_target = target[:TOOL_TARGET_MAX_CHARS]
        else:
            self.last_tool_target = None


class TranscriptProgressReader:
    """一份本地 transcript 的增量读取器（目录句柄 + 已读偏移）。

    每次 :meth:`refresh` 用 ``dir_fd`` 相对打开（``O_NOFOLLOW``），只解析
    新增的完整行；文件被替换（inode 变化）或变短时从零重新解析——进度是
    瞬时快照，重新计数不违反「最新值取胜」。链接与特殊文件永不跟随
    （:attr:`rejected` 置位后不再读取）；暂时不可读（如被 unlink）保留上次
    快照，下轮再试。
    """

    def __init__(self, dir_fd: int, name: str, *, max_bytes: int = TRANSCRIPT_MAX_BYTES) -> None:
        self._dir_fd = dir_fd
        self._name = name
        self._max_bytes = max_bytes
        self._parser = _TranscriptParser()
        self._offset = 0
        self._identity: tuple[int, int] | None = None
        self._size = 0
        self._rejected = False

    @property
    def rejected(self) -> bool:
        """链接/特殊文件：永不读取，不产出进度。"""
        return self._rejected

    @property
    def steps(self) -> int:
        return self._parser.steps

    @property
    def last_tool_name(self) -> str | None:
        return self._parser.last_tool_name

    @property
    def last_tool_target(self) -> str | None:
        return self._parser.last_tool_target

    @property
    def size(self) -> int:
        """最近一次观察到的文件大小（bytes 快照）。"""
        return self._size

    def refresh(self) -> None:
        if self._rejected:
            return
        try:
            info = os.stat(self._name, dir_fd=self._dir_fd, follow_symlinks=False)
        except OSError:
            return
        if not stat.S_ISREG(info.st_mode):
            self._rejected = True
            return
        identity = (info.st_dev, info.st_ino)
        if identity != self._identity or info.st_size < self._offset:
            self._parser = _TranscriptParser()
            self._offset = 0
            self._identity = identity
        self._size = info.st_size
        remaining_budget = self._max_bytes - self._offset
        if remaining_budget <= 0 or info.st_size <= self._offset:
            return
        try:
            fd = os.open(
                self._name,
                os.O_RDONLY | _NOFOLLOW | _CLOEXEC,
                dir_fd=self._dir_fd,
            )
        except OSError:
            return
        try:
            with os.fdopen(fd, "rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read(
                    min(_READ_CHUNK_BYTES, remaining_budget, info.st_size - self._offset)
                )
        except OSError:
            return
        self._parser.feed(chunk)
        self._offset += len(chunk)


class PiChildProgressSource:
    """轮询本地目录（inproc/docker 的 ``session_dir/subagent-artifacts``）。

    目录句柄纪律：目录本身以 ``O_NOFOLLOW | O_DIRECTORY`` 打开一次并持有，
    文件一律相对该句柄打开、不跟随链接。目录尚不存在（尚未派发子代理）
    时 ``poll()`` 返回空——缺席不是观察者失败；目录打开的其他错误
    （权限、链接替换等）原样抛出，由接线层按有界退避发 ``kernel_status``
    警告。返回列表按文件名排序，确定性可测。
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        max_files: int = PROGRESS_MAX_FILES,
        max_file_bytes: int = TRANSCRIPT_MAX_BYTES,
    ) -> None:
        self._directory = Path(directory)
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes
        self._dir_fd: int | None = None
        self._closed = False
        self._readers: dict[str, TranscriptProgressReader] = {}
        # 归属安全：只报告**自上轮以来有变化**的 transcript。前台守卫下同
        # 目录会躺着上一次派发已冻结的同名 (agent, index) 文件，首次见到
        # 只建基线不上报——否则旧文件会被错配给新在飞 call 的 child
        # （与 R2「无法区分宁可不关联」同一条不伪造口径）。
        self._reported: dict[str, tuple] = {}

    def poll(self) -> list[ProgressFact]:
        if self._closed:
            raise ValueError("progress source is closed")
        fd = self._ensure_dir()
        if fd is None:
            return []
        parsed = []
        for name in os.listdir(fd):
            item = parse_artifact_filename(name)
            if item is not None and item.kind == "transcript":
                parsed.append((name, item))
        parsed.sort(key=lambda pair: pair[0])
        tracked = parsed[: self._max_files]
        facts = []
        for name, item in tracked:
            reader = self._readers.get(name)
            if reader is None:
                reader = TranscriptProgressReader(fd, name, max_bytes=self._max_file_bytes)
                self._readers[name] = reader
            reader.refresh()
            if reader.rejected:
                continue
            fact = ProgressFact(
                agent=item.agent,
                index=item.index,
                steps=reader.steps,
                last_tool_name=reader.last_tool_name,
                last_tool_target=reader.last_tool_target,
                bytes=reader.size,
            )
            signature = (
                fact.steps,
                fact.last_tool_name,
                fact.last_tool_target,
                fact.bytes,
            )
            previous = self._reported.get(name)
            self._reported[name] = signature
            if previous is None or previous == signature:
                continue
            facts.append(fact)
        return facts

    def close(self) -> None:
        self._closed = True
        if self._dir_fd is not None:
            os.close(self._dir_fd)
            self._dir_fd = None

    def _ensure_dir(self) -> int | None:
        if self._dir_fd is not None:
            return self._dir_fd
        # 目录自身是链接属边界违例（宿主从不跟随链接）：响亮失败，由接线层
        # 发 kernel_status 警告；macOS 上 O_NOFOLLOW|O_DIRECTORY 对链接报
        # 的 ENOTDIR 与「路径是普通文件」不可区分，因此先 lstat 判别。
        try:
            info = os.lstat(self._directory)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode):
            raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), str(self._directory))
        try:
            self._dir_fd = os.open(
                self._directory,
                os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC,
            )
        except (FileNotFoundError, NotADirectoryError):
            return None
        return self._dir_fd


def _entry_kind(entry: Any) -> str:
    """SDK 条目类型的纯小写字符串（与 ``e2b_env._entry_kind`` 同一语义）。

    ``EntryInfo.type`` 是枚举（字符串形如 ``"FileType.DIR"``），测试替身
    或旧载荷可能直接给值；缺失按普通文件（列表的默认）。
    """
    raw = getattr(entry, "type", None)
    value = getattr(raw, "value", raw)
    if isinstance(value, str) and value:
        return value.strip().lower()
    return "file"


class _E2bFileState:
    """一个远端 transcript 的增量状态（按列表大小驱动的偏移）。"""

    def __init__(self) -> None:
        self.parser = _TranscriptParser()
        self.offset = 0
        self.size = 0
        self.seen = False

    def fact(self, agent: str, index: int) -> ProgressFact:
        return ProgressFact(
            agent=agent,
            index=index,
            steps=self.parser.steps,
            last_tool_name=self.parser.last_tool_name,
            last_tool_target=self.parser.last_tool_target,
            bytes=self.size,
        )


class E2bChildProgressSource:
    """E2B 面的 pi 族进度源：数据来自 ``sandbox.files.list`` / ``read``。

    限幅与 R2 回收同一组（文件数、单文件字节、遍历深度）。读取前先以
    列表元数据限幅：SDK 不报大小的文件**不整份载入**（与工程验证矩阵的
    E2B 读取口径一致），超限文件只如实报 ``bytes``、内容冻结。目录与
    链接条目跳过不跟随；单文件读取失败保留上次快照，列表失败原样抛出
    （接线层按有界退避处理）。
    """

    def __init__(
        self,
        sandbox: Any,
        artifacts_root: str,
        *,
        max_files: int = PROGRESS_MAX_FILES,
        max_file_bytes: int = TRANSCRIPT_MAX_BYTES,
        list_depth: int = SUBAGENT_RECOVER_MAX_DEPTH,
    ) -> None:
        self._sandbox = sandbox
        self._root = artifacts_root
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes
        self._list_depth = list_depth
        self._closed = False
        self._states: dict[str, _E2bFileState] = {}
        self._reported: dict[str, tuple] = {}

    def poll(self) -> list[ProgressFact]:
        if self._closed:
            raise ValueError("progress source is closed")
        entries = self._sandbox.files.list(
            self._root,
            depth=self._list_depth,
            request_timeout=E2B_PROGRESS_REQUEST_TIMEOUT_SECONDS,
        )
        items = []
        for entry in entries:
            if _entry_kind(entry) != "file":
                continue
            path = str(getattr(entry, "path", "") or "")
            if not path:
                continue
            item = parse_artifact_filename(path.rsplit("/", 1)[-1])
            if item is None or item.kind != "transcript":
                continue
            items.append((path, item, getattr(entry, "size", None)))
        items.sort(key=lambda triple: triple[0])
        facts = []
        for path, item, size in items[: self._max_files]:
            fact = self._refresh(path, item.agent, item.index, size)
            if fact is None:
                continue
            # 与本地源同一条归属安全规则：只报告自上轮以来有变化的文件，
            # 首次见到只建基线——冻结的旧派发文件不得错配给新在飞 child。
            signature = (
                fact.steps,
                fact.last_tool_name,
                fact.last_tool_target,
                fact.bytes,
            )
            previous = self._reported.get(path)
            self._reported[path] = signature
            if previous is None or previous == signature:
                continue
            facts.append(fact)
        return facts

    def close(self) -> None:
        self._closed = True

    def _refresh(self, path: str, agent: str, index: int, size: Any) -> ProgressFact | None:
        state = self._states.get(path)
        if state is None:
            state = _E2bFileState()
            self._states[path] = state
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            # 读取前限幅：大小未知就不整份载入；已有快照保留，否则不伪造。
            return state.fact(agent, index) if state.seen else None
        if size > self._max_file_bytes:
            state.size = size
            state.seen = True
            return state.fact(agent, index)
        if state.seen and size == state.offset:
            state.size = size
            return state.fact(agent, index)
        try:
            data = self._sandbox.files.read(
                path, request_timeout=E2B_PROGRESS_REQUEST_TIMEOUT_SECONDS
            )
        except Exception:  # noqa: BLE001 - 单文件失败不拖垮整轮，保留上次快照
            return state.fact(agent, index) if state.seen else None
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not isinstance(data, (bytes, bytearray)):
            return state.fact(agent, index) if state.seen else None
        data = bytes(data)
        if len(data) > self._max_file_bytes:
            state.size = len(data)
            state.seen = True
            return state.fact(agent, index)
        if len(data) < state.offset:
            state.parser = _TranscriptParser()
            state.offset = 0
        state.parser.feed(data[state.offset :])
        state.offset = len(data)
        state.size = len(data)
        state.seen = True
        return state.fact(agent, index)
