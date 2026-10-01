""".env → frozen HostConfig/ExecutionSpec → real ACP new/resume lifecycle."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from glink_harness.config import ConfigError, build_config, load_config, validate_host_config
from glink_harness.events import EventBus
from glink_harness.runs.runner import RunManager
from glink_harness.store import FAILED, FINISHED, RunStore
from acp_pack import write_acp_pack
from test_kernel_acp_runner import acp_command, payloads, read_events, read_log

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = '["deepseek-official","deepseek-v4-flash"]'
TARGET_MODEL = '["openrouter","openai/gpt-6-sol"]'
MODEL_OPTIONS = [{
    "id": "model", "name": "Model", "category": "model", "type": "select",
    "currentValue": DEFAULT_MODEL,
    "options": [
        {"group": "deepseek-official", "name": "DeepSeek", "options": [{
            "value": DEFAULT_MODEL, "name": "Flash",
        }]},
        {"group": "openrouter", "name": "OpenRouter", "options": [{
            "value": TARGET_MODEL, "name": "GPT",
        }]},
    ],
}]
REASONING_OPTION = {
    "id": "reasoning_effort", "name": "Reasoning effort", "type": "select",
    "currentValue": "high", "options": [
        {"value": "high", "name": "High"}, {"value": "xhigh", "name": "Extra High"},
    ],
}
SOURCE_TABLE = '''[capabilities.session_config]
model = { source = "OPENAI_MODEL", encoding = "dsh-model" }
reasoning_effort = { source = "OPENAI_REASONING_EFFORT" }

'''


def wired_config(
    tmp_path: Path, settings: dict, *, env: dict | None = None,
    source_table: str = SOURCE_TABLE,
):
    command = acp_command(tmp_path, settings)
    packs = write_acp_pack(tmp_path, command)
    manifest = packs / "dsh" / "harness.toml"
    manifest.write_text(
        manifest.read_text().replace("[image]\n", source_table + "[image]\n"),
        encoding="utf-8",
    )
    dotenv = tmp_path / ".env"
    if not dotenv.exists():
        dotenv.write_text(
            "OPENAI_MODEL=openai/gpt-6-sol\nOPENAI_REASONING_EFFORT=xhigh\n",
            encoding="utf-8",
        )
    values = {
        "GLINK_HARNESS": "dsh", "GLINK_HARNESSES_DIR": str(packs),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
    }
    values.update(env or {})
    return load_config(values, REPO_ROOT, env_file=dotenv)


def test_dotenv_model_and_effort_reach_new_session_and_actual_events(tmp_path: Path) -> None:
    log = tmp_path / "agent.jsonl"
    config = wired_config(tmp_path, {
        "FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True,
        "FAKE_ACP_CONFIG_OPTIONS": MODEL_OPTIONS + [REASONING_OPTION],
    })
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    record = manager.start("用部署声明的 GPT 与 xhigh 写报告")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None and final.status == FINISHED
    ready = payloads(read_events(store, record.run_id), "session_ready")[0]
    assert {option["id"]: option["currentValue"] for option in ready["configOptions"]} == {
        "model": TARGET_MODEL, "reasoning_effort": "xhigh",
    }
    assert [(entry["configId"], entry["value"]) for entry in read_log(log)
            if entry["event"] == "config_option"] == [
        ("model", TARGET_MODEL), ("reasoning_effort", "xhigh"),
    ]
    assert record.spec.family == "acp" and record.spec.pack == "dsh"


@pytest.mark.parametrize("key", ["OPENAI_MODEL", "OPENAI_REASONING_EFFORT"])
def test_missing_declared_source_refuses_host_startup(tmp_path: Path, key: str) -> None:
    # Empty process value overrides a nonempty file value, not a fallback.
    with pytest.raises(ConfigError, match=key):
        wired_config(tmp_path, {}, env={key: ""})


def test_process_environment_overrides_dotenv_and_is_frozen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = wired_config(tmp_path, {})
    monkeypatch.setenv("GLINK_HARNESS", "dsh")
    monkeypatch.setenv("GLINK_HARNESSES_DIR", str(seed.harnesses_dir))
    monkeypatch.setenv("LOCAL_WEB_DATA_ROOT", str(tmp_path / "process-data"))
    monkeypatch.setenv("OPENAI_MODEL", "openrouter/openai/gpt-6-sol")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "high")
    config = load_config(repo_root=REPO_ROOT, env_file=tmp_path / ".env")
    monkeypatch.setenv("OPENAI_MODEL", "unavailable/model")
    assert config.session_config == (
        ("model", "openrouter/openai/gpt-6-sol", "dsh-model"),
        ("reasoning_effort", "high", "value"),
    )
    # Direct overrides must not bypass the startup gate either.
    from dataclasses import replace
    with pytest.raises(ConfigError, match="OPENAI_REASONING_EFFORT"):
        validate_host_config(replace(config, openai_reasoning_effort=""))


@pytest.mark.parametrize("pack", ["pi", "codex", "opencode"])
def test_unwired_packs_do_not_require_model_or_effort(pack: str) -> None:
    config = build_config({"GLINK_HARNESS": pack}, REPO_ROOT)
    assert config.session_config == ()


def test_repository_dsh_declares_sources_and_has_no_dead_model_default() -> None:
    config = build_config({
        "GLINK_HARNESS": "dsh", "OPENAI_MODEL": "openai/gpt-6-sol",
        "OPENAI_REASONING_EFFORT": "xhigh",
    }, REPO_ROOT)
    assert config.session_config == (
        ("model", "openai/gpt-6-sol", "dsh-model"),
        ("reasoning_effort", "xhigh", "value"),
    )
    text = (REPO_ROOT / "harnesses/dsh/home/.dsh/settings.yaml").read_text()
    assert "agent-default-model:" not in text
    assert "apiKeyEnv: OPENROUTER_API_KEY" in text


@pytest.mark.parametrize("mismatch", [False, True])
def test_resume_applies_current_dotenv_without_rewriting_parent_history(
    tmp_path: Path, mismatch: bool,
) -> None:
    log = tmp_path / "agent.jsonl"
    dotenv = tmp_path / ".env"
    dotenv.write_text("OPENAI_MODEL=deepseek-v4-flash\nOPENAI_REASONING_EFFORT=high\n")
    settings = {
        "FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True,
        "FAKE_ACP_CONFIG_OPTIONS": MODEL_OPTIONS + [REASONING_OPTION],
    }
    old_config = wired_config(tmp_path, settings)
    store = RunStore(old_config.data_root / "runs")
    old_manager = RunManager(old_config, EventBus(), store)
    parent = old_manager.start("首轮使用旧部署值")
    first = old_manager.wait(parent.run_id, timeout=30)
    assert first is not None and first.status == FINISHED
    parent_events = read_events(store, parent.run_id)
    dotenv.write_text("OPENAI_MODEL=openai/gpt-6-sol\nOPENAI_REASONING_EFFORT=xhigh\n")
    config = wired_config(tmp_path, {**settings, "FAKE_ACP_CONFIG_NO_CHANGE": mismatch})
    manager = RunManager(config, EventBus(), store)
    child = manager.start("追问应用当前部署", parent_run_id=parent.run_id)
    final = manager.wait(child.run_id, timeout=30)
    assert final is not None and final.status == (FAILED if mismatch else FINISHED)
    if mismatch:
        assert "currentValue" in (final.error or "")
        assert read_events(store, parent.run_id) == parent_events
        entries = read_log(log)
        resume = next(i for i, entry in enumerate(entries) if entry["event"] == "session_resume")
        assert not [entry for entry in entries[resume:] if entry["event"] == "prompt"]
        return
    ready = payloads(read_events(store, child.run_id), "session_ready")[0]
    assert ready["sessionId"] == first.session_id
    assert {option["id"]: option["currentValue"] for option in ready["configOptions"]} == {
        "model": TARGET_MODEL, "reasoning_effort": "xhigh",
    }
    entries = read_log(log)
    resume = next(i for i, entry in enumerate(entries) if entry["event"] == "session_resume")
    assert [(entry["configId"], entry["value"]) for entry in entries[resume:]
            if entry["event"] == "config_option"] == [
        ("model", TARGET_MODEL), ("reasoning_effort", "xhigh"),
    ]
    assert read_events(store, parent.run_id) == parent_events
    original = payloads(parent_events, "session_ready")[0]
    assert original["configOptions"][0]["currentValue"] == DEFAULT_MODEL


@pytest.mark.parametrize("scenario", [
    "missing_id", "missing_value", "mismatch", "missing_response", "missing_response_id",
    "missing_current", "reasoning_mismatch", "later_model_reset", "ambiguous_model",
    "unsupported_effort", "set_error",
])
def test_wired_session_fails_closed_before_prompt(tmp_path: Path, scenario: str) -> None:
    log = tmp_path / "agent.jsonl"
    options = copy.deepcopy(MODEL_OPTIONS + [REASONING_OPTION])
    settings = {"FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True,
                "FAKE_ACP_CONFIG_OPTIONS": options}
    if scenario == "missing_id":
        options.pop(0)
    elif scenario == "missing_value":
        options[0]["options"].pop()
    elif scenario == "mismatch":
        settings["FAKE_ACP_CONFIG_NO_CHANGE"] = True
    elif scenario in {"missing_response", "missing_response_id", "missing_current"}:
        response = {}
        if scenario == "missing_response_id":
            response = {"configOptions": [REASONING_OPTION]}
        elif scenario == "missing_current":
            model = copy.deepcopy(options[0])
            del model["currentValue"]
            response = {"configOptions": [model, REASONING_OPTION]}
        settings["FAKE_ACP_CONFIG_RESPONSES"] = {"model": response}
    elif scenario == "reasoning_mismatch":
        model = {**options[0], "currentValue": TARGET_MODEL}
        settings["FAKE_ACP_CONFIG_RESPONSES"] = {
            "reasoning_effort": {"configOptions": [model, REASONING_OPTION]},
        }
    elif scenario == "later_model_reset":
        effort = {**REASONING_OPTION, "currentValue": "xhigh"}
        settings["FAKE_ACP_CONFIG_RESPONSES"] = {
            "reasoning_effort": {"configOptions": [options[0], effort]},
        }
    elif scenario == "ambiguous_model":
        options[0]["options"].append({"group": "other", "name": "Other", "options": [{
            "value": '["other","openai/gpt-6-sol"]', "name": "Same model",
        }]})
    elif scenario == "unsupported_effort":
        options[1]["options"].pop()
    elif scenario == "set_error":
        settings["FAKE_ACP_CONFIG_ERROR"] = True
    config = wired_config(tmp_path, settings)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    run = manager.start("不允许声明与实际分离")
    final = manager.wait(run.run_id, timeout=30)
    assert final is not None and final.status == FAILED
    assert final.stop_reason == "execution_error"
    assert not payloads(read_events(store, run.run_id), "session_ready")
    assert not [entry for entry in read_log(log) if entry["event"] == "prompt"]


@pytest.mark.parametrize("already_current", [False, True])
def test_explicit_provider_route_disambiguates_and_current_value_is_idempotent(
    tmp_path: Path, already_current: bool,
) -> None:
    log = tmp_path / "agent.jsonl"
    options = copy.deepcopy(MODEL_OPTIONS + [REASONING_OPTION])
    options[0]["options"].append({"group": "other", "name": "Other", "options": [{
        "value": '["other","openai/gpt-6-sol"]', "name": "Same model",
    }]})
    if already_current:
        options[0]["currentValue"] = TARGET_MODEL
        options[1]["currentValue"] = "xhigh"
    config = wired_config(tmp_path, {
        "FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True,
        "FAKE_ACP_CONFIG_OPTIONS": options,
    }, env={"OPENAI_MODEL": "openrouter/openai/gpt-6-sol"})
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    run = manager.start("精确选择 OpenRouter 路由")
    final = manager.wait(run.run_id, timeout=30)
    assert final is not None and final.status == FINISHED
    mutations = [entry for entry in read_log(log) if entry["event"] == "config_option"]
    assert len(mutations) == (0 if already_current else 2)
    ready = payloads(read_events(store, run.run_id), "session_ready")[0]
    assert ready["configOptions"][0]["currentValue"] == TARGET_MODEL


@pytest.mark.parametrize("explicit_route", [False, True])
def test_model_uniqueness_uses_catalogue_beyond_display_limit(
    tmp_path: Path, explicit_route: bool,
) -> None:
    """B1：第 101 项同模型路由不可被显示上限隐去。"""
    log = tmp_path / "agent.jsonl"
    choices = [{"value": TARGET_MODEL, "name": "GPT"}]
    choices += [{"value": f'["openrouter","other/model-{i}"]', "name": f"Other {i}"}
                for i in range(99)]
    choices.append({"value": '["other","openai/gpt-6-sol"]', "name": "Same model"})
    config = wired_config(tmp_path, {
        "FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True,
        "FAKE_ACP_CONFIG_OPTIONS": [{**MODEL_OPTIONS[0], "options": choices}, REASONING_OPTION],
    }, env={"OPENAI_MODEL": "openrouter/openai/gpt-6-sol"} if explicit_route else {})
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    run = manager.start("完整广告中才可判断模型唯一性")
    final = manager.wait(run.run_id, timeout=30)
    assert final is not None and final.status == (FINISHED if explicit_route else FAILED)
    if explicit_route:
        ready = payloads(read_events(store, run.run_id), "session_ready")[0]
        assert ready["configOptions"][0]["currentValue"] == TARGET_MODEL
        assert len(ready["configOptions"][0]["options"]) == 100
    else:
        assert "匹配 2 个" in (final.error or "")
        assert not [entry for entry in read_log(log) if entry["event"] == "prompt"]


@pytest.mark.parametrize("phase", ["initial", "initial_beyond_display_limit", "model_response", "effort_response"])
@pytest.mark.parametrize("duplicate_id", ["model", "reasoning_effort"])
def test_declared_config_id_must_be_unique_in_every_advertisement(
    tmp_path: Path, phase: str, duplicate_id: str,
) -> None:
    """B2：不能挑重复 id 的首项来证明实际配置。"""
    log = tmp_path / "agent.jsonl"
    actual_model = {**MODEL_OPTIONS[0], "currentValue": TARGET_MODEL}
    duplicated = copy.deepcopy([actual_model, REASONING_OPTION])
    extra = copy.deepcopy(actual_model if duplicate_id == "model" else REASONING_OPTION)
    extra["currentValue"] = DEFAULT_MODEL if duplicate_id == "model" else "xhigh"
    if phase == "initial_beyond_display_limit":
        duplicated += [{"id": f"unused-{i}", "name": "Unused", "type": "text"}
                       for i in range(20)]
        duplicated.append(extra)
    else:
        duplicated.insert(1, extra)
    settings = {"FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True}
    if phase in {"initial", "initial_beyond_display_limit"}:
        settings["FAKE_ACP_CONFIG_OPTIONS"] = duplicated
    else:
        settings["FAKE_ACP_CONFIG_OPTIONS"] = MODEL_OPTIONS + [REASONING_OPTION]
        mutated_id = "model" if phase == "model_response" else "reasoning_effort"
        if phase == "effort_response":
            for option in duplicated:
                if option["id"] == "reasoning_effort":
                    option["currentValue"] = "xhigh"
        settings["FAKE_ACP_CONFIG_RESPONSES"] = {
            mutated_id: {"configOptions": duplicated},
        }
    config = wired_config(tmp_path, settings)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    run = manager.start("重复配置 id 必须失败")
    final = manager.wait(run.run_id, timeout=30)
    assert final is not None and final.status == FAILED
    assert duplicate_id in (final.error or "") and "唯一" in (final.error or "")
    assert not [entry for entry in read_log(log) if entry["event"] == "prompt"]


@pytest.mark.parametrize("valid_json_route", [False, True])
def test_non_json_route_siblings_cannot_supply_or_hide_a_valid_route(
    tmp_path: Path, valid_json_route: bool,
) -> None:
    """只接受可解析的路由：全非 JSON 失败，混合唯一合法路由可用。"""
    log = tmp_path / "agent.jsonl"
    choices = [{"value": "not JSON", "name": "unsupported sibling"}]
    if valid_json_route:
        choices.append({"value": TARGET_MODEL, "name": "GPT"})
    config = wired_config(tmp_path, {
        "FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True,
        "FAKE_ACP_CONFIG_OPTIONS": [{**MODEL_OPTIONS[0], "options": choices}, REASONING_OPTION],
    })
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    run = manager.start("非 JSON 旁项不参与路由身份匹配")
    final = manager.wait(run.run_id, timeout=30)
    assert final is not None and final.status == (FINISHED if valid_json_route else FAILED)
    if valid_json_route:
        ready = payloads(read_events(store, run.run_id), "session_ready")[0]
        assert ready["configOptions"][0]["currentValue"] == TARGET_MODEL
        assert ready["configOptions"][0]["options"][0]["value"] == "not JSON"
    else:
        assert "匹配 0 个" in (final.error or "")
        assert not [entry for entry in read_log(log) if entry["event"] == "prompt"]


@pytest.mark.parametrize("option_type", ["text", None])
@pytest.mark.parametrize("phase", ["initial", "effort_response"])
def test_applied_options_require_explicit_select_type_and_advertised_value(
    tmp_path: Path, option_type: str | None, phase: str,
) -> None:
    """B3：非 select 或缺 type 不能绕过广告集校验。"""
    log = tmp_path / "agent.jsonl"
    effort = copy.deepcopy(REASONING_OPTION)
    if option_type is None:
        del effort["type"]
    else:
        effort["type"] = option_type
        del effort["options"]
    settings = {"FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True}
    if phase == "initial":
        settings["FAKE_ACP_CONFIG_OPTIONS"] = MODEL_OPTIONS + [effort]
    else:
        effort["currentValue"] = "xhigh"
        settings["FAKE_ACP_CONFIG_OPTIONS"] = MODEL_OPTIONS + [REASONING_OPTION]
        settings["FAKE_ACP_CONFIG_RESPONSES"] = {"reasoning_effort": {"configOptions": [
            {**MODEL_OPTIONS[0], "currentValue": TARGET_MODEL}, effort,
        ]}}
    config = wired_config(tmp_path, settings)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    run = manager.start("应用选项类型必须受支持且显式声明")
    final = manager.wait(run.run_id, timeout=30)
    assert final is not None and final.status == FAILED
    assert "type" in (final.error or "")
    entries = read_log(log)
    assert not [entry for entry in entries if entry["event"] == "prompt"]
    if phase == "initial":
        assert not [entry for entry in entries
                    if entry["event"] == "config_option" and entry["configId"] == "reasoning_effort"]


@pytest.mark.parametrize("effort_first", [False, True])
def test_reasoning_choices_are_read_after_model_switch(
    tmp_path: Path, effort_first: bool,
) -> None:
    """B4：合法来源表任意行序，都先模型、后 effort。"""
    source_table = '''[capabilities.session_config]
reasoning_effort = { source = "OPENAI_REASONING_EFFORT" }
model = { source = "OPENAI_MODEL", encoding = "dsh-model" }

''' if effort_first else SOURCE_TABLE
    log = tmp_path / "agent.jsonl"
    target_options = [{**MODEL_OPTIONS[0], "currentValue": TARGET_MODEL}, REASONING_OPTION]
    config = wired_config(tmp_path, {
        "FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True,
        # The fallback model advertises no reasoning knob; target model does.
        "FAKE_ACP_CONFIG_OPTIONS": MODEL_OPTIONS,
        "FAKE_ACP_CONFIG_RESPONSES": {"model": {"configOptions": target_options}},
    }, source_table=source_table)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    run = manager.start("模型切换后才有 reasoning 选项")
    final = manager.wait(run.run_id, timeout=30)
    assert final is not None and final.status == FINISHED
    ready = payloads(read_events(store, run.run_id), "session_ready")[0]
    assert ready["configOptions"][1]["currentValue"] == "xhigh"
    assert [(entry["configId"], entry["value"]) for entry in read_log(log)
            if entry["event"] == "config_option"] == [
        ("model", TARGET_MODEL), ("reasoning_effort", "xhigh"),
    ]
