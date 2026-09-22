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
