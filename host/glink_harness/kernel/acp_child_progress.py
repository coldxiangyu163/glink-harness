"""ACP 族（dsh）子代理运行中进度源（R3a，docs/architecture.md §3.5「运行中进度（R3a）」）。

格式事实钉自 docs/evidence/acp-subagent-zstd-spike.txt（dsh 0.1.7 实测，私有
session v4 格式）：

* 会话文件为 zstd 压缩 JSONL，主/子会话并列于
  ``<状态根>/.dsh/sessions/--<workspace>--/<sessionId>/session.v4.jsonl.zstd``；
  帧结构 ``{type, seq, time, data}``。文件持续重写（有 session.lock），增量
  读取不可行，只能**整份重读**，因此单文件有大小上限。
* 关联链在主会话内：``tool/call``（``data.callId``/``name``/``arguments``）
  与**相邻**的 ``subagent/catalog``（``data.childId``/``label``/``mode``）给出
  callId↔childId；catalog.data 内无 callId，关联只靠帧相邻位置。catalog 出现在
  非 subagent call 之后时不配对（防御）。
* 子会话头 ``{type:"session", id, parentSession, origin:"subagent",
  delegationDepth:1}`` 判别主/子；子会话内 ``step/start``、``tool/call``、
  ``tool/result``、``step/end`` 帧给逐步进度：``steps`` 取 ``tool/call`` 计数，
  ``lastToolName``/``lastToolTarget``（限幅）取最后一帧，``bytes`` 取源文件字节数。

``zstandard`` 是可选依赖（``glink-harness[acp-progress]``）：缺失时 :func:`available`
返回 False、:meth:`AcpChildProgressSource.poll` 如实返回空——该面无进度，不报错。

本模块只提供解析与轮询源；接线（throttle、``subagent_updated`` 投影、``n`` 映射）
在 runner 侧，进度事实带 ``call_id``/``child_id`` 供接线层映射。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

try:  # 可选依赖 glink-harness[acp-progress]；缺失时本模块整体降级为「无进度」。
    import zstandard as _zstd
except ImportError:  # pragma: no cover - 由 monkeypatch 测试覆盖降级路径
    _zstd = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

#: 包清单 ``capabilities.child_progress`` 取值：本模块解析的格式名。族代码只按这个
#: 能力声明选择进度源，不按包名识别 dsh。
FORMAT = "dsh-session-zstd"

#: 会话根相对 agent 状态根（每 run 的 ``HOME``）的位置。
SESSIONS_RELATIVE = (".dsh", "sessions")

#: dsh 会话文件名（主/子会话同名，按所在目录的 sessionId 区分）。
SESSION_FILENAME = "session.v4.jsonl.zstd"

#: 单份压缩会话文件的大小上限。会话是 KB 级（spike 实测主 20K/子 12K），
#: 超限即该会话（及其关联）本轮无进度，不报错。
MAX_SESSION_FILE_BYTES = 8 << 20

#: 解压后的大小上限（防 zip-bomb 形态的损坏/伪造文件）。
_MAX_DECOMPRESSED_BYTES = 64 << 20

#: ``lastToolTarget`` 的限幅（与仓库既有路径/错误文本限幅口径一致）。
MAX_TARGET_CHARS = 200

#: 从 ``tool/call`` 的 ``arguments`` 提取工具目标的键白名单（按优先级）。
#: arguments 是字符串化 JSON（spike 实测）；取第一个出现的字符串值。
_TARGET_KEYS = (
    "path",
    "file_path",
    "file",
    "command",
    "cmd",
    "query",
    "url",
    "uri",
    "pattern",
)


def available() -> bool:
    """可选依赖 zstandard 是否可用；不可用则 ACP 面如实无进度（不报错）。"""

    return _zstd is not None


@dataclass(frozen=True, slots=True)
class ChildLink:
    """主会话内一对相邻 ``tool/call`` + ``subagent/catalog`` 给出的关联。"""

    call_id: str
    child_id: str
    label: str | None


@dataclass(frozen=True, slots=True)
class ProgressFact:
    """一个子代理当前的进度快照（瞬时事实，最新值取胜，不参与终态判定）。"""

    call_id: str
    child_id: str
    steps: int
    last_tool_name: str | None
    last_tool_target: str | None
    bytes: int


def _iter_frames(path: Path, max_file_bytes: int) -> Iterator[dict[str, Any]]:
    """整份读取并逐行解析一份 zstd 会话；任何失败都如实 yield 完已得部分。

    文件持续重写，末行可能是撕裂行——跳过不可解析的行，下一轮轮询自然补齐。
    超限、解压失败、读取失败都表现为「无帧」而非异常。
    """

    try:
        if path.stat().st_size > max_file_bytes:
            return
        raw = path.read_bytes()
    except OSError:
        return
    try:
        text = _zstd.ZstdDecompressor().decompress(
            raw, max_output_size=_MAX_DECOMPRESSED_BYTES
        ).decode("utf-8")
    except Exception:  # zstd 错误、Unicode 错误：损坏或伪造文件，按无进度处理
        log.debug("acp 子会话解压失败，本轮跳过: %s", path)
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except ValueError:
            continue  # 撕裂行/坏行：跳过，不污染整份解析
        if isinstance(frame, dict):
            yield frame


def _truncate(value: str) -> str:
    return value[:MAX_TARGET_CHARS]


def _tool_target(arguments: Any) -> str | None:
    """从 ``tool/call`` 的 arguments（字符串化 JSON 或已解析对象）取限幅目标。"""

    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None
    if not isinstance(arguments, dict):
        return None
    for key in _TARGET_KEYS:
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return _truncate(value)
    return None


def parse_main_session(
    path: str | Path, *, max_file_bytes: int = MAX_SESSION_FILE_BYTES
) -> list[ChildLink]:
    """按序解析主会话，给出 ``[(callId, childId, label)]`` 关联表。

    catalog 只与**紧邻其前**的 subagent ``tool/call`` 配对；出现在其他帧
    （含非 subagent 的 tool/call）之后的 catalog 不配对。同一 call 的关联
    取第一次出现。
    """

    links: list[ChildLink] = []
    seen_calls: set[str] = set()
    pending_call_id: str | None = None  # 紧邻其前的一帧是 subagent tool/call
    for frame in _iter_frames(Path(path), max_file_bytes):
        frame_type = frame.get("type")
        data = frame.get("data")
        if not isinstance(data, dict):
            data = {}
        if frame_type == "tool/call":
            pending_call_id = (
                data.get("callId") if data.get("name") == "subagent" else None
            )
            if not isinstance(pending_call_id, str):
                pending_call_id = None
            continue
        if frame_type == "subagent/catalog":
            child_id = data.get("childId")
            if (
                pending_call_id is not None
                and isinstance(child_id, str)
                and child_id
                and pending_call_id not in seen_calls
            ):
                label = data.get("label")
                links.append(
                    ChildLink(
                        call_id=pending_call_id,
                        child_id=child_id,
                        label=label if isinstance(label, str) else None,
                    )
                )
                seen_calls.add(pending_call_id)
            pending_call_id = None
            continue
        pending_call_id = None
    return links


def parse_child_session(
    path: str | Path,
    *,
    call_id: str,
    child_id: str,
    max_file_bytes: int = MAX_SESSION_FILE_BYTES,
) -> ProgressFact | None:
    """解析子会话，提取一份进度快照；不是预期子会话或不可解析时返回 None。

    防御：会话头必须 ``origin=="subagent"`` 且 ``id`` 等于期望的 childId——
    目录名对上但内容不符（损坏/重写中）时不伪造进度。
    """

    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > max_file_bytes:
        return None
    steps = 0
    last_name: str | None = None
    last_target: str | None = None
    header_seen = False
    for frame in _iter_frames(path, max_file_bytes):
        frame_type = frame.get("type")
        if not header_seen:
            if frame_type != "session":
                continue
            header_seen = True
            if frame.get("origin") != "subagent" or frame.get("id") != child_id:
                return None
            continue
        if frame_type == "tool/call":
            data = frame.get("data")
            if not isinstance(data, dict):
                data = {}
            steps += 1
            name = data.get("name")
            last_name = name if isinstance(name, str) and name else None
            last_target = _tool_target(data.get("arguments"))
    if not header_seen:
        return None
    return ProgressFact(
        call_id=call_id,
        child_id=child_id,
        steps=steps,
        last_tool_name=last_name,
        last_tool_target=last_target,
        bytes=size,
    )


class AcpChildProgressSource:
    """轮询状态根 sessions 目录树的 ACP 子代理进度源（与 pi 源同形）。

    每轮 :meth:`poll` 整份重读目录树：先扫全部 ``session.v4.jsonl.zstd``，
    从各会话提取 callId↔childId 关联，再按 childId（== 会话目录名）读子会话。
    任何单文件失败（缺失/超限/损坏）只让对应 child 本轮无进度，不抛出。
    zstandard 缺失时 :meth:`poll` 恒为空。
    """

    def __init__(
        self,
        sessions_root: str | Path,
        *,
        max_file_bytes: int = MAX_SESSION_FILE_BYTES,
    ) -> None:
        self._root = Path(sessions_root)
        self._max_file_bytes = max_file_bytes
        self._closed = False

    def poll(self) -> list[ProgressFact]:
        if self._closed or not available():
            return []
        try:
            files = sorted(self._root.rglob(SESSION_FILENAME))
        except OSError:
            return []
        by_session_id = {path.parent.name: path for path in files}
        links: list[ChildLink] = []
        seen_calls: set[str] = set()
        for path in files:
            for link in parse_main_session(
                path, max_file_bytes=self._max_file_bytes
            ):
                if link.call_id in seen_calls:
                    continue
                seen_calls.add(link.call_id)
                links.append(link)
        facts: list[ProgressFact] = []
        for link in links:
            child_path = by_session_id.get(link.child_id)
            if child_path is None:
                continue  # 子会话尚未落盘/已被回收：该 child 无进度，不伪造
            fact = parse_child_session(
                child_path,
                call_id=link.call_id,
                child_id=link.child_id,
                max_file_bytes=self._max_file_bytes,
            )
            if fact is not None:
                facts.append(fact)
        return facts

    def close(self) -> None:
        self._closed = True
