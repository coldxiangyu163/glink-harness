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
    template_manifest,
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
        # Never fall back to the operator's own credential file in a test.
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "operator-web-search.json"),
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


def _prepare(config, tmp_path: Path, name: str = "run-1"):
    """Render a per-run runtime directory (the only supported call shape)."""
    run_dir = tmp_path / "runs" / name
    return prepare_agent_dir(
        config,
        runtime_dir=run_dir / "pi-agent",
        session_dir=run_dir / "sessions",
    )


def test_models_json_uses_literal_endpoint_and_model(tmp_path: Path, template_dir: Path) -> None:
    config = _config(tmp_path, template_dir)
    runtime = _prepare(config, tmp_path)
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
    runtime = _prepare(config, tmp_path)
    document = json.loads((runtime.directory / "models.json").read_text(encoding="utf-8"))
    compat = document["providers"][PROVIDER_ID]["models"][0]["compat"]
    assert compat["thinkingFormat"] == "openrouter"
    assert compat["supportsReasoningEffort"] is True


def test_models_json_does_not_fabricate_a_price_table(
    tmp_path: Path, template_dir: Path
) -> None:
    """No price is known, so none is claimed: pi reports only token counters."""
    config = _config(tmp_path, template_dir)
    runtime = _prepare(config, tmp_path)
    document = json.loads((runtime.directory / "models.json").read_text(encoding="utf-8"))
    model = document["providers"][PROVIDER_ID]["models"][0]
    assert "cost" not in model


def test_model_credential_goes_to_auth_json_not_the_environment(
    tmp_path: Path, template_dir: Path
) -> None:
    config = _config(tmp_path, template_dir)
    runtime = _prepare(config, tmp_path)

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
    runtime = _prepare(config, tmp_path)
    assert (runtime.directory / "settings.json").is_file()
    assert (runtime.directory / "AGENTS.md").is_file()
    assert (runtime.directory / "extensions" / "hello.js").is_file()


def test_two_runs_render_into_separate_directories(tmp_path: Path, template_dir: Path) -> None:
    """Per-run configuration: no run may write into another run's runtime dir."""
    config = _config(tmp_path, template_dir)
    first = _prepare(config, tmp_path, "run-a")
    second = _prepare(config, tmp_path, "run-b")
    assert first.directory != second.directory
    assert first.session_dir != second.session_dir
    (first.directory / "extensions" / "only-first.js").write_text("x\n", encoding="utf-8")
    assert not (second.directory / "extensions" / "only-first.js").exists()


def test_removed_template_entry_and_credential_do_not_survive_a_rerender(
    tmp_path: Path, template_dir: Path
) -> None:
    """The old merge-copy kept stale extensions and credentials; replacing does not."""
    config = _config(tmp_path, template_dir, PI_GLINK_WEB_SEARCH_KEY="exa-secret")
    run_dir = tmp_path / "runs" / "run-1"
    runtime = prepare_agent_dir(
        config, runtime_dir=run_dir / "pi-agent", session_dir=run_dir / "sessions"
    )
    assert (runtime.directory / "extensions" / "hello.js").is_file()
    assert (runtime.directory / WEB_SEARCH_FILENAME).is_file()

    # The template loses its extension and the configuration loses the key.
    (template_dir / "extensions" / "hello.js").unlink()
    template_dir.joinpath("extensions").rmdir()
    bare = _config(tmp_path, template_dir)
    rerendered = prepare_agent_dir(
        bare,
        runtime_dir=run_dir / "pi-agent",
        session_dir=run_dir / "sessions",
    )
    assert not (rerendered.directory / "extensions").exists()
    assert not (rerendered.directory / WEB_SEARCH_FILENAME).exists()


def test_missing_endpoint_or_model_is_refused(tmp_path: Path, template_dir: Path) -> None:
    with pytest.raises(ConfigError, match="OPENAI_BASE_URL"):
        _prepare(_config(tmp_path, template_dir, OPENAI_BASE_URL=""), tmp_path)
    with pytest.raises(ConfigError, match="OPENAI_MODEL"):
        _prepare(_config(tmp_path, template_dir, OPENAI_MODEL=""), tmp_path)


def test_argv_and_env_redirect_pi_to_the_runtime_directory(tmp_path: Path, template_dir: Path) -> None:
    config = _config(tmp_path, template_dir)
    runtime = _prepare(config, tmp_path)
    argv = build_pi_argv(config, runtime, extra=["--no-session"])
    assert argv[:2] == [config.pi_bin, "--mode"]
    assert argv[2] == "rpc"
    assert "--provider" in argv and PROVIDER_ID in argv
    assert "--model" in argv and "openai/gpt-5.6-sol" in argv
    assert argv[-1] == "--no-session"

    env = build_pi_env(config, runtime, base_env={"PATH": "/usr/bin"})
    assert env["PI_CODING_AGENT_DIR"] == str(runtime.directory)
    assert env["PI_CODING_AGENT_SESSION_DIR"] == str(runtime.session_dir)
    assert str(config.session_dir) not in env["PI_CODING_AGENT_SESSION_DIR"], (
        "the session directory is the run's own, not the shared base"
    )
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
    runtime = _prepare(config, tmp_path)
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
    runtime = _prepare(config, tmp_path)
    copied = json.loads((runtime.directory / WEB_SEARCH_FILENAME).read_text(encoding="utf-8"))
    assert copied == {"braveApiKey": "BSA_operator"}


def test_missing_web_search_credential_is_not_an_error(
    tmp_path: Path, template_dir: Path
) -> None:
    config = _config(
        tmp_path, template_dir, PI_GLINK_WEB_SEARCH_CONFIG=str(tmp_path / "absent.json")
    )
    runtime = _prepare(config, tmp_path)
    # Keyless pi-web-access (Exa MCP) stays available; the run contract does not
    # require a credential for a working search.
    assert not (runtime.directory / WEB_SEARCH_FILENAME).exists()
    assert config.has_web_search_key is False


def test_template_manifest_lists_the_reviewable_entries(tmp_path: Path, template_dir: Path) -> None:
    """All three faces deliver the same manifest (AGENTS/settings/extensions/…)."""
    manifest = template_manifest(template_dir)
    relative = {entry for entry, _path, _is_dir in manifest}
    assert {"settings.json", "AGENTS.md", "extensions", "extensions/hello.js"} <= relative
    # A symlink in the template is never delivered as a path into the host.
    (template_dir / "prompts").mkdir()
    (template_dir / "prompts" / "escape.md").symlink_to(tmp_path / "outside.md")
    manifest = template_manifest(template_dir)
    assert "prompts/escape.md" not in {entry for entry, _p, _d in manifest}


def test_template_settings_declare_the_web_access_package() -> None:
    settings = json.loads((REPO_ROOT / "agent" / "settings.json").read_text(encoding="utf-8"))
    assert "npm:pi-web-access" in settings["packages"]
    assert settings["defaultThinkingLevel"] == "medium"


#: Obligations the appended research contract must keep carrying (anchors, not
#: exact wording) after the discipline rewrite.
_RESEARCH_PROMPT_OBLIGATIONS = (
    "report.md",
    "工作区",
    "严禁编造",
    "未核验",
    "推断",
    "就近",
    "来源分层",
    "覆盖",
    "未涉及",
    "不等待用户确认",
    "来源与核查",
    "PASS",
    "BLOCKED",
)

#: Confirmation-gate phrasings the research prompt must not contain: a run has
#: no interactive user, so a gate would stall the only delivery path.
_RESEARCH_PROMPT_GATE_PHRASES = (
    "Proceed with this",
    'Reply "yes"',
    "请确认计划",
    "确认后再继续",
    "先征求用户同意",
    "等待用户确认后",
)

#: Tool or agent names the templates must not name: they are not part of this
#: runtime's capability surface (no subagents, no Feynman alpha tools).
_FORBIDDEN_TEMPLATE_REFERENCES = ("subagent", "alpha_search", "memory_remember", "Task")


def test_research_prompt_keeps_the_obligations_without_a_confirmation_gate() -> None:
    """The appended system prompt stays a single-agent contract with no gate."""
    text = (REPO_ROOT / "agent" / "research-prompt.md").read_text(encoding="utf-8")
    for obligation in _RESEARCH_PROMPT_OBLIGATIONS:
        assert obligation in text, obligation
    for phrase in _RESEARCH_PROMPT_GATE_PHRASES:
        assert phrase not in text, phrase
    for forbidden in _FORBIDDEN_TEMPLATE_REFERENCES:
        assert forbidden not in text, forbidden
    # Assertion control names the banned comparatives explicitly.
    assert '"最优"' in text and '"最稳妥"' in text


def test_deepresearch_template_is_delivered_with_no_missing_tool_reference() -> None:
    """`/deepresearch` is a prompt template: the file name becomes the command."""
    path = REPO_ROOT / "agent" / "prompts" / "deepresearch.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    frontmatter, _, body = text[4:].partition("\n---\n")
    assert "description:" in frontmatter
    # The borrowed skeleton is credited with its version and license.
    assert "feynman" in text and "0.3.48" in text and "MIT" in text
    assert "$@" in body
    for forbidden in _FORBIDDEN_TEMPLATE_REFERENCES:
        assert forbidden not in text, forbidden
    # The template travels into the run's PI_CODING_AGENT_DIR like every other
    # reviewed template entry.
    delivered = {entry for entry, _path, _is_dir in template_manifest(REPO_ROOT / "agent")}
    assert "prompts/deepresearch.md" in delivered
