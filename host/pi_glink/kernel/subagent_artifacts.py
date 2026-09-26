"""pi-subagents 产物布局的解析与子代理关联（R2，pi 族专用）。

只有 pi 族产生这些文件；布局是 pi-subagents 的**私有实现**（不是官方文档化
API），由 ``tests/test_kernel_subagent_artifacts.py`` 对照
``tests/fixtures/subagent-artifacts/`` 钉住——包升级改变布局时测试立刻变红，
而不是静默丢失增强（docs/pi-integration-notes.md §10 的同一口径）。

源码事实（pi-subagents 0.40.0 ``src/shared/artifacts.ts``、
``src/extension/rpc.ts``）：

* 文件名 ``<runId>_<safeAgent>_<index>_{input,output}.{md}``、
  ``..._transcript.jsonl``、``..._meta.json``；``safeAgent`` 把非
  ``[\\w.-]`` 字符替换为 ``_``（因此 agent 名本身可以含下划线——
  解析一律**从右向左**：扩展名、kind、数字 index 先定位，中间段才是 agent）。
* ``index`` 是**同一次派发内**的子代理序号（0 起，跨 agent 连续），
  与 run 文档的全局 ``n`` 不是同一坐标系；文件名里的 ``runId`` 是
  pi-subagents 自己的派发 id，与 toolCallId 无可见映射。
* 关联因此是**序数启发式**：同一 agent 名下，child 文档按 ``n`` 排序、
  文件按 ``(index, 文件名)`` 排序，数量相等才一一配对；任何一侧数量不符
  或 agent 对不上，该 agent 的全部 child 标记 ``unmatched``——不错配。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

#: ``<runId>_<agent>_<index>_<kind>.<ext>``，从右向左解析（agent 可含下划线）。
_ARTIFACT_NAME = re.compile(
    r"^(?P<runid>[^/]+?)_(?P<agent>[\w.-]+)_(?P<index>\d+)"
    r"_(?P<kind>input|output|transcript|meta)\.(?P<ext>md|jsonl|json)$"
)

#: _meta.json 中进入 child 文档的白名单字段。``cost`` 被刻意排除：宿主不内置
#: 价格表，pi-subagents 的估费不是宿主事实，透传会让 run 记录出现伪造计费
#: （architecture.md §3.4 的同一口径）。
_META_USAGE_FIELDS = ("input", "output", "cacheRead", "cacheWrite", "turns")


@dataclass(frozen=True)
class ArtifactName:
    """一个解析出的产物文件名（相对路径保持原样）。"""

    path: str
    agent: str
    index: int
    kind: str  # input | output | transcript | meta


@dataclass
class Association:
    """关联结果：``by_n`` 只含成功配对；两侧剩余都如实列出。"""

    by_n: dict[int, list[ArtifactName]] = field(default_factory=dict)
    unmatched_children: list[int] = field(default_factory=list)
    unmatched_files: list[ArtifactName] = field(default_factory=list)


def parse_artifact_filename(name: str) -> ArtifactName | None:
    """解析一个产物相对路径的 basename；不符合钉住布局返回 ``None``。"""
    match = _ARTIFACT_NAME.match(name.rsplit("/", 1)[-1])
    if match is None:
        return None
    return ArtifactName(
        path=name,
        agent=match.group("agent"),
        index=int(match.group("index")),
        kind=match.group("kind"),
    )


def read_meta_enrichment(raw: bytes) -> dict[str, Any] | None:
    """从 ``_meta.json`` 字节提取白名单增强字段；不可解析返回 ``None``。

    返回键：``exitCode``、``durationMs``、``toolCount``、``model``、``usage``
    （仅 :data:`_META_USAGE_FIELDS` 的计数项）。缺失的键不伪造、不补零。
    """
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    enrichment: dict[str, Any] = {}
    for key in ("exitCode", "durationMs", "toolCount", "model"):
        value = data.get(key)
        if isinstance(value, (int, str)) and not isinstance(value, bool):
            enrichment[key] = value
    usage = data.get("usage")
    if isinstance(usage, dict):
        counters = {
            key: usage[key]
            for key in _META_USAGE_FIELDS
            if isinstance(usage.get(key), int) and not isinstance(usage.get(key), bool)
        }
        if counters:
            enrichment["usage"] = counters
    return enrichment or None


def associate_children(
    children: list[Mapping[str, Any]],
    artifacts: list[ArtifactName],
) -> Association:
    """把产物文件按（agent, 序数）关联到 child 文档。

    同一 agent 名下：文档按 ``n`` 升序、文件按 ``(index, path)`` 升序，
    数量相等才一一配对；数量不符或对不上 agent 的，两侧都进 unmatched——
    序数启发式宁可不关联，也不错配（§3.5 的诚实口径）。
    """
    result = Association()
    children_by_agent: dict[str, list[Mapping[str, Any]]] = {}
    for child in children:
        agent = str(child.get("agent") or "")
        n = child.get("n")
        if not agent or not isinstance(n, int):
            if isinstance(n, int):
                result.unmatched_children.append(n)
            continue
        children_by_agent.setdefault(agent, []).append(child)
    files_by_agent: dict[str, list[ArtifactName]] = {}
    for artifact in artifacts:
        files_by_agent.setdefault(artifact.agent, []).append(artifact)

    for agent, group in children_by_agent.items():
        files = sorted(files_by_agent.pop(agent, []), key=lambda a: (a.index, a.path))
        file_groups = _group_by_index(files)
        group = sorted(group, key=lambda c: c["n"])
        if len(file_groups) != len(group) or any(
            len({f.kind for f in fg}) != len(fg) for fg in file_groups
        ):
            # 数量不符或同 index 同 kind 冲突：该 agent 的配对不可信，整体不关联。
            result.unmatched_children.extend(c["n"] for c in group)
            result.unmatched_files.extend(files)
            continue
        for child, file_group in zip(group, file_groups):
            result.by_n[child["n"]] = list(file_group)
    for leftovers in files_by_agent.values():
        result.unmatched_files.extend(leftovers)
    return result


def _group_by_index(files: list[ArtifactName]) -> list[list[ArtifactName]]:
    """``index`` 相同的文件（input/output/transcript/meta 四种 kind）归一组。"""
    groups: dict[int, list[ArtifactName]] = {}
    for artifact in files:
        groups.setdefault(artifact.index, []).append(artifact)
    return [groups[index] for index in sorted(groups)]
