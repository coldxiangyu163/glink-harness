""".env → frozen HostConfig/ExecutionSpec → real ACP new/resume lifecycle."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from pi_glink.config import ConfigError, build_config, load_config, validate_host_config
from pi_glink.events import EventBus
from pi_glink.runs.runner import RunManager
from pi_glink.store import FAILED, FINISHED, RunStore
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


def wired_config(tmp_path: Path, settings: dict, *, env: dict | None = None):
    command = acp_command(tmp_path, settings)
    packs = write_acp_pack(tmp_path, command)
    manifest = packs / "dsh" / "harness.toml"
    manifest.write_text(
        manifest.read_text().replace("[image]\n", SOURCE_TABLE + "[image]\n"),
        encoding="utf-8",
    )
    dotenv = tmp_path / ".env"
    if not dotenv.exists():
        dotenv.write_text(
            "OPENAI_MODEL=openai/gpt-6-sol\nOPENAI_REASONING_EFFORT=xhigh\n",
            encoding="utf-8",
        )
    values = {
        "PI_GLINK_HARNESS": "dsh", "PI_GLINK_HARNESSES_DIR": str(packs),
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
    monkeypatch.setenv("PI_GLINK_HARNESS", "dsh")
    monkeypatch.setenv("PI_GLINK_HARNESSES_DIR", str(seed.harnesses_dir))
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
    config = build_config({"PI_GLINK_HARNESS": pack}, REPO_ROOT)
    assert config.session_config == ()


def test_repository_dsh_declares_sources_and_has_no_dead_model_default() -> None:
    config = build_config({
        "PI_GLINK_HARNESS": "dsh", "OPENAI_MODEL": "openai/gpt-6-sol",
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


def test_reasoning_choices_are_read_after_model_switch(tmp_path: Path) -> None:
    log = tmp_path / "agent.jsonl"
    target_options = [{**MODEL_OPTIONS[0], "currentValue": TARGET_MODEL}, REASONING_OPTION]
    config = wired_config(tmp_path, {
        "FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True,
        # The fallback model advertises no reasoning knob; target model does.
        "FAKE_ACP_CONFIG_OPTIONS": MODEL_OPTIONS,
        "FAKE_ACP_CONFIG_RESPONSES": {"model": {"configOptions": target_options}},
    })
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    run = manager.start("模型切换后才有 reasoning 选项")
    final = manager.wait(run.run_id, timeout=30)
    assert final is not None and final.status == FINISHED
    ready = payloads(read_events(store, run.run_id), "session_ready")[0]
    assert ready["configOptions"][1]["currentValue"] == "xhigh"
