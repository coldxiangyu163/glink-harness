"""GLINK-DSH-02：dsh 的 Jina fetch bundle（out-of-tree）与每 run ACP profile 覆盖层。

验证仓库事实与宿主渲染：bundle 清单与 cordis patch、每 run `home/` 覆盖层里的 ACP
profile 清单、Glink 身份与上游遥测/badge 禁用、两个 ACP Dockerfile 的安装落点与自检。
不导入或启动 dsh、不构建镜像、不发网络请求（仓库默认环境没有 YAML 解析依赖，
patch 按文本结构断言；真实解析与生效证据在
docs/acp-integration-notes.md §18 的镜像/模板与 run 记录里）。

这些断言把「交付方式」钉在代码里：bundle 源码只存在一份（`harnesses/dsh/bundles/`），
镜像在构建期把它装进 dsh 安装自身的 node_modules，覆盖层只提供引用它的 profile 清单。
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

from glink_harness.config import build_config
from glink_harness.kernel.acp_env import prepare_acp_runtime

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE_DIR = REPO_ROOT / "harnesses" / "dsh" / "bundles" / "dsh-web-fetch-jina"
BUNDLE_MANIFEST = BUNDLE_DIR / "package.json"
BUNDLE_PATCH = BUNDLE_DIR / "cordis.patch.yml"
PROFILE_DIR = REPO_ROOT / "harnesses" / "dsh" / "home" / ".dsh" / "profiles" / "acp"
DOCKERFILES = (
    REPO_ROOT / "docker" / "Dockerfile.acp-worker",
    REPO_ROOT / "docker" / "e2b" / "Dockerfile.acp-e2b",
)
HARNESS_TOML = REPO_ROOT / "harnesses" / "dsh" / "harness.toml"
CHECK_SH = REPO_ROOT / "scripts" / "check.sh"

#: 镜像内安装落点：dsh 全局安装自身的依赖目录，`resolveBundleDir` 的第一个解析锚点。
INSTALL_ROOT = (
    "/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@glink-harness/dsh-web-fetch-jina"
)
COPY_SOURCE = "harnesses/dsh/bundles/dsh-web-fetch-jina"
#: bundle 运行时依赖的上游锚点（与镜像内实测一致；dsh 0.1.5-rc.3 的 cordis 版本）。
CORDIS_PEER = "4.0.2"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def harness_version() -> str:
    with HARNESS_TOML.open("rb") as handle:
        manifest = tomllib.load(handle)
    # 清单 v2：上游版本钉点在 [[image.packages]]，ACP 包的 agent 条目即 dsh 本体。
    agents = [item for item in manifest["image"]["packages"] if item["role"] == "agent"]
    assert [item["package"] for item in agents] == ["@deepseek-ai/dsh"]
    return agents[0]["version"]


def bundle_patch_text() -> str:
    return BUNDLE_PATCH.read_text(encoding="utf-8")


def test_bundle_manifest_is_an_out_of_tree_dsh_bundle() -> None:
    manifest = load_json(BUNDLE_MANIFEST)
    assert manifest["name"] == "@glink-harness/dsh-web-fetch-jina"
    assert manifest["private"] is True
    assert manifest["type"] == "module"
    assert manifest["main"] == "lib/index.js"
    assert manifest["dsh"]["bundle"]["patch"] == "./cordis.patch.yml"
    assert (BUNDLE_DIR / "lib" / "index.js").is_file()
    assert (BUNDLE_DIR / "lib" / "provider.js").is_file()
    assert (BUNDLE_DIR / "lib" / "policy.js").is_file()


def test_bundle_peer_ranges_cover_the_pinned_dsh_version() -> None:
    version = harness_version()
    peer = load_json(BUNDLE_MANIFEST)["peerDependencies"]
    dsh_peers = {name: value for name, value in peer.items() if name.startswith("@deepseek-ai/dsh-")}
    assert dsh_peers, "bundle 必须声明要用的 dsh 包（版本兼容检查读 peerDependencies）"
    for name, value in dsh_peers.items():
        assert value == f"^{version}", f"{name} 的 peer 范围必须覆盖镜像钉版 {version}"
    assert peer["@deepseek-ai/cordis"] == CORDIS_PEER
    assert peer["@deepseek-ai/dsh-web"] == f"^{version}"


def test_bundle_patch_registers_jina_and_points_the_web_row_at_it() -> None:
    text = bundle_patch_text()
    # 插入行：patch 相对路径（由 dsh 转为 file URL），配置只带引用名，不带密钥。
    inserted = re.search(
        r"- id: web-fetch-jina\n"
        r"      name: '\./lib/index\.js'\n"
        r"      config:\n"
        r"(?P<config>(?:        [^\n]+\n)+)",
        text,
    )
    assert inserted is not None, "bundle patch 必须插入 web-fetch-jina 行（patch 相对路径）"
    config = inserted.group("config")
    assert "apiKeyEnv: JINA_API_KEY" in config
    assert "baseURL: https://r.jina.ai" in config
    assert re.search(r"timeoutMs: [1-9]\d*", config)
    assert re.search(r"maxResponseBytes: [1-9]\d*", config)
    assert re.search(r"maxBodyChars: [1-9]\d*", config)
    assert (BUNDLE_DIR / "lib" / "index.js").is_file()

    # web 行显式选择 Jina；选择由配置决定，不依赖注册顺序。
    assert re.search(
        r"- id: web\n"
        r"  config:\n"
        r"    searchProvider: deepseek-official\n"
        r"    fetchProvider: jina\n",
        text,
    ), "web 行必须显式把 fetchProvider 指向 jina"

    # 原失败面（GLINK-DSH-01）只禁用、不删除：误配时 fail closed 而不是静默回退。
    assert re.search(r"- id: web-fetch-http\n  disabled: true\n", text)

    # Jina 读取 PDF / JS 页面常超过 tool-web 的 30s 默认 fetch 预算。
    tool_web = re.search(r"- id: tool-web\n  config:\n(?P<config>(?:    [^\n]+\n)+)", text)
    assert tool_web is not None
    assert "fetch: true" in tool_web.group("config")
    timeout = re.search(r"fetchTimeoutMs: (\d+)", tool_web.group("config"))
    assert timeout is not None and int(timeout.group(1)) >= 60_000


def test_bundle_lib_does_not_hardcode_a_key_or_read_one_from_config() -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in sorted((BUNDLE_DIR / "lib").glob("*.js")))
    assert "JINA_API_KEY" in text  # 只出现为凭据引用名
    assert not re.search(r"jina_[A-Za-z0-9]{10,}", text), "lib/ 不应出现 Jina 密钥形态的字面量"
    assert "apiKey:" not in text, "配置面不提供字面密钥字段"


def test_profile_overlay_lists_the_shipped_bundles_plus_the_jina_bundle() -> None:
    manifest = load_json(PROFILE_DIR / "package.json")
    assert manifest["name"] == "dsh-profile-acp"
    bundles = manifest["dsh"]["profile"]["bundles"]
    assert bundles == [
        "@deepseek-ai/dsh-base",
        "@deepseek-ai/dsh-acp-app",
        "@glink-harness/dsh-web-fetch-jina",
    ]
    assert manifest["dsh"]["profile"]["patchReload"] == "startup"
    # 覆盖层只带清单、用户层与默认 settings（部署偏好版本化，ADR 0016；
    # 不含 key——apiKeyEnv 只是引用名）：bundle 代码由镜像安装，重建即更新。
    patch_text = (PROFILE_DIR / "cordis.patch.yml").read_text(encoding="utf-8")
    # base bundle ships the native present package but leaves it unmounted;
    # the ACP profile mounts the tool explicitly without disturbing Jina's bundle.
    assert re.search(
        r"- insert:\n    - id: tool-present\n"
        r"      name: '@deepseek-ai/dsh-tool-present'\n"
        r"      config:\n        maxFiles: 8\s*$", patch_text
    )
    overlay_files = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "harnesses" / "dsh" / "home").rglob("*")
        if path.is_file()
    )
    assert overlay_files == [
        "harnesses/dsh/home/.dsh/patches/llm-pi-ai.yml",
        "harnesses/dsh/home/.dsh/profiles/acp/cordis.patch.yml",
        "harnesses/dsh/home/.dsh/profiles/acp/package.json",
        "harnesses/dsh/home/.dsh/settings.yaml",
    ]
    # provider 接入结构的单源是 --patch 覆盖层（DSH-03，挂载时同步注册路由）；
    # 它只能携带凭据引用名，不能携带 key 值。
    patch_text = (
        REPO_ROOT / "harnesses" / "dsh" / "home" / ".dsh" / "patches" / "llm-pi-ai.yml"
    ).read_text(encoding="utf-8")
    assert "apiKeyEnv: OPENROUTER_API_KEY" in patch_text and "sk-" not in patch_text
    settings_text = (REPO_ROOT / "harnesses" / "dsh" / "home" / ".dsh" / "settings.yaml").read_text(encoding="utf-8")
    assert "sk-" not in settings_text, "overlay settings 不能携带 key 值"


def profile_override(text: str, row_id: str) -> str:
    """读取唯一的顶层用户覆盖行，避免重复覆盖把已禁用的行重新开启。"""
    rows = [
        row for row in re.split(r"(?m)(?=^- )", text)
        if row.startswith(f"- id: {row_id}\n")
    ]
    assert len(rows) == 1, f"profile 必须恰好覆盖一次 {row_id}"
    return rows[0]


def test_profile_owns_glink_identity_and_preserves_dsh_tool_guidance() -> None:
    text = (PROFILE_DIR / "cordis.patch.yml").read_text(encoding="utf-8")
    prompt = profile_override(text, "system-prompt")
    assert re.search(r"(?m)^    includeHarnessIdentity: false$", prompt)
    prefix = re.search(r"(?m)^    personaPrefix: >-\n((?:      [^\n]+\n)+)", prompt)
    assert prefix is not None
    assert " ".join(prefix.group(1).split()) == (
        "You are Glink, an AI assistant for research, analysis, and document creation."
    )
    # config 整行替换而非合并，原 ACP workspace suffix 必须一起保留。
    assert re.search(r"(?m)^    personaSuffix: Your working directory is \{\{cwd\}\}\.$", prompt)
    assert "includeRuntimeContext: false" not in text
    assert "complete: true" not in text


@pytest.mark.parametrize("row_id", ["session-telemetry-otel", "skill-badge"])
def test_profile_explicitly_disables_upstream_uploads_and_badges(row_id: str) -> None:
    text = (PROFILE_DIR / "cordis.patch.yml").read_text(encoding="utf-8")
    row = profile_override(text, row_id)
    assert re.search(r"(?m)^  disabled: true$", row)


def test_glink_profile_is_delivered_to_a_fresh_run_home(tmp_path: Path) -> None:
    cfg = build_config({
        "GLINK_HARNESS": "dsh",
        "OPENAI_MODEL": "test/model",
        "OPENAI_REASONING_EFFORT": "high",
    }, REPO_ROOT)
    runtime = prepare_acp_runtime(cfg, runtime_dir=tmp_path / "acp-agent")
    delivered = runtime.home / ".dsh" / "profiles" / "acp" / "cordis.patch.yml"
    text = delivered.read_text(encoding="utf-8")
    assert text == (PROFILE_DIR / "cordis.patch.yml").read_text(encoding="utf-8")
    assert "includeHarnessIdentity: false" in profile_override(text, "system-prompt")
    for row_id in ("session-telemetry-otel", "skill-badge"):
        assert "disabled: true" in profile_override(text, row_id)
    assert delivered.stat().st_mode & 0o777 == 0o600
    # DSH-03：--patch 覆盖层随 home/ 交付到每 run HOME，launch 按放置后的
    # HOME 拼 `--patch <HOME>/.dsh/patches/llm-pi-ai.yml`。
    overlay = runtime.home / ".dsh" / "patches" / "llm-pi-ai.yml"
    assert overlay.read_text(encoding="utf-8") == (
        PROFILE_DIR.parents[1] / "patches" / "llm-pi-ai.yml"
    ).read_text(encoding="utf-8")
    assert overlay.stat().st_mode & 0o777 == 0o600
    assert runtime.credential_dests == ()


def test_both_acp_images_install_the_same_bundle_and_self_check_it() -> None:
    for dockerfile in DOCKERFILES:
        text = dockerfile.read_text(encoding="utf-8")
        assert f"COPY {COPY_SOURCE} {INSTALL_ROOT}" in text, f"{dockerfile.name} 必须在构建期安装 bundle"
        assert f"test -f {INSTALL_ROOT}/package.json" in text, f"{dockerfile.name} 自检必须确认 bundle 存在"
        # 安装落点必须是可解析的 dsh 依赖位（而不是某个 profile 目录）。
        assert INSTALL_ROOT.startswith("/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/")
    first, second = (path.read_text(encoding="utf-8") for path in DOCKERFILES)
    assert first.count(INSTALL_ROOT) == second.count(INSTALL_ROOT) == 2


def test_check_entrypoint_runs_the_bundle_unit_tests_with_the_dsh_stub_registration() -> None:
    text = CHECK_SH.read_text(encoding="utf-8")
    assert "harnesses/dsh/bundles/*/tests/*.test.mjs" in text
    assert "harnesses/dsh/bundles/dsh-web-fetch-jina/tests/support/register-stubs.mjs" in text
