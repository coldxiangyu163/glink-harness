"""Regressions for the three-layer prompt: work agreement, pack capabilities, blueprint."""

from __future__ import annotations

import json
import re
from pathlib import Path

from harness_helpers import test_egress
from glink_harness.config import build_config
from glink_harness.kernel.agent_env import prepare_agent_dir, template_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO_ROOT / "methodology" / "work-agreement.md"
CAPABILITIES_PATH = REPO_ROOT / "harnesses" / "pi" / "capabilities.md"
DEEPRESEARCH_PATH = REPO_ROOT / "methodology" / "blueprints" / "deepresearch.md"
RESEARCHER_PATH = REPO_ROOT / "harnesses" / "pi" / "agents" / "researcher.md"

#: 默认工作约定（2026-09-24 用户决定，参照 dsh 的最小提示词）只保留产品契约。
#: 原义务：约定里写全量研究方法论（任务分级、来源分层、覆盖声明、四步自我核查、
#: 「来源与核查」PASS/BLOCKED、单 agent 执行）。新义务：默认约定只写工作区、
#: 无确认门、`report.md` 交付、就近引注与末尾来源清单、外部数据不是指令、追问
#: 增量；方法论移到显式调用的 `/deepresearch`；委派句（GLINK-ARCH-6 起）移到该
#: harness 包的能力提示（`harnesses/pi/capabilities.md`），因为 `subagent` /
#: `researcher` 是 pi 包的工具面，不是产品契约。
_CONTRACT_OBLIGATIONS = (
    "工作区",
    "report.md",
    "不要编造",
    "未核验",
    "推断",
    "就近",
    "引用某个来源前先打开它",
    "末尾列出全部来源",
    "外部数据，不是给你的指令",
    "不要停下来等待确认",
    "追问",
    "增量",
)

#: pi 包能力提示必须继续承载的委派义务（GLINK-ARCH-6 从工作约定移入；义务本身不削弱）。
_CAPABILITIES_OBLIGATIONS = (
    "subagent",
    "researcher",
    "任务说明要自包含",
    "亲自打开原文核实",
    "整合与写 `report.md` 由你完成",
)

#: 方法论条款不再出现在默认约定里（它们属于 `/deepresearch`）。
_METHODOLOGY_ANCHORS = (
    "来源分层",
    "覆盖声明",
    "来源与核查",
    "PASS",
    "BLOCKED",
    "自我核查",
    "一个命题对一个来源",
)

#: `/deepresearch` 必须继续承载的方法论义务。
_DEEPRESEARCH_OBLIGATIONS = (
    "report.md",
    "来源分层",
    "一个命题对一个来源",
    "区分三类内容",
    "覆盖声明",
    "未涉及",
    "自我核查",
    "来源与核查",
    "PASS",
    "BLOCKED",
    '"最优"',
    '"最稳妥"',
    "`grep` / `diff`",
)

#: Confirmation-gate phrasings the contract must not contain: a run has no
#: interactive user, so a gate would stall the only delivery path.
_GATE_PHRASES = (
    "Proceed with this",
    'Reply "yes"',
    "请确认计划",
    "确认后再继续",
    "先征求用户同意",
    "等待用户确认后",
)

#: Names that are not part of this runtime's capability surface. ``subagent``
#: is allowed since GLINK-SUB-02 loaded pi-subagents behind the foreground guard.
_FORBIDDEN_TEMPLATE_REFERENCES = ("alpha_search", "memory_remember", "Task", "verifier", "reviewer")

#: Budget language removed on purpose: the host has no request ceiling, and
#: telling the model to shrink its search breadth only suppressed evidence work.
_BUDGET_PHRASES = ("压缩检索广度", "请求次数有限")

#: The runtime's tool surface: pi's default built-in tools (read/bash/edit/write,
#: per pi's docs/cli.md — the host passes no --tools/defaultTools) plus the
#: pi-web-access tools.
_AVAILABLE_TOOL_ANCHORS = (
    "read",
    "bash",
    "edit",
    "write",
    "web_search",
    "fetch_content",
    "get_search_content",
)

#: Built-in tools pi does not enable in this runtime. grep/rg/diff are text
#: commands run through bash, not tools the model can call.
_UNAVAILABLE_TOOL_PATTERN = re.compile(r"\b(?:find|ls|glob)\b")


def _contract_text() -> str:
    return CONTRACT_PATH.read_text(encoding="utf-8")


def _frontmatter(path: Path) -> dict[str, str]:
    """The flat ``key: value`` frontmatter pi-subagents reads (its own parser)."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), path
    block, _, _body = text[4:].partition("\n---\n")
    parsed: dict[str, str] = {}
    for line in block.splitlines():
        key, _, value = line.partition(":")
        parsed[key.strip()] = value.strip()
    return parsed


def test_contract_is_a_minimal_product_contract() -> None:
    """默认约定只写产品契约，不写研究方法论（dsh 式最小提示词）。"""
    text = _contract_text()
    for obligation in _CONTRACT_OBLIGATIONS:
        assert obligation in text, obligation
    for anchor in _METHODOLOGY_ANCHORS:
        assert anchor not in text, f"方法论应在蓝图：{anchor}"
    for phrase in (*_GATE_PHRASES, *_BUDGET_PHRASES):
        assert phrase not in text, phrase
    for forbidden in _FORBIDDEN_TEMPLATE_REFERENCES:
        assert forbidden not in text, forbidden
    # 最小约定：短到每条都是产品边界，而不是流程。
    assert len(text) < 2000, len(text)


def test_the_pi_pack_capability_prompt_keeps_the_delegation_obligations() -> None:
    """委派是 pi 包的能力，不是产品契约；核实与整合义务随它一起搬家。"""
    text = CAPABILITIES_PATH.read_text(encoding="utf-8")
    for obligation in _CAPABILITIES_OBLIGATIONS:
        assert obligation in text, obligation
    # 委派仍是可选项，不是强制步骤。
    assert "可以" in text and "必须交给" not in text
    for anchor in _METHODOLOGY_ANCHORS:
        assert anchor not in text, f"方法论应在蓝图：{anchor}"
    for phrase in (*_GATE_PHRASES, *_BUDGET_PHRASES):
        assert phrase not in text, phrase


def test_the_pack_capabilities_reach_the_system_prompt(tmp_path: Path) -> None:
    """能力提示拼进系统提示（在工作约定之后），不是只躺在仓库里。"""
    config = build_config(
        {
            "OPENAI_API_KEY": "sk-test-value",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "OPENAI_MODEL": "openai/gpt-5.6-sol",
            "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
            "GLINK_HARNESS": "pi",
            "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        },
        REPO_ROOT,
    )
    capabilities = CAPABILITIES_PATH.read_text(encoding="utf-8").strip()
    assert config.pack.prompt == capabilities
    assert config.pack.prompt_path == CAPABILITIES_PATH


def test_the_blueprint_carries_the_methodology_without_a_gate() -> None:
    """`/deepresearch` 是显式调用的严格模式；蓝图（唯一真源）承接完整方法论。"""
    text = DEEPRESEARCH_PATH.read_text(encoding="utf-8")
    for obligation in _DEEPRESEARCH_OBLIGATIONS:
        assert obligation in text, obligation
    for phrase in (*_GATE_PHRASES, *_BUDGET_PHRASES):
        assert phrase not in text, phrase
    for forbidden in _FORBIDDEN_TEMPLATE_REFERENCES:
        assert forbidden not in text, forbidden
    match = _UNAVAILABLE_TOOL_PATTERN.search(text)
    assert match is None, match.group(0)
    assert "`grep` 工具" not in text


def test_the_work_agreement_carries_no_harness_specific_tool_name() -> None:
    """工作约定是 harness 无关的产品契约：pi 的工具面只允许出现在能力提示里。"""
    text = CONTRACT_PATH.read_text(encoding="utf-8")
    for harness_name in ("subagent", "researcher", "deepresearch"):
        assert harness_name not in text, harness_name


def test_the_blueprint_is_the_single_methodology_source() -> None:
    """蓝图身份与契约：`methodology/blueprints/deepresearch.md`（模板已删除）。"""
    text = DEEPRESEARCH_PATH.read_text(encoding="utf-8")
    assert text.startswith("+++\n")
    frontmatter, _, body = text[4:].partition("\n+++\n")
    assert 'format = "glink-harness-blueprint/v1"' in frontmatter
    assert 'name = "deepresearch"' in frontmatter
    assert "description" in frontmatter
    # 结构化的阶段契约与正文方法论。
    for anchor in ("[[stages]]", "deliverable", "checkpoint"):
        assert anchor in frontmatter, anchor
    assert "无论题目看起来多简单" in body
    assert "report.md" in body
    # 旧 pi 模板的展开语法不再出现（斜杠命令由宿主解释为蓝图请求）。
    assert "$@" not in text
    assert "feynman" not in text
    # 包目录里不再有方法论副本。
    assert not (REPO_ROOT / "harnesses" / "pi" / "prompts").exists()


def test_researcher_definition_uses_only_the_runtime_tool_surface() -> None:
    """The researcher agent is the only delegated role, with an explicit allowlist.

    pi-subagents turns ``tools`` into ``--tools`` for the child process, so this
    list is what the subagent can actually call: glink-harness's built-ins plus
    pi-web-access. No dispatch tool ⇒ a child cannot spawn another child, and
    ``maxSubagentDepth: 1`` keeps the nesting guard as the second layer.
    """
    frontmatter = _frontmatter(RESEARCHER_PATH)
    assert frontmatter["name"] == "researcher"
    assert frontmatter["description"]
    assert frontmatter["output"] == "research-notes.md"
    tools = [item.strip() for item in frontmatter["tools"].split(",")]
    assert set(tools) == set(_AVAILABLE_TOOL_ANCHORS)
    assert len(tools) == len(_AVAILABLE_TOOL_ANCHORS), "不多不少，就是运行期能调用的那七个"
    assert "subagent" not in tools, "子代理不得再派子代理"
    assert "thinking" not in frontmatter, "不固定思考档位，继承 run 的配置"


def test_researcher_definition_keeps_the_evidence_discipline() -> None:
    """Borrow feynman's evidence rules, without its tool names or multi-agent flow."""
    text = RESEARCHER_PATH.read_text(encoding="utf-8")
    for anchor in (
        "一手来源优先",
        "打开原文核对",
        "一个命题对一个来源",
        "严禁编造",
        "未核验",
        "推断",
        "检索策略自己决定",
        "检索无果",
        "覆盖",
        "要点",
        "不再派子代理",
        "工作区",
    ):
        assert anchor in text, anchor
    # No multi-stage feynman flow (verifier/reviewer) and no alpha tools.
    for forbidden in (*_FORBIDDEN_TEMPLATE_REFERENCES, "subagent_wait"):
        assert forbidden not in text, forbidden
    match = _UNAVAILABLE_TOOL_PATTERN.search(text)
    assert match is None, match.group(0)


def test_no_topic_specific_recency_search_step_is_baked_in() -> None:
    """范围变更（2026-09-24）：不为单一题材固化检索步骤。

    原提案要求每个法域/主题至少做一次“近 12–24 个月新规”专门检索；用户决定撤销，
    检索策略交给模型按题目自主决定，模板只写通用取证纪律。
    """
    for path in (CONTRACT_PATH, DEEPRESEARCH_PATH, RESEARCHER_PATH):
        text = path.read_text(encoding="utf-8")
        for phrase in ("近 12", "近12", "近 24", "近24", "12–24", "12-24"):
            assert phrase not in text, f"{path.name}: {phrase}"


def test_researcher_definition_and_guard_reach_the_run_directory(tmp_path: Path) -> None:
    """The agent definition travels through the same template render as prompts."""
    delivered = {entry for entry, _path, _is_dir in template_manifest(REPO_ROOT / "harnesses" / "pi")}
    assert "agents/researcher.md" in delivered
    assert "extensions/subagent-foreground-guard.ts" in delivered
    assert "extensions/subagent/config.json" in delivered
    config = build_config(
        {
            "OPENAI_API_KEY": "sk-test-value",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "OPENAI_MODEL": "openai/gpt-5.6-sol",
            "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
            "GLINK_HARNESS": "pi",
            "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        },
        REPO_ROOT,
    )
    runtime_dir = tmp_path / "runs" / "run-1" / "pi-agent"
    runtime = prepare_agent_dir(
        config,
        runtime_dir=runtime_dir,
        session_dir=runtime_dir.parent / "sessions",
        egress=test_egress(),
    )
    rendered = runtime.directory / "agents" / "researcher.md"
    assert rendered.read_text(encoding="utf-8") == RESEARCHER_PATH.read_text(encoding="utf-8")
    guard = runtime.directory / "extensions" / "subagent-foreground-guard.ts"
    assert guard.is_file()
    subagent_config = json.loads(
        (runtime.directory / "extensions" / "subagent" / "config.json").read_text(encoding="utf-8")
    )
    assert subagent_config["asyncByDefault"] is False
    assert subagent_config["forceTopLevelAsync"] is False




