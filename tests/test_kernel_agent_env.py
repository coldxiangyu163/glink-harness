"""Regressions for the runtime pi configuration directory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pi_glink.config import ConfigError, build_config
from pi_glink.kernel.agent_env import (
    AUTH_FILENAME,
    IMAGE_PACKAGE_NODE_MODULES,
    PROVIDER_ID,
    WEB_SEARCH_FILENAME,
    build_pi_argv,
    build_pi_env,
    local_package_source,
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
    """All three faces deliver the same manifest (AGENTS/settings/agents/extensions/…)."""
    (template_dir / "agents").mkdir()
    (template_dir / "agents" / "researcher.md").write_text("---\nname: researcher\n---\n", encoding="utf-8")
    manifest = template_manifest(template_dir)
    relative = {entry for entry, _path, _is_dir in manifest}
    assert {
        "settings.json",
        "AGENTS.md",
        "agents",
        "agents/researcher.md",
        "extensions",
        "extensions/hello.js",
    } <= relative
    # A symlink in the template is never delivered as a path into the host.
    (template_dir / "extensions" / "escape.js").symlink_to(tmp_path / "outside.js")
    manifest = template_manifest(template_dir)
    assert "extensions/escape.js" not in {entry for entry, _p, _d in manifest}


def test_agents_directory_is_rendered_into_the_run_configuration(
    tmp_path: Path, template_dir: Path
) -> None:
    """pi-subagents discovers user-scope agents in ``PI_CODING_AGENT_DIR/agents``.

    Without this template entry the rendered run directory would have no
    ``researcher.md`` and the dispatch tool would fall back to the package's own
    builtin agents (or to user-scope ``~/.agents`` files outside the run).
    """
    (template_dir / "agents").mkdir()
    (template_dir / "agents" / "researcher.md").write_text("# researcher\n", encoding="utf-8")
    config = _config(tmp_path, template_dir)
    runtime = _prepare(config, tmp_path)
    assert (runtime.directory / "agents" / "researcher.md").read_text(encoding="utf-8") == "# researcher\n"


def test_template_settings_declare_pinned_packages_and_quiet_subagent_surfaces() -> None:
    """模板声明的是精确版本，并且把子代理面固定在最保守取值。

    原义务（GLINK-SUB-01）：模板只加载 pi-web-access，pi-subagents 只预装不加载。
    GLINK-SUB-02 改为加载 pi-subagents，并用工作台自有的前台守卫扩展兜住 async；
    模板因此同时声明两个精确 pin，并在同一份 settings.json 里关掉 watchdog——
    它由 pi-subagents 从 ``subagents.watchdog`` 读取（src/watchdog/settings.ts）。
    版本一致性与镜像 ARG 的对应由 test_repository_boundaries.py 卡住。
    """
    settings = json.loads((REPO_ROOT / "harnesses" / "pi" / "settings.json").read_text(encoding="utf-8"))
    assert settings["packages"] == ["npm:pi-web-access@0.31.0", "npm:pi-subagents@0.40.0"]
    assert settings["defaultThinkingLevel"] == "medium"
    watchdog = settings["subagents"]["watchdog"]
    assert watchdog["enabled"] is False
    assert watchdog["main"]["enabled"] is False
    assert watchdog["children"]["enabled"] is False


# -- 容器面预装包：镜像内本地加载，不在 run 里联网安装（GLINK-PKG-01） ----------


def _template_settings(template_dir: Path, packages: list[object]) -> None:
    (template_dir / "settings.json").write_text(
        json.dumps(
            {"defaultThinkingLevel": "medium", "packages": packages},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _rendered_settings(config, tmp_path: Path) -> dict:
    runtime = _prepare(config, tmp_path)
    return json.loads((runtime.directory / "settings.json").read_text(encoding="utf-8"))


def test_container_faces_load_the_prebaked_packages_from_the_image(
    tmp_path: Path, template_dir: Path
) -> None:
    """Docker/E2B 的 packages 指向镜像内路径，pi 直接加载（不再每 run npm install）。"""
    _template_settings(template_dir, ["npm:pi-web-access@0.31.0", "npm:pi-subagents@0.40.0"])
    expected = [
        f"{IMAGE_PACKAGE_NODE_MODULES}/pi-web-access",
        f"{IMAGE_PACKAGE_NODE_MODULES}/pi-subagents",
    ]
    for executor in ("docker", "e2b"):
        config = _config(tmp_path, template_dir, LOCAL_WEB_EXECUTOR=executor)
        settings = _rendered_settings(config, tmp_path)
        assert settings["packages"] == expected
        # 其他设置项原样保留，只改 packages。
        assert settings["defaultThinkingLevel"] == "medium"


def test_inproc_face_keeps_the_pinned_npm_source(tmp_path: Path, template_dir: Path) -> None:
    """本机 face 行为不变：仍是 pip 式的运行期安装，只是版本被钉住。"""
    _template_settings(template_dir, ["npm:pi-web-access@0.31.0"])
    config = _config(tmp_path, template_dir)  # 默认 executor=inproc
    settings = _rendered_settings(config, tmp_path)
    assert settings["packages"] == ["npm:pi-web-access@0.31.0"]


def test_container_faces_leave_unmapped_entries_untouched(
    tmp_path: Path, template_dir: Path
) -> None:
    """只改写预装包；其他来源原样保留，不猜、不静默改写。"""
    _template_settings(
        template_dir,
        ["npm:pi-web-access@0.31.0", "npm:other-tools@1.0.0", "./local-tools", "git:github.com/x/y@v1"],
    )
    config = _config(tmp_path, template_dir, LOCAL_WEB_EXECUTOR="docker")
    settings = _rendered_settings(config, tmp_path)
    assert settings["packages"] == [
        f"{IMAGE_PACKAGE_NODE_MODULES}/pi-web-access",
        "npm:other-tools@1.0.0",
        "./local-tools",
        "git:github.com/x/y@v1",
    ]


def test_container_faces_keep_the_object_form_filters(tmp_path: Path, template_dir: Path) -> None:
    """对象形式的 source 同样映射到镜像内路径，过滤字段不动。"""
    _template_settings(
        template_dir,
        [{"source": "npm:pi-web-access@0.31.0", "skills": []}],
    )
    config = _config(tmp_path, template_dir, LOCAL_WEB_EXECUTOR="e2b")
    settings = _rendered_settings(config, tmp_path)
    assert settings["packages"] == [
        {"source": f"{IMAGE_PACKAGE_NODE_MODULES}/pi-web-access", "skills": []}
    ]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("npm:pi-web-access", f"{IMAGE_PACKAGE_NODE_MODULES}/pi-web-access"),
        ("npm:pi-web-access@0.31.0", f"{IMAGE_PACKAGE_NODE_MODULES}/pi-web-access"),
        ("npm:pi-web-access@^0.31.0", f"{IMAGE_PACKAGE_NODE_MODULES}/pi-web-access"),
        ("npm:pi-subagents@0.40.0", f"{IMAGE_PACKAGE_NODE_MODULES}/pi-subagents"),
        ("npm:other-tools@1.0.0", None),
        ("npm:@scope/tools@1.0.0", None),
        ("git:github.com/x/y@v1", None),
        ("./local-tools", None),
        ("/opt/pi-packages/node_modules/pi-web-access", None),
    ],
)
def test_local_package_source_only_maps_prebaked_names(source: str, expected: str | None) -> None:
    assert local_package_source(source) == expected


def test_container_faces_refuse_a_malformed_settings_file(
    tmp_path: Path, template_dir: Path
) -> None:
    """坏配置明确失败，而不是让容器静默以降级配置启动。"""
    (template_dir / "settings.json").write_text("{not json\n", encoding="utf-8")
    config = _config(tmp_path, template_dir, LOCAL_WEB_EXECUTOR="docker")
    with pytest.raises(ConfigError, match="settings.json"):
        _prepare(config, tmp_path)


def test_missing_settings_file_is_not_an_error(tmp_path: Path, template_dir: Path) -> None:
    """模板没有 settings.json 时没有可改写的 packages，渲染照常完成。"""
    (template_dir / "settings.json").unlink()
    config = _config(tmp_path, template_dir, LOCAL_WEB_EXECUTOR="e2b")
    runtime = _prepare(config, tmp_path)
    assert not (runtime.directory / "settings.json").exists()


def test_repository_template_declares_only_prebaked_packages() -> None:
    """模板里的每个 npm 包都必须能在容器面映射到镜像内副本。

    否则容器面会退回运行期 npm install —— 这正是本次要消除的每 run 延迟。
    版本 pin 的跨文件一致性在 test_repository_boundaries.py。
    """
    settings = json.loads((REPO_ROOT / "harnesses" / "pi" / "settings.json").read_text(encoding="utf-8"))
    sources = []
    for entry in settings["packages"]:
        sources.append(entry if isinstance(entry, str) else entry["source"])
    assert sources, "模板必须声明检索扩展"
    for source in sources:
        assert source.startswith("npm:"), source
        assert local_package_source(source) is not None, source
