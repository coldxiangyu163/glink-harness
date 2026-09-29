#!/usr/bin/env python3
"""覆盖率总量 ratchet 门禁。

``scripts/check.sh`` 先用 ``pytest --cov`` 采集数据，再调用本脚本，把实测总覆盖率与
``scripts/coverage-baseline.json`` 的 ``percent`` 比较：低于基线即失败（退出 1）。这是
**总量 ratchet**，不是 diff 覆盖率；只保证整体不倒退。

基线方向锁：工作区基线不得低于 git ``HEAD`` 版本的值（防"顺手调低基线让门禁变绿"）。
git 不可用、目录不在仓库或 HEAD 没有该文件时锁跳过——它是 best-effort 的防倒退护栏，
不是评审的替代。唯一豁免通道是显式设置 ``GLINK_ALLOW_BASELINE_DROP=1``，且必须在提交
说明里写明被替代的义务（docs/engineering.md「需要先取得决定的变更」）。

主动升基线：确认覆盖率确实因新增测试或新增代码被覆盖而变高后，把基线改成新的实测值
（向下取整到 0.1 精度，不要四舍五入抬高），随代码一起提交。降低基线必须说明被替代的
义务，不能只因门禁失败而改。

单独运行（需先有 pytest --cov 产生的数据）：

    COVERAGE_FILE=<scratch>/.coverage python scripts/check-coverage.py
"""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

import coverage

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / "scripts" / "coverage-baseline.json"
#: 方向锁的唯一显式豁免通道：设为 ``1`` 表示"我知道基线在降，义务另行说明"。
ALLOW_DROP_ENV = "GLINK_ALLOW_BASELINE_DROP"
#: 浮点比较容差（与主比较同口径）：只在显著低于时才判失败。
_TOLERANCE = 1e-9


def read_percent(path: Path) -> float:
    """解析基线文件的 ``percent``。

    文件缺失、JSON 损坏、缺键或 ``percent`` 非数值一律抛 ``ValueError``——基线是
    ratchet 的支点，读不出就 fail-closed，绝不猜测或静默当作 0。
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return float(raw["percent"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(
            f"基线文件 {path} 不可读或不是合法基线（{type(error).__name__}: {error}）"
        ) from error


def head_percent(repo_root: Path) -> float | None:
    """HEAD 版基线的 ``percent``；取不到时返回 ``None``（方向锁跳过）。

    取不到的情形：git 不在 PATH、目录不在仓库（部署副本）、HEAD 没有该文件
    （基线首次引入）或 HEAD 版本自身损坏。锁失效不等于门禁失效：主 ratchet
    比较照常执行。
    """
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "show",
                "HEAD:scripts/coverage-baseline.json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return float(json.loads(completed.stdout)["percent"])
    except (ValueError, KeyError, TypeError):
        return None


def main() -> int:
    try:
        floor = read_percent(BASELINE_PATH)
    except ValueError as error:
        print(f"[FAIL] {error}：fail-closed，不猜测基线。")
        return 1

    head = head_percent(REPO_ROOT)
    if head is not None and floor + _TOLERANCE < head:
        if os.environ.get(ALLOW_DROP_ENV) == "1":
            print(
                f"[WARN] {ALLOW_DROP_ENV}=1：显式豁免基线方向锁"
                f"（HEAD {head:.1f}% → 工作区 {floor:.1f}%）；须说明被替代的义务。"
            )
        else:
            print(
                f"[FAIL] 工作区基线 {floor:.1f}% 低于 HEAD 版 {head:.1f}%：基线只升不降。"
                f"确需降低时说明被替代的义务，并显式设置 {ALLOW_DROP_ENV}=1。"
            )
            return 1

    cov = coverage.Coverage()
    cov.load()
    actual = cov.report(file=io.StringIO(), show_missing=False)
    print(f"[覆盖率] 实测 {actual:.1f}%（基线 {floor:.1f}%）")
    if actual + _TOLERANCE < floor:
        print(
            f"[FAIL] 总覆盖率 {actual:.1f}% 低于基线 {floor:.1f}%："
            "改动代码缺少覆盖或删除了测试；确需降低基线时说明被替代的义务。",
        )
        return 1
    print("[PASS] 覆盖率不低于基线")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
