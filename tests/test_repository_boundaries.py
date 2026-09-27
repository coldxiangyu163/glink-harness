"""Mechanical repository boundaries; not a source-copy detector or permission system."""

from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SOURCE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".mts", ".cts"}
_JS_TOKENS = re.compile(
    r"//[^\n]*|/\*[\s\S]*?\*/|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\""
    r"|`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*|[^\s]"
)
_PRIVATE_PI = re.compile(r"(?:^|/)@earendil-works/pi-coding-agent/(?:src|dist)(?:/|$)")


def git_paths(root: Path, *, include_untracked: bool = False) -> list[Path]:
    """Read the index and, for source checks, non-ignored worktree additions."""
    args = ["git", "-c", "core.fsmonitor=false", "ls-files", "-z", "--cached"]
    if include_untracked:
        args += ["--others", "--exclude-standard"]
    output = subprocess.check_output(args, cwd=root)
    return sorted({Path(name.decode("utf-8")) for name in output.split(b"\0") if name})


def runtime_files(paths: list[Path]) -> list[str]:
    """Reject known runtime/config paths without reading credential contents."""
    forbidden = []
    for path in paths:
        env_file = (path.name == ".env" or path.name.startswith(".env.")) and not path.name.endswith(".example")
        credential = path.name in {"auth.json", "web-search.json", "models-store.json"} and path.parts[0] != "tests"
        runtime = path.parts[0] == "data" or path.parts[:3] == ("harnesses", "pi", "sessions")
        if env_file or credential or runtime:
            forbidden.append(path.as_posix())
    return forbidden


def forbidden_imports(path: Path, text: str) -> list[str]:
    """Inspect Python imports and ordinary literal JS/TS module specifiers.

    Dynamically constructed paths and renamed/copied implementations still need
    semantic review. TS extensions and public package imports remain allowed.
    """
    modules = []
    if path.suffix == ".py":
        for node in ast.walk(ast.parse(text, filename=str(path))):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.append(node.module)
            elif isinstance(node, ast.Call) and node.args:
                function = node.func
                dynamic = isinstance(function, ast.Name) and function.id == "__import__"
                dynamic |= (
                    isinstance(function, ast.Attribute)
                    and isinstance(function.value, ast.Name)
                    and function.value.id == "importlib"
                    and function.attr == "import_module"
                )
                if dynamic and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    modules.append(node.args[0].value)
        return [name for name in modules if name.split(".")[0] in {"frontier_agent", "apodex"}]

    tokens = [match.group() for match in _JS_TOKENS.finditer(text) if not match.group().startswith(("//", "/*"))]
    for index, token in enumerate(tokens):
        candidate = None
        if token in {"from", "import"} and index + 1 < len(tokens):
            candidate = tokens[index + 1]
        if token in {"import", "require"} and tokens[index + 1:index + 2] == ["("] and index + 2 < len(tokens):
            candidate = tokens[index + 2]
        if candidate and candidate[0] in {"'", '"', "`"}:
            if candidate[0] == "`":
                if "${" not in candidate:
                    modules.append(candidate[1:-1])
            else:
                modules.append(ast.literal_eval(candidate.replace("\\/", "/")))
    return [name for name in modules if _PRIVATE_PI.search(name)]


def source_violations(root: Path) -> list[str]:
    violations = []
    for path in git_paths(root, include_untracked=True):
        if path.parts[0] not in {"host", "web", "harnesses", "methodology"} or path.suffix not in _SOURCE_SUFFIXES:
            continue
        source = root / path
        if source.is_symlink():
            violations.append(f"{path}: source symlink needs explicit ownership review")
        elif source.is_file():
            for module in forbidden_imports(path, source.read_text(encoding="utf-8")):
                violations.append(f"{path}: forbidden import {module}")
    return violations


#: 导入方向：``host/pi_glink/kernel/`` 是接入层，位于编排层之下，不得反向导入编排层
#: 或宿主编排模块。``pi_glink.runs`` 承接 run 生命周期与资源台账（编排层）。
_FORBIDDEN_KERNEL_MODULES = (
    "pi_glink.runs",
    "pi_glink.tenancy",
    "pi_glink.auth",
    "pi_glink.accounts",
    "pi_glink.ownership",
    "pi_glink.blueprints",
    "pi_glink.app",
    "pi_glink.cli",
)
_FORBIDDEN_KERNEL_PREFIXES = ("pi_glink.web_",)

#: ``store`` 是显式白名单：三个 harness 族模块读 ``RunRecord`` 与会话种子（族策略的
#: 事实来源），其余 kernel 模块不得读宿主持久化。
_KERNEL_STORE_WHITELIST = frozenset(
    {
        "host/pi_glink/kernel/harness.py",
        "host/pi_glink/kernel/harness_pi.py",
        "host/pi_glink/kernel/harness_acp.py",
    }
)


def kernel_forbidden_imports(relative: Path, text: str) -> list[str]:
    """Imports of one kernel module that point back at the orchestration layer.

    Relative imports are resolved against the module's own package, so
    ``from ..store import RunRecord`` counts as ``pi_glink.store`` whether it is
    written relative or absolute. Dynamic ``__import__``/``import_module``
    string literals are inspected like the other import checks.
    """
    package = list(relative.with_suffix("").parts)[1:]  # drop the ``host/`` root
    package = package[:-1]
    modules: list[str] = []
    for node in ast.walk(ast.parse(text, filename=str(relative))):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                modules.append(".".join([*base, node.module] if node.module else base))
            else:
                modules.append(node.module or "")
        elif isinstance(node, ast.Call) and node.args:
            function = node.func
            dynamic = isinstance(function, ast.Name) and function.id == "__import__"
            dynamic |= (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "importlib"
                and function.attr == "import_module"
            )
            if dynamic and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                modules.append(node.args[0].value)
    violations = []
    for module in modules:
        if module == "pi_glink.store" or module.startswith("pi_glink.store."):
            if relative.as_posix() not in _KERNEL_STORE_WHITELIST:
                violations.append(f"{relative}: forbidden import {module}")
            continue
        if any(module == item or module.startswith(item + ".") for item in _FORBIDDEN_KERNEL_MODULES):
            violations.append(f"{relative}: forbidden import {module}")
        elif any(module.startswith(prefix) for prefix in _FORBIDDEN_KERNEL_PREFIXES):
            violations.append(f"{relative}: forbidden import {module}")
    return violations


def kernel_import_violations(root: Path) -> list[str]:
    """Run the direction rule over every module currently in the kernel package."""
    violations = []
    for path in sorted((root / "host" / "pi_glink" / "kernel").glob("*.py")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        violations.extend(kernel_forbidden_imports(relative, path.read_text(encoding="utf-8")))
    return violations


def worker_versions(text: str) -> tuple[str, str]:
    node = re.search(r"^FROM\s+(node:\d+\.\d+\.\d+-[\w.-]+)(?:@sha256:[a-f0-9]{64})?\s*$", text, re.MULTILINE)
    pi = re.search(r"^ARG\s+PI_VERSION=(\d+\.\d+\.\d+(?:-[\w.-]+)?)\s*$", text, re.MULTILINE)
    if node is None or pi is None:
        raise ValueError("worker Dockerfile needs exact Node and pi versions")
    return node.group(1), pi.group(1)


def worker_package_versions(text: str) -> tuple[str, str]:
    """The exact versions of the prebaked pi packages (both worker images).

    The repository declares both packages in ``harnesses/pi/settings.json`` (the runtime
    source for the in-process face) and both images install the same pins into
    ``/opt/pi-packages``. A floating or missing declaration is a failure, not a
    warning.
    """
    parsed = []
    for name in ("PI_WEB_ACCESS_VERSION", "PI_SUBAGENTS_VERSION"):
        match = re.search(rf"^ARG\s+{name}=(\d+\.\d+\.\d+(?:-[\w.-]+)?)\s*$", text, re.MULTILINE)
        if match is None:
            raise ValueError(f"worker Dockerfile needs an exact {name}")
        parsed.append(match.group(1))
    return parsed[0], parsed[1]


def guard_packages(text: str) -> list[str]:
    """The package names the image-local guard script refuses to start without."""
    match = re.search(r"for pkg in ([^;\n]+); do", text)
    if match is None:
        raise ValueError("worker Dockerfile needs the prebaked package guard")
    return match.group(1).split()


def template_package_pins() -> dict[str, str]:
    """``harnesses/pi/settings.json`` npm packages as ``name -> declared spec``."""
    settings = json.loads((ROOT / "harnesses" / "pi" / "settings.json").read_text(encoding="utf-8"))
    pins: dict[str, str] = {}
    for entry in settings["packages"]:
        source = entry if isinstance(entry, str) else entry["source"]
        if not source.startswith("npm:"):
            continue
        spec = source[4:]
        if spec.startswith("@"):
            raise ValueError("scoped template packages need an explicit parser")
        name, _at, version = spec.partition("@")
        pins[name] = version
    return pins


def test_no_runtime_credentials_or_data_are_tracked() -> None:
    assert runtime_files(git_paths(ROOT)) == []


def test_production_sources_respect_external_kernel_boundaries() -> None:
    assert source_violations(ROOT) == []


def test_kernel_layer_does_not_import_the_orchestration_layer() -> None:
    assert kernel_import_violations(ROOT) == []


def test_kernel_import_direction_rejects_orchestration_and_store() -> None:
    """反例：kernel 反向导入编排层/宿主模块或绕过白名单读 store 必须变红。"""
    kernel_module = Path("host/pi_glink/kernel/new_module.py")
    assert kernel_forbidden_imports(kernel_module, "from ..runs import RunManager") == [
        "host/pi_glink/kernel/new_module.py: forbidden import pi_glink.runs"
    ]
    assert kernel_forbidden_imports(kernel_module, "import pi_glink.runs.runner") != []
    assert kernel_forbidden_imports(kernel_module, "from ..store import RunRecord") == [
        "host/pi_glink/kernel/new_module.py: forbidden import pi_glink.store"
    ]
    assert kernel_forbidden_imports(kernel_module, "from pi_glink.tenancy import TenantContext") != []
    assert kernel_forbidden_imports(kernel_module, "from ..web_routes import HostRequestHandler") != []
    assert kernel_forbidden_imports(kernel_module, "__import__('pi_glink.blueprints')") != []
    # 反例在编排层同范围成立：族模块也不能反向依赖编排层。
    assert kernel_forbidden_imports(Path("host/pi_glink/kernel/harness.py"), "from ..runs import RunManager") != []


def test_kernel_import_direction_allows_lower_layers_and_the_store_whitelist() -> None:
    kernel_module = Path("host/pi_glink/kernel/new_module.py")
    assert kernel_forbidden_imports(kernel_module, "from ..config import HostConfig") == []
    assert kernel_forbidden_imports(kernel_module, "from ..redaction import SecretRedactor") == []
    assert kernel_forbidden_imports(kernel_module, "from .harness import HarnessFamily") == []
    for relative in sorted(_KERNEL_STORE_WHITELIST):
        assert kernel_forbidden_imports(Path(relative), "from ..store import RunRecord") == [], relative


def test_kernel_import_direction_covers_new_modules(tmp_path: Path) -> None:
    kernel_dir = tmp_path / "host" / "pi_glink" / "kernel"
    kernel_dir.mkdir(parents=True)
    source = kernel_dir / "new.py"
    source.write_text("from ..runs import RunManager\n")
    assert kernel_import_violations(tmp_path) == [
        "host/pi_glink/kernel/new.py: forbidden import pi_glink.runs"
    ]
    source.write_text("from ..kernel.harness import HarnessFamily\n")
    assert kernel_import_violations(tmp_path) == []


def test_worker_version_declarations_are_exact_and_equal() -> None:
    docker = worker_versions((ROOT / "docker/Dockerfile.worker").read_text())
    e2b = worker_versions((ROOT / "docker/e2b/Dockerfile.e2b").read_text())
    assert docker == e2b, "Docker/E2B version declarations diverged; this does not check deployed images"


@pytest.mark.parametrize("name", [".env", ".env.backup", "harnesses/pi/auth.json", "harnesses/pi/web-search.json", "harnesses/pi/models-store.json", "harnesses/pi/sessions/a.jsonl", "data/runs/a/meta.json"])
def test_runtime_file_rule_rejects_protected_paths(name: str) -> None:
    assert runtime_files([Path(name)]) == [name]


def test_examples_and_synthetic_json_fixtures_are_allowed() -> None:
    assert runtime_files([Path(".env.example"), Path(".env.docker.example"), Path("tests/fixtures/auth.json"), Path("docs/architecture.md")]) == []


@pytest.mark.parametrize("text", [
    "import frontier_agent", "from frontier_agent.core import Agent", "import apodex as legacy",
    "from apodex.runtime import run", "__import__('frontier_agent.core')",
    "importlib.import_module('apodex.runtime')",
])
def test_python_import_rule_rejects_legacy_runtime(text: str) -> None:
    assert forbidden_imports(Path("host/module.py"), text)


@pytest.mark.parametrize("text", [
    "import { X } from '@earendil-works/pi-coding-agent/dist/internal.js';",
    "export { X } from '@earendil-works/pi-coding-agent/src/internal.ts';",
    "import('@earendil-works/pi-coding-agent/dist/internal.js');",
    "require('../node_modules/@earendil-works/pi-coding-agent/dist/internal.js');",
    "import(`@earendil-works/pi-coding-agent/src/internal.ts`);",
])
def test_js_import_rule_rejects_private_pi_paths(text: str) -> None:
    assert forbidden_imports(Path("harnesses/pi/extensions/example.ts"), text)


def test_public_extensions_comments_and_local_python_imports_are_allowed() -> None:
    assert forbidden_imports(Path("harnesses/pi/extensions/example.ts"), """
        // import X from '@earendil-works/pi-coding-agent/dist/internal.js';
        /* require('@earendil-works/pi-coding-agent/src/internal.ts'); */
        import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';
        const description = "from '@earendil-works/pi-coding-agent/dist/internal.js'";
    """) == []
    assert forbidden_imports(Path("host/module.py"), "from . import frontier_agent\nfrom pi_glink.kernel import rpc") == []


@pytest.mark.parametrize("text", [
    "FROM node:24-bookworm-slim\nARG PI_VERSION=0.86.1\n",
    "FROM node:24.21.0-bookworm-slim\nARG PI_VERSION=latest\n",
    "FROM node:24.21.0-bookworm-slim\n",
])
def test_floating_or_missing_worker_versions_fail(text: str) -> None:
    with pytest.raises(ValueError, match="exact"):
        worker_versions(text)


def test_worker_version_comparison_detects_each_runtime_change() -> None:
    original = "FROM node:24.21.0-bookworm-slim\nARG PI_VERSION=0.86.1\n"
    assert worker_versions(original) != worker_versions(original.replace("24.21.0", "24.22.0"))
    assert worker_versions(original) != worker_versions(original.replace("0.86.1", "0.87.0"))


def test_prebaked_package_versions_are_exact_and_equal() -> None:
    docker = worker_package_versions((ROOT / "docker/Dockerfile.worker").read_text())
    e2b = worker_package_versions((ROOT / "docker/e2b/Dockerfile.e2b").read_text())
    assert docker == e2b, "Docker/E2B prebaked package versions diverged"


#: The ACP worker images' pinned upstream packages, per harness pack:
#: ``pack -> (image ARG, npm package)``. The pack manifest carries the same
#: pair; this mapping is the test's independent oracle, not a third source.
ACP_PACK_PINS = {
    "dsh": ("DSH_VERSION", "@deepseek-ai/dsh"),
    "codex": ("CODEX_ACP_VERSION", "@agentclientprotocol/codex-acp"),
    "opencode": ("OPENCODE_AI_VERSION", "opencode-ai"),
}


def acp_image_version(text: str, arg: str) -> str:
    """One exact version ARG from an ACP Dockerfile (floating forms fail)."""
    match = re.search(rf"^ARG\s+{arg}=(\S+)\s*$", text, re.MULTILINE)
    if match is None:
        raise ValueError(f"ACP Dockerfile needs an exact {arg}")
    value = match.group(1)
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[\w.]+)?", value):
        raise ValueError(f"{arg} must be an exact version; got {value!r}")
    return value


def harness_pack_manifest(name: str) -> dict:
    import tomllib

    path = ROOT / "harnesses" / name / "harness.toml"
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_acp_pack_pins_match_both_image_build_arguments() -> None:
    """每个 ACP 包的 package/version 与两个镜像的同名 ARG 逐字一致。

    GLINK-ARCH-6 把命令与上游 pin 收进包清单；镜像 ARG 仍是构建事实，两者
    漂移会让包声明的版本与镜像实际装的版本不一致（或反之）。
    """
    docker = (ROOT / "docker" / "Dockerfile.acp-worker").read_text(encoding="utf-8")
    e2b = (ROOT / "docker" / "e2b" / "Dockerfile.acp-e2b").read_text(encoding="utf-8")
    for pack, (arg, package) in ACP_PACK_PINS.items():
        manifest = harness_pack_manifest(pack)
        assert manifest["name"] == pack and manifest["family"] == "acp"
        assert manifest["package"] == package, pack
        assert manifest["version"] == acp_image_version(docker, arg), pack
        assert manifest["version"] == acp_image_version(e2b, arg), pack
        assert f'"{package}@${{{arg}}}"' in docker, pack
        assert f'"{package}@${{{arg}}}"' in e2b, pack
        assert manifest["command"], f"{pack} 清单必须声明 agent argv"


def test_a_floating_acp_image_argument_fails_the_pin_rule() -> None:
    with pytest.raises(ValueError, match="exact"):
        acp_image_version("ARG DSH_VERSION=latest\n", "DSH_VERSION")
    with pytest.raises(ValueError, match="exact"):
        acp_image_version("ARG DSH_VERSION=0.1\n", "DSH_VERSION")


def test_template_package_pin_matches_the_image_build_argument() -> None:
    """镜像与宿主声明来自同一份 pin：模板的 npm 版本必须等于对应镜像 ARG。"""
    docker_web, docker_subagents = worker_package_versions(
        (ROOT / "docker/Dockerfile.worker").read_text()
    )
    e2b_web, e2b_subagents = worker_package_versions(
        (ROOT / "docker/e2b/Dockerfile.e2b").read_text()
    )
    pins = template_package_pins()
    # GLINK-SUB-02：模板从“只加载 pi-web-access”改为同时加载 pi-subagents
    # （前台守卫扩展兜住 async）；两个 pin 都必须与镜像 ARG 对齐。
    assert set(pins) == {"pi-web-access", "pi-subagents"}
    assert pins["pi-web-access"] == docker_web == e2b_web, "pi-web-access pin 与镜像 ARG 漂移"
    assert pins["pi-subagents"] == docker_subagents == e2b_subagents, "pi-subagents pin 与镜像 ARG 漂移"


def test_worker_dockerfiles_install_and_guard_the_prebaked_packages() -> None:
    """两个镜像都要按 ARG 安装，并用同一个守卫脚本验证关键包存在。

    守卫脚本的包列表与 ``CONTAINER_LOCAL_PACKAGES`` 的完整对应关系（含 pi-subagents
    尚未被守卫覆盖的已知缺口）在 tests/test_kernel_docker_env.py 卡住。
    """
    for relative in ("docker/Dockerfile.worker", "docker/e2b/Dockerfile.e2b"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "pi-web-access@${PI_WEB_ACCESS_VERSION}" in text, relative
        assert "pi-subagents@${PI_SUBAGENTS_VERSION}" in text, relative
        assert "/opt/pi-packages" in text, relative
        # 与 pi 自己的 managed install 一致：不自动装 @earendil-works/pi-* peer。
        assert "npm install --prefix /opt/pi-packages --legacy-peer-deps" in text, relative
        assert "pi-web-access" in set(guard_packages(text)), relative
        assert "exec \"$@\"" in text, relative


@pytest.mark.parametrize("text", [
    "ARG PI_SUBAGENTS_VERSION=0.40.0\n",
    "ARG PI_WEB_ACCESS_VERSION=0.31.0\n",
    "ARG PI_WEB_ACCESS_VERSION=^0.31.0\nARG PI_SUBAGENTS_VERSION=0.40.0\n",
    "ARG PI_WEB_ACCESS_VERSION=0.31.0\nARG PI_SUBAGENTS_VERSION=latest\n",
    "ARG PI_WEB_ACCESS_VERSION=0.31.0\nARG PI_SUBAGENTS_VERSION=v0.40.0\n",
])
def test_floating_or_missing_prebaked_package_versions_fail(text: str) -> None:
    with pytest.raises(ValueError, match="exact"):
        worker_package_versions(text)


def test_guard_package_list_is_not_optional() -> None:
    with pytest.raises(ValueError, match="guard"):
        guard_packages("RUN echo no guard here\n")


def test_git_discovery_covers_tracked_secrets_and_new_sources(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".env\nnode_modules/\n")
    (tmp_path / ".env").write_text("synthetic fixture, not a credential\n")
    subprocess.run(["git", "add", "-f", ".env"], cwd=tmp_path, check=True)
    assert runtime_files(git_paths(tmp_path)) == [".env"]

    (tmp_path / "host").mkdir()
    source = tmp_path / "host/new.py"
    source.write_text("import frontier_agent\n")
    assert source_violations(tmp_path) == ["host/new.py: forbidden import frontier_agent"]
    source.write_text("from pi_glink.kernel import rpc\n")
    assert source_violations(tmp_path) == []
    source.unlink()
    source.symlink_to(tmp_path / ".env")
    assert source_violations(tmp_path) == ["host/new.py: source symlink needs explicit ownership review"]


def test_the_distribution_declares_the_kernel_subpackage() -> None:
    """The packaging config must cover ``pi_glink.kernel`` and ``pi_glink.runs``.

    The deployment unit is the repository directory (``web/``, ``agent/`` and
    ``scripts/`` are resolved relative to it); the package declaration only has
    to make the host importable, and both the kernel adapters and the run
    orchestration package are part of the host. Checked without importing
    setuptools: the declared layout must match the subpackage names.
    """
    import fnmatch  # noqa: PLC0415
    import tomllib  # noqa: PLC0415

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    setuptools = config["tool"]["setuptools"]
    assert setuptools["package-dir"] == {"": "host"}, "包根必须指向 host/"
    declared = setuptools.get("packages")
    for name in ("pi_glink.kernel", "pi_glink.runs"):
        if isinstance(declared, list):
            assert name in declared
            continue
        find = setuptools["packages"]["find"]
        assert find["where"] == ["host"]
        assert any(fnmatch.fnmatchcase(name, pattern) for pattern in find["include"]), name
