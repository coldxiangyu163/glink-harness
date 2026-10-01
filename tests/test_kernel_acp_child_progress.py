"""dsh zstd 会话格式与 ACP 子代理进度源的 pin 测试（R3a）。

帧形状对照 docs/evidence/acp-subagent-zstd-spike.txt（dsh 0.1.7 实测）钉住在
``tests/fixtures/acp-child-session/``（手工合成、zstandard 压缩的一对主/子会话）：
主会话 ``tool/call`` + 相邻 ``subagent/catalog`` 得 callId↔childId；子会话头
``origin:"subagent"`` + step/tool 帧给进度。dsh 升级改变格式时这里立刻变红，
而不是静默丢失进度（与 pi-subagents 布局 pin 同一口径）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# zstandard 是可选依赖（pyproject acp-progress extra）：未装它的环境下整个
# 模块跳过，而不是收集期 ImportError（与契约「缺失时如实无进度」一致）。
# 下面的导入必须晚于 importorskip，因此显式豁免 E402。
zstandard = pytest.importorskip("zstandard")

from glink_harness.kernel import acp_child_progress as progress  # noqa: E402
from glink_harness.kernel.acp_child_progress import (  # noqa: E402
    MAX_SESSION_FILE_BYTES,
    MAX_TARGET_CHARS,
    AcpChildProgressSource,
    ChildLink,
    available,
    parse_child_session,
    parse_main_session,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "acp-child-session"
MAIN_ID = "a9b72aeb-1111-4111-8111-111111111111"
CHILD_ID = "e3282967-2222-4222-8222-222222222222"
CALL_ID = "call_00_EKCEXAMPLE"
MAIN_SESSION = FIXTURE / MAIN_ID / "session.v4.jsonl.zstd"
CHILD_SESSION = FIXTURE / CHILD_ID / "session.v4.jsonl.zstd"


def _write_session(directory: Path, frames: list[dict]) -> Path:
    """把帧列表按 dsh 布局写成一份 zstd 会话文件。"""

    directory.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(f, ensure_ascii=False) + "\n" for f in frames)
    path = directory / "session.v4.jsonl.zstd"
    path.write_bytes(zstandard.ZstdCompressor().compress(payload.encode("utf-8")))
    return path


def _subagent_call(seq: int, call_id: str) -> dict:
    return {
        "type": "tool/call",
        "seq": seq,
        "data": {
            "callId": call_id,
            "name": "subagent",
            "arguments": json.dumps(
                {"description": "委派", "prompt": "做子任务", "run_in_background": False},
                ensure_ascii=False,
            ),
        },
    }


def _catalog(seq: int, child_id: str, label: str = "子任务") -> dict:
    return {
        "type": "subagent/catalog",
        "seq": seq,
        "data": {"version": 0, "childId": child_id, "mode": "one-shot", "label": label},
    }


# -- 布局 pin -------------------------------------------------------------------


def test_fixture_layout_is_pinned() -> None:
    entries = sorted(path.parent.name + "/" + path.name for path in FIXTURE.rglob("*.zstd"))
    assert entries == [
        f"{MAIN_ID}/session.v4.jsonl.zstd",
        f"{CHILD_ID}/session.v4.jsonl.zstd",
    ], "fixture 与钉住的主/子会话布局不一致"


def test_available_when_zstandard_installed() -> None:
    assert available() is True


# -- 主会话关联解析 ---------------------------------------------------------------


def test_parse_main_session_pairs_call_with_adjacent_catalog() -> None:
    links = parse_main_session(MAIN_SESSION)
    assert links == [
        ChildLink(call_id=CALL_ID, child_id=CHILD_ID, label="调研 zstd 会话格式")
    ]


def test_catalog_after_non_subagent_call_is_not_paired(tmp_path: Path) -> None:
    path = _write_session(
        tmp_path / MAIN_ID,
        [
            {"type": "session", "seq": 0, "id": MAIN_ID, "delegationDepth": 0},
            {
                "type": "tool/call",
                "seq": 1,
                "data": {"callId": "call_x", "name": "web_search", "arguments": "{}"},
            },
            _catalog(2, "child-x"),
        ],
    )
    assert parse_main_session(path) == [], "catalog 出现在非 subagent call 后不得配对"


def test_catalog_not_immediately_adjacent_is_not_paired(tmp_path: Path) -> None:
    path = _write_session(
        tmp_path / MAIN_ID,
        [
            {"type": "session", "seq": 0, "id": MAIN_ID, "delegationDepth": 0},
            _subagent_call(1, "call_a"),
            {"type": "assistant/message", "seq": 2, "data": {"text": "间隔帧"}},
            _catalog(3, "child-a"),
        ],
    )
    assert parse_main_session(path) == [], "catalog 只与紧邻其前的 subagent call 配对"


def test_multiple_dispatches_keep_frame_order(tmp_path: Path) -> None:
    path = _write_session(
        tmp_path / MAIN_ID,
        [
            {"type": "session", "seq": 0, "id": MAIN_ID, "delegationDepth": 0},
            _subagent_call(1, "call_a"),
            _catalog(2, "child-a", label="甲"),
            _subagent_call(3, "call_b"),
            _catalog(4, "child-b", label="乙"),
        ],
    )
    links = parse_main_session(path)
    assert [(link.call_id, link.child_id, link.label) for link in links] == [
        ("call_a", "child-a", "甲"),
        ("call_b", "child-b", "乙"),
    ]


def test_second_catalog_for_same_call_is_not_paired(tmp_path: Path) -> None:
    path = _write_session(
        tmp_path / MAIN_ID,
        [
            {"type": "session", "seq": 0, "id": MAIN_ID, "delegationDepth": 0},
            _subagent_call(1, "call_a"),
            _catalog(2, "child-a"),
            _catalog(3, "child-b"),
        ],
    )
    links = parse_main_session(path)
    assert [(link.call_id, link.child_id) for link in links] == [("call_a", "child-a")]


# -- 子会话进度提取 ---------------------------------------------------------------


def test_parse_child_session_extracts_progress_facts() -> None:
    fact = parse_child_session(CHILD_SESSION, call_id=CALL_ID, child_id=CHILD_ID)
    assert fact is not None
    assert fact.call_id == CALL_ID
    assert fact.child_id == CHILD_ID
    assert fact.steps == 2, "steps 取 tool/call 帧计数"
    assert fact.last_tool_name == "read_file"
    assert fact.last_tool_target == "/tmp/notes.md"
    assert fact.bytes == CHILD_SESSION.stat().st_size, "bytes 是源文件字节数快照"


def test_parse_child_session_rejects_main_session() -> None:
    assert (
        parse_child_session(MAIN_SESSION, call_id=CALL_ID, child_id=MAIN_ID) is None
    ), "非 subagent 会话不得产出进度"


def test_parse_child_session_rejects_id_mismatch() -> None:
    assert (
        parse_child_session(CHILD_SESSION, call_id=CALL_ID, child_id="别的-child")
        is None
    ), "目录名对上但会话头 id 不符时不伪造进度"


def test_parse_child_session_refuses_oversized_file(tmp_path: Path) -> None:
    path = _write_session(
        tmp_path / CHILD_ID,
        [
            {
                "type": "session",
                "seq": 0,
                "id": CHILD_ID,
                "origin": "subagent",
                "parentSession": MAIN_ID,
                "delegationDepth": 1,
            }
        ],
    )
    limit = path.stat().st_size - 1
    assert (
        parse_child_session(path, call_id=CALL_ID, child_id=CHILD_ID, max_file_bytes=limit)
        is None
    ), "超限即该 child 无进度，不报错"


def test_last_tool_target_is_truncated(tmp_path: Path) -> None:
    long_path = "/tmp/" + "x" * (MAX_TARGET_CHARS + 50)
    path = _write_session(
        tmp_path / CHILD_ID,
        [
            {
                "type": "session",
                "seq": 0,
                "id": CHILD_ID,
                "origin": "subagent",
                "parentSession": MAIN_ID,
                "delegationDepth": 1,
            },
            {
                "type": "tool/call",
                "seq": 1,
                "data": {
                    "callId": "call_t",
                    "name": "read_file",
                    "arguments": json.dumps({"path": long_path}),
                },
            },
        ],
    )
    fact = parse_child_session(path, call_id=CALL_ID, child_id=CHILD_ID)
    assert fact is not None
    assert fact.last_tool_target == long_path[:MAX_TARGET_CHARS]


def test_torn_tail_line_does_not_poison_parsing(tmp_path: Path) -> None:
    path = _write_session(
        tmp_path / CHILD_ID,
        [
            {
                "type": "session",
                "seq": 0,
                "id": CHILD_ID,
                "origin": "subagent",
                "parentSession": MAIN_ID,
                "delegationDepth": 1,
            },
            {
                "type": "tool/call",
                "seq": 1,
                "data": {"callId": "call_t", "name": "web_search", "arguments": "{}"},
            },
        ],
    )
    # 模拟持续重写中的撕裂末行：追加半个 JSON 行后重新压缩。
    frames_text = zstandard.ZstdDecompressor().decompress(path.read_bytes()).decode()
    path.write_bytes(
        zstandard.ZstdCompressor().compress(
            (frames_text + '{"type": "tool/call", "seq": 2, "da').encode()
        )
    )
    fact = parse_child_session(path, call_id=CALL_ID, child_id=CHILD_ID)
    assert fact is not None
    assert fact.steps == 1, "撕裂行被跳过，下一轮轮询自然补齐"


# -- 轮询源 -----------------------------------------------------------------------


def test_source_poll_maps_links_to_facts() -> None:
    source = AcpChildProgressSource(FIXTURE)
    facts = source.poll()
    source.close()
    assert len(facts) == 1
    fact = facts[0]
    assert (fact.call_id, fact.child_id) == (CALL_ID, CHILD_ID)
    assert fact.steps == 2
    assert fact.last_tool_name == "read_file"


def test_source_skips_child_without_session_file(tmp_path: Path) -> None:
    _write_session(
        tmp_path / MAIN_ID,
        [
            {"type": "session", "seq": 0, "id": MAIN_ID, "delegationDepth": 0},
            _subagent_call(1, "call_a"),
            _catalog(2, "child-未落盘"),
        ],
    )
    source = AcpChildProgressSource(tmp_path)
    assert source.poll() == [], "子会话缺失即该 child 无进度，不伪造"


def test_source_skips_corrupt_child_session(tmp_path: Path) -> None:
    _write_session(
        tmp_path / MAIN_ID,
        [
            {"type": "session", "seq": 0, "id": MAIN_ID, "delegationDepth": 0},
            _subagent_call(1, "call_a"),
            _catalog(2, CHILD_ID),
        ],
    )
    corrupt = tmp_path / CHILD_ID
    corrupt.mkdir()
    (corrupt / "session.v4.jsonl.zstd").write_bytes(b"not a zstd stream")
    source = AcpChildProgressSource(tmp_path)
    assert source.poll() == [], "损坏文件只让对应 child 无进度，不抛出"


def test_source_refuses_oversized_sessions(tmp_path: Path) -> None:
    _write_session(
        tmp_path / MAIN_ID,
        [
            {"type": "session", "seq": 0, "id": MAIN_ID, "delegationDepth": 0},
            _subagent_call(1, "call_a"),
            _catalog(2, CHILD_ID),
        ],
    )
    _write_session(
        tmp_path / CHILD_ID,
        [
            {
                "type": "session",
                "seq": 0,
                "id": CHILD_ID,
                "origin": "subagent",
                "parentSession": MAIN_ID,
                "delegationDepth": 1,
            }
        ],
    )
    source = AcpChildProgressSource(tmp_path, max_file_bytes=16)
    assert source.poll() == [], "超限时关联与进度都为无，不报错"
    assert MAX_SESSION_FILE_BYTES == 8 << 20


def test_source_unavailable_without_zstandard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(progress, "_zstd", None)
    assert available() is False
    source = AcpChildProgressSource(FIXTURE)
    assert source.poll() == [], "zstandard 缺失时该面如实无进度，不报错"


def test_close_stops_polling() -> None:
    source = AcpChildProgressSource(FIXTURE)
    source.close()
    assert source.poll() == []
