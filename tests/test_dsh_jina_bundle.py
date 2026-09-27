"""GLINK-DSH-02：dsh 的 Jina fetch bundle（out-of-tree）与每 run ACP profile 覆盖层。

只读仓库事实：bundle 清单与 cordis patch、每 run `home/` 覆盖层里的 ACP profile 清单、
两个 ACP Dockerfile 的安装落点与自检。不导入 dsh、不构建镜像、不发网络请求（仓库默认
环境没有 YAML 解析依赖，patch 按文本结构断言；真实解析与生效证据在
docs/acp-integration-notes.md §18 的镜像/模板与 run 记录里）。

这些断言把「交付方式」钉在代码里：bundle 源码只存在一份（`harnesses/dsh/bundles/`），
镜像在构建期把它装进 dsh 安装自身的 node_modules，覆盖层只提供引用它的 profile 清单。
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

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
    "/usr/local/lib/node_modules/@deepseek-ai/dsh/node_modules/@pi-glink/dsh-web-fetch-jina"
)
COPY_SOURCE = "harnesses/dsh/bundles/dsh-web-fetch-jina"
#: bundle 运行时依赖的上游锚点（与镜像内实测一致；dsh 0.1.5-rc.3 的 cordis 版本）。
CORDIS_PEER = "4.0.2"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def harness_version() -> str:
    with HARNESS_TOML.open("rb") as handle:
        manifest = tomllib.load(handle)
    return manifest["version"]


def bundle_patch_text() -> str:
    return BUNDLE_PATCH.read_text(encoding="utf-8")


def test_bundle_manifest_is_an_out_of_tree_dsh_bundle() -> None:
    manifest = load_json(BUNDLE_MANIFEST)
    assert manifest["name"] == "@pi-glink/dsh-web-fetch-jina"
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
        "@pi-glink/dsh-web-fetch-jina",
    ]
    assert manifest["dsh"]["profile"]["patchReload"] == "startup"
    # 覆盖层只带清单与用户层，不带 bundle 代码：代码由镜像安装，重建即更新。
    patch_text = (PROFILE_DIR / "cordis.patch.yml").read_text(encoding="utf-8")
    assert patch_text.strip().endswith("[]"), "profile 用户层必须保持空列表"
    overlay_files = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "harnesses" / "dsh" / "home").rglob("*")
        if path.is_file()
    )
    assert overlay_files == [
        "harnesses/dsh/home/.dsh/profiles/acp/cordis.patch.yml",
        "harnesses/dsh/home/.dsh/profiles/acp/package.json",
    ]


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
