"""Regressions for the appended working contract and the `/deepresearch` template."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pi_glink.config import build_config
from pi_glink.kernel.agent_env import prepare_agent_dir, template_manifest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO_ROOT / "agent" / "research-prompt.md"
DEEPRESEARCH_PATH = REPO_ROOT / "agent" / "prompts" / "deepresearch.md"
RESEARCHER_PATH = REPO_ROOT / "agent" / "agents" / "researcher.md"

#: The contract grades work before acting: a fast path that answers a single
#: fact (with a source, without plan/report artifacts) and a decidable list of
#: cases that qualify for the deep flow. These are anchors, not exact wording.
_CONTRACT_GRADING_ANCHORS = (
    "直接回答",
    "深入任务",
    "满足以下任一条",
    "不写计划文件",
    "不产出 `report.md`",
    "一两次检索",
    "在回答中给出来源",
)

#: Obligations the deep flow must keep carrying (anchors, not exact wording)
#: after the rewrite into dsh-style rules.
_DEEP_FLOW_OBLIGATIONS = (
    "report.md",
    "工作区",
    "严禁编造",
    "未核验",
    "推断",
    "就近",
    "来源分层",
    "覆盖",
    "未涉及",
    "来源与核查",
    "PASS",
    "BLOCKED",
    "不等待用户确认",
    "磁盘复验",
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

#: Tool or agent names the templates must not name: they are not part of this
#: runtime's capability surface (no subagents, no Feynman alpha tools).
#: NOTE（GLINK-SUB-02，暂时保留）：任务 5（提示词改写为委派纪律）按 Lead 指令暂缓，
#: 所以仍按“运行期不向模型暴露委派工具名”的原口径断言；pi-subagents 已加载、前台守卫
#: 已生效（agent/extensions/），但主 agent 的工作约定还没改。恢复任务 5 时一并调整。
_FORBIDDEN_TEMPLATE_REFERENCES = ("subagent", "alpha_search", "memory_remember", "Task")

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


def test_contract_grades_work_before_starting_the_deep_flow() -> None:
    """A single fact is answered directly; only qualifying tasks get a report."""
    text = _contract_text()
    for anchor in _CONTRACT_GRADING_ANCHORS:
        assert anchor in text, anchor
    # The product regression behind this rule: asking for the weather must not
    # start a plan/report flow.
    assert "天气" in text
    # The deep flow is decidable from the task itself, not from habit.
    assert "专业判断" in text and "用户明确要求报告" in text
    # Multi-turn follow-up is incremental, not a full re-run of the research.
    assert "追问" in text and "增量" in text


def test_contract_keeps_the_deep_flow_obligations_without_a_gate() -> None:
    """The appended system prompt stays a single-agent contract with no gate."""
    text = _contract_text()
    for obligation in _DEEP_FLOW_OBLIGATIONS:
        assert obligation in text, obligation
    for phrase in _GATE_PHRASES:
        assert phrase not in text, phrase
    for forbidden in _FORBIDDEN_TEMPLATE_REFERENCES:
        assert forbidden not in text, forbidden
    # Assertion control names the banned comparatives explicitly.
    assert '"最优"' in text and '"最稳妥"' in text


def test_contract_and_template_only_name_tools_the_runtime_provides() -> None:
    """pi-glink ships pi's default toolset plus pi-web-access; nothing else."""
    text = _contract_text() + DEEPRESEARCH_PATH.read_text(encoding="utf-8")
    for tool in _AVAILABLE_TOOL_ANCHORS:
        assert tool in text, tool
    # File search and listing are shell work here, not tools: naming find/ls/glob
    # as tools would send the model after tools it cannot call.
    match = _UNAVAILABLE_TOOL_PATTERN.search(text)
    assert match is None, match.group(0)
    assert "`grep` 工具" not in text
    # The text commands the contract does name are explicitly delegated to bash.
    assert "用 `bash`" in text and "`rg`" in text and "`diff`" in text
    for forbidden in _FORBIDDEN_TEMPLATE_REFERENCES:
        assert forbidden not in text, forbidden


def test_researcher_definition_uses_only_the_runtime_tool_surface() -> None:
    """The researcher agent is the only delegated role, with an explicit allowlist.

    pi-subagents turns ``tools`` into ``--tools`` for the child process, so this
    list is what the subagent can actually call: pi-glink's built-ins plus
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
    for forbidden in (*_FORBIDDEN_TEMPLATE_REFERENCES, "verifier", "reviewer"):
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
    delivered = {entry for entry, _path, _is_dir in template_manifest(REPO_ROOT / "agent")}
    assert "agents/researcher.md" in delivered
    assert "extensions/subagent-foreground-guard.ts" in delivered
    assert "extensions/subagent/config.json" in delivered
    config = build_config(
        {
            "OPENAI_API_KEY": "sk-test-value",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "OPENAI_MODEL": "openai/gpt-5.6-sol",
            "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
            "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
            "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "operator-web-search.json"),
        },
        REPO_ROOT,
    )
    runtime_dir = tmp_path / "runs" / "run-1" / "pi-agent"
    runtime = prepare_agent_dir(
        config,
        runtime_dir=runtime_dir,
        session_dir=runtime_dir.parent / "sessions",
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


def test_deepresearch_template_forces_the_deep_flow_and_is_delivered() -> None:
    """`/deepresearch` is a prompt template: the file name becomes the command."""
    text = DEEPRESEARCH_PATH.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    frontmatter, _, body = text[4:].partition("\n---\n")
    assert "description:" in frontmatter
    # The borrowed skeleton is credited with its version and license.
    assert "feynman" in text and "0.3.48" in text and "MIT" in text
    assert "$@" in body
    # The explicit entry skips the fast path instead of leaving it to judgment.
    assert "强制" in body and "不适用" in body
    assert "report.md" in body
    for forbidden in _FORBIDDEN_TEMPLATE_REFERENCES:
        assert forbidden not in text, forbidden
    # The template travels into the run's PI_CODING_AGENT_DIR like every other
    # reviewed template entry.
    delivered = {entry for entry, _path, _is_dir in template_manifest(REPO_ROOT / "agent")}
    assert "prompts/deepresearch.md" in delivered


def test_prompt_templates_are_rendered_into_the_run_directory(tmp_path: Path) -> None:
    """The prompt template reaches the per-run directory, not only the manifest."""
    config = build_config(
        {
            "OPENAI_API_KEY": "sk-test-value",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "OPENAI_MODEL": "openai/gpt-5.6-sol",
            "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
            "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
            # Never fall back to the operator's own credential file in a test.
            "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "operator-web-search.json"),
        },
        REPO_ROOT,
    )
    runtime_dir = tmp_path / "runs" / "run-1" / "pi-agent"
    runtime = prepare_agent_dir(
        config,
        runtime_dir=runtime_dir,
        session_dir=runtime_dir.parent / "sessions",
    )
    rendered = runtime.directory / "prompts" / "deepresearch.md"
    assert rendered.read_text(encoding="utf-8") == DEEPRESEARCH_PATH.read_text(encoding="utf-8")
