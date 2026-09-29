"""pi 族子代理运行中进度源（R3a）的单元测试。

契约见 docs/architecture.md §3.5「运行中进度（R3a）」：进度是瞬时事实
（最新值取胜），只含 facts（步骤数/最近工具名与目标/字节数快照），限幅、
不伪造——没有进度源或读不出内容的 child 不产出任何进度。

transcript 格式钉住 pi-subagents 0.40.0 ``ChildTranscriptWriter``
（``src/shared/child-transcript.ts``）：JSONL，每行一条带 ``recordType`` 的
记录，``tool_start`` 携带 ``toolName`` 与 ``argsPreview``。fixture 见
``tests/fixtures/child-progress/``（按该源码手工合成的样本）。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from pi_glink.kernel.child_progress import (
    E2B_PROGRESS_REQUEST_TIMEOUT_SECONDS,
    PROGRESS_MAX_FILES,
    TOOL_NAME_MAX_CHARS,
    TOOL_TARGET_MAX_CHARS,
    E2bChildProgressSource,
    PiChildProgressSource,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "child-progress"


def _record(record_type: str, **extra: object) -> bytes:
    record = {
        "version": 1,
        "recordType": record_type,
        "source": "foreground",
        "runId": "p1r2i3",
        "agent": "researcher",
        "cwd": "/workspace",
        "ts": 1,
        "timestamp": "2026-09-26T00:00:00.000Z",
    }
    record.update(extra)
    return json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"


def _tool_start(name: str | None = "read", preview: str | None = None) -> bytes:
    extra: dict[str, object] = {"sourceEventType": "tool_execution_start"}
    if name is not None:
        extra["toolName"] = name
    if preview is not None:
        extra["argsPreview"] = preview
    return _record("tool_start", **extra)


def _write(path: Path, chunks: list[bytes]) -> int:
    data = b"".join(chunks)
    path.write_bytes(data)
    return len(data)


def _stage_fixture(tmp_path: Path) -> Path:
    target = tmp_path / "subagent-artifacts"
    shutil.copytree(FIXTURE, target)
    return target


# -- 本地目录源：fixture 钉住 ----------------------------------------------------


def test_fixture_progress_facts(tmp_path: Path) -> None:
    directory = _stage_fixture(tmp_path)
    source = PiChildProgressSource(directory)
    assert source.poll() == [], "首轮只建基线"
    for transcript in directory.glob("*_transcript.jsonl"):
        with transcript.open("ab") as handle:
            handle.write(_tool_start("bash", "ls"))
    facts = {(fact.agent, fact.index): fact for fact in source.poll()}
    source.close()

    assert set(facts) == {("researcher", 0), ("researcher", 1)}
    first = facts[("researcher", 0)]
    assert first.steps == 3
    assert first.last_tool_name == "bash"
    assert first.bytes == len((directory / "p1r2i3_researcher_0_transcript.jsonl").read_bytes())
    second = facts[("researcher", 1)]
    assert second.steps == 2
    assert second.bytes == len((directory / "p1r2i3_researcher_1_transcript.jsonl").read_bytes())


def test_only_transcript_siblings_are_considered(tmp_path: Path) -> None:
    directory = _stage_fixture(tmp_path)
    (directory / "p1r2i3_researcher_0_input.md").write_text("输入", encoding="utf-8")
    (directory / "p1r2i3_researcher_0_meta.json").write_text("{}", encoding="utf-8")
    (directory / "notes.txt").write_text("无关", encoding="utf-8")
    source = PiChildProgressSource(directory)
    assert source.poll() == [], "首轮只建基线"
    for transcript in directory.glob("*_transcript.jsonl"):
        with transcript.open("ab") as handle:
            handle.write(_tool_start("bash", "ls"))
    facts = source.poll()
    source.close()
    assert all(fact.index in {0, 1} for fact in facts)
    assert len(facts) == 2


# -- 增量读取 --------------------------------------------------------------------


def test_partial_line_waits_for_completion(tmp_path: Path) -> None:
    directory = tmp_path / "subagent-artifacts"
    directory.mkdir()
    transcript = directory / "p1r2i3_researcher_0_transcript.jsonl"
    full_second = _tool_start("write", "out.md")
    cut = len(full_second) // 2
    transcript.write_bytes(_tool_start("read", "a.md") + full_second[:cut])

    source = PiChildProgressSource(directory)
    assert source.poll() == [], "首轮只建基线（半行挂起不产出）"

    # 半行补齐后只新增一步——已读偏移之前的内容不重算。
    with transcript.open("ab") as handle:
        handle.write(full_second[cut:])
    (fact,) = source.poll()
    source.close()
    assert fact.steps == 2
    assert (fact.last_tool_name, fact.last_tool_target) == ("write", "out.md")
    assert fact.bytes == len(transcript.read_bytes())


def test_malformed_and_foreign_records_do_not_count(tmp_path: Path) -> None:
    directory = tmp_path / "subagent-artifacts"
    directory.mkdir()
    transcript = directory / "p1r2i3_researcher_0_transcript.jsonl"
    _write(
        transcript,
        [
            b"not json at all\n",
            b"null\n",
            b"[1, 2]\n",
            _record("message", role="assistant", text="正文"),
            _record("tool_end", toolName="read"),
            _record("stdout", text="noise"),
            '{"type":"message","role":"assistant","text":"旧格式"}\n'.encode("utf-8"),
            b"\n",
            _tool_start("read", "ok.md"),
        ],
    )
    source = PiChildProgressSource(directory)
    assert source.poll() == [], "首轮只建基线（畸形与无关记录不产出）"
    with transcript.open("ab") as handle:
        handle.write(_tool_start("write", "after.md"))
    (fact,) = source.poll()
    source.close()
    assert fact.steps == 2
    assert (fact.last_tool_name, fact.last_tool_target) == ("write", "after.md")


def test_nameless_tool_start_keeps_known_values(tmp_path: Path) -> None:
    directory = tmp_path / "subagent-artifacts"
    directory.mkdir()
    transcript = directory / "p1r2i3_researcher_0_transcript.jsonl"
    _write(
        transcript,
        [
            _tool_start("read", "first.md"),
            _tool_start(name=None),  # 无名记录：计数但不覆盖已知的最近工具
            _tool_start("bash"),  # 有名字但没有 argsPreview：目标如实为空
        ],
    )
    source = PiChildProgressSource(directory)
    assert source.poll() == [], "首轮只建基线"
    with transcript.open("ab") as handle:
        handle.write(_tool_start(name=None))  # 无名记录只计数、不覆盖已知最近工具
    (fact,) = source.poll()
    source.close()
    assert fact.steps == 4
    assert fact.last_tool_name == "bash"
    assert fact.last_tool_target is None


def test_tool_name_and_target_are_bounded(tmp_path: Path) -> None:
    directory = tmp_path / "subagent-artifacts"
    directory.mkdir()
    transcript = directory / "p1r2i3_researcher_0_transcript.jsonl"
    _write(transcript, [_tool_start("n" * 500, "t" * 500)])
    source = PiChildProgressSource(directory)
    assert source.poll() == [], "首轮只建基线"
    # 无名记录只计数不覆盖已知最近工具：签名变化触发上报，限幅断言仍作用在
    # 长名/长目标上。
    with transcript.open("ab") as handle:
        handle.write(_tool_start(name=None))
    (fact,) = source.poll()
    source.close()
    assert fact.last_tool_name == "n" * TOOL_NAME_MAX_CHARS
    assert fact.last_tool_target == "t" * TOOL_TARGET_MAX_CHARS


def test_replaced_or_shrunk_file_recounts_from_scratch(tmp_path: Path) -> None:
    directory = tmp_path / "subagent-artifacts"
    directory.mkdir()
    transcript = directory / "p1r2i3_researcher_0_transcript.jsonl"
    _write(transcript, [_tool_start("read", "a.md"), _tool_start("read", "b.md")])
    source = PiChildProgressSource(directory)
    assert source.poll() == [], "首轮只建基线"

    # 同一 inode 变短：从零重新解析，不在旧计数上叠加。
    _write(transcript, [_tool_start("write", "new.md")])
    (fact,) = source.poll()
    assert fact.steps == 1
    assert (fact.last_tool_name, fact.last_tool_target) == ("write", "new.md")

    # 替换为新 inode（writer 初始化即 truncate+重写之外的另一种替换形态）。
    replacement = directory / ".replacement"
    _write(replacement, [_tool_start("bash", "ls"), _tool_start("read", "x.md")])
    os.replace(replacement, transcript)
    (fact,) = source.poll()
    source.close()
    assert fact.steps == 2
    assert (fact.last_tool_name, fact.last_tool_target) == ("read", "x.md")


def test_oversized_partial_line_is_dropped_not_consumed(tmp_path: Path) -> None:
    directory = tmp_path / "subagent-artifacts"
    directory.mkdir()
    transcript = directory / "p1r2i3_researcher_0_transcript.jsonl"
    # 半行超过上限（writer 单行有 32KB 载荷界，越界即畸形）：丢弃，不拿半截猜。
    transcript.write_bytes(b'{"recordType":"tool_start","pad":"' + b"x" * (600 * 1024))
    source = PiChildProgressSource(directory)
    assert source.poll() == [], "首轮只建基线"
    with transcript.open("ab") as handle:
        handle.write(b'"}\n' + _tool_start("read", "after.md"))
    (fact,) = source.poll()
    source.close()
    assert fact.steps == 1
    assert (fact.last_tool_name, fact.last_tool_target) == ("read", "after.md")


def test_read_cap_freezes_content_but_reports_size(tmp_path: Path) -> None:
    directory = tmp_path / "subagent-artifacts"
    directory.mkdir()
    transcript = directory / "p1r2i3_researcher_0_transcript.jsonl"
    _write(transcript, [_tool_start("read", "a.md"), _tool_start("write", "b.md")])
    # 上限卡在两条记录之间：只数到第一条，bytes 仍是文件大小快照。
    first_len = len(_tool_start("read", "a.md"))
    source = PiChildProgressSource(directory, max_file_bytes=first_len)
    assert source.poll() == [], "首轮只建基线"
    # 上限之后不再读取：文件继续增长也不追加步骤（bytes 仍如实跟随文件大小）。
    with transcript.open("ab") as handle:
        handle.write(_tool_start("bash", "ls"))
    (fact,) = source.poll()
    source.close()
    assert fact.steps == 1
    assert fact.bytes == len(transcript.read_bytes())


# -- 目录纪律 --------------------------------------------------------------------


def test_missing_directory_is_empty_progress_until_it_appears(tmp_path: Path) -> None:
    directory = tmp_path / "subagent-artifacts"
    source = PiChildProgressSource(directory)
    assert source.poll() == []
    assert source.poll() == []  # 目录缺席不是观察者失败，只是还没有进度

    directory.mkdir()
    transcript = directory / "p1r2i3_researcher_0_transcript.jsonl"
    _write(transcript, [_tool_start("read", "a.md")])
    assert source.poll() == [], "文件出现后首轮仍只建基线"
    with transcript.open("ab") as handle:
        handle.write(_tool_start("write", "b.md"))
    (fact,) = source.poll()
    source.close()
    assert fact.steps == 2


def test_symlink_transcript_is_never_followed(tmp_path: Path) -> None:
    directory = tmp_path / "subagent-artifacts"
    directory.mkdir()
    outside = tmp_path / "outside.jsonl"
    _write(outside, [_tool_start("read", "secret.md")])
    os.symlink(outside, directory / "p1r2i3_researcher_0_transcript.jsonl")
    _write(directory / "p1r2i3_researcher_1_transcript.jsonl", [_tool_start("write", "ok.md")])

    source = PiChildProgressSource(directory)
    assert source.poll() == [], "首轮只建基线（链接不产出也不上报）"
    with (directory / "p1r2i3_researcher_1_transcript.jsonl").open("ab") as handle:
        handle.write(_tool_start("read", "more.md"))
    facts = {(fact.agent, fact.index): fact for fact in source.poll()}
    source.close()
    # 链接不产出任何进度（不读目标、不伪造）；普通文件不受影响。
    assert set(facts) == {("researcher", 1)}
    assert outside.read_bytes().count(b"tool_start") == 1


def test_directory_symlink_fails_loudly(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    source = PiChildProgressSource(link)
    with pytest.raises(OSError):
        source.poll()


def test_max_files_bound_skips_runaway_extras(tmp_path: Path) -> None:
    directory = tmp_path / "subagent-artifacts"
    directory.mkdir()
    for index in range(PROGRESS_MAX_FILES + 2):
        _write(
            directory / f"p1r2i3_researcher_{index:03d}_transcript.jsonl",
            [_tool_start("read", f"{index}.md")],
        )
    source = PiChildProgressSource(directory)
    assert source.poll() == [], "首轮只建基线"
    for index in range(PROGRESS_MAX_FILES + 2):
        with (directory / f"p1r2i3_researcher_{index:03d}_transcript.jsonl").open("ab") as fh:
            fh.write(_tool_start("write", f"{index}.md"))
    facts = source.poll()
    source.close()
    assert len(facts) == PROGRESS_MAX_FILES
    # 排序后的前 N 份被跟踪，超出的一律不读取不伪造。
    assert max(fact.index for fact in facts) == PROGRESS_MAX_FILES - 1


def test_frozen_transcripts_are_never_misattributed(tmp_path: Path) -> None:
    """归属安全（评审阻塞项）：上一次派发冻结的同名 (agent, index) transcript
    不得产生 fact——它会被错配给新在飞 call 的 child。只有增长的文件上报。"""
    directory = tmp_path / "subagent-artifacts"
    directory.mkdir()
    _write(directory / "a1_researcher_0_transcript.jsonl", [_tool_start("read", "old.md")])
    _write(directory / "b2_researcher_0_transcript.jsonl", [_tool_start("read", "new.md")])
    source = PiChildProgressSource(directory)
    assert source.poll() == [], "首轮只建基线：旧冻结文件与当前文件都不立即上报"
    with (directory / "b2_researcher_0_transcript.jsonl").open("ab") as fh:
        fh.write(_tool_start("write", "new2.md"))
    facts = source.poll()
    assert [(fact.steps, fact.last_tool_name) for fact in facts] == [(2, "write")]
    assert facts[0].bytes > 0
    assert source.poll() == [], "无变化不再重复上报"
    source.close()


def test_close_is_idempotent_and_poll_after_close_raises(tmp_path: Path) -> None:
    source = PiChildProgressSource(tmp_path)
    source.close()
    source.close()
    with pytest.raises(ValueError):
        source.poll()


# -- E2B 变体（fake SDK） ----------------------------------------------------------


class _FakeEntry:
    def __init__(self, path: str, size: int | None, kind: object = "file") -> None:
        self.path = path
        self.size = size
        self.type = kind


class _FakeFiles:
    """e2b ``sandbox.files`` 的最小替身：list 报元数据，read 整份返回。"""

    def __init__(self, contents: dict[str, bytes | str]) -> None:
        self.contents = dict(contents)
        self.kinds: dict[str, object] = {}
        self.report_size = True
        self.read_calls: list[str] = []
        self.read_errors: set[str] = set()
        self.list_depths: list[int] = []
        #: SDK 请求超时参数（GLINK-OBS-01 第 4 项：单次调用必须显式有界）。
        self.list_timeouts: list[float | None] = []
        self.read_timeouts: list[float | None] = []

    def list(
        self, root: str, depth: int = 1, request_timeout: float | None = None
    ) -> list[_FakeEntry]:
        self.list_depths.append(depth)
        self.list_timeouts.append(request_timeout)
        entries = []
        for path, data in sorted(self.contents.items()):
            kind = self.kinds.get(path, "file")
            size = len(data.encode("utf-8") if isinstance(data, str) else data)
            entries.append(_FakeEntry(path, size if self.report_size else None, kind))
        return entries

    def read(self, path: str, request_timeout: float | None = None) -> bytes | str:
        self.read_calls.append(path)
        self.read_timeouts.append(request_timeout)
        if path in self.read_errors:
            raise RuntimeError("sandbox read failed")
        return self.contents[path]


class _FakeSandbox:
    def __init__(self, contents: dict[str, bytes | str]) -> None:
        self.files = _FakeFiles(contents)


ROOT = "/home/user/session/subagent-artifacts"
P0 = f"{ROOT}/p1r2i3_researcher_0_transcript.jsonl"
P1 = f"{ROOT}/p1r2i3_researcher_1_transcript.jsonl"


def test_e2b_progress_facts_and_incremental_growth() -> None:
    sandbox = _FakeSandbox(
        {
            P0: _tool_start("read", "a.md") + _tool_start("write", "b.md"),
            P1: _tool_start("bash", "ls"),
        }
    )
    source = E2bChildProgressSource(sandbox, ROOT)
    assert source.poll() == [], "首轮只建基线"
    sandbox.files.contents[P0] = sandbox.files.contents[P0] + _tool_start("read", "c.md")  # type: ignore[operator]
    sandbox.files.contents[P1] = sandbox.files.contents[P1] + _tool_start("write", "d.md")  # type: ignore[operator]
    facts = {(fact.agent, fact.index): fact for fact in source.poll()}
    assert facts[("researcher", 0)].steps == 3
    assert (facts[("researcher", 0)].last_tool_name, facts[("researcher", 0)].last_tool_target) == (
        "read",
        "c.md",
    )
    assert facts[("researcher", 0)].bytes == len(sandbox.files.contents[P0])
    assert facts[("researcher", 1)].steps == 2

    # 无变化：不产生 fact，大小不变的文件也不重复整份载入。
    reads_before = list(sandbox.files.read_calls)
    assert source.poll() == []
    assert sandbox.files.read_calls == reads_before
    sandbox.files.contents[P0] = sandbox.files.contents[P0] + _tool_start("read", "e.md")  # type: ignore[operator]
    facts = {(fact.agent, fact.index): fact for fact in source.poll()}
    source.close()
    assert facts[("researcher", 0)].steps == 4
    assert (facts[("researcher", 0)].last_tool_name, facts[("researcher", 0)].last_tool_target) == (
        "read",
        "e.md",
    )
    assert ("researcher", 1) not in facts, "未变化的文件不上报"
    assert sandbox.files.list_depths and all(depth >= 1 for depth in sandbox.files.list_depths)
    # 单次调用有界：list/read 都显式传 request_timeout（E2B SDK 默认 60s 会让
    # 一次卡住的调用拖垮整轮观察；GLINK-OBS-01 第 4 项）。
    assert sandbox.files.list_timeouts and all(
        timeout == E2B_PROGRESS_REQUEST_TIMEOUT_SECONDS
        for timeout in sandbox.files.list_timeouts
    )
    assert sandbox.files.read_timeouts and all(
        timeout == E2B_PROGRESS_REQUEST_TIMEOUT_SECONDS
        for timeout in sandbox.files.read_timeouts
    )


def test_e2b_str_payload_is_decoded() -> None:
    sandbox = _FakeSandbox({P0: (_tool_start("read", "a.md")).decode("utf-8")})
    source = E2bChildProgressSource(sandbox, ROOT)
    assert source.poll() == []
    sandbox.files.contents[P0] = (
        sandbox.files.contents[P0] + _tool_start("write", "b.md").decode("utf-8")
    )
    (fact,) = source.poll()
    source.close()
    assert fact.steps == 2


def test_e2b_skips_dirs_links_and_foreign_names() -> None:
    other = f"{ROOT}/p1r2i3_researcher_0_meta.json"
    notes = f"{ROOT}/notes.txt"
    linked = f"{ROOT}/p1r2i3_researcher_9_transcript.jsonl"
    sandbox = _FakeSandbox(
        {
            P0: _tool_start("read", "a.md"),
            other: b"{}",
            notes: b"noise",
            linked: _tool_start("read", "secret.md"),
        }
    )
    sandbox.files.kinds[linked] = "symlink"
    sandbox.files.kinds[notes] = "dir"
    source = E2bChildProgressSource(sandbox, ROOT)
    assert source.poll() == []
    sandbox.files.contents[P0] = sandbox.files.contents[P0] + _tool_start("write", "b.md")
    facts = source.poll()
    source.close()
    assert [(fact.agent, fact.index) for fact in facts] == [("researcher", 0)]
    assert linked not in sandbox.files.read_calls
    assert notes not in sandbox.files.read_calls


def test_e2b_unknown_size_is_not_loaded() -> None:
    sandbox = _FakeSandbox({P0: _tool_start("read", "a.md")})
    sandbox.files.report_size = False
    source = E2bChildProgressSource(sandbox, ROOT)
    # SDK 不报大小：不整份载入（读取前限幅口径），也不伪造零进度。
    assert source.poll() == []
    source.close()
    assert sandbox.files.read_calls == []


def test_e2b_oversize_is_not_loaded_but_size_is_reported() -> None:
    sandbox = _FakeSandbox({P0: _tool_start("read", "a.md")})
    size = len(sandbox.files.contents[P0])
    source = E2bChildProgressSource(sandbox, ROOT, max_file_bytes=size - 1)
    assert source.poll() == [], "首轮只建基线（大小如实记录但不发射）"
    sandbox.files.contents[P0] = sandbox.files.contents[P0] + _tool_start("write", "b.md")
    (fact,) = source.poll()
    source.close()
    assert fact.bytes == len(sandbox.files.contents[P0])
    assert fact.steps == 0
    assert sandbox.files.read_calls == []


def test_e2b_read_error_keeps_last_snapshot_and_list_error_propagates() -> None:
    sandbox = _FakeSandbox({P0: _tool_start("read", "a.md")})
    source = E2bChildProgressSource(sandbox, ROOT)
    assert source.poll() == []
    # 内容增长但读取失败：保留上次快照（签名未变 → 不上报，不抹掉已知进度）。
    sandbox.files.contents[P0] = sandbox.files.contents[P0] + _tool_start("write", "b.md")  # type: ignore[operator]
    sandbox.files.read_errors.add(P0)
    assert source.poll() == []
    sandbox.files.read_errors.clear()
    (fact,) = source.poll()
    assert fact.steps == 2

    class _BrokenFiles:
        def list(
            self, root: str, depth: int = 1, request_timeout: float | None = None
        ) -> list[_FakeEntry]:
            raise RuntimeError("sandbox gone")

    sandbox.files = _BrokenFiles()  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        source.poll()
    source.close()


def test_e2b_list_timeouts_leave_the_source_reusable() -> None:
    """fake SDK 连续超时：poll 如实抛出（由接线层退避重试），恢复后照常出 fact。

    GLINK-OBS-01 第 4 项：真实 pi r2 里 5 次 60s 超时后观察者永久停止，
    run 剩下 20 分钟无进度。任务要求「临时失败不导致永久失明」，所以源本身
    不吞掉超时（否则接线层无法退避），也必须在超时之后仍可用。
    """
    sandbox = _FakeSandbox({P0: _tool_start("read", "a.md")})
    source = E2bChildProgressSource(sandbox, ROOT)
    assert source.poll() == [], "首轮只建基线"

    original = sandbox.files.list
    calls = {"n": 0}

    def flaky_list(
        root: str, depth: int = 1, request_timeout: float | None = None
    ) -> list[_FakeEntry]:
        calls["n"] += 1
        if calls["n"] <= 6:
            raise TimeoutError("Request timed out (fake SDK)")
        return original(root, depth=depth, request_timeout=request_timeout)

    sandbox.files.list = flaky_list  # type: ignore[method-assign]
    for _ in range(6):
        with pytest.raises(TimeoutError):
            source.poll()

    sandbox.files.contents[P0] = sandbox.files.contents[P0] + _tool_start("write", "b.md")  # type: ignore[operator]
    (fact,) = source.poll()
    assert fact.steps == 2
    source.close()


def test_e2b_shrunk_file_recounts() -> None:
    sandbox = _FakeSandbox({P0: _tool_start("read", "a.md") + _tool_start("write", "b.md")})
    source = E2bChildProgressSource(sandbox, ROOT)
    assert source.poll() == []
    sandbox.files.contents[P0] = _tool_start("bash", "ls")
    (fact,) = source.poll()
    source.close()
    assert fact.steps == 1
    assert (fact.last_tool_name, fact.last_tool_target) == ("bash", "ls")


def test_e2b_max_files_bound() -> None:
    contents = {
        f"{ROOT}/p1r2i3_researcher_{index:03d}_transcript.jsonl": _tool_start("read", "a.md")
        for index in range(PROGRESS_MAX_FILES + 1)
    }
    sandbox = _FakeSandbox(contents)
    source = E2bChildProgressSource(sandbox, ROOT)
    assert source.poll() == []
    for path in contents:
        sandbox.files.contents[path] = sandbox.files.contents[path] + _tool_start("write", "b.md")  # type: ignore[operator]
    facts = source.poll()
    source.close()
    assert len(facts) == PROGRESS_MAX_FILES


def test_e2b_close_idempotent_and_poll_after_close_raises() -> None:
    source = E2bChildProgressSource(_FakeSandbox({}), ROOT)
    source.close()
    source.close()
    with pytest.raises(ValueError):
        source.poll()
