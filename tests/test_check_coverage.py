"""``scripts/check-coverage.py`` 的 ratchet 比较逻辑与基线方向锁。

用合成 ``.coverage`` 数据（子进程里采集一个 4 语句执行 3 条的合成模块 → 恒 75.0%）
与临时基线文件驱动真实脚本入口，断言退出码：实测不低于基线退出 0，低于退出 1，
基线损坏 fail-closed。方向锁覆盖三种形态：低于 HEAD 拒绝、显式环境变量豁免、
git 不可用/无 HEAD 版本时跳过（锁失效不拖垮主 ratchet）。

采集与被测脚本都在子进程里跑：既不与 pytest-cov 的 tracer 嵌套互相干扰
（避免本文件行覆盖被夺走拉低门禁实测值），也走真实的 ``__main__`` 入口。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check-coverage.py"
#: 4 条语句执行 3 条（分支不可达）：report 恒 75.0%。
TARGET_SOURCE = 'VALUE = 1\nTOTAL = VALUE + 1\nif TOTAL == 99:\n    raise SystemExit("never")\n'


def _stage_repo(tmp_path: Path) -> Path:
    """把被测脚本复制进临时仓库骨架，基线路径随脚本位置解析。"""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copyfile(SCRIPT, repo / "scripts" / "check-coverage.py")
    return repo


def _write_baseline(repo: Path, percent: float | str) -> None:
    value = percent if isinstance(percent, str) else round(percent, 1)
    (repo / "scripts" / "coverage-baseline.json").write_text(
        json.dumps({"percent": value}), encoding="utf-8"
    )


def _measure_into(repo: Path, data_file: Path) -> None:
    """在子进程里采集合成模块的覆盖率数据（include 限定为该模块）。"""
    target = repo / "target.py"
    target.write_text(TARGET_SOURCE, encoding="utf-8")
    program = "\n".join(
        [
            "import coverage",
            f"cov = coverage.Coverage(data_file={str(data_file)!r}, include=[{str(target)!r}])",
            "cov.start()",
            f"exec(compile(open({str(target)!r}, encoding='utf-8').read(), {str(target)!r}, 'exec'),"
            " {'__name__': 'synthetic'})",
            "cov.stop()",
            "cov.save()",
        ]
    )
    subprocess.run([sys.executable, "-c", program], check=True, capture_output=True, text=True)


def _run_check(repo: Path, data_file: Path, **env_extra: str) -> subprocess.CompletedProcess:
    """以真实入口跑脚本；COVERAGE_FILE 指向合成数据，环境变量可注入豁免通道。"""
    env = {**os.environ, "COVERAGE_FILE": str(data_file), **env_extra}
    return subprocess.run(
        [sys.executable, str(repo / "scripts" / "check-coverage.py")],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(repo),
    )


def _git_repo_with_baseline(repo: Path, percent: float) -> None:
    """初始化 tmp git 仓库并把基线提交进 HEAD（identity 内联，不碰全局配置）。"""
    _write_baseline(repo, percent)
    git = ["git", "-C", str(repo)]
    identity = ["-c", "user.name=test", "-c", "user.email=test@example.test"]
    for command in (
        ["init", "-b", "main"],
        ["add", "scripts/coverage-baseline.json"],
        [*identity, "commit", "-m", "baseline"],
    ):
        subprocess.run([*git, *command], check=True, capture_output=True, text=True)


def test_actual_at_or_above_the_parsed_baseline_passes(tmp_path: Path) -> None:
    repo = _stage_repo(tmp_path)
    data = tmp_path / ".coverage"
    _measure_into(repo, data)
    # 实测恒 75.0%：基线解析出的 floor 直接决定结果（低于→过；相等→过）。
    for floor in (75.0, 74.9):
        _write_baseline(repo, floor)
        done = _run_check(repo, data)
        assert done.returncode == 0, f"floor={floor}: {done.stdout}{done.stderr}"
        assert "[PASS]" in done.stdout


def test_actual_below_baseline_fails_with_exit_1(tmp_path: Path) -> None:
    repo = _stage_repo(tmp_path)
    data = tmp_path / ".coverage"
    _measure_into(repo, data)
    _write_baseline(repo, 90.0)
    done = _run_check(repo, data)
    assert done.returncode == 1
    assert "实测 75.0%（基线 90.0%）" in done.stdout
    assert "[FAIL]" in done.stdout


@pytest.mark.parametrize(
    "body",
    [
        "{ broken json",  # JSON 损坏
        '{"scope": "no percent key"}',  # 缺 percent
        '{"percent": "high"}',  # percent 非数值
    ],
)
def test_corrupt_baseline_fails_closed(tmp_path: Path, body: str) -> None:
    repo = _stage_repo(tmp_path)
    data = tmp_path / ".coverage"
    _measure_into(repo, data)
    (repo / "scripts" / "coverage-baseline.json").write_text(body, encoding="utf-8")
    done = _run_check(repo, data)
    assert done.returncode == 1
    assert "fail-closed" in done.stdout


def test_direction_lock_rejects_a_baseline_below_head(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git 不在 PATH；方向锁在门禁环境由 check.sh 保留 PATH 提供")
    repo = _stage_repo(tmp_path)
    data = tmp_path / ".coverage"
    _measure_into(repo, data)
    _git_repo_with_baseline(repo, 80.0)
    _write_baseline(repo, 70.0)  # 实测 75% 仍高于工作区基线：只有方向锁会拒绝。
    done = _run_check(repo, data)
    assert done.returncode == 1
    assert "低于 HEAD 版 80.0%" in done.stdout


def test_direction_lock_honors_the_explicit_drop_env(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git 不在 PATH；方向锁在门禁环境由 check.sh 保留 PATH 提供")
    repo = _stage_repo(tmp_path)
    data = tmp_path / ".coverage"
    _measure_into(repo, data)
    _git_repo_with_baseline(repo, 80.0)
    _write_baseline(repo, 70.0)
    done = _run_check(repo, data, GLINK_ALLOW_BASELINE_DROP="1")
    assert done.returncode == 0
    assert "[WARN]" in done.stdout and "GLINK_ALLOW_BASELINE_DROP" in done.stdout


def test_direction_lock_skips_where_head_has_no_baseline(tmp_path: Path) -> None:
    # 非 git 目录（部署副本形态）：锁跳过，主 ratchet 照常判定。
    repo = _stage_repo(tmp_path)
    data = tmp_path / ".coverage"
    _measure_into(repo, data)
    _write_baseline(repo, 70.0)
    done = _run_check(repo, data)
    assert done.returncode == 0
    assert "HEAD" not in done.stdout
    if shutil.which("git") is not None:
        # git 仓库但 HEAD 还没有基线文件（基线首次引入的形态）：同样跳过。
        subprocess.run(
            ["git", "-C", str(repo), "init", "-b", "main"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.test",
             "commit", "--allow-empty", "-m", "empty"],
            check=True,
            capture_output=True,
            text=True,
        )
        done = _run_check(repo, data)
        assert done.returncode == 0
        assert "HEAD" not in done.stdout
