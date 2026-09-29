"""Exercise the check wrapper without recursively running tests or a live agent."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from shlex import quote

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check.sh"


def check_fixture(tmp_path: Path, *, python_status: int = 0, node_status: int = 0, preflight_status: int = 0, optional_status: int = 0, dev_status: int = 0):
    repo = tmp_path / "checkout with spaces"
    for directory in (
        "scripts",
        ".venv/bin",
        "tests/web",
        "tests/extensions",
        "harnesses/dsh/bundles/dsh-web-fetch-jina/tests/support",
        "host",
        "tools",
    ):
        (repo / directory).mkdir(parents=True)
    shutil.copyfile(SCRIPT, repo / "scripts/check.sh")
    (repo / "tests/web/example.test.mjs").write_text("// discovered but not executed by the stub\n")
    (repo / "tests/extensions/example.test.mjs").write_text(
        "// discovered but not executed by the stub\n"
    )
    (repo / "harnesses/dsh/bundles/dsh-web-fetch-jina/tests/example.test.mjs").write_text(
        "// discovered but not executed by the stub\n"
    )
    (repo / "harnesses/dsh/bundles/dsh-web-fetch-jina/tests/support/register-stubs.mjs").write_text(
        "// discovered but not executed by the stub\n"
    )
    python = repo / ".venv/bin/python"
    python.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "-c" ]; then exit {preflight_status}; fi\n'
        # 两次内联脚本调用：先可选依赖预检，再开发工具预检（用标记文件区分）。
        'if [ "$1" = "-" ]; then\n'
        f'  if [ -e {quote(str(repo / "dev-precheck.marker"))} ]; then exit {dev_status}; fi\n'
        f'  : > {quote(str(repo / "dev-precheck.marker"))}\n'
        f'  exit {optional_status};\n'
        'fi\n'
        f'/usr/bin/env > {quote(str(repo / "python-env.txt"))}\n'
        f'printf "%s\\n" "$@" > {quote(str(repo / "python-args.txt"))}\n'
        f'printf "%s " "$@" >> {quote(str(repo / "python-calls.txt"))}\n'
        f'printf "\\n" >> {quote(str(repo / "python-calls.txt"))}\n'
        f"exit {python_status}\n"
    )
    python.chmod(0o755)
    node = repo / "tools/node"
    node.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-e" ]; then exit 0; fi\n'
        f'/usr/bin/env > {quote(str(repo / "node-env.txt"))}\n'
        f'printf "%s\\n" "$@" >> {quote(str(repo / "node-args.txt"))}\n'
        f"exit {node_status}\n"
    )
    node.chmod(0o755)
    temp = tmp_path / "temporary"
    temp.mkdir()
    personal = tmp_path / "personal"
    personal.mkdir()
    env = {
        "PATH": f"{repo / 'tools'}{os.pathsep}{os.defpath}",
        "HOME": str(personal),
        "TMPDIR": str(temp),
        "OPENAI_API_KEY": "synthetic-model-credential",
        "E2B_API_KEY": "synthetic-cloud-credential",
        "HTTPS_PROXY": "http://invalid.example:1",
        "PI_CODING_AGENT_DIR": str(personal / ".pi"),
        "PYTHONPATH": str(personal),
        "PYTEST_ADDOPTS": "--collect-only",
        "NODE_OPTIONS": "--inspect",
    }
    return repo, env, temp


def optional_preflight_function():
    """执行 check.sh 内嵌的同一份 Python 预检逻辑，不触及真实依赖安装。"""
    script = SCRIPT.read_text()
    match = re.search(r"check_optional_deps\(\) \{\n.*?<<'PY'\n(.*?)\nPY\n\}", script, re.S)
    assert match, "check.sh 必须在隔离环境下定义可复用的可选依赖预检"
    namespace = {"__name__": "check_optional_deps_test"}
    exec(compile(match.group(1), str(SCRIPT), "exec"), namespace)
    assert script.index("check_optional_deps") < script.index("run_check '事件 schema")
    assert "if ! check_optional_deps; then" in script
    return namespace["missing_optional_dependencies"]


def test_optional_preflight_fails_closed_on_missing_extras() -> None:
    check = optional_preflight_function()

    def fake_import(name: str):
        if name in {"zstandard", "e2b"}:
            raise ModuleNotFoundError(name)
        return object()

    assert check(fake_import) == ["zstandard", "e2b"]
    for absent in ("zstandard", "e2b"):
        # 默认参数绑定循环变量：闭包只往回看一次，B023 不再把它当逃生管。
        def import_one_missing(name: str, _absent: str = absent):
            if name == _absent:
                raise ModuleNotFoundError(name)
            return object()
        assert check(import_one_missing) == [absent]
    assert check(lambda _: object()) == []
    assert "acp-progress" in SCRIPT.read_text()
    assert '".[e2b]"' in SCRIPT.read_text()


def invoke(repo: Path, env: dict[str, str], *args: str):
    return subprocess.run(
        ["/bin/bash", str(repo / "scripts/check.sh"), *args],
        cwd=repo.parent,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def read_env(path: Path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def test_checks_use_disposable_environment_and_report_optional_scope(tmp_path: Path) -> None:
    repo, env, temp = check_fixture(tmp_path)
    result = invoke(repo, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[SKIP]" in result.stdout
    assert "不包含浏览器" in result.stdout
    for name in ("python", "node"):
        actual = read_env(repo / f"{name}-env.txt")
        for secret in ("OPENAI_API_KEY", "E2B_API_KEY", "HTTPS_PROXY", "PI_CODING_AGENT_DIR", "PYTEST_ADDOPTS", "NODE_OPTIONS"):
            assert secret not in actual
        home = Path(actual["HOME"])
        assert home.is_relative_to(temp)
        assert not home.exists(), "temporary HOME must be removed after success"
        assert actual["PYTHONPATH"] == str(repo / "host")
        assert actual["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    # Python 调用顺序：schema 生成物 → ruff → mypy → pytest（带覆盖率采集）→ 覆盖率 ratchet。
    calls = [line.split() for line in (repo / "python-calls.txt").read_text().splitlines()]
    assert calls[0] == ["scripts/gen-event-schema.py", "--check"]
    assert calls[1][:4] == ["-m", "ruff", "check", "--cache-dir"]
    assert calls[1][4].endswith("ruff-cache")
    assert calls[1][5:] == ["host", "scripts", "tests"]
    assert calls[2][:3] == ["-m", "mypy", "--cache-dir"]
    assert calls[2][3].endswith("mypy-cache")
    assert calls[2][4:] == ["host/pi_glink"]
    assert len(calls) == 5
    assert calls[4] == ["scripts/check-coverage.py"]
    pytest_args = calls[3]
    assert pytest_args[:3] == ["-m", "pytest", "tests"]
    assert "pytest_cov" in pytest_args
    assert "--cov=host/pi_glink" in pytest_args
    assert "--cov-report=" in pytest_args
    assert "--deselect=tests/test_ui_tokens.py::test_ui_tokens_match_standard_library" in pytest_args
    assert "no:cacheprovider" in pytest_args
    # 两个 node 收集/执行范围：前端事件模型（tests/web）与 agent 扩展单元测试
    # （tests/extensions，GLINK-SUB-02 强制前台守卫），再加 dsh bundle 单测
    # （GLINK-DSH-02，--import 注册 dsh seam 替身）；stub 以追加方式记录两次调用。
    assert (repo / "node-args.txt").read_text().splitlines() == [
        "--test",
        "tests/web/example.test.mjs",
        "tests/extensions/example.test.mjs",
        "--import",
        "./harnesses/dsh/bundles/dsh-web-fetch-jina/tests/support/register-stubs.mjs",
        "--test",
        "harnesses/dsh/bundles/dsh-web-fetch-jina/tests/example.test.mjs",
    ]
    assert list(temp.iterdir()) == []


@pytest.mark.parametrize("status", [1, 5])
def test_python_failure_or_empty_suite_propagates_and_stops_node(tmp_path: Path, status: int) -> None:
    repo, env, temp = check_fixture(tmp_path, python_status=status)
    result = invoke(repo, env)
    assert result.returncode == status
    assert f"exit {status}" in result.stderr
    assert "无模型必需检查完成" not in result.stdout
    assert not (repo / "node-env.txt").exists()
    assert list(temp.iterdir()) == []


def test_node_failure_is_not_reported_as_success(tmp_path: Path) -> None:
    repo, env, temp = check_fixture(tmp_path, node_status=7)
    result = invoke(repo, env)
    assert result.returncode == 7
    assert "exit 7" in result.stderr
    assert "无模型必需检查完成" not in result.stdout
    assert list(temp.iterdir()) == []


def test_missing_dependencies_stop_without_installing_or_running_suites(tmp_path: Path) -> None:
    repo, env, temp = check_fixture(tmp_path, preflight_status=1)
    result = invoke(repo, env)
    assert result.returncode == 2
    assert "前置检查失败" in result.stderr
    assert not (repo / "python-env.txt").exists()
    assert not (repo / "node-env.txt").exists()
    assert list(temp.iterdir()) == []


def test_missing_optional_deps_fail_before_suites_without_installation(tmp_path: Path) -> None:
    repo, env, temp = check_fixture(tmp_path, optional_status=2)
    result = invoke(repo, env)
    assert result.returncode == 2
    assert "可选依赖预检失败" in result.stderr
    assert not (repo / "python-env.txt").exists()
    assert not (repo / "node-env.txt").exists()
    assert list(temp.iterdir()) == []


def test_missing_dev_tools_fail_before_suites_without_installation(tmp_path: Path) -> None:
    repo, env, temp = check_fixture(tmp_path, dev_status=2)
    result = invoke(repo, env)
    assert result.returncode == 2
    assert "开发工具预检失败" in result.stderr
    assert not (repo / "python-env.txt").exists()
    assert not (repo / "node-env.txt").exists()
    assert list(temp.iterdir()) == []


def test_missing_node_tests_is_a_failure_not_a_green_empty_run(tmp_path: Path) -> None:
    repo, env, temp = check_fixture(tmp_path)
    (repo / "tests/web/example.test.mjs").unlink()
    (repo / "tests/extensions/example.test.mjs").unlink()
    result = invoke(repo, env)
    assert result.returncode == 2
    assert "空测试集" in result.stderr
    assert not (repo / "python-env.txt").exists()
    assert list(temp.iterdir()) == []


def test_missing_bundle_tests_is_a_failure_not_a_green_empty_run(tmp_path: Path) -> None:
    repo, env, temp = check_fixture(tmp_path)
    (repo / "harnesses/dsh/bundles/dsh-web-fetch-jina/tests/example.test.mjs").unlink()
    result = invoke(repo, env)
    assert result.returncode == 2
    assert "空测试集" in result.stderr
    assert not (repo / "python-env.txt").exists()
    assert list(temp.iterdir()) == []


def test_missing_bundle_stub_registration_is_a_failure_not_a_green_empty_run(tmp_path: Path) -> None:
    repo, env, temp = check_fixture(tmp_path)
    (repo / "harnesses/dsh/bundles/dsh-web-fetch-jina/tests/support/register-stubs.mjs").unlink()
    result = invoke(repo, env)
    assert result.returncode == 2
    assert "空测试集" in result.stderr
    assert not (repo / "python-env.txt").exists()
    assert list(temp.iterdir()) == []


def test_unsupported_modes_do_not_accidentally_run_live_checks(tmp_path: Path) -> None:
    repo, env, temp = check_fixture(tmp_path)
    result = invoke(repo, env, "--live")
    assert result.returncode == 2
    assert "不接受参数" in result.stderr
    assert not (repo / "python-env.txt").exists()
    assert list(temp.iterdir()) == []
