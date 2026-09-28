"""Regressions for the run store: the SQLite run database and child documents.

Records and events live in ``runs.db`` (WAL); the terminal frame and the
terminal record are one transaction. These tests use temporary directories
only and drive the store through its public API; synthetic corruption goes
through raw SQL on a private connection.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from pi_glink.store import (
    CANCELLED,
    CHILD_READ_LIMIT,
    DATABASE_NAME,
    FAILED,
    FINISHED,
    RUNNING,
    SCHEMA_VERSION,
    RunAlreadyTerminal,
    RunSpec,
    RunStore,
    StoreError,
    StoreWriteError,
)

from store_helpers import finish, stored_record

SPEC = RunSpec(family="pi", pack="pi", executor="inproc")


@pytest.fixture()
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "runs")


def terminal(run_id: str, seq: int, status: str = FINISHED, **payload: Any) -> dict[str, Any]:
    kind = {FINISHED: "run_finished", FAILED: "run_failed", CANCELLED: "run_cancelled"}[status]
    return {
        "type": kind,
        "runId": run_id,
        "seq": seq,
        "ts": time.time(),
        "payload": {"status": status, "cleanup": {"released": True, "failures": [], "external": {}}, **payload},
    }


def raw(store: RunStore) -> sqlite3.Connection:
    """A second, independent connection to the same database file."""
    return sqlite3.connect(store.database_path, isolation_level=None)


# -- records ----------------------------------------------------------------------


def test_create_inserts_a_running_row_and_a_private_run_directory(store: RunStore, tmp_path: Path) -> None:
    record = store.create("研究一个问题", run_id="abc123", workspace=tmp_path / "ws", spec=SPEC)
    assert record.status == RUNNING
    assert record.run_id == "abc123"
    assert record.event_count == 0
    assert store.get("abc123") == record
    assert store.database_path == store.root / DATABASE_NAME
    assert stat.S_IMODE(store.database_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.run_dir("abc123").stat().st_mode) == 0o700
    assert not (store.run_dir("abc123") / "meta.json").exists(), "记录只在数据库里"
    assert store.session_dir("abc123") == store.run_dir("abc123") / "sessions"


def test_update_validates_fields_and_refuses_terminal_fields(store: RunStore) -> None:
    store.create("q", run_id="r1", spec=SPEC)
    updated = store.update("r1", session_id="s-1", container_name="c-1")
    assert updated.session_id == "s-1" and store.get("r1").container_name == "c-1"
    with pytest.raises(ValueError, match="unknown run fields"):
        store.update("r1", nonsense=1)
    for field, value in (
        ("status", FINISHED), ("finished_at", 1.0), ("event_count", 3),
        ("cleanup", {"released": True}),
    ):
        with pytest.raises(ValueError, match="终态字段"):
            store.update("r1", **{field: value})
    assert store.get("r1").status == RUNNING, "update 不能把 run 写成终态"
    with pytest.raises(KeyError):
        store.update("missing", session_id="x")


def test_list_is_newest_first_and_tenant_scoped(store: RunStore) -> None:
    first = store.create("first", run_id="a", tenant_id="t1", spec=SPEC)
    second = store.create("second", run_id="b", tenant_id="t2", spec=SPEC)
    assert [record.run_id for record in store.list()] == [second.run_id, first.run_id]
    assert [record.run_id for record in store.list_for("t1")] == ["a"]
    assert store.get_for("a", "t2") is None and store.get_for("a", "t1") == first
    assert [record.run_id for record in store.list(limit=1)] == ["b"]


def test_unknown_run_reads_as_none(store: RunStore) -> None:
    assert store.get("nope") is None
    assert store.list() == []
    assert store.last_seq("nope") == 0
    assert list(store.iter_events("nope")) == []


# -- events -------------------------------------------------------------------------


def test_events_are_keyed_by_run_and_seq(store: RunStore) -> None:
    store.create("q", run_id="r1", spec=SPEC)
    store.append_event("r1", {"type": "run_started", "seq": 1})
    store.append_event("r1", {"type": "assistant_delta", "seq": 2, "payload": {"text": "你好"}})
    events = list(store.iter_events("r1"))
    assert [event["seq"] for event in events] == [1, 2]
    assert events[1]["payload"]["text"] == "你好"
    assert [event["seq"] for event in store.iter_events("r1", after=1)] == [2]
    assert store.last_seq("r1") == 2
    with pytest.raises(StoreWriteError):
        store.append_event("r1", {"type": "assistant_delta", "seq": 2})
    with pytest.raises(ValueError):
        store.append_event("r1", {"type": "assistant_delta", "seq": 0})
    with pytest.raises(ValueError, match="终帧"):
        store.append_event("r1", terminal("r1", 3))
    with pytest.raises(KeyError):
        store.append_event("missing", {"type": "run_started", "seq": 1})


def test_replay_pages_past_the_page_size(store: RunStore) -> None:
    store.create("q", run_id="r1", spec=SPEC)
    for seq in range(1, 1206):
        store.append_event("r1", {"type": "assistant_delta", "seq": seq})
    assert [event["seq"] for event in store.iter_events("r1")] == list(range(1, 1206))
    assert [event["seq"] for event in store.iter_events("r1", after=1200)] == [1201, 1202, 1203, 1204, 1205]


def test_committed_rows_survive_a_restart(tmp_path: Path) -> None:
    first = RunStore(tmp_path / "runs")
    first.create("q", run_id="r1", spec=SPEC)
    first.append_event("r1", {"type": "run_started", "seq": 1})
    first.commit_terminal("r1", terminal("r1", 2))
    first.close()

    reopened = RunStore(tmp_path / "runs")
    assert [event["type"] for event in reopened.iter_events("r1")] == ["run_started", "run_finished"]
    record = reopened.get("r1")
    assert record.status == FINISHED and record.event_count == 2
    assert reopened.committed_terminal("r1")["payload"]["status"] == FINISHED


# -- the terminal transaction ----------------------------------------------------------


def test_commit_terminal_writes_the_frame_and_the_state_together(store: RunStore) -> None:
    store.create("q", run_id="r1", spec=SPEC)
    store.append_event("r1", {"type": "run_started", "seq": 1})
    record = store.commit_terminal("r1", terminal("r1", 2, reportPath="/tmp/report.md"))
    assert record.status == FINISHED and record.is_terminal
    assert record.report_path == "/tmp/report.md"
    assert record.finished_at is not None and record.event_count == 2
    assert store.get("r1") == record
    with pytest.raises(ValueError, match="终帧"):
        store.commit_terminal("r1", {"type": "assistant_delta", "seq": 3})


def test_a_failed_terminal_status_write_leaves_neither_the_frame_nor_the_state(
    store: RunStore,
) -> None:
    """Terminal frame means terminal state: a row update that fails rolls the frame back.

    Red→green mechanism: writing the frame and the record in two transactions
    would leave a committed ``run_finished`` row next to a ``running`` record.
    """
    store.create("q", run_id="r1", spec=SPEC)
    store.append_event("r1", {"type": "run_started", "seq": 1})
    with raw(store) as connection:
        connection.execute(
            "CREATE TRIGGER refuse_status BEFORE UPDATE ON runs "
            "BEGIN SELECT RAISE(ABORT, 'synthetic status write failure'); END"
        )
    with pytest.raises(StoreWriteError):
        store.commit_terminal("r1", terminal("r1", 2))
    assert [event["seq"] for event in store.iter_events("r1")] == [1], "终帧不得脱离终态落库"
    assert store.get("r1").status == RUNNING
    assert store.committed_terminal("r1") is None
    assert store.running_ids() == ["r1"]


def test_a_terminal_run_accepts_no_further_frames(store: RunStore) -> None:
    store.create("q", run_id="r1", spec=SPEC)
    store.commit_terminal("r1", terminal("r1", 1, status=CANCELLED))
    with pytest.raises(RunAlreadyTerminal):
        store.append_event("r1", {"type": "assistant_delta", "seq": 2})
    with pytest.raises(RunAlreadyTerminal):
        store.commit_terminal("r1", terminal("r1", 2, status=FAILED))
    assert store.get("r1").status == CANCELLED
    assert [event["seq"] for event in store.iter_events("r1")] == [1]


def test_concurrent_create_and_commit_lose_no_rows(store: RunStore) -> None:
    writers = 8
    barrier = threading.Barrier(writers)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(10)
            run_id = f"c{index:02d}"
            store.create(f"并发 {index}", run_id=run_id, tenant_id="tenant-a", spec=SPEC)
            store.append_event(run_id, {"type": "run_started", "seq": 1})
            store.commit_terminal(run_id, terminal(run_id, 2))
        except BaseException as exc:  # noqa: BLE001 - reported through the assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    records = store.list(limit=None)
    assert len(records) == writers
    assert all(record.status == FINISHED and record.event_count == 2 for record in records)
    assert store.holding_records() == [] and store.running_ids() == []


# -- admission inside the insert transaction -------------------------------------------


def test_the_admission_check_sees_holds_and_a_refusal_leaves_nothing(store: RunStore) -> None:
    store.create("泄漏", run_id="leak", tenant_id="t1", spec=SPEC)
    finish(store, "leak", FINISHED, cleanup={"released": False, "failures": ["synthetic"], "external": {}})
    seen: list[list[str]] = []

    def refuse(holding) -> None:
        seen.append([record.run_id for record in holding])
        raise RuntimeError("quota")

    with pytest.raises(RuntimeError, match="quota"):
        store.create("被拒", run_id="refused", spec=SPEC, admission_check=refuse)
    assert seen == [["leak"]]
    assert store.get("refused") is None
    assert not store.run_dir("refused").exists(), "拒绝时不得留下 run 目录"

    admitted = store.create("放行", run_id="ok", spec=SPEC, admission_check=lambda holding: None)
    assert store.get("ok") == admitted


def test_holding_records_follow_cleanup_and_adjudication(store: RunStore) -> None:
    store.create("a", run_id="held", tenant_id="t1", spec=SPEC)
    store.create("b", run_id="released", tenant_id="t2", spec=SPEC)
    finish(store, "held", FINISHED, cleanup={"released": False, "failures": ["x"], "external": {}})
    finish(store, "released", FINISHED, cleanup={"released": True, "failures": [], "external": {}})
    assert [record.run_id for record in store.holding_records()] == ["held"]
    assert store.holding_records("t2") == []
    store.update("held", resource_adjudication={"decision": "released", "note": "已核对", "at": time.time()})
    assert store.holding_records() == []


# -- exclusive ids -------------------------------------------------------------------------


class _Uuid:
    """A stand-in for ``uuid.uuid4()`` whose ``.hex`` the test controls."""

    def __init__(self, value: str) -> None:
        self.hex = value


def test_a_generated_id_collision_is_regenerated_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pi_glink.store as store_module  # noqa: PLC0415

    store = RunStore(tmp_path / "runs")
    store.create("原有研究", run_id="dup", spec=SPEC)
    before = stored_record(store, "dup")

    ids = iter(["dup", "freshid00001"])
    monkeypatch.setattr(store_module.uuid, "uuid4", lambda: _Uuid(next(ids)))

    created = store.create("新研究", spec=SPEC)
    assert created.run_id == "freshid00001", "碰撞后必须重新生成 id"
    assert stored_record(store, "dup") == before, "既有 run 的记录不得被覆盖"

    with pytest.raises(FileExistsError):
        store.create("覆盖尝试", run_id="dup", spec=SPEC)
    assert stored_record(store, "dup") == before


def test_an_existing_directory_is_never_adopted(store: RunStore) -> None:
    store.run_dir("stray").mkdir(parents=True)
    with pytest.raises(FileExistsError):
        store.create("q", run_id="stray", spec=SPEC)
    assert store.get("stray") is None


def test_generated_ids_are_exhausted_after_bounded_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pi_glink.store as store_module  # noqa: PLC0415

    store = RunStore(tmp_path / "runs")
    store.create("占用 id", run_id="collide00001", spec=SPEC)
    monkeypatch.setattr(store_module.uuid, "uuid4", lambda: _Uuid("collide00001"))
    with pytest.raises(RuntimeError, match="唯一"):
        store.create("无法分配 id", spec=SPEC)


def test_the_workspace_can_be_derived_from_the_generated_id(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    record = store.create("问题", workspace_root=tmp_path / "workspaces", spec=SPEC)
    assert record.workspace == str(tmp_path / "workspaces" / record.run_id)
    assert store.get(record.run_id) == record


# -- the database file -------------------------------------------------------------------


def test_a_database_of_another_schema_version_is_refused(tmp_path: Path) -> None:
    """No migration: a database written by another schema version is not opened."""
    root = tmp_path / "runs"
    RunStore(root).close()
    with sqlite3.connect(root / DATABASE_NAME) as connection:
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    with pytest.raises(StoreError):
        RunStore(root)
    # The adjacent legal input, the current version, still opens.
    with sqlite3.connect(root / DATABASE_NAME) as connection:
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    RunStore(root).close()


def test_a_symlinked_database_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir(mode=0o700)
    target = tmp_path / "elsewhere.db"
    target.write_bytes(b"")
    os.symlink(target, root / DATABASE_NAME)
    with pytest.raises(StoreError):
        RunStore(root)
    assert target.read_bytes() == b"", "不得跟随符号链接写入"


def test_legacy_run_directories_are_not_read(tmp_path: Path) -> None:
    """Old ``meta.json`` / ``events.jsonl`` directories are not migrated or counted."""
    root = tmp_path / "runs"
    legacy = root / "legacy1"
    legacy.mkdir(parents=True)
    (legacy / "meta.json").write_text(
        json.dumps({"run_id": "legacy1", "prompt": "旧", "status": RUNNING, "created_at": 1.0}),
        encoding="utf-8",
    )
    (legacy / "events.jsonl").write_text('{"type": "run_started", "seq": 1}\n', encoding="utf-8")
    store = RunStore(root)
    assert store.get("legacy1") is None
    assert store.list(limit=None) == [] and store.holding_records() == []
    assert store.running_ids() == []


def test_an_unparseable_row_fails_closed(store: RunStore) -> None:
    store.create("q", run_id="r1", spec=SPEC)
    with raw(store) as connection:
        connection.execute("UPDATE runs SET record = '{ nope' WHERE run_id = 'r1'")
    with pytest.raises(StoreError):
        store.get("r1")
    with pytest.raises(StoreError):
        store.list(limit=None)


def test_a_row_without_a_usable_run_spec_fails_closed(store: RunStore) -> None:
    store.create("q", run_id="r1", spec=SPEC)
    with raw(store) as connection:
        connection.execute("UPDATE runs SET record = json_remove(record, '$.spec.pack') WHERE run_id = 'r1'")
    with pytest.raises(StoreError):
        store.get("r1")


def test_create_records_the_run_spec_in_the_admission_insert(store: RunStore) -> None:
    spec = RunSpec(
        family="acp", pack="dsh", executor="docker", blueprint="synthetic",
        credential_origin="tenant",
    )
    record = store.create("问题", run_id="facts1", spec=spec)
    assert record.spec == spec
    # agent 不进 create：它是 run 启动后才知道的事实（session_id 先例）。
    assert record.agent is None
    assert store.get("facts1").spec == spec
    stored = stored_record(store, "facts1")
    assert stored["spec"] == {
        "family": "acp",
        "pack": "dsh",
        "executor": "docker",
        "blueprint": "synthetic",
        "credential_origin": "tenant",
    }
    for legacy in ("harness", "harness_pack", "executor", "blueprint"):
        assert legacy not in stored


# -- child-run documents (§3.5) -------------------------------------------------


def _child(n: int, **overrides: object) -> dict:
    document: dict[str, object] = {
        "n": n,
        "toolCallId": "call_1",
        "agent": "researcher",
        "task": f"调研任务 {n}",
        "status": "running",
        "resultPreview": None,
        "enrichment": "none",
    }
    document.update(overrides)
    return document  # type: ignore[return-value]


def test_child_document_roundtrip_through_the_directory_handle(store: RunStore) -> None:
    store.create("q", run_id="r1", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    store.write_child("r1", _child(1))
    store.write_child("r1", _child(0))

    path = store.child_doc_path("r1", 0)
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["task"] == "调研任务 0"
    assert store.read_child("r1", 1)["status"] == "running"
    page = store.read_children("r1")
    assert [document["n"] for document in page.documents] == [0, 1]
    assert page.skipped == 0

    # A state migration republishes the whole document atomically.
    store.write_child("r1", _child(0, status="finished", resultPreview="结论"))
    assert store.read_child("r1", 0)["resultPreview"] == "结论"


def test_read_children_skips_and_counts_unusable_documents(store: RunStore, tmp_path: Path) -> None:
    store.create("q", run_id="r1", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    store.write_child("r1", _child(0))
    corrupt = store.run_dir("r1") / "children" / "1"
    corrupt.mkdir(parents=True)
    (corrupt / "child.json").write_text("not json", encoding="utf-8")
    mismatched = store.run_dir("r1") / "children" / "2"
    mismatched.mkdir(parents=True)
    (mismatched / "child.json").write_text(json.dumps(_child(7)), encoding="utf-8")

    page = store.read_children("r1")
    assert [document["n"] for document in page.documents] == [0]
    assert page.skipped == 2, "不可读/不可解析/序号不符的文档必须如实计数"
    assert store.read_child("r1", 1) is None
    assert store.read_child("r1", 2) is None

    # A run without a children tree is not corruption.
    assert store.read_children("r2").documents == ()
    assert store.read_children("r2").skipped == 0


def test_read_children_is_bounded(store: RunStore) -> None:
    store.create("q", run_id="r1", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    for n in range(CHILD_READ_LIMIT + 6):
        store.write_child("r1", _child(n))
    page = store.read_children("r1")
    assert len(page.documents) == CHILD_READ_LIMIT
    assert [document["n"] for document in page.documents] == list(range(CHILD_READ_LIMIT))
    assert page.skipped == 6, "超出上限的文档计入 skipped，不静默缺席"


def test_read_children_reports_an_unopenable_tree(store: RunStore) -> None:
    store.create("q", run_id="r1", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    # The children root exists but is not a directory: one unreadable unit,
    # reported instead of silently equated with absence.
    (store.run_dir("r1") / "children").write_text("not a directory", encoding="utf-8")
    page = store.read_children("r1")
    assert page.documents == ()
    assert page.skipped == 1


def test_write_child_validates_the_sequence_number(store: RunStore) -> None:
    store.create("q", run_id="r1", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    for bad in (-1, True, "0", None, 1.5):
        with pytest.raises(ValueError, match="n"):
            store.write_child("r1", {"n": bad})
    assert store.read_children("r1").documents == ()
