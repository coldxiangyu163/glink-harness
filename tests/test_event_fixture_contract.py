"""权威事件样例库与 fold 契约（ADR 0021）的 Python 侧契约测试。

``tests/fixtures/events/*.jsonl`` 是跨仓契约载体（改样例 = 改契约）：同一份样例
喂两侧 fold——glink web reducer 的快照断言在 ``tests/web/fixture-contract.test.mjs``，
本文件钉住 ULegal trajectory 语义（``tests/trajectory_reference.py`` 参考 fold，
业务里程碑 + 终帧展示快照）的投影行。两份测试读同一份 ``manifest.json`` 期望，
本文件另断言：

* 每个样例逐帧过 ``event_schema`` 信封校验（词汇/版本/字段；legacy 样例逐帧报缺 ``v``）；
* 清单与样例文件一一对应、``forms`` 与样例实际事件形态一致、每个样例带 ``#`` 注释头；
* 清单内两侧共享的核心不变量（status/texts/toolCount）一致。

演进护栏：新事件形态先入样例库（加样例 + 清单条目）再进实现。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from glink_harness import event_schema
from trajectory_reference import fold

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "events"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
SAMPLES = {name: entry for name, entry in MANIFEST.items() if not name.startswith("$")}


def frames_of(name: str) -> list[dict[str, Any]]:
    """One fixture's frames; ``#`` lines are the sample's comment header, not frames."""
    return [
        json.loads(line)
        for line in (FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


# -- 样例库自身：schema 校验、清单对应、注释头 -------------------------------------


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_every_fixture_frame_passes_the_schema(name: str) -> None:
    frames = frames_of(name)
    assert frames, name
    for frame in frames:
        found = event_schema.envelope_problems(frame)
        if name == "legacy-unversioned":
            assert any("v=None" in problem for problem in found), (name, frame["seq"], found)
        else:
            assert found == [], (name, frame["seq"], found)


def test_the_manifest_covers_every_fixture_and_forms_match() -> None:
    on_disk = {path.stem for path in FIXTURES.glob("*.jsonl")}
    assert set(SAMPLES) == on_disk, (sorted(set(SAMPLES) - on_disk), sorted(on_disk - set(SAMPLES)))
    for name, entry in SAMPLES.items():
        forms = sorted({frame["type"] for frame in frames_of(name)})
        assert sorted(entry["forms"]) == forms, name
        assert entry["source"] and entry["semantics"], name


def test_every_fixture_opens_with_a_comment_header() -> None:
    for name in SAMPLES:
        first = (FIXTURES / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()[0]
        assert first.startswith("# "), f"{name}: 首行必须是覆盖语义点的注释头"


# -- trajectory 投影契约 --------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_trajectory_projection_matches_the_manifest(name: str) -> None:
    expected = SAMPLES[name]["trajectory"]
    assert fold(frames_of(name)) == {
        "milestones": expected["milestones"],
        "snapshot": expected["snapshot"],
        "rejected": expected["rejected"],
    }, name


# -- 两侧共享的核心不变量 ---------------------------------------------------------


def test_shared_core_invariants_are_consistent_between_consumers() -> None:
    for name, entry in SAMPLES.items():
        web = entry["web"]
        snapshot = entry["trajectory"]["snapshot"]
        if snapshot is None:
            assert web["status"] == "running" and web["texts"] == [], name
            continue
        assert web["status"] == snapshot["status"], name
        assert web["texts"] == snapshot["texts"], name
        assert web["toolCount"] == snapshot["toolCount"], name
