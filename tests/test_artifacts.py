"""Regressions for the safe artifact boundary and the run report pipeline.

Audit reproduction: ``_collect_report`` accepted a ``report.md`` symlink that
pointed outside the workspace, read the target and (when it contained a
credential) rewrote it through the link. The HTTP report endpoint had the same
shape. These tests pin the replacement: only regular files inside the run's own
directories are read, reads and writes are bounded, writes never follow a link,
and a rejected artifact is visible as such instead of being served or hidden.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from pi_glink.artifacts import (
    ArtifactError,
    copy_regular_file,
    read_regular_file,
    replace_with_bytes,
    replace_with_text,
)
from pi_glink.config import build_config
from pi_glink.events import EventBus
from pi_glink.kernel.rpc import PiRpcClient
from pi_glink.kernel.runner import RunManager
from pi_glink.store import FINISHED, RunStore

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_PI = Path(__file__).resolve().parent / "fixtures" / "fake_pi.py"
SECRET = "AUDIT_DUMMY_SECRET_0123456789abcdef"


# -- the artifact reader ------------------------------------------------------


def test_reads_a_regular_file_and_rejects_links_and_special_files(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    regular = root / "report.md"
    regular.write_text("# ok\n", encoding="utf-8")
    assert read_regular_file(regular, root=root) == b"# ok\n"

    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    link = root / "linked.md"
    link.symlink_to(outside)
    with pytest.raises(ArtifactError, match="符号链接"):
        read_regular_file(link, root=root)

    directory = root / "dir"
    directory.mkdir()
    with pytest.raises(ArtifactError, match="非普通文件"):
        read_regular_file(directory, root=root)

    if hasattr(__import__("os"), "mkfifo"):
        fifo = root / "pipe"
        __import__("os").mkfifo(fifo)
        with pytest.raises(ArtifactError):
            read_regular_file(fifo, root=root)

    with pytest.raises(ArtifactError, match="允许范围"):
        read_regular_file(outside, root=root)
    assert outside.read_text(encoding="utf-8") == "secret\n"


def test_read_is_bounded_before_and_during_the_read(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    big = root / "big.md"
    big.write_bytes(b"x" * 5000)
    with pytest.raises(ArtifactError, match="上限"):
        read_regular_file(big, max_bytes=100, root=root)
    assert read_regular_file(big, max_bytes=5000, root=root) == b"x" * 5000


def test_replace_never_writes_through_a_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("original\n", encoding="utf-8")
    link = tmp_path / "report.md"
    link.symlink_to(outside)

    replace_with_text(link, "replaced\n")

    assert outside.read_text(encoding="utf-8") == "original\n", "the link target was rewritten"
    assert not link.is_symlink(), "the symlink itself must be replaced"
    assert link.read_text(encoding="utf-8") == "replaced\n"


def test_replace_breaks_a_hard_link(tmp_path: Path) -> None:
    original = tmp_path / "original.md"
    original.write_text("original\n", encoding="utf-8")
    alias = tmp_path / "alias.md"
    alias.hardlink_to(original)

    replace_with_bytes(alias, b"replaced\n")

    assert original.read_text(encoding="utf-8") == "original\n"
    assert alias.read_text(encoding="utf-8") == "replaced\n"


def test_copy_refuses_a_link_and_keeps_the_destination_host_owned(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(outside)
    destination = tmp_path / "host" / "copy.md"
    with pytest.raises(ArtifactError):
        copy_regular_file(link, destination, root=tmp_path)
    assert not destination.exists()

    regular = tmp_path / "regular.md"
    regular.write_text("data\n", encoding="utf-8")
    assert copy_regular_file(regular, destination, root=tmp_path) == 5
    assert destination.read_text(encoding="utf-8") == "data\n"


# -- the run pipeline ---------------------------------------------------------


def _config(tmp_path: Path, **overrides: str):
    values = {
        "OPENAI_API_KEY": SECRET,
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent.json"),
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


def _manager(config, tmp_path: Path, *, environment: dict[str, str] | None = None):
    def factory(argv, *, cwd=None, env=None, on_event=None):
        merged = dict(env or {})
        merged["FAKE_PI_WRITE_REPORT"] = "1"
        merged.update(environment or {})
        return PiRpcClient(
            [sys.executable, str(FAKE_PI), *argv[1:]],
            cwd=cwd,
            env=merged,
            on_event=on_event,
        )

    store = RunStore(config.data_root / "runs")
    return RunManager(config, EventBus(), store, client_factory=factory), store


def test_report_is_copied_to_a_host_owned_artifact(tmp_path: Path) -> None:
    config = _config(tmp_path)
    manager, store = _manager(config, tmp_path, environment={"FAKE_PI_ECHO_SECRET": "1"})
    record = manager.start("写一份含密钥的报告")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == FINISHED
    assert final.report_status == "ok"
    assert final.redacted is True
    artifact = Path(final.report_path or "")
    assert artifact.is_file()
    assert artifact.is_relative_to(store.run_dir(record.run_id))
    assert artifact.parent.name == "artifacts"
    text = artifact.read_text(encoding="utf-8")
    assert "fake pi" in text
    # The copy is redacted at collection time; the credential never reaches the
    # served artifact even though the kernel wrote it into the workspace file.
    assert SECRET not in text
    assert "[REDACTED]" in text


def test_symlinked_report_is_rejected_and_the_target_is_untouched(tmp_path: Path) -> None:
    """The audit's reproduction: a link must not become a read/write capability."""
    outside = tmp_path / "outside-report.md"
    outside.write_text(
        f"# 外部文件\n\nOPENAI_API_KEY={SECRET}\n", encoding="utf-8"
    )
    original = outside.read_text(encoding="utf-8")

    config = _config(tmp_path)
    manager, store = _manager(
        config, tmp_path, environment={"FAKE_PI_REPORT_SYMLINK": str(outside)}
    )
    record = manager.start("报告是符号链接")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == FINISHED
    assert final.report_path is None
    assert final.report_status == "rejected"
    assert final.stop_reason == "report_rejected"
    assert outside.read_text(encoding="utf-8") == original, (
        "the external target must keep its exact bytes"
    )
    events = [
        json.loads(line)
        for line in store.events_path(record.run_id).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    warnings = [
        event
        for event in events
        if event["type"] == "kernel_status" and event["payload"].get("message") == "report_rejected"
    ]
    assert warnings, "a rejected artifact must be visible in the run stream"
    assert events[-1]["type"] == "run_finished"
    assert events[-1]["payload"]["reportStatus"] == "rejected"


def test_oversized_report_is_rejected_not_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pi_glink.kernel.runner.REPORT_MAX_BYTES", 64)
    config = _config(tmp_path)
    manager, _store = _manager(config, tmp_path)
    record = manager.start("超大报告")
    final = manager.wait(record.run_id, timeout=30)
    assert final.status == FINISHED
    assert final.report_status == "rejected"
    assert final.report_path is None


def test_a_cancelled_run_keeps_the_work_it_already_produced(tmp_path: Path) -> None:
    """The existing convention: stop preserves artifacts; it must survive the new path."""
    config = _config(tmp_path)
    manager, store = _manager(config, tmp_path)
    record = manager.start("NOSETTLE 先写报告再取消")
    assert _wait_for(
        lambda: (Path(record.workspace) / "report.md").is_file(), timeout=20
    )
    assert manager.stop(record.run_id) is True
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == "cancelled"
    assert final.report_status == "ok"
    assert final.report_path is not None and Path(final.report_path).is_file()
    assert "fake pi" in Path(final.report_path).read_text(encoding="utf-8")


def _wait_for(predicate, *, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()
