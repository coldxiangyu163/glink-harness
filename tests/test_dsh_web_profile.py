"""dsh 官方 Search/Fetch 的配置与交付契约（不启动真实 agent 或网络）。

提供者实现归上游，不复制内核或用替身测试自建 provider。这里保护宿主实际交付的
profile、凭据引用、预算、镜像自检，以及旧 Jina profile 续接时不遮蔽新配置。
真实 provider 解析和请求验收必须另在钉版 E2B 中执行。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from glink_harness.config import build_config
from glink_harness.kernel.acp_env import prepare_acp_runtime

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / "harnesses/dsh"
PROFILE = PACK / "home/.dsh/profiles/glink-acp"
NATIVE_PROVIDER = "/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-web-deepseek-api"


def config():
    return build_config({
        "GLINK_HARNESS": "dsh",
        "OPENAI_MODEL": "test/model",
        "OPENAI_REASONING_EFFORT": "high",
    }, ROOT)


def row(text: str, row_id: str) -> str:
    rows = [part for part in re.split(r"(?m)(?=^- )", text) if part.startswith(f"- id: {row_id}\n")]
    assert len(rows) == 1, f"profile 必须恰好覆盖一次 {row_id}"
    return rows[0]


def test_dsh_version_and_native_profile_are_pinned() -> None:
    pack = config().pack
    assert pack.command == ("dsh", "--profile", "glink-acp")
    assert [(p.package, p.version) for p in pack.image.packages] == [
        ("@deepseek-ai/dsh", "0.2.1-rc.2"),
    ]
    manifest = json.loads((PROFILE / "package.json").read_text())
    assert manifest["name"] == "dsh-profile-glink-acp"
    assert manifest["dsh"]["profile"] == {
        "bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-acp-app"],
        "patchReload": "startup",
    }
    assert not (PACK / "bundles/dsh-web-fetch-jina").exists()
    assert not (PACK / "home/.dsh/profiles/acp").exists()


def test_openrouter_catalog_supports_deepseek_max_without_losing_gpt() -> None:
    text = (PACK / "home/.dsh/patches/llm-pi-ai.yml").read_text()
    assert "api: openai-completions" in text
    assert "baseURL: https://openrouter.ai/api/v1" in text
    blocks = re.split(r"(?m)(?=^          - id: )", text)[1:]
    models = {block.splitlines()[0].split(": ", 1)[1]: block for block in blocks}
    assert set(models) == {"openai/gpt-5.6-sol", "deepseek/deepseek-v4.1-flash-1017"}
    deepseek = models["deepseek/deepseek-v4.1-flash-1017"]
    assert "contextWindow: 1048576" in deepseek
    assert "maxTokens: 32768" in deepseek
    assert "off: none" in deepseek
    assert "low: low" in deepseek
    assert "high: high" in deepseek
    assert "max: max" in deepseek
    assert "xhigh" not in deepseek
    assert "xhigh: xhigh" in models["openai/gpt-5.6-sol"]


def test_profile_selects_both_direct_apis_without_fallback() -> None:
    text = (PROFILE / "cordis.patch.yml").read_text()
    web = row(text, "web")
    assert re.search(r"(?m)^    searchProvider: deepseek-search-api$", web)
    assert re.search(r"(?m)^    fetchProvider: deepseek-fetch-api$", web)
    provider = row(text, "web-deepseek-api")
    assert re.search(r"(?m)^    apiKeyEnv: DEEPSEEK_API_KEY$", provider)
    assert re.search(r"(?m)^    baseURL: https://api.deepseek.com/v1/tools$", provider)
    assert re.search(r"(?m)^    sendOriginalQuestion: false$", provider)
    assert "apiKey:" not in text and "JINA_API_KEY" not in text
    for disabled in ("web-fetch-http", "web-search-deepseek"):
        assert re.search(r"(?m)^  disabled: true$", row(text, disabled))
    tool = row(text, "tool-web")
    assert re.search(r"(?m)^    fetch: true$", tool)
    assert re.search(r"(?m)^    searchTimeoutMs: 60000$", tool)
    assert re.search(r"(?m)^    fetchTimeoutMs: 60000$", tool)


def test_profile_preserves_identity_and_native_context_without_retired_cwd_variable() -> None:
    text = (PROFILE / "cordis.patch.yml").read_text()
    prompt = row(text, "system-prompt")
    assert re.search(r"(?m)^    includeHarnessIdentity: false$", prompt)
    prefix = re.search(r"(?m)^    personaPrefix: >-\n((?:      [^\n]+\n)+)", prompt)
    assert prefix is not None
    assert " ".join(prefix.group(1).split()) == (
        "You are Glink, an AI assistant for research, analysis, and document creation."
    )
    # 0.2.1 不再注册 cwd 变量；工作目录由原生 working-directory 必需上下文提供。
    assert "{{cwd}}" not in text
    assert "includeRuntimeContext: false" not in text
    assert "complete: true" not in text
    assert re.search(r"(?m)^  disabled: true$", row(text, "session-telemetry-otel"))
    # 新 base 不再挂载 skill-badge；不引入 badge bundle 或覆盖不存在的行。
    assert "skill-badge" not in text
    assert re.search(
        r"- insert:\n    - id: tool-present\n"
        r"      name: '@deepseek-ai/dsh-tool-present'\n"
        r"      config:\n        maxFiles: 8\s*$", text,
    )


def test_glink_profile_and_provider_patch_are_delivered_to_fresh_run(tmp_path: Path) -> None:
    runtime = prepare_acp_runtime(config(), runtime_dir=tmp_path / "runtime")
    overlay = PACK / "home"
    files = sorted(p.relative_to(overlay).as_posix() for p in overlay.rglob("*") if p.is_file())
    assert files == [
        ".dsh/patches/llm-pi-ai.yml",
        ".dsh/profiles/glink-acp/cordis.patch.yml",
        ".dsh/profiles/glink-acp/package.json",
        ".dsh/settings.yaml",
    ]
    for relative in files:
        delivered = runtime.home / relative
        assert delivered.read_bytes() == (overlay / relative).read_bytes()
        assert delivered.stat().st_mode & 0o777 == 0o600
    assert runtime.credential_dests == ()
    patch = (runtime.home / ".dsh/patches/llm-pi-ai.yml").read_text()
    assert "apiKeyEnv: OPENROUTER_API_KEY" in patch and "sk-" not in patch


def test_legacy_jina_profile_does_not_shadow_selected_profile_on_resume(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    old = parent / ".dsh/profiles/acp"
    old.mkdir(parents=True)
    old_manifest = json.dumps({"dsh": {"profile": {"bundles": ["@glink-harness/dsh-web-fetch-jina"]}}})
    (old / "package.json").write_text(old_manifest)
    (old / "cordis.patch.yml").write_text("- id: web\n  config:\n    fetchProvider: jina\n")
    session = parent / ".dsh/sessions/project/session/session.v3.jsonl.zstd"
    session.parent.mkdir(parents=True)
    session.write_bytes(b"synthetic-opaque-session")
    before = {p.relative_to(parent): p.read_bytes() for p in parent.rglob("*") if p.is_file()}

    cfg = config()
    runtime = prepare_acp_runtime(cfg, runtime_dir=tmp_path / "child", inherit_from=parent)
    selected = runtime.home / ".dsh/profiles" / cfg.pack.command[2]
    assert json.loads((selected / "package.json").read_text())["dsh"]["profile"]["bundles"] == [
        "@deepseek-ai/dsh-base", "@deepseek-ai/dsh-acp-app",
    ]
    assert "fetchProvider: deepseek-fetch-api" in (selected / "cordis.patch.yml").read_text()
    # 只改变选择，不改父历史，也不清理继承的旧 profile 或不透明会话。
    for relative, payload in before.items():
        assert (parent / relative).read_bytes() == payload
        assert (runtime.home / relative).read_bytes() == payload


def test_images_require_native_provider_and_do_not_copy_custom_bundle() -> None:
    for relative in ("docker/Dockerfile.acp-worker", "docker/e2b/Dockerfile.acp-e2b"):
        text = (ROOT / relative).read_text()
        assert "jina" not in text.lower()
        assert f"test -f {NATIVE_PROVIDER}/lib/index.js" in text
        assert "deepseek-web-api=present" in text
        assert '"@deepseek-ai/dsh@${DSH_VERSION}"' in text


def test_research_still_requires_opening_sources_before_citation() -> None:
    agreement = (ROOT / "methodology/work-agreement.md").read_text()
    assert "引用某个来源前先打开它" in agreement
    assert "检索结果只用于判断该看哪一页" in agreement
