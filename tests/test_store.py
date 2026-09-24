"""Regressions for the run store: metadata lifecycle and the event log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pi_glink.store import CANCELLED, FINISHED, RUNNING, RunStore


@pytest.fixture()
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "runs")


def test_create_writes_meta_and_returns_running_record(store: RunStore, tmp_path: Path) -> None:
    record = store.create("研究一个问题", run_id="abc123", workspace=tmp_path / "ws")
    assert record.status == RUNNING
    assert record.run_id == "abc123"
    assert record.event_count == 0
    assert (store.run_dir("abc123") / "meta.json").is_file()
    assert store.get("abc123") == record


def test_update_is_atomic_and_validates_fields(store: RunStore) -> None:
    store.create("q", run_id="r1")
    updated = store.update("r1", status=FINISHED, report_path="/tmp/report.md")
    assert updated.status == FINISHED
    assert updated.is_terminal is True
    assert store.get("r1").report_path == "/tmp/report.md"
    with pytest.raises(ValueError, match="unknown run fields"):
        store.update("r1", nonsense=1)
    with pytest.raises(KeyError):
        store.update("missing", status=FINISHED)


def test_list_is_newest_first(store: RunStore) -> None:
    first = store.create("first", run_id="a")
    second = store.create("second", run_id="b")
    listed = store.list()
    assert [record.run_id for record in listed] == [second.run_id, first.run_id]


def test_events_append_one_json_object_per_line(store: RunStore) -> None:
    store.create("q", run_id="r1")
    store.append_event("r1", {"type": "run_started", "seq": 1})
    store.append_event("r1", {"type": "assistant_delta", "seq": 2, "payload": {"text": "你好"}})
    lines = store.events_path("r1").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["payload"]["text"] == "你好"


def test_unknown_run_reads_as_none(store: RunStore) -> None:
    assert store.get("nope") is None
    assert store.list() == []


def test_terminal_status_helper_covers_all_ends(store: RunStore) -> None:
    store.create("q", run_id="r1")
    assert store.update("r1", status=CANCELLED).is_terminal is True
    assert store.update("r1", status=RUNNING).is_terminal is False


# -- exclusive run directory creation (GLINK-HARDEN-01) ------------------------


class _Uuid:
    """A stand-in for ``uuid.uuid4()`` whose ``.hex`` the test controls."""

    def __init__(self, value: str) -> None:
        self.hex = value


def test_a_generated_id_collision_is_regenerated_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pi_glink.store as store_module  # noqa: PLC0415

    store = RunStore(tmp_path / "runs")
    original = store.create("原有研究", run_id="dup")
    meta_before = (store.run_dir("dup") / "meta.json").read_bytes()

    ids = iter(["dup", "freshid00001"])
    monkeypatch.setattr(store_module.uuid, "uuid4", lambda: _Uuid(next(ids)))

    created = store.create("新研究")
    assert created.run_id == "freshid00001", "碰撞后必须重新生成 id"
    assert original.prompt == "原有研究"
    assert (store.run_dir("dup") / "meta.json").read_bytes() == meta_before, (
        "既有 run 的记录不得被覆盖"
    )
    assert store.get("dup").prompt == "原有研究"

    with pytest.raises(FileExistsError):
        store.create("覆盖尝试", run_id="dup")
    assert (store.run_dir("dup") / "meta.json").read_bytes() == meta_before


def test_generated_ids_are_exhausted_after_bounded_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pi_glink.store as store_module  # noqa: PLC0415

    store = RunStore(tmp_path / "runs")
    store.create("占用 id", run_id="collide00001")
    monkeypatch.setattr(store_module.uuid, "uuid4", lambda: _Uuid("collide00001"))
    with pytest.raises(RuntimeError, match="唯一"):
        store.create("无法分配 id")


def test_the_workspace_can_be_derived_from_the_generated_id(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    record = store.create("问题", workspace_root=tmp_path / "workspaces")
    assert record.workspace == str(tmp_path / "workspaces" / record.run_id)
    assert store.get(record.run_id) == record


def test_run_directories_are_created_private(tmp_path: Path) -> None:
    import stat  # noqa: PLC0415

    store = RunStore(tmp_path / "runs")
    record = store.create("问题")
    store.append_event(record.run_id, {"type": "run_started", "seq": 1})
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.run_dir(record.run_id).stat().st_mode) == 0o700
