"""文档漂移守卫：已删除机制不得再被描述成现行事实。

GLINK-ARCH-3 收敛掉的是 E2B 面的 ``pi --mode json`` 单向事件流、``tee`` 到沙箱内
JSONL、断流按文件补齐、``LFLineFramer``/``EventCatchUpFailed`` 与
``E2BSandboxExecutionAdapter``；现行通道是 RPC 双向 stdio（``docs/architecture.md``
§5/§5.2，三面均 ``--mode rpc``）。这些关键词只允许出现在明确的历史叙述行——行内
含「旧」「删除」「废除」「历史」之一；README 或现行文档段落里命中即视为漂移。

守卫自带反例：``drift_violations``/``mode_json_argv_violations`` 直接在合成文本上
验证“现行叙述变红、历史叙述放行、形近的名字（``E2BSandboxRpcExecutionAdapter``，
词边界反例）不误伤”。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 现行文档（历史证据 ``docs/evidence/`` 与集成笔记按 brief 不在扫描范围）。
#: ``docs/multi-harness-plan.md`` 自 GLINK-ARCH-6 起纳入：§3.5 的已删除 e2b json 机制
#: 描述必须留在带「删除」等历史标记的行上。
DRIFT_DOCUMENTS = (
    "README.md",
    "docs/pi-boundary.md",
    "docs/architecture.md",
    "docs/multi-harness-plan.md",
)

#: 已删除机制的关键词。``tee`` 与「按文件补齐」是同一套 tee/补读机制的入口，
#: 只在词边界上匹配，避免误伤普通英文单词。
_DRIFT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("--mode json", re.compile(r"--mode\s+json")),
    ("LFLineFramer", re.compile(r"LFLineFramer")),
    ("EventCatchUpFailed", re.compile(r"EventCatchUpFailed")),
    ("E2BSandboxExecutionAdapter", re.compile(r"E2BSandboxExecutionAdapter")),
    ("/home/user/events.jsonl", re.compile(r"/home/user/events\.jsonl")),
    ("tee", re.compile(r"(?<![A-Za-z])tee(?![A-Za-z])")),
    ("按文件补齐", re.compile(r"按文件补齐")),
)

#: 一行只要含其中之一，就明确是在讲旧机制而不是现行事实。
_HISTORICAL_MARKERS = ("旧", "删除", "废除", "历史")

#: 已删除的 argv 形式：``pi --mode json``。现行三面都传 ``--mode rpc``。
_MODE_JSON_ARGV = re.compile(r"""['"]--mode['"]\s*,\s*['"]json['"]""")


def drift_violations(
    text: str, *, historical_markers: tuple[str, ...] = _HISTORICAL_MARKERS
) -> list[str]:
    """Report every drift keyword that is not on a historical narration line."""
    violations: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if any(marker in line for marker in historical_markers):
            continue
        for label, pattern in _DRIFT_PATTERNS:
            if pattern.search(line):
                violations.append(f"line {number}: {label}")
    return violations


def mode_json_argv_violations(text: str) -> list[str]:
    """Report ``"--mode", "json"`` argv literals (any quote style or spacing)."""
    return [
        f"line {number}: {match.group()!r}"
        for number, line in enumerate(text.splitlines(), start=1)
        for match in [_MODE_JSON_ARGV.search(line)]
        if match is not None
    ]


def host_source_files() -> list[Path]:
    return sorted((ROOT / "host").rglob("*.py"))


def test_current_documents_do_not_describe_deleted_mechanisms() -> None:
    violations = []
    for relative in DRIFT_DOCUMENTS:
        text = (ROOT / relative).read_text(encoding="utf-8")
        violations.extend(f"{relative}#{item}" for item in drift_violations(text))
    assert violations == []


def test_host_sources_do_not_pass_the_deleted_json_argv() -> None:
    violations = []
    for path in host_source_files():
        for item in mode_json_argv_violations(path.read_text(encoding="utf-8")):
            violations.append(f"{path.relative_to(ROOT)}#{item}")
    assert violations == []


def test_drift_guard_flags_current_claims() -> None:
    """反例：把已删除机制写成现行事实必须变红。"""
    assert drift_violations(
        "| 云沙箱 | `e2b` | pi 与工作区都在 E2B 沙箱内（`--mode json` 单向事件流） |"
    ) == ["line 1: --mode json"]
    assert drift_violations("断流时按已交付行数补齐（按文件补齐），不漏不重。") == [
        "line 1: 按文件补齐"
    ]
    assert drift_violations("宿主迭代 SDK 的 stdout 流并 tee 到沙箱内 JSONL。") == [
        "line 1: tee"
    ]
    assert drift_violations("现行分帧器是 LFLineFramer，错误类型是 EventCatchUpFailed。") != []


def test_drift_guard_allows_historical_narration() -> None:
    """正例：明确标注为历史/已删除的叙述放行。"""
    assert drift_violations(
        "旧 `--mode json` 路径运行中不能 abort；整套 LFLineFramer、"
        "EventCatchUpFailed 已随旧 E2BSandboxExecutionAdapter 删除，"
        "历史 `/home/user/events.jsonl` 不再作为权威。"
    ) == []
    assert drift_violations("删除前按文件补齐是事实，现在是断流即失败。") == []


def test_drift_guard_does_not_confuse_the_rpc_adapter_name() -> None:
    """形近的 ``E2BSandboxRpcExecutionAdapter`` 不是被删除的 ``E2BSandboxExecutionAdapter``，不得被误伤。"""
    text = "沙箱面沿用 E2BSandboxRpcExecutionAdapter；三面均 `--mode rpc`。"
    assert drift_violations(text) == []


def test_mode_json_argv_rule_flags_the_deleted_launch_form() -> None:
    assert mode_json_argv_violations('argv = ["pi", "--mode", "json", prompt]') != []
    assert mode_json_argv_violations("subprocess.run(['pi', '--mode', 'json'])") != []
    assert mode_json_argv_violations('argv = ["pi", "--mode", "rpc", prompt]') == []
    assert mode_json_argv_violations('PI_GLINK_PI_MODE = "json"  # 配置名不是 argv') == []
