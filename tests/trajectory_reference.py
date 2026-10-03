"""trajectory 投影的参考 fold（ADR 0021 的语言中立语义规范）。

ULegal trajectory 定位为「业务里程碑 + 展示 fold」：只投影里程碑行
（run_started / session_ready / run_stage / deliverables_declared / 终帧）与
终帧时刻的展示快照；delta、tool/subagent 直播帧不投影为行，只参与快照的
推导（最终正文、工具卡数）。本模块是该语义的可执行参考，由
tests/test_event_fixture_contract.py 用权威样例库（tests/fixtures/events/）
钉住；ULegal 侧的契约测试应对同一样例产出相同的投影行。

两条纪律与前端 reducer（web/modules/run-reducer.mjs）一致：

* 版本：只折叠 ``v`` 等于 schema 当前版本的帧，其余计入 ``rejected``、不投影
  （旧日志没有 ``v``，整日志不可读，没有迁移）；
* 权威正文：``assistant_message`` 逐 message 替换最后一个未闭文本块（无则追加）；
  ``turnScoped`` 帧合并替换本回合全部文本块；``truncated`` 帧比已流式正文短
  时保留流式拼接。快照的 ``texts`` 必须与 web reducer 的文本块一致——这是
  两侧共享的核心不变量。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from glink_harness import event_schema

#: 投影为里程碑行之外、但会追加展示条目的帧（它们改变「未闭文本块」的尾部判定，
#: 与 reducer 的 appendItem 对齐）。
_TAIL_RESET_TYPES = frozenset(
    {
        "turn_started",
        "thinking_delta",
        "tool_started",
        "subagent_started",
        "run_stage",
        "kernel_status",
    }
)

_TERMINAL_TYPES = event_schema.TERMINAL_TYPES


def _is_current(frame: Mapping[str, Any]) -> bool:
    expected = event_schema.EVENT_VERSIONS.get(frame.get("type", ""))
    return expected is not None and frame.get("v") == expected


def _append_delta(blocks: list[dict[str, Any]], turn: int, text: str, tail: list[str]) -> None:
    if not text:
        return
    if tail[0] == "text" and blocks and not blocks[-1]["closed"] and blocks[-1]["turn"] == turn:
        blocks[-1]["text"] += text
    else:
        blocks.append({"text": text, "turn": turn, "closed": False, "truncated": False})
    tail[0] = "text"


def _apply_message(blocks: list[dict[str, Any]], turn: int, payload: Mapping[str, Any], tail: list[str]) -> None:
    text = payload.get("text")
    if not isinstance(text, str) or text == "":
        return
    truncated = payload.get("truncated") is True
    if payload.get("turnScoped") is True:
        indexes = [i for i, block in enumerate(blocks) if block["turn"] == turn]
        if not indexes:
            blocks.append({"text": text, "turn": turn, "closed": True, "truncated": truncated})
        else:
            streamed = "".join(blocks[i]["text"] for i in indexes)
            keep_streamed = truncated and len(streamed) > len(text)
            merged = {
                "text": streamed if keep_streamed else text,
                "turn": turn,
                "closed": True,
                "truncated": truncated,
            }
            last = indexes[-1]
            kept: list[dict[str, Any]] = []
            for i, block in enumerate(blocks):
                if i in indexes:
                    if i == last:
                        kept.append(merged)
                    continue
                kept.append(block)
            blocks[:] = kept
    elif tail[0] == "text" and blocks and not blocks[-1]["closed"]:
        keep_streamed = truncated and len(blocks[-1]["text"]) > len(text)
        blocks[-1] = {
            "text": blocks[-1]["text"] if keep_streamed else text,
            "turn": blocks[-1]["turn"],
            "closed": True,
            "truncated": truncated,
        }
    else:
        blocks.append({"text": text, "turn": turn, "closed": True, "truncated": truncated})
    tail[0] = "text"


def fold(frames: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Fold one run's event stream into trajectory milestone rows + display snapshot.

    Returns ``{"milestones": [...], "snapshot": {...} | None, "rejected": int}``:
    ``snapshot`` 只在折到终帧时存在（未终结的样例没有展示快照）。
    """
    milestones: list[list[Any]] = []
    blocks: list[dict[str, Any]] = []
    tool_ids: list[str] = []
    tail = ["other"]
    turn = 0
    rejected = 0
    snapshot: dict[str, Any] | None = None
    for frame in frames:
        if not _is_current(frame):
            rejected += 1
            continue
        event_type = frame["type"]
        payload = frame.get("payload") or {}
        if event_type == "run_started":
            milestones.append(["started", payload.get("prompt", "")])
        elif event_type == "session_ready":
            milestones.append(["session", payload.get("sessionId"), payload.get("model")])
        elif event_type == "run_stage":
            milestones.append(["stage", payload.get("stage"), payload.get("attempt")])
        elif event_type == "deliverables_declared":
            files = payload.get("files") if isinstance(payload.get("files"), list) else []
            milestones.append(["deliverables", [f.get("path") for f in files if isinstance(f, Mapping)]])
        elif event_type == "turn_started":
            turn += 1
        elif event_type == "turn_finished":
            for block in blocks:
                block["closed"] = True
        elif event_type == "assistant_delta":
            _append_delta(blocks, turn, payload.get("text") or "", tail)
        elif event_type == "assistant_message":
            _apply_message(blocks, turn, payload, tail)
        elif event_type == "tool_started":
            tool_call_id = payload.get("toolCallId")
            if isinstance(tool_call_id, str) and tool_call_id not in tool_ids:
                tool_ids.append(tool_call_id)
        elif event_type in _TERMINAL_TYPES:
            milestones.append(["terminal", payload.get("status")])
            deliverables = payload.get("deliverables")
            snapshot = {
                "status": payload.get("status"),
                "stopReason": payload.get("stopReason"),
                "error": payload.get("error"),
                "reportStatus": payload.get("reportStatus"),
                "redacted": payload.get("redacted") is True,
                "children": len(payload.get("children") or []),
                "texts": [block["text"] for block in blocks],
                "toolCount": len(tool_ids),
                "deliverables": {
                    item["path"]: item.get("status")
                    for item in deliverables
                    if isinstance(item, Mapping) and isinstance(item.get("path"), str)
                }
                if isinstance(deliverables, list)
                else {},
            }
        if event_type in _TAIL_RESET_TYPES:
            tail[0] = "other"
    return {"milestones": milestones, "snapshot": snapshot, "rejected": rejected}
