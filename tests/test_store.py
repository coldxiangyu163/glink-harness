"""Regressions for the run store: metadata lifecycle and the event log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pi_glink.store import (
    RunSpec,
    CANCELLED,
    CHILD_READ_LIMIT,
    FINISHED,
    RUNNING,
    RunStore,
)


@pytest.fixture()
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "runs")


def test_create_writes_meta_and_returns_running_record(store: RunStore, tmp_path: Path) -> None:
    record = store.create("研究一个问题", run_id="abc123", workspace=tmp_path / "ws", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    assert record.status == RUNNING
    assert record.run_id == "abc123"
    assert record.event_count == 0
    assert (store.run_dir("abc123") / "meta.json").is_file()
    assert store.get("abc123") == record


def test_update_is_atomic_and_validates_fields(store: RunStore) -> None:
    store.create("q", run_id="r1", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    updated = store.update("r1", status=FINISHED, report_path="/tmp/report.md")
    assert updated.status == FINISHED
    assert updated.is_terminal is True
    assert store.get("r1").report_path == "/tmp/report.md"
    with pytest.raises(ValueError, match="unknown run fields"):
        store.update("r1", nonsense=1)
    with pytest.raises(KeyError):
        store.update("missing", status=FINISHED)


def test_list_is_newest_first(store: RunStore) -> None:
    first = store.create("first", run_id="a", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    second = store.create("second", run_id="b", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    listed = store.list()
    assert [record.run_id for record in listed] == [second.run_id, first.run_id]


def test_events_append_one_json_object_per_line(store: RunStore) -> None:
    store.create("q", run_id="r1", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    store.append_event("r1", {"type": "run_started", "seq": 1})
    store.append_event("r1", {"type": "assistant_delta", "seq": 2, "payload": {"text": "你好"}})
    lines = store.events_path("r1").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["payload"]["text"] == "你好"


def test_unknown_run_reads_as_none(store: RunStore) -> None:
    assert store.get("nope") is None
    assert store.list() == []


def test_terminal_status_helper_covers_all_ends(store: RunStore) -> None:
    store.create("q", run_id="r1", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
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
    original = store.create("原有研究", run_id="dup", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    meta_before = (store.run_dir("dup") / "meta.json").read_bytes()

    ids = iter(["dup", "freshid00001"])
    monkeypatch.setattr(store_module.uuid, "uuid4", lambda: _Uuid(next(ids)))

    created = store.create("新研究", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    assert created.run_id == "freshid00001", "碰撞后必须重新生成 id"
    assert original.prompt == "原有研究"
    assert (store.run_dir("dup") / "meta.json").read_bytes() == meta_before, (
        "既有 run 的记录不得被覆盖"
    )
    assert store.get("dup").prompt == "原有研究"

    with pytest.raises(FileExistsError):
        store.create("覆盖尝试", run_id="dup", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    assert (store.run_dir("dup") / "meta.json").read_bytes() == meta_before


def test_generated_ids_are_exhausted_after_bounded_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pi_glink.store as store_module  # noqa: PLC0415

    store = RunStore(tmp_path / "runs")
    store.create("占用 id", run_id="collide00001", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    monkeypatch.setattr(store_module.uuid, "uuid4", lambda: _Uuid("collide00001"))
    with pytest.raises(RuntimeError, match="唯一"):
        store.create("无法分配 id", spec=RunSpec(family="pi", pack="pi", executor="inproc"))


def test_the_workspace_can_be_derived_from_the_generated_id(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    record = store.create("问题", workspace_root=tmp_path / "workspaces", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    assert record.workspace == str(tmp_path / "workspaces" / record.run_id)
    assert store.get(record.run_id) == record


def test_run_directories_are_created_private(tmp_path: Path) -> None:
    import stat  # noqa: PLC0415

    store = RunStore(tmp_path / "runs")
    record = store.create("问题", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    store.append_event(record.run_id, {"type": "run_started", "seq": 1})
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.run_dir(record.run_id).stat().st_mode) == 0o700


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


# -- harness / executor / agent facts on the record -----------------------------


def test_create_records_the_run_spec_with_the_atomic_admission_write(
    store: RunStore,
) -> None:
    """RunSpec 与 run id 同一次准入写落盘，读回与 meta.json 原文一致。"""
    spec = RunSpec(
        family="acp", pack="dsh", executor="docker", blueprint="synthetic",
        credential_origin="tenant",
    )
    record = store.create("问题", run_id="facts1", spec=spec)
    assert record.spec == spec
    # agent 不进 create：它是 run 启动后才知道的事实（session_id 先例）。
    assert record.agent is None

    loaded = store.get("facts1")
    assert loaded is not None and loaded.spec == spec
    listed = {item.run_id: item for item in store.list(limit=None)}
    assert listed["facts1"].spec == spec

    meta = json.loads(
        (store.run_dir("facts1") / "meta.json").read_text(encoding="utf-8")
    )
    assert meta["spec"] == {
        "family": "acp",
        "pack": "dsh",
        "executor": "docker",
        "blueprint": "synthetic",
        "credential_origin": "tenant",
    }
    # 旧的平铺字段不再写出：RunSpec 是唯一来源。
    for legacy in ("harness", "harness_pack", "executor", "blueprint"):
        assert legacy not in meta


def test_meta_without_a_usable_run_spec_is_unreadable(
    store: RunStore, tmp_path: Path
) -> None:
    """缺 ``spec`` 或 ``spec`` 不完整的 meta.json 按不可读处理，不做迁移。

    RunSpec 之前写出的记录（平铺的 harness/harness_pack/executor/blueprint）
    同样不可读：不猜默认值，记录不出现在 get/list 中，并作为不可读目录计入
    全局持有，直到人工裁决（见 docs/adr/0001、docs/architecture.md §2）。相邻
    合法输入（含未知键的完整 meta）仍被已知键过滤正常读回。
    """
    base = {
        "prompt": "记录",
        "status": FINISHED,
        "created_at": 123.0,
        "workspace": str(tmp_path / "ws"),
        "session_id": "old-session",
        "unknown_future_key": {"anything": 1},
    }
    spec = {
        "family": "pi",
        "pack": "pi",
        "executor": "inproc",
        "blueprint": None,
        "credential_origin": "host",
    }
    legacy = {"harness": "pi", "harness_pack": "pi", "executor": "inproc", "blueprint": None}
    for run_id, meta in (
        ("complete1", {**base, "spec": spec}),
        ("nospec1", dict(base)),
        ("legacy1", {**base, **legacy}),
        ("nopack1", {**base, "spec": {k: v for k, v in spec.items() if k != "pack"}}),
        ("emptyexec1", {**base, "spec": {**spec, "executor": ""}}),
        ("noorigin1", {**base, "spec": {k: v for k, v in spec.items() if k != "credential_origin"}}),
        ("badblueprint1", {**base, "spec": {**spec, "blueprint": ""}}),
        ("notamap1", {**base, "spec": "pi/pi/inproc"}),
    ):
        directory = store.run_dir(run_id)
        directory.mkdir(parents=True, mode=0o700)
        (directory / "meta.json").write_text(
            json.dumps({"run_id": run_id, **meta}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    record = store.get("complete1")
    assert record is not None
    assert record.spec == RunSpec(family="pi", pack="pi", executor="inproc")
    assert record.session_id == "old-session"
    unreadable = [
        "badblueprint1", "emptyexec1", "legacy1", "noorigin1", "nopack1", "nospec1", "notamap1",
    ]
    for run_id in unreadable:
        assert store.get(run_id) is None
    assert [item.run_id for item in store.list(limit=None)] == ["complete1"]
    assert store.unreadable_holds() == unreadable
