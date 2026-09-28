"""Regressions for the in-process run index (GLINK-STORE-IDX).

The index accelerates run listing, tenant listing, the admission resource
ledger and the operator's ``--unreleased`` view. It is an accelerator, not a
second fact source: every scenario below compares the indexed reads against
``RunStore.scan_all()`` — the uncached walk over ``runs/*/meta.json`` that
builds the index in the first place — and against a direct, independent read of
the metadata files. The counting regressions pin the performance claim: once
the index is warm, listing and admission read no ``meta.json`` at all.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from pi_glink import store as store_module
from pi_glink.config import HostConfig, build_config
from pi_glink.events import EventBus
from pi_glink.kernel.rpc import PiRpcClient
from pi_glink.runs.runner import ResourcesUnreleased, RunManager
from pi_glink.store import FINISHED, RUNNING, RunStore, RunSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_PI = Path(__file__).resolve().parent / "fixtures" / "fake_pi.py"


def make_config(tmp_path: Path, **overrides: str) -> HostConfig:
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


def fake_factory(*, write_report: bool = False):
    def factory(argv, *, cwd=None, env=None, on_event=None):
        merged = dict(env or {})
        merged["FAKE_PI_WRITE_REPORT"] = "1" if write_report else "0"
        return PiRpcClient(
            [sys.executable, str(FAKE_PI), *argv[1:]],
            cwd=cwd,
            env=merged,
            on_event=on_event,
        )

    return factory


def terminal_frame(
    run_id: str,
    *,
    released: bool = True,
    seq: int = 1,
    status: str = FINISHED,
) -> dict[str, Any]:
    return {
        "type": "run_finished" if status == FINISHED else "run_failed",
        "runId": run_id,
        "seq": seq,
        "ts": time.time(),
        "payload": {
            "status": status,
            "stopReason": None if released else "kernel_exit",
            "reportPath": None,
            "reportStatus": "none",
            "cleanup": {"released": released, "failures": [], "external": {}},
        },
    }


def reference_unsettled(store: RunStore) -> list[str]:
    """Independent reference: ``meta.json`` still says ``running``."""
    ids: list[str] = []
    for entry in sorted(store.root.iterdir(), key=lambda item: item.name):
        if not entry.is_dir():
            continue
        data = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
        if data.get("status") == RUNNING:
            ids.append(entry.name)
    return ids


def assert_index_matches_disk(store: RunStore) -> None:
    scan = store.scan_all()
    assert store.list(limit=None) == scan
    for tenant in ("tenant-a", "tenant-b"):
        assert store.list_for(tenant, limit=None) == [
            record for record in scan if record.tenant_id == tenant
        ]
    assert store.holding_records() == [record for record in scan if record.holds_resources]
    for tenant in ("tenant-a", "tenant-b"):
        assert store.holding_records(tenant) == [
            record
            for record in scan
            if record.holds_resources and record.tenant_id == tenant
        ]
    assert store.unsettled() == reference_unsettled(store)


def counting_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every ``_load_meta`` call (the per-run ``meta.json`` read)."""
    reads: list[str] = []
    original = RunStore._load_meta

    def counting(self: RunStore, run_id: str):
        reads.append(run_id)
        return original(self, run_id)

    monkeypatch.setattr(RunStore, "_load_meta", counting)
    return reads


def _fd_path(fd: int) -> str:
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0].decode()


# -- index == full scan -------------------------------------------------------


def test_the_index_agrees_with_a_full_scan_across_every_write_path(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.create(
        "甲的问题", run_id="alpha1", tenant_id="tenant-a", creator_user_id="u1",
        spec=RunSpec(family="pi", pack="pi", executor="inproc"),
    )
    store.create(
        "乙的问题", run_id="beta1", tenant_id="tenant-b", creator_user_id="u2",
        spec=RunSpec(family="pi", pack="pi", executor="inproc"),
    )
    store.create("无 owner 的记录", run_id="ownerless1", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    store.append_event("alpha1", {"type": "run_started", "runId": "alpha1", "seq": 1})
    assert_index_matches_disk(store)

    # Terminal frame committed but the settled copy not written yet: the
    # projection (not the raw meta) is what every reader must see.
    store.commit_terminal("alpha1", terminal_frame("alpha1", released=False, seq=2))
    assert store.get("alpha1").status == FINISHED
    assert store.get("alpha1").holds_resources is True
    assert_index_matches_disk(store)

    # Settled copy written, cleanup still unconfirmed: the hold survives.
    settled = store.settle("alpha1")
    assert settled is not None and settled.holds_resources is True
    assert_index_matches_disk(store)

    # A released cleanup and an operator adjudication each return capacity.
    store.update(
        "beta1",
        status=FINISHED,
        finished_at=time.time(),
        cleanup={"released": True, "failures": [], "external": {}},
    )
    store.update(
        "ownerless1",
        status=FINISHED,
        finished_at=time.time(),
        cleanup={"released": False, "failures": ["synthetic"], "external": {}},
    )
    assert {record.run_id for record in store.holding_records()} == {"alpha1", "ownerless1"}
    assert_index_matches_disk(store)

    store.update(
        "alpha1",
        resource_adjudication={"decision": "released", "note": "已核对", "at": time.time()},
    )
    assert {record.run_id for record in store.holding_records()} == {"ownerless1"}
    assert_index_matches_disk(store)


def test_the_index_survives_a_restart_rebuild(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A new process starts with an empty registry and rebuilds from disk."""
    store = RunStore(tmp_path / "runs")
    store.create("终帧未落定", run_id="r1", tenant_id="tenant-a", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    store.commit_terminal("r1", terminal_frame("r1", released=False, seq=1))
    store.create("进行中", run_id="r2", tenant_id="tenant-a", spec=RunSpec(family="pi", pack="pi", executor="inproc"))

    monkeypatch.setattr(store_module, "_INDEXES", {})
    restarted = RunStore(tmp_path / "runs")
    assert restarted.get("r1").status == FINISHED
    assert restarted.get("r1").holds_resources is True
    assert restarted.get("r1").event_count == 1
    assert restarted.get("r2").status == RUNNING
    assert restarted.unsettled() == ["r1", "r2"], "meta.json still says running for both"
    assert_index_matches_disk(restarted)


def test_a_reconciled_interruption_reaches_the_index(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.create("上一进程遗留", run_id="orphan", tenant_id="tenant-a", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    manager = RunManager(make_config(tmp_path), EventBus(), store)

    manager._commit_interrupted("orphan")  # noqa: SLF001 - the restart path
    assert store.get("orphan").status == "failed"
    assert store.get("orphan").stop_reason == "interrupted"
    assert store.get("orphan").holds_resources is True, "unknown release still holds"
    store.settle("orphan")
    assert_index_matches_disk(store)


# -- the index is actually used -----------------------------------------------


def test_listing_and_holding_queries_read_no_meta_after_warmup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RunStore(tmp_path / "runs")
    for index in range(6):
        store.create(
            f"问题 {index}",
            run_id=f"r{index:02d}",
            tenant_id="tenant-a" if index % 2 else "tenant-b",
            spec=RunSpec(family="pi", pack="pi", executor="inproc"),
        )
    store.list(limit=None)  # warm the index

    reads = counting_reads(monkeypatch)
    assert len(store.list(limit=None)) == 6
    assert len(store.list_for("tenant-a", limit=None)) == 3
    assert len(store.holding_records()) == 6, "every running run holds its resources"
    assert len(store.holding_records("tenant-a")) == 3
    assert store.get("r00") is not None
    assert store.unsettled() == [f"r{index:02d}" for index in range(6)]
    assert reads == [], "列表、租户过滤与资源查询必须走索引，不得逐目录读 meta.json"


def test_a_committed_terminal_reaches_the_index_without_rereading_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RunStore(tmp_path / "runs")
    store.create("终帧", run_id="r1", tenant_id="tenant-a", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    store.list(limit=None)  # warm the index

    reads = counting_reads(monkeypatch)
    store.commit_terminal("r1", terminal_frame("r1", released=False, seq=1))
    record = store.get("r1")
    assert record.status == FINISHED and record.holds_resources is True
    assert [item.run_id for item in store.holding_records()] == ["r1"]
    assert reads == [], "终帧提交后索引必须自更新，不得回读目录"
    assert store.list(limit=None) == store.scan_all()


def test_a_terminal_commit_whose_fsync_fails_never_reaches_the_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RunStore(tmp_path / "runs")
    store.create("终帧落盘失败", run_id="r1", tenant_id="tenant-a", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    store.list(limit=None)  # warm the index
    original_fsync = os.fsync

    def failing_fsync(fd: int) -> None:
        if _fd_path(fd).endswith("events.jsonl"):
            raise OSError(errno.EIO, "synthetic fsync failure")
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", failing_fsync)
    with pytest.raises(OSError):
        store.commit_terminal("r1", terminal_frame("r1", released=False, seq=1))

    assert store.get("r1").status == RUNNING
    assert store.committed_terminal("r1") is None
    assert [item.run_id for item in store.holding_records()] == ["r1"]
    assert store.get("r1").event_count == 0, "a frame that was never durable is not projected"
    assert store.unsettled() == ["r1"]
    assert_index_matches_disk(store)


def test_concurrent_create_and_commit_lose_no_entries(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.list(limit=None)  # build before the writers start
    writers = 8
    barrier = threading.Barrier(writers)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(10)
            run_id = f"c{index:02d}"
            store.create(f"并发 {index}", run_id=run_id, tenant_id="tenant-a", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
            store.commit_terminal(run_id, terminal_frame(run_id, released=True, seq=1))
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
    assert all(record.status == FINISHED for record in records)
    assert store.holding_records() == []
    assert store.list(limit=None) == store.scan_all()


# -- the admission path -------------------------------------------------------


def test_admission_reads_no_run_directory_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    for index in range(4):
        store.create(f"旧 run {index}", run_id=f"old{index}", tenant_id="tenant-a", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    for index in (1, 2, 3):
        store.update(
            f"old{index}",
            status=FINISHED,
            finished_at=time.time(),
            cleanup={"released": True, "failures": [], "external": {}},
        )
    store.update(
        "old0",
        status=FINISHED,
        finished_at=time.time(),
        cleanup={"released": False, "failures": ["synthetic"], "external": {}},
    )
    store.list(limit=None)  # warm the index
    manager = RunManager(config, EventBus(), store, client_factory=fake_factory())

    reads = counting_reads(monkeypatch)
    with pytest.raises(ResourcesUnreleased) as refusal:
        manager._check_admission(None)  # noqa: SLF001 - the admission routine itself
    assert refusal.value.run_ids == ["old0"]
    assert reads == [], "准入必须在锁内读内存索引，不得遍历 run 目录"

    store.update(
        "old0",
        resource_adjudication={"decision": "released", "note": "已核对", "at": time.time()},
    )
    reads.clear()
    record = manager.start("准入后的新 run")
    admission_reads = list(reads)
    assert manager.wait(record.run_id, timeout=30).status == FINISHED
    assert_index_matches_disk(store)
    assert [item for item in admission_reads if item.startswith("old")] == [], (
        "准入不得回读旧 run 的 meta.json: " + repr(admission_reads)
    )
