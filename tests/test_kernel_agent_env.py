"""Regressions for the runtime pi configuration directory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pi_glink.config import ConfigError, build_config
from pi_glink.kernel.agent_env import (
    AUTH_FILENAME,
    PROVIDER_ID,
    WEB_SEARCH_FILENAME,
    build_pi_argv,
    build_pi_env,
    prepare_agent_dir,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def template_dir(tmp_path: Path) -> Path:
    template = tmp_path / "agent-template"
    (template / "extensions").mkdir(parents=True)
    (template / "settings.json").write_text('{"defaultThinkingLevel": "high"}\n', encoding="utf-8")
    (template / "AGENTS.md").write_text("# research agent\n", encoding="utf-8")
    (template / "extensions" / "hello.js").write_text("export default () => {};\n", encoding="utf-8")
    return template


def _config(tmp_path: Path, template_dir: Path, **overrides: str):
    values = {
        "OPENAI_API_KEY": "sk-secret-value",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "openai/gpt-5.6-sol",
        "PI_CODING_AGENT_DIR": str(template_dir),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


def test_models_json_uses_literal_endpoint_and_model(tmp_path: Path, template_dir: Path) -> None:
    config = _config(tmp_path, template_dir)
    runtime = prepare_agent_dir(config)
    document = json.loads((runtime.directory / "models.json").read_text(encoding="utf-8"))
    provider = document["providers"][PROVIDER_ID]
    assert provider["baseUrl"] == "https://example.invalid/v1"
    assert provider["api"] == "openai-completions"
    model = provider["models"][0]
    assert model["id"] == "openai/gpt-5.6-sol"
    assert model["reasoning"] is True
    assert model["contextWindow"] == config.context_window
    assert model["maxTokens"] == config.max_output_tokens
    assert runtime.model_ref == f"{PROVIDER_ID}/openai/gpt-5.6-sol"


def test_thinking_format_is_openrouter_style(tmp_path: Path, template_dir: Path) -> None:
    """Without this compat entry pi sends no thinking parameter at all.

    See docs/pi-integration-notes.md §2: the request must carry
    ``reasoning: {effort}`` for the upstream to stream reasoning back.
    """
    config = _config(tmp_path, template_dir)
    runtime = prepare_agent_dir(config)
    document = json.loads((runtime.directory / "models.json").read_text(encoding="utf-8"))
    compat = document["providers"][PROVIDER_ID]["models"][0]["compat"]
    assert compat["thinkingFormat"] == "openrouter"
    assert compat["supportsReasoningEffort"] is True


def test_model_credential_goes_to_auth_json_not_the_environment(
    tmp_path: Path, template_dir: Path
) -> None:
    config = _config(tmp_path, template_dir)
    runtime = prepare_agent_dir(config)

    # models.json carries neither the key nor a placeholder for it…
    rendered = (runtime.directory / "models.json").read_text(encoding="utf-8")
    assert "sk-secret-value" not in rendered
    assert "$OPENAI_API_KEY" not in rendered

    # …the key lives in pi's own credential store, mode 0600…
    auth = runtime.directory / AUTH_FILENAME
    payload = json.loads(auth.read_text(encoding="utf-8"))
    assert payload == {PROVIDER_ID: {"type": "api_key", "key": "sk-secret-value"}}
    assert auth.stat().st_mode & 0o777 == 0o600

    # …and the kernel process environment does not contain it at all.
    env = build_pi_env(config, runtime, base_env={"PATH": "/usr/bin"})
    assert "OPENAI_API_KEY" not in env


def test_template_entries_are_copied_next_to_generated_models(tmp_path: Path, template_dir: Path) -> None:
    config = _config(tmp_path, template_dir)
    runtime = prepare_agent_dir(config)
    assert (runtime.directory / "settings.json").is_file()
    assert (runtime.directory / "AGENTS.md").is_file()
    assert (runtime.directory / "extensions" / "hello.js").is_file()


def test_missing_endpoint_or_model_is_refused(tmp_path: Path, template_dir: Path) -> None:
    with pytest.raises(ConfigError, match="OPENAI_BASE_URL"):
        prepare_agent_dir(_config(tmp_path, template_dir, OPENAI_BASE_URL=""))
    with pytest.raises(ConfigError, match="OPENAI_MODEL"):
        prepare_agent_dir(_config(tmp_path, template_dir, OPENAI_MODEL=""))


def test_argv_and_env_redirect_pi_to_the_runtime_directory(tmp_path: Path, template_dir: Path) -> None:
    config = _config(tmp_path, template_dir)
    runtime = prepare_agent_dir(config)
    argv = build_pi_argv(config, runtime, extra=["--no-session"])
    assert argv[:2] == [config.pi_bin, "--mode"]
    assert argv[2] == "rpc"
    assert "--provider" in argv and PROVIDER_ID in argv
    assert "--model" in argv and "openai/gpt-5.6-sol" in argv
    assert argv[-1] == "--no-session"

    env = build_pi_env(config, runtime, base_env={"PATH": "/usr/bin"})
    assert env["PI_CODING_AGENT_DIR"] == str(runtime.directory)
    assert env["PI_CODING_AGENT_SESSION_DIR"] == str(config.session_dir)
    assert "OPENAI_API_KEY" not in env, "the model key travels via auth.json, not the environment"
    assert env["PATH"] == "/usr/bin"


def test_config_repr_does_not_leak_the_key(tmp_path: Path, template_dir: Path) -> None:
    config = _config(tmp_path, template_dir)
    assert "sk-secret-value" not in repr(config)
    assert config.has_openai_key is True


# -- pi-web-access credentials ------------------------------------------------


def test_web_search_key_is_rendered_into_the_runtime_directory(
    tmp_path: Path, template_dir: Path
) -> None:
    config = _config(tmp_path, template_dir, PI_GLINK_WEB_SEARCH_KEY="exa-secret")
    runtime = prepare_agent_dir(config)
    payload = json.loads((runtime.directory / WEB_SEARCH_FILENAME).read_text(encoding="utf-8"))
    assert payload == {"exaApiKey": "exa-secret"}
    assert (runtime.directory / WEB_SEARCH_FILENAME).stat().st_mode & 0o777 == 0o600
    assert config.has_web_search_key is True


def test_operator_web_search_file_is_copied_when_no_env_key(
    tmp_path: Path, template_dir: Path
) -> None:
    source = tmp_path / "operator-web-search.json"
    source.write_text('{"braveApiKey": "BSA_operator"}\n', encoding="utf-8")
    config = _config(
        tmp_path, template_dir, PI_GLINK_WEB_SEARCH_CONFIG=str(source)
    )
    runtime = prepare_agent_dir(config)
    copied = json.loads((runtime.directory / WEB_SEARCH_FILENAME).read_text(encoding="utf-8"))
    assert copied == {"braveApiKey": "BSA_operator"}


def test_missing_web_search_credential_is_not_an_error(
    tmp_path: Path, template_dir: Path
) -> None:
    config = _config(
        tmp_path, template_dir, PI_GLINK_WEB_SEARCH_CONFIG=str(tmp_path / "absent.json")
    )
    runtime = prepare_agent_dir(config)
    # Keyless pi-web-access (Exa MCP) stays available; the run contract does not
    # require a credential for a working search.
    assert not (runtime.directory / WEB_SEARCH_FILENAME).exists()
    assert config.has_web_search_key is False


def test_template_settings_declare_the_web_access_package() -> None:
    settings = json.loads((REPO_ROOT / "agent" / "settings.json").read_text(encoding="utf-8"))
    assert "npm:pi-web-access" in settings["packages"]
    assert settings["defaultThinkingLevel"] == "medium"
