"""Regressions for the safe artifact boundary and the run report pipeline.

Audit reproductions:

* ``_collect_report`` accepted a ``report.md`` symlink that pointed outside the
  workspace, read the target and rewrote it through the link;
* (R1 review, R2) the replacement still checked containment with ``realpath``
  and then opened the file again by name, so an ancestor directory swapped for
  a link between the check and the open made the host read an external file.

These tests pin the directory-anchored replacement: every component below a
run root is opened without following links, identities are re-verified after
the open, writes are published inside the verified directory, and a refused
artifact is visible as such instead of being served or hidden. The race tests
inject the swap at the exact step between walking and opening.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from glink_harness import artifacts as artifacts_module
from glink_harness.artifacts import ArtifactError, ArtifactMissing, ArtifactTree
from glink_harness.config import build_config
from glink_harness.events import EventBus
from glink_harness.kernel.rpc import PiRpcClient
from glink_harness.runs.runner import RunManager
from glink_harness.store import FINISHED, RunStore
from store_helpers import events_text

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_PI = Path(__file__).resolve().parent / "fixtures" / "fake_pi.py"
SECRET = "AUDIT_DUMMY_SECRET_0123456789abcdef"
INSIDE = b"INSIDE-BYTES"
OUTSIDE = b"OUTSIDE_SYNTHETIC"


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    """``root/sub/report.md`` (inside) and ``outside/report.md`` (external)."""
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "report.md").write_bytes(INSIDE)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "report.md").write_bytes(OUTSIDE)
    return root, outside


def _swap_to_link(root: Path, outside: Path) -> None:
    (root / "sub").rename(root / "moved-sub")
    (root / "sub").symlink_to(outside, target_is_directory=True)


# -- the directory-anchored reader --------------------------------------------


def test_reads_a_regular_file_and_rejects_links_and_special_files(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "report.md").write_text("# ok\n", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n", encoding="utf-8")
    (root / "linked.md").symlink_to(outside)
    (root / "dir").mkdir()
    os.mkfifo(root / "pipe")

    with ArtifactTree(root) as tree:
        assert tree.read("report.md") == b"# ok\n"
        assert tree.read(root / "report.md") == b"# ok\n", "absolute paths inside the root work"
        with pytest.raises(ArtifactError, match="符号链接"):
            tree.read("linked.md")
        with pytest.raises(ArtifactError, match="非普通文件"):
            tree.read("dir")
        started = time.monotonic()
        with pytest.raises(ArtifactError, match="非普通文件"):
            tree.read("pipe")
        assert time.monotonic() - started < 5, "a FIFO must be refused, never waited on"
        with pytest.raises(ArtifactMissing):
            tree.read("absent.md")
        for escape in (outside, "../outside.md", "sub/../../outside.md", root / ".." / "outside.md"):
            with pytest.raises(ArtifactError, match="允许范围"):
                tree.read(escape)
    assert outside.read_text(encoding="utf-8") == "secret\n"


def test_a_root_that_is_itself_a_link_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "report.md").write_bytes(OUTSIDE)
    (tmp_path / "root").symlink_to(real, target_is_directory=True)
    with pytest.raises(ArtifactError):
        ArtifactTree(tmp_path / "root")


def test_read_is_bounded_before_and_during_the_read(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "big.md").write_bytes(b"x" * 5000)
    with ArtifactTree(root) as tree:
        with pytest.raises(ArtifactError, match="上限"):
            tree.read("big.md", max_bytes=100)
        with pytest.raises(ArtifactError, match="上限"):
            tree.size("big.md", max_bytes=100)
        assert tree.read("big.md", max_bytes=5000) == b"x" * 5000
        assert tree.size("big.md", max_bytes=5000) == 5000


def test_an_ancestor_directory_link_is_refused_for_reads_and_writes(tmp_path: Path) -> None:
    root, outside = _layout(tmp_path)
    _swap_to_link(root, outside)
    with ArtifactTree(root) as tree:
        with pytest.raises(ArtifactError, match="链接"):
            tree.read("sub/report.md")
        with pytest.raises(ArtifactError, match="链接"):
            tree.size("sub/report.md")
        with pytest.raises(ArtifactError, match="链接"):
            tree.replace("sub/report.md", b"REWRITTEN")
        with pytest.raises(ArtifactError, match="链接"):
            tree.replace("sub/new.md", b"CREATED", create_parents=True)
        assert tree.files(max_files=10) == ["moved-sub/report.md"], "links are never walked"
    assert (outside / "report.md").read_bytes() == OUTSIDE
    assert sorted(path.name for path in outside.iterdir()) == ["report.md"]


def test_ancestor_swap_between_walk_and_open_never_reads_the_external_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2 reproduction on the new reader: the swap lands after the walk."""
    root, outside = _layout(tmp_path)
    original_walk = ArtifactTree._walk

    def walk_then_swap(self, directories, *, create):
        chain = original_walk(self, directories, create=create)
        _swap_to_link(root, outside)
        return chain

    monkeypatch.setattr(ArtifactTree, "_walk", walk_then_swap)
    with ArtifactTree(root) as tree:
        with pytest.raises(ArtifactError, match="替换"):
            tree.read("sub/report.md")
    assert (outside / "report.md").read_bytes() == OUTSIDE


def test_ancestor_swap_during_the_walk_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The swap lands just before the ``sub`` component is opened."""
    root, outside = _layout(tmp_path)
    original_open = os.open
    swapped: list[bool] = []

    def swapping_open(path, flags, *args, **kwargs):
        if path == "sub" and not swapped and kwargs.get("dir_fd") is not None:
            swapped.append(True)
            _swap_to_link(root, outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(artifacts_module.os, "open", swapping_open)
    with ArtifactTree(root) as tree:
        with pytest.raises(ArtifactError, match="链接"):
            tree.read("sub/report.md")
    assert swapped == [True]
    assert (outside / "report.md").read_bytes() == OUTSIDE


def test_ancestor_swap_before_publishing_a_write_never_touches_the_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, outside = _layout(tmp_path)
    original_verify = ArtifactTree._verify

    def swap_then_verify(chain, parts):
        _swap_to_link(root, outside)
        return original_verify(chain, parts)

    monkeypatch.setattr(ArtifactTree, "_verify", staticmethod(swap_then_verify))
    with ArtifactTree(root) as tree:
        with pytest.raises(ArtifactError, match="替换"):
            tree.replace("sub/report.md", b"REWRITTEN")
    assert (outside / "report.md").read_bytes() == OUTSIDE
    assert sorted(path.name for path in outside.iterdir()) == ["report.md"]
    moved = root / "moved-sub"
    assert (moved / "report.md").read_bytes() == INSIDE
    assert sorted(path.name for path in moved.iterdir()) == ["report.md"], "no temp file left"


def test_leaf_swapped_after_the_check_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leaf replaced by a link (or another file) between stat and open."""
    root, outside = _layout(tmp_path)
    original_stat = ArtifactTree._leaf_stat

    def stat_then_swap(parent, leaf, shown):
        info = original_stat(parent, leaf, shown)
        target = root / "sub" / "report.md"
        target.unlink()
        target.symlink_to(outside / "report.md")
        return info

    monkeypatch.setattr(ArtifactTree, "_leaf_stat", staticmethod(stat_then_swap))
    with ArtifactTree(root) as tree:
        with pytest.raises(ArtifactError):
            tree.read("sub/report.md")
    assert (outside / "report.md").read_bytes() == OUTSIDE


def test_replace_never_writes_through_a_symlink(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("original\n", encoding="utf-8")
    (root / "report.md").symlink_to(outside)

    with ArtifactTree(root) as tree:
        tree.replace("report.md", b"replaced\n")

    assert outside.read_text(encoding="utf-8") == "original\n", "the link target was rewritten"
    assert not (root / "report.md").is_symlink(), "the symlink itself must be replaced"
    assert (root / "report.md").read_text(encoding="utf-8") == "replaced\n"


def test_replace_breaks_a_hard_link_and_is_bounded(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    original = tmp_path / "original.md"
    original.write_text("original\n", encoding="utf-8")
    (root / "alias.md").hardlink_to(original)

    with ArtifactTree(root) as tree:
        tree.replace("alias.md", b"replaced\n")
        with pytest.raises(ArtifactError, match="上限"):
            tree.replace("alias.md", b"x" * 11, max_bytes=10)

    assert original.read_text(encoding="utf-8") == "original\n"
    assert (root / "alias.md").read_text(encoding="utf-8") == "replaced\n"


def test_remove_unlinks_only_the_named_regular_file(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "nested").mkdir(parents=True)
    leaf = root / "nested" / "credentials.json"
    leaf.write_text("secret\n", encoding="utf-8")
    keep = root / "nested" / "settings.json"
    keep.write_text("keep\n", encoding="utf-8")

    with ArtifactTree(root) as tree:
        assert tree.remove("nested/credentials.json") is True
        assert tree.remove("nested/credentials.json") is False, "已不存在不是错误"
        with pytest.raises(ArtifactError):
            tree.remove("nested")  # 目录不是可删对象
        with pytest.raises(ArtifactError):
            tree.remove("../outside")  # 越界路径一律拒绝

    assert not leaf.exists()
    assert keep.read_text(encoding="utf-8") == "keep\n"


def test_remove_refuses_a_link_without_touching_the_target(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("outside\n", encoding="utf-8")
    (root / "credentials.json").symlink_to(outside)

    with ArtifactTree(root) as tree:
        with pytest.raises(ArtifactError, match="链接"):
            tree.remove("credentials.json")

    assert outside.read_text(encoding="utf-8") == "outside\n", "链接目标不得被删"
    assert (root / "credentials.json").is_symlink()


# -- the run pipeline ---------------------------------------------------------


def _config(tmp_path: Path, **overrides: str):
    values = {
        "OPENAI_API_KEY": SECRET,
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
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
        for line in events_text(store, record.run_id).splitlines()
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
    monkeypatch.setattr("glink_harness.runs.limits.REPORT_MAX_BYTES", 64)
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
    assert manager.stop(record.run_id).accepted is True
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
