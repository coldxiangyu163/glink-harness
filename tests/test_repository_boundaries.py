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
        runtime = path.parts[0] == "data" or path.parts[:2] == ("agent", "sessions")
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
        if path.parts[0] not in {"host", "web", "agent"} or path.suffix not in _SOURCE_SUFFIXES:
            continue
        source = root / path
        if source.is_symlink():
            violations.append(f"{path}: source symlink needs explicit ownership review")
        elif source.is_file():
            for module in forbidden_imports(path, source.read_text(encoding="utf-8")):
                violations.append(f"{path}: forbidden import {module}")
    return violations


def worker_versions(text: str) -> tuple[str, str]:
    node = re.search(r"^FROM\s+(node:\d+\.\d+\.\d+-[\w.-]+)(?:@sha256:[a-f0-9]{64})?\s*$", text, re.MULTILINE)
    pi = re.search(r"^ARG\s+PI_VERSION=(\d+\.\d+\.\d+(?:-[\w.-]+)?)\s*$", text, re.MULTILINE)
    if node is None or pi is None:
        raise ValueError("worker Dockerfile needs exact Node and pi versions")
    return node.group(1), pi.group(1)


def worker_package_versions(text: str) -> tuple[str, str]:
    """The exact versions of the prebaked pi packages (both worker images).

    The repository declares both packages in ``agent/settings.json`` (the runtime
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
    """``agent/settings.json`` npm packages as ``name -> declared spec``."""
    settings = json.loads((ROOT / "agent" / "settings.json").read_text(encoding="utf-8"))
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


def test_worker_version_declarations_are_exact_and_equal() -> None:
    docker = worker_versions((ROOT / "docker/Dockerfile.worker").read_text())
    e2b = worker_versions((ROOT / "docker/e2b/Dockerfile.e2b").read_text())
    assert docker == e2b, "Docker/E2B version declarations diverged; this does not check deployed images"


@pytest.mark.parametrize("name", [".env", ".env.backup", "agent/auth.json", "agent/web-search.json", "agent/models-store.json", "agent/sessions/a.jsonl", "data/runs/a/meta.json"])
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
    assert forbidden_imports(Path("agent/extensions/example.ts"), text)


def test_public_extensions_comments_and_local_python_imports_are_allowed() -> None:
    assert forbidden_imports(Path("agent/extensions/example.ts"), """
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
    """The packaging config must cover ``pi_glink.kernel``.

    The deployment unit is the repository directory (``web/``, ``agent/`` and
    ``scripts/`` are resolved relative to it); the package declaration only has
    to make the host importable, and ``pi_glink.kernel`` is part of the host.
    Checked without importing setuptools: the declared layout must match the
    subpackage name.
    """
    import fnmatch  # noqa: PLC0415
    import tomllib  # noqa: PLC0415

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    setuptools = config["tool"]["setuptools"]
    assert setuptools["package-dir"] == {"": "host"}, "包根必须指向 host/"
    declared = setuptools.get("packages")
    if isinstance(declared, list):
        assert "pi_glink.kernel" in declared
        return
    find = setuptools["packages"]["find"]
    assert find["where"] == ["host"]
    assert any(fnmatch.fnmatchcase("pi_glink.kernel", pattern) for pattern in find["include"])
