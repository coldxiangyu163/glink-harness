"""Layout pins for pi-subagents artifacts and the ordinal association (R2).

pi-subagents 的产物布局是它的私有实现（不是官方文档化 API）。这些测试对照
``tests/fixtures/subagent-artifacts/``（按 0.40.0 源码与真实产物合成的样本）
钉住命名规则、``_meta.json`` 白名单提取与「（agent, 序数）」关联规则——
包升级若改变布局，这里立刻变红，而不是静默丢失增强。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from glink_harness.kernel.subagent_artifacts import (
    associate_children,
    parse_artifact_filename,
    read_meta_enrichment,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "subagent-artifacts"


def _child(n: int, agent: str, task: str = "任务") -> dict:
    return {"n": n, "agent": agent, "task": task, "status": "finished"}


# -- the pinned layout ---------------------------------------------------------


def test_fixture_names_all_parse() -> None:
    names = sorted(path.name for path in FIXTURE.iterdir())
    assert names == [
        "a1b2c3d4_researcher_0_input.md",
        "a1b2c3d4_researcher_0_meta.json",
        "a1b2c3d4_researcher_0_output.md",
        "a1b2c3d4_researcher_0_transcript.jsonl",
    ], "fixture 与钉住的四件套命名不一致"
    parsed = {name: parse_artifact_filename(name) for name in names}
    assert all(item is not None for item in parsed.values())
    kinds = {item.kind for item in parsed.values() if item}
    assert kinds == {"input", "output", "transcript", "meta"}
    assert {item.agent for item in parsed.values() if item} == {"researcher"}
    assert {item.index for item in parsed.values() if item} == {0}


def test_name_parsing_is_right_to_left_so_agents_may_contain_underscores() -> None:
    item = parse_artifact_filename("a1b2c3d4_researcher_v2_3_output.md")
    assert item is not None
    assert (item.agent, item.index, item.kind) == ("researcher_v2", 3, "output")
    # 不是四件套之一的文件不参与关联。
    assert parse_artifact_filename("a1b2c3d4_researcher_0_notes.txt") is None
    assert parse_artifact_filename("report.md") is None
    assert parse_artifact_filename("a1b2c3d4_researcher_x_output.md") is None


def test_meta_enrichment_whitelists_counters_and_never_cost() -> None:
    raw = (FIXTURE / "a1b2c3d4_researcher_0_meta.json").read_bytes()
    enrichment = read_meta_enrichment(raw)
    assert enrichment == {
        "exitCode": 0,
        "durationMs": 43210,
        "toolCount": 6,
        "model": "test/model:high",
        "usage": {"input": 1234, "output": 567, "cacheRead": 890, "cacheWrite": 0, "turns": 4},
    }
    assert "cost" not in json.dumps(enrichment), "估费不是宿主事实，永不进入文档"
    assert read_meta_enrichment(b"not json") is None
    assert read_meta_enrichment(b"[]") is None
    # 缺字段不补零、不伪造。
    assert read_meta_enrichment(b'{"exitCode": 1}') == {"exitCode": 1}


# -- the ordinal association ----------------------------------------------------


def _files(spec: list[tuple[str, int, str]]) -> list:
    """``(agent, index, kind)`` 列表转成解析后的 ArtifactName。"""
    return [
        item
        for agent, index, kind in spec
        if (item := parse_artifact_filename(f"a1b2c3d4_{agent}_{index}_{kind}.json")) is not None
    ]


def test_single_call_children_pair_by_agent_and_index() -> None:
    children = [_child(0, "researcher"), _child(1, "researcher")]
    artifacts = _files(
        [("researcher", 0, "meta"), ("researcher", 0, "output"),
         ("researcher", 1, "meta"), ("researcher", 1, "output")]
    )
    result = associate_children(children, artifacts)
    assert result.unmatched_children == [] and result.unmatched_files == []
    assert [a.kind for a in result.by_n[0]] == ["meta", "output"]
    assert {a.index for a in result.by_n[1]} == {1}


def test_index_collision_across_calls_is_honestly_unmatched() -> None:
    """两次调用各派一个同名 agent：两个 index 0 无法区分，宁可不关联。"""
    children = [_child(0, "researcher"), _child(1, "researcher")]
    artifacts = _files(
        [("researcher", 0, "meta"), ("researcher", 0, "output")]
    )
    # 模拟第二次调用的同 index 文件（不同 runId 前缀，解析后 index 同为 0）。
    artifacts += [
        item
        for name in ("b2c3d4e5_researcher_0_meta.json", "b2c3d4e5_researcher_0_output.md")
        if (item := parse_artifact_filename(name)) is not None
    ]
    result = associate_children(children, artifacts)
    assert sorted(result.unmatched_children) == [0, 1]
    assert result.by_n == {}
    assert len(result.unmatched_files) == 4


def test_count_mismatch_leaves_the_whole_agent_unpaired() -> None:
    children = [_child(0, "researcher")]
    artifacts = _files(
        [("researcher", 0, "meta"), ("researcher", 1, "meta")]
    )
    result = associate_children(children, artifacts)
    assert result.unmatched_children == [0]
    assert result.by_n == {}


def test_files_without_a_matching_child_are_listed_not_dropped() -> None:
    children = [_child(0, "researcher")]
    artifacts = _files([("verifier", 0, "meta"), ("researcher", 0, "meta")])
    result = associate_children(children, artifacts)
    assert [a.agent for a in result.unmatched_files] == ["verifier"]
    assert [a.kind for a in result.by_n[0]] == ["meta"]


def test_children_without_an_agent_never_pair() -> None:
    """ACP 族风格的空 agent 文档不参与 pi 产物关联（不伪造）。"""
    children = [_child(0, "")]
    artifacts = _files([("researcher", 0, "meta")])
    result = associate_children(children, artifacts)
    assert result.unmatched_children == [0]
    assert result.by_n == {}


def test_pairing_zip_is_strict_so_a_broken_length_invariant_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配对循环的 zip 必须是 strict 的（B905 语义锚定）。

    ``associate_children`` 只在 ``len(file_groups) == len(group)`` 判等后配对。
    若该不变量将来被破坏（例如分组逻辑改动后谎报长度），strict zip 会当场
    抛 ``ValueError``，而不是静默截断、把文件配到错误的 child 上——序数
    启发式的义务是宁可不关联也不错配。
    """

    class _LyingGroups:
        """``__len__`` 谎报为 1（骗过判等），实际产出 2 组。"""

        def __len__(self) -> int:
            return 1

        def __iter__(self):
            real = _files([("researcher", 0, "meta"), ("researcher", 1, "meta")])
            return iter([[real[0]], [real[1]]])

    monkeypatch.setattr(
        "glink_harness.kernel.subagent_artifacts._group_by_index", lambda _files: _LyingGroups()
    )
    children = [_child(0, "researcher")]
    artifacts = _files([("researcher", 0, "meta"), ("researcher", 1, "meta")])
    with pytest.raises(ValueError):
        associate_children(children, artifacts)
