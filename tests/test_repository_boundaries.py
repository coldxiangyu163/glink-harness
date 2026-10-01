"""Mechanical repository boundaries; not a source-copy detector or permission system."""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import subprocess
import sys
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


# 每个 kernel 文件允许导入：标准库、kernel 内部，及下表列出的外部模块。
# 下表是 glink_harness 宿主依赖的唯一许可真源；新增文件必须显式登记（即使暂无宿主依赖）。
# config 提供运行期只读配置；artifacts 提供安全产物树；redaction 提供凭据清扫；
# contracts 是共享的中性词汇；events/event_schema 提供结构化事件的词汇/模式；
# 根包的 __version__ 用于 ACP 协议版本；store 仅族基类 harness.py 与窄协议适配器
# run_views.py（R1-1 会话来源协议的宿主侧 RunStore 实现）读取记录/会话种子；
# pi 族自 R1-2、ACP 族自 R1-3 起只经 SessionSource 协议访问这些事实，不再
# import store。
# 第三方 e2b SDK 只在 E2B 适配器延迟加载，不把所有第三方包视作标准库。
_KERNEL_IMPORT_ALLOWLIST: dict[str, frozenset[str]] = {
    "__init__.py": frozenset(),
    "acp_child_progress.py": frozenset({"zstandard"}),  # ACP 会话压缩格式的可选解码器
    "acp_e2b_env.py": frozenset({"glink_harness.artifacts", "glink_harness.redaction"}),
    "archive_channel.py": frozenset(),  # 归档通道自足：标准库 tar/json/hashlib
    "acp_env.py": frozenset({"glink_harness.artifacts", "glink_harness.config", "glink_harness.redaction"}),
    "acp_execution.py": frozenset({"glink_harness.__version__", "glink_harness.config"}),
    "acp_session_config.py": frozenset(),
    "acp_state_compat.py": frozenset({"glink_harness.artifacts"}),  # 已知退役审计的目标副本兼容
    "acp_projection.py": frozenset({"glink_harness.events", "glink_harness.redaction"}),
    "acp_rpc.py": frozenset(),
    "acp_sandbox_park.py": frozenset({"glink_harness.config", "glink_harness.contracts"}),  # 沙箱泊车只读宿主配置的 TTL
    "agent_env.py": frozenset({"glink_harness.config"}),
    "child_progress.py": frozenset(),
    "conversation_lease.py": frozenset({"glink_harness.contracts"}),  # 租约台账事实：纯标准库，自足
    "docker_env.py": frozenset({"glink_harness.config"}),
    "e2b_env.py": frozenset({"e2b", "glink_harness.artifacts", "glink_harness.config"}),
    "execution.py": frozenset({"glink_harness.config"}),
    "framing.py": frozenset(),
    "harness.py": frozenset({"glink_harness.config", "glink_harness.contracts", "glink_harness.redaction", "glink_harness.store"}),
    "harness_acp.py": frozenset({"glink_harness.config", "glink_harness.contracts", "glink_harness.events", "glink_harness.redaction"}),  # R1-3：ACP 族只经 SessionSource 协议，不再 import store
    "harness_pi.py": frozenset({"glink_harness.artifacts", "glink_harness.config", "glink_harness.contracts", "glink_harness.redaction"}),  # R1-2：pi 族只经 SessionSource 协议，不再 import store
    "outcome.py": frozenset(),
    "projection.py": frozenset({"glink_harness.event_schema", "glink_harness.events", "glink_harness.redaction"}),
    "rpc.py": frozenset(),
    "run_views.py": frozenset({"glink_harness.artifacts", "glink_harness.store"}),  # R1-1 窄协议与宿主侧 RunStore 适配器
    "sandbox.py": frozenset({"glink_harness.config"}),
    "subagent_artifacts.py": frozenset(),
    "sweep.py": frozenset(),
}
# 反表不是许可来源：阻止通过修改上表重新放行原先明确禁止的编排模块。
_KERNEL_MUST_REJECT = frozenset({
    "glink_harness.runs", "glink_harness.tenancy", "glink_harness.auth", "glink_harness.accounts",
    "glink_harness.ownership", "glink_harness.blueprints", "glink_harness.app", "glink_harness.cli",
    "glink_harness.web_routes", "glink_harness.web_anything",
})


def kernel_forbidden_imports(relative: Path, text: str) -> list[str]:
    """Resolve static and literal dynamic imports; deny anything outside this file's permissions."""
    package = list(relative.with_suffix("").parts)[1:-1]  # drop host/ and the filename
    allowed = _KERNEL_IMPORT_ALLOWLIST.get(relative.name)
    if allowed is None:
        return [f"{relative}: unlisted kernel module"]
    modules: list[str] = []
    for node in ast.walk(ast.parse(text, filename=str(relative))):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                module = ".".join([*base, node.module] if node.module else base)
            else:
                module = node.module or ""
            # `from .. import app` targets glink_harness.app, not the root package.
            if module == "glink_harness":
                modules.extend(f"glink_harness.{alias.name}" for alias in node.names)
            else:
                modules.append(module)
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
        if module.startswith("glink_harness.kernel.") or module == "glink_harness.kernel":
            continue
        # Only a listed root-level host module may be used; submodules of an
        # allowed host module are not implicitly allowed to widen the boundary.
        if module in allowed or module.split(".")[0] in sys.stdlib_module_names and not module.startswith("glink_harness"):
            continue
        violations.append(f"{relative}: forbidden import {module}")
    return violations


def kernel_import_violations(root: Path) -> list[str]:
    """Run the direction rule over every module currently in the kernel package."""
    violations = []
    for path in sorted((root / "host" / "glink_harness" / "kernel").glob("*.py")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        violations.extend(kernel_forbidden_imports(relative, path.read_text(encoding="utf-8")))
    return violations


#: 编排层（``runs/runner.py``）对接入层的依赖只经抽象（docs/architecture.md §4）：
#: 族与 sandbox 的选择入口、执行契约与运行期协议。模块 -> 允许导入的名字；
#: ``None`` 表示整个模块都是抽象。具体族（``harness_pi`` / ``harness_acp``、
#: ``acp_*``）、具体 sandbox（``docker_env`` / ``e2b_env``、``InprocSandbox`` 等）
#: 与具体运行期（``AgentRuntime`` / ``AcpRuntime``）都不在内：runner 不按类型分派，
#: 也就写不出两族运行期的联合类型。
_RUNNER_KERNEL_ALLOWLIST: dict[str, frozenset[str] | None] = {
    "glink_harness.kernel.harness": None,
    "glink_harness.kernel.execution": frozenset({
        "ExecutionAdapter", "ExecutionSpec", "ExecutionOutcome", "CleanupReport", "SessionInfo",
    }),
    "glink_harness.kernel.sweep": None,
    "glink_harness.kernel.sandbox": frozenset(
        {"Sandbox", "SANDBOXES", "open_sandbox", "Launch", "Spawn", "CleanupReport"}
    ),
    "glink_harness.kernel.agent_env": frozenset({"RunCredentials"}),
}
_RUNNER = Path("host/glink_harness/runs/runner.py")


def runner_kernel_imports(relative: Path, text: str) -> list[str]:
    """Kernel imports of the orchestration module outside the abstraction allowlist."""
    package = list(relative.with_suffix("").parts)[1:-1]
    tree = ast.parse(text, filename=str(relative))
    library_names = {"importlib"}
    loader_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            library_names.update(alias.asname or alias.name for alias in node.names
                                 if alias.name == "importlib")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "importlib":
            loader_names.update(alias.asname or alias.name for alias in node.names
                                if alias.name == "import_module")
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            function = node.func
            dynamic = (isinstance(function, ast.Name)
                       and (function.id == "__import__" or function.id in loader_names))
            dynamic |= (isinstance(function, ast.Attribute)
                        and isinstance(function.value, ast.Name)
                        and function.value.id in library_names and function.attr == "import_module")
            if (dynamic and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                module = node.args[0].value
                if module.startswith("."):
                    argument = (node.args[1] if len(node.args) > 1 else
                                next((kw.value for kw in node.keywords if kw.arg == "package"), None))
                    base = (argument.value if isinstance(argument, ast.Constant)
                            and isinstance(argument.value, str) else ".".join(package))
                    try:
                        module = importlib.util.resolve_name(module, base)
                    except (ImportError, ValueError):
                        violations.append(f"{relative}: unresolvable relative import {module}")
                if module.startswith("glink_harness.kernel"):
                    violations.append(f"{relative}: forbidden import {module}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("glink_harness.kernel"):
                    # ``import glink_harness.kernel.x`` exposes every name: not an abstraction edge.
                    violations.append(f"{relative}: forbidden import {alias.name}")
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            base = package[: len(package) - node.level + 1]
            module = ".".join([*base, node.module] if node.module else base)
        else:
            module = node.module or ""
        if module == "glink_harness.kernel":
            names = [alias.name for alias in node.names]
            violations.extend(f"{relative}: forbidden import glink_harness.kernel.{name}" for name in names)
            continue
        if not module.startswith("glink_harness.kernel."):
            continue
        if module not in _RUNNER_KERNEL_ALLOWLIST:
            violations.append(f"{relative}: forbidden import {module}")
            continue
        allowed = _RUNNER_KERNEL_ALLOWLIST[module]
        if allowed is None:
            continue
        for alias in node.names:
            if alias.name not in allowed:
                violations.append(f"{relative}: forbidden import {module}.{alias.name}")
    return violations


def worker_node_tag(text: str) -> str:
    node = re.search(r"^FROM\s+(node:\d+\.\d+\.\d+-[\w.-]+)(?:@sha256:[a-f0-9]{64})?\s*$", text, re.MULTILINE)
    if node is None:
        raise ValueError("worker Dockerfile needs an exact Node tag")
    return node.group(1)


def guard_packages(text: str) -> list[str]:
    """The package names the image-local guard script refuses to start without."""
    match = re.search(r"for pkg in ([^;\n]+); do", text)
    if match is None:
        raise ValueError("worker Dockerfile needs the prebaked package guard")
    return match.group(1).split()


def pack_artifacts_module():
    """``scripts/generate-pack-artifacts.py`` loaded by path (it is not a package)."""
    import importlib.util
    import sys

    path = ROOT / "scripts" / "generate-pack-artifacts.py"
    spec = importlib.util.spec_from_file_location("generate_pack_artifacts", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


_ARTIFACT_SOURCES = (
    "docker/Dockerfile.worker",
    "docker/e2b/Dockerfile.e2b",
    "docker/Dockerfile.acp-worker",
    "docker/e2b/Dockerfile.acp-e2b",
    "docker/e2b/templates.json",
    "harnesses/pi/settings.json",
)


def synthetic_repo(tmp_path: Path) -> Path:
    """A copy of the manifests and generated artifacts in a temporary directory."""
    import shutil

    root = tmp_path / "repo"
    for relative in _ARTIFACT_SOURCES:
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, root / relative)
    for manifest in (ROOT / "harnesses").glob("*/harness.toml"):
        target = root / manifest.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(manifest, target)
    return root


def test_no_runtime_credentials_or_data_are_tracked() -> None:
    assert runtime_files(git_paths(ROOT)) == []


def test_production_sources_respect_external_kernel_boundaries() -> None:
    assert source_violations(ROOT) == []


def test_kernel_layer_does_not_import_the_orchestration_layer() -> None:
    assert kernel_import_violations(ROOT) == []


def test_kernel_import_direction_rejects_orchestration_and_store() -> None:
    """反例：kernel 反向导入编排层/宿主模块或绕过白名单读 store 必须变红。"""
    kernel_module = Path("host/glink_harness/kernel/acp_env.py")
    assert kernel_forbidden_imports(kernel_module, "from ..runs import RunManager") == [
        "host/glink_harness/kernel/acp_env.py: forbidden import glink_harness.runs"
    ]
    assert kernel_forbidden_imports(kernel_module, "import glink_harness.runs.runner") != []
    assert kernel_forbidden_imports(kernel_module, "from ..store import RunRecord") == [
        "host/glink_harness/kernel/acp_env.py: forbidden import glink_harness.store"
    ]
    assert kernel_forbidden_imports(kernel_module, "from glink_harness.tenancy import TenantContext") != []
    assert kernel_forbidden_imports(kernel_module, "from ..web_routes import HostRequestHandler") != []
    assert kernel_forbidden_imports(kernel_module, "__import__('glink_harness.blueprints')") != []
    # 反例在编排层同范围成立：族模块也不能反向依赖编排层。
    assert kernel_forbidden_imports(Path("host/glink_harness/kernel/harness.py"), "from ..runs import RunManager") != []


def test_kernel_import_direction_allows_lower_layers_and_the_store_whitelist() -> None:
    kernel_module = Path("host/glink_harness/kernel/harness_pi.py")
    assert kernel_forbidden_imports(kernel_module, "from ..config import HostConfig") == []
    assert kernel_forbidden_imports(Path("host/glink_harness/kernel/acp_env.py"), "from ..redaction import SecretRedactor") == []
    assert kernel_forbidden_imports(kernel_module, "from .harness import HarnessFamily") == []
    assert kernel_forbidden_imports(kernel_module, "from ..contracts import Verdict") == []
    assert kernel_forbidden_imports(kernel_module, "from pathlib import Path") == []
    assert kernel_forbidden_imports(Path("host/glink_harness/kernel/e2b_env.py"), "from e2b import Sandbox") == []
    # R1-2：pi 族已移出 store 白名单，反向回归必须变红。
    assert kernel_forbidden_imports(kernel_module, "from ..store import RunRecord") == [
        "host/glink_harness/kernel/harness_pi.py: forbidden import glink_harness.store"
    ]
    # R1-3：ACP 族同样移出 store 白名单，反向回归必须变红。
    acp_module = Path("host/glink_harness/kernel/harness_acp.py")
    assert kernel_forbidden_imports(acp_module, "from ..store import RunRecord") == [
        "host/glink_harness/kernel/harness_acp.py: forbidden import glink_harness.store"
    ]
    for name in ("harness.py", "run_views.py"):
        relative = Path("host/glink_harness/kernel") / name
        assert kernel_forbidden_imports(relative, "from ..store import RunRecord") == [], relative


def test_kernel_rule_rejects_unlisted_parent_module_and_root_alias(tmp_path: Path) -> None:
    """新增接入层模块不能借未列名的宿主模块绕过导入方向。"""
    kernel_dir = tmp_path / "host" / "glink_harness" / "kernel"
    kernel_dir.mkdir(parents=True)
    source = kernel_dir / "acp_env.py"
    for text, name in (
        ("from ..accounts import AccountStore\n", "glink_harness.accounts"),
        ("from ..app import main\n", "glink_harness.app"),
        ("from .. import app\n", "glink_harness.app"),
        ("from ..new_orchestrator import run\n", "glink_harness.new_orchestrator"),
        ("importlib.import_module('glink_harness.new_orchestrator')\n", "glink_harness.new_orchestrator"),
    ):
        source.write_text(text)
        assert kernel_import_violations(tmp_path) == [
            f"host/glink_harness/kernel/acp_env.py: forbidden import {name}"
        ]


def test_kernel_import_direction_covers_new_modules(tmp_path: Path) -> None:
    kernel_dir = tmp_path / "host" / "glink_harness" / "kernel"
    kernel_dir.mkdir(parents=True)
    source = kernel_dir / "new.py"
    source.write_text("from ..runs import RunManager\n")
    assert kernel_import_violations(tmp_path) == ["host/glink_harness/kernel/new.py: unlisted kernel module"]
    source.write_text("from ..kernel.harness import HarnessFamily\n")
    assert kernel_import_violations(tmp_path) == ["host/glink_harness/kernel/new.py: unlisted kernel module"]


def test_kernel_allowlist_cannot_relicense_orchestration_modules() -> None:
    assert set(_KERNEL_IMPORT_ALLOWLIST) == {p.name for p in (ROOT / "host/glink_harness/kernel").glob("*.py")}
    for relative, allowed in _KERNEL_IMPORT_ALLOWLIST.items():
        assert not any(
            name == denied or name.startswith(denied + ".")
            for name in allowed for denied in _KERNEL_MUST_REJECT
        ), relative
        assert not any(name.startswith("glink_harness.web_") for name in allowed), relative
        assert not any(name == "glink_harness.store" or name.startswith("glink_harness.store.") for name in allowed) or relative in {
            "harness.py", "run_views.py"
        }, relative
    for name in _KERNEL_MUST_REJECT:
        assert kernel_forbidden_imports(Path("host/glink_harness/kernel/harness.py"), f"__import__({name!r})")


def test_session_source_protocol_covers_the_six_faces_without_runstore() -> None:
    """R1-1：窄协议覆盖设计 §2.3 的六个协议面，且协议面不引用 RunStore 具体类型。

    方法面 ⇄ 协议面：get（父记录只读视图）、iter_events（父 run 事件扫描/
    能力矩阵）、run_dir/session_dir/conversations_root/conversation_dir
    （目录布局推导）、read_session_seed（父会话种子有界读）、
    read_children/read_child/write_child（子代理文档双向读写）；
    e2b 租约/泊车事实由 get + conversations_root/conversation_dir 组合
    （租约文件读取本体在 kernel/conversation_lease.py，不经 store）。
    适配器必须显式以协议为基类、且不得继承 RunStore（收窄才成立）。
    """
    relative = Path("host/glink_harness/kernel/run_views.py")
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"), filename=str(relative))
    classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
    protocol = classes["SessionSource"]
    methods = {
        node.name: node for node in protocol.body if isinstance(node, ast.FunctionDef)
    }
    expected = {
        "get", "iter_events",
        "run_dir", "session_dir", "conversations_root", "conversation_dir",
        "read_session_seed",
        "read_children", "read_child", "write_child",
    }
    assert expected <= set(methods)
    for method in methods.values():
        arguments = [
            *method.args.posonlyargs, *method.args.args, *method.args.kwonlyargs,
        ]
        segments = [ast.unparse(arg.annotation) for arg in arguments if arg.annotation]
        if method.returns is not None:
            segments.append(ast.unparse(method.returns))
        assert all("RunStore" not in segment for segment in segments), method.name
    adapter = classes["RunStoreSessionSource"]
    bases = {ast.unparse(base) for base in adapter.bases}
    assert "SessionSource" in bases
    assert "RunStore" not in bases


def host_module_imports(relative: Path, text: str) -> list[str]:
    """Absolute module names imported by one host module (relative resolved)."""
    package = list(relative.with_suffix("").parts)[1:-1]
    modules: list[str] = []
    for node in ast.walk(ast.parse(text, filename=str(relative))):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                modules.append(".".join([*base, node.module] if node.module else base))
            elif node.module:
                modules.append(node.module)
    return modules


def test_contract_vocabulary_module_stays_a_leaf() -> None:
    """契约词汇模块是两层共享的叶子，不得反向导入 kernel 或编排层。

    它被 kernel 与编排层同时导入；若它自己反向导入任一层，方向规则就会被
    绕成环/通道，这里如实卡住。
    """
    relative = Path("host/glink_harness/contracts.py")
    imported = host_module_imports(relative, (ROOT / relative).read_text(encoding="utf-8"))
    assert [name for name in imported if name.split(".")[0] == "glink_harness"] == []


def test_contract_vocabulary_lives_in_the_neutral_module() -> None:
    """M16：公开拒绝码与终态词汇的定义点是中性契约模块，不是 kernel 转发。

    对基线红：``glink_harness.contracts`` 不存在，两个类型只能从
    ``kernel.harness``（或经 ``runs.runner`` 转发）导入；修复后定义点、
    HTTP 消费方都直指契约模块。
    """
    from glink_harness.contracts import FollowupUnavailable, Verdict  # noqa: PLC0415

    assert FollowupUnavailable.__module__ == "glink_harness.contracts"
    assert Verdict.__module__ == "glink_harness.contracts"
    import glink_harness.web_routes as web_routes  # noqa: PLC0415

    assert web_routes.FollowupUnavailable is FollowupUnavailable


def test_runner_depends_on_kernel_abstractions_only() -> None:
    assert runner_kernel_imports(_RUNNER, (ROOT / _RUNNER).read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "text",
    [
        "from ..kernel.acp_env import AcpRuntime",
        "from ..kernel.acp_execution import AcpExecutionAdapter",
        "from ..kernel.harness_pi import PiHarnessFamily",
        "from ..kernel.docker_env import container_name_for",
        "from ..kernel.e2b_env import SANDBOX_WORKSPACE",
        "from ..kernel.sandbox import DockerSandbox",
        "from ..kernel.agent_env import AgentRuntime",
        "from ..kernel.projection import Projector",
        "from ..kernel import acp_env",
        "import glink_harness.kernel.acp_env",
        "from glink_harness.kernel.acp_env import AcpRuntime",
    ],
)
def test_runner_rule_rejects_concrete_families_sandboxes_and_runtimes(text: str) -> None:
    """反例：runner 导入具体族/sandbox/运行期（任一写法）都必须变红。"""
    assert runner_kernel_imports(_RUNNER, text) != []


def test_runner_rule_allows_the_abstractions() -> None:
    text = (
        "from ..contracts import FollowupUnavailable, Verdict\n"
        "from ..kernel.harness import HarnessFamily, FrameProjector, select_family\n"
        "from ..kernel.execution import ExecutionAdapter\n"
        "from ..kernel.sandbox import Sandbox, open_sandbox\n"
        "from ..kernel.agent_env import RunCredentials\n"
        "from ..kernel.sweep import SessionSweep\n"
        "from ..redaction import credential_secrets_from_text\n"
    )
    assert runner_kernel_imports(_RUNNER, text) == []


def test_worker_node_tags_are_exact_and_equal() -> None:
    """Node 基础镜像不是包的属性：四个 Dockerfile 手写同一精确 tag。"""
    tags = {
        relative: worker_node_tag((ROOT / relative).read_text())
        for relative in _ARTIFACT_SOURCES
        if relative.endswith(("Dockerfile.worker", "Dockerfile.e2b", "Dockerfile.acp-worker", "Dockerfile.acp-e2b"))
    }
    assert len(set(tags.values())) == 1, f"Node tag diverged: {tags}; this does not check deployed images"


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
    assert forbidden_imports(Path("host/module.py"), "from . import frontier_agent\nfrom glink_harness.kernel import rpc") == []


@pytest.mark.parametrize("text", [
    "FROM node:24-bookworm-slim\n",
    "FROM node:latest\n",
    "ARG PI_VERSION=0.86.1\n",
])
def test_floating_or_missing_worker_node_tag_fails(text: str) -> None:
    with pytest.raises(ValueError, match="exact"):
        worker_node_tag(text)


def test_generated_artifacts_match_the_pack_manifests() -> None:
    """清单是唯一真源：Dockerfile ARG、templates.json 与 settings.json 必须等于生成结果。

    修复方式是改 harnesses/<包>/harness.toml 后运行
    ``python3 scripts/generate-pack-artifacts.py``，不是手改产物。
    """
    module = pack_artifacts_module()
    outputs = module.render_all(ROOT)
    assert set(outputs) == set(_ARTIFACT_SOURCES)
    assert module.drift(ROOT, outputs) == []


def test_every_manifest_package_is_installed_from_its_build_argument() -> None:
    """生成器只写 ARG 值；每个镜像的安装命令必须用 ``<package>@${ARG}`` 引用它。"""
    module = pack_artifacts_module()
    packs = module.load_packs(ROOT / "harnesses")
    for pack in packs:
        build = module.IMAGES[pack.image.target]
        for relative in build.dockerfiles:
            text = (ROOT / relative).read_text(encoding="utf-8")
            for item in pack.image.packages:
                assert f"{item.package}@${{{item.arg}}}" in text, (pack.name, relative, item.arg)


def test_templates_json_carries_the_e2b_parameters_the_config_defaults_name() -> None:
    from glink_harness import config

    templates = json.loads((ROOT / "docker/e2b/templates.json").read_text(encoding="utf-8"))["templates"]
    assert templates["pi-worker"]["alias"] == config._DEFAULT_E2B_TEMPLATE
    assert templates["acp-worker"]["alias"] == config._DEFAULT_ACP_E2B_TEMPLATE
    assert "--dockerfile docker/e2b/Dockerfile.acp-e2b" in templates["acp-worker"]["build_command"]


def test_generator_detects_a_hand_edited_build_argument(tmp_path: Path) -> None:
    module = pack_artifacts_module()
    root = synthetic_repo(tmp_path)
    assert module.drift(root, module.render_all(root)) == []
    dockerfile = root / "docker/e2b/Dockerfile.acp-e2b"
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8").replace("ARG DSH_VERSION=0.1.5-rc.3", "ARG DSH_VERSION=9.9.9"),
        encoding="utf-8",
    )
    assert module.drift(root, module.render_all(root)) == ["docker/e2b/Dockerfile.acp-e2b"]


def test_generator_detects_a_manifest_bump_until_regenerated(tmp_path: Path) -> None:
    module = pack_artifacts_module()
    root = synthetic_repo(tmp_path)
    manifest = root / "harnesses/pi/harness.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace('version = "0.31.0"', 'version = "0.32.0"'),
        encoding="utf-8",
    )
    outputs = module.render_all(root)
    assert set(module.drift(root, outputs)) == {
        "docker/Dockerfile.worker",
        "docker/e2b/Dockerfile.e2b",
        "docker/e2b/templates.json",
        "harnesses/pi/settings.json",
    }
    assert "ARG PI_WEB_ACCESS_VERSION=0.32.0\n" in outputs["docker/Dockerfile.worker"]
    assert "npm:pi-web-access@0.32.0" in outputs["harnesses/pi/settings.json"]


def test_generator_refuses_an_undeclared_version_pin(tmp_path: Path) -> None:
    module = pack_artifacts_module()
    root = synthetic_repo(tmp_path)
    dockerfile = root / "docker/Dockerfile.acp-worker"
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8") + "ARG EXTRA_AGENT_VERSION=1.0.0\n", encoding="utf-8"
    )
    with pytest.raises(module.GenerationError, match="EXTRA_AGENT_VERSION"):
        module.render_all(root)


def test_generator_refuses_a_missing_or_duplicated_build_argument(tmp_path: Path) -> None:
    module = pack_artifacts_module()
    root = synthetic_repo(tmp_path)
    dockerfile = root / "docker/e2b/Dockerfile.e2b"
    original = dockerfile.read_text(encoding="utf-8")
    dockerfile.write_text(original.replace("ARG PI_SUBAGENTS_VERSION=0.40.0\n", ""), encoding="utf-8")
    with pytest.raises(module.GenerationError, match="PI_SUBAGENTS_VERSION"):
        module.render_all(root)
    dockerfile.write_text(original + "ARG PI_SUBAGENTS_VERSION=0.40.0\n", encoding="utf-8")
    with pytest.raises(module.GenerationError, match="恰好出现一次"):
        module.render_all(root)


def test_generator_refuses_conflicting_declarations_in_one_image(tmp_path: Path) -> None:
    module = pack_artifacts_module()
    root = synthetic_repo(tmp_path)
    manifest = root / "harnesses/codex/harness.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + '\n[[image.packages]]\narg = "DSH_VERSION"\npackage = "@deepseek-ai/dsh"\n'
        'version = "0.2.0"\nrole = "agent"\n',
        encoding="utf-8",
    )
    with pytest.raises(module.GenerationError, match="冲突"):
        module.render_all(root)


def test_worker_dockerfiles_install_and_guard_the_prebaked_packages() -> None:
    """两个镜像都预装到同一路径，并用同一个守卫脚本验证关键包存在。

    按 ARG 安装由 :func:`test_every_manifest_package_is_installed_from_its_build_argument`
    卡住；守卫脚本的包列表与 pi 包清单预装包名的完整对应关系（含 pi-subagents
    尚未被守卫覆盖的已知缺口）在 tests/test_kernel_docker_env.py 卡住。
    """
    for relative in ("docker/Dockerfile.worker", "docker/e2b/Dockerfile.e2b"):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "/opt/pi-packages" in text, relative
        # 与 pi 自己的 managed install 一致：不自动装 @earendil-works/pi-* peer。
        assert "npm install --prefix /opt/pi-packages --legacy-peer-deps" in text, relative
        assert "pi-web-access" in set(guard_packages(text)), relative
        assert "exec \"$@\"" in text, relative


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
    source.write_text("from glink_harness.kernel import rpc\n")
    assert source_violations(tmp_path) == []
    source.unlink()
    source.symlink_to(tmp_path / ".env")
    assert source_violations(tmp_path) == ["host/new.py: source symlink needs explicit ownership review"]


def test_the_distribution_declares_the_kernel_subpackage() -> None:
    """The packaging config must cover ``glink_harness.kernel`` and ``glink_harness.runs``.

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
    for name in ("glink_harness.kernel", "glink_harness.runs"):
        if isinstance(declared, list):
            assert name in declared
            continue
        find = setuptools["packages"]["find"]
        assert find["where"] == ["host"]
        assert any(fnmatch.fnmatchcase(name, pattern) for pattern in find["include"]), name


def test_lifecycle_stages_depend_on_kernel_abstractions_only():
    path = Path("host/glink_harness/runs/stages.py")
    assert runner_kernel_imports(path, (ROOT / path).read_text(encoding="utf-8")) == []


def test_runs_package_never_imports_harness_specific_modules():
    for path in sorted((ROOT / "host/glink_harness/runs").rglob("*.py")):
        relative = path.relative_to(ROOT)
        # The existing resolver understands aliases, absolute/relative imports
        # and root-package imports. Do not permit concrete families in new
        # lifecycle modules just because the runner was split into stages.
        violations = runner_kernel_imports(relative, path.read_text(encoding="utf-8"))
        assert not [v for v in violations if any(name in v for name in (
            "kernel.harness_acp", "kernel.harness_pi", "kernel.acp_",
            "AcpRuntime", "AgentRuntime", "PiExecutionAdapter",
        ))], violations


@pytest.mark.parametrize("source", [
    "__import__('glink_harness.kernel.harness_acp')",
    "import importlib; importlib.import_module('glink_harness.kernel.harness_pi')",
])
def test_orchestration_guard_rejects_literal_dynamic_kernel_imports(source):
    assert runner_kernel_imports(Path("host/glink_harness/runs/stages.py"), source)


def test_abstract_execution_module_does_not_expose_a_concrete_driver_to_stages():
    assert runner_kernel_imports(Path("host/glink_harness/runs/stages.py"),
                                 "from ..kernel.execution import PiExecutionAdapter")


@pytest.mark.parametrize("source", [
    "import importlib; importlib.import_module('..kernel.harness_acp', __package__)",
    "from importlib import import_module; import_module('glink_harness.kernel.harness_acp')",
    "import importlib as il; il.import_module('glink_harness.kernel.harness_acp')",
    "from importlib import import_module as load; load('..kernel.harness_acp', __package__)",
])
def test_orchestration_dynamic_import_aliases_and_relative_names_cannot_bypass_guard(source):
    violations = runner_kernel_imports(Path("host/glink_harness/runs/stages.py"), source)
    assert violations and "glink_harness.kernel.harness_acp" in violations[0]


def test_dynamic_relative_host_imports_are_not_mistaken_for_kernel_dependencies():
    assert runner_kernel_imports(Path("host/glink_harness/runs/stages.py"),
                                 "import importlib as il; il.import_module('.plugins', __package__)") == []
