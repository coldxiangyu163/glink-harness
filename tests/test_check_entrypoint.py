"""Exercise the check wrapper without recursively running tests or a live agent."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from shlex import quote

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check.sh"


def check_fixture(tmp_path: Path, *, python_status: int = 0, node_status: int = 0, preflight_status: int = 0):
    repo = tmp_path / "checkout with spaces"
    for directory in ("scripts", ".venv/bin", "tests/web", "tests/extensions", "host", "tools"):
        (repo / directory).mkdir(parents=True)
    shutil.copyfile(SCRIPT, repo / "scripts/check.sh")
    (repo / "tests/web/example.test.mjs").write_text("// discovered but not executed by the stub\n")
    (repo / "tests/extensions/example.test.mjs").write_text(
        "// discovered but not executed by the stub\n"
    )
    python = repo / ".venv/bin/python"
    python.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "-c" ]; then exit {preflight_status}; fi\n'
        f'/usr/bin/env > {quote(str(repo / "python-env.txt"))}\n'
        f'printf "%s\\n" "$@" > {quote(str(repo / "python-args.txt"))}\n'
        f"exit {python_status}\n"
    )
    python.chmod(0o755)
    node = repo / "tools/node"
    node.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-e" ]; then exit 0; fi\n'
        f'/usr/bin/env > {quote(str(repo / "node-env.txt"))}\n'
        f'printf "%s\\n" "$@" > {quote(str(repo / "node-args.txt"))}\n'
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
    args = (repo / "python-args.txt").read_text().splitlines()
    assert args[:3] == ["-m", "pytest", "tests"]
    assert "--deselect=tests/test_ui_tokens.py::test_ui_tokens_match_standard_library" in args
    assert "no:cacheprovider" in args
    # 两个收集范围：前端事件模型（tests/web）与 agent 扩展单元测试
    # （tests/extensions，GLINK-SUB-02 强制前台守卫）。
    assert (repo / "node-args.txt").read_text().splitlines() == [
        "--test",
        "tests/web/example.test.mjs",
        "tests/extensions/example.test.mjs",
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


def test_missing_node_tests_is_a_failure_not_a_green_empty_run(tmp_path: Path) -> None:
    repo, env, temp = check_fixture(tmp_path)
    (repo / "tests/web/example.test.mjs").unlink()
    (repo / "tests/extensions/example.test.mjs").unlink()
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
