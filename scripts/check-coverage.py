#!/usr/bin/env python3
"""覆盖率总量 ratchet 门禁。

``scripts/check.sh`` 先用 ``pytest --cov`` 采集数据，再调用本脚本，把实测总覆盖率与
``scripts/coverage-baseline.json`` 的 ``percent`` 比较：低于基线即失败（退出 1）。这是
**总量 ratchet**，不是 diff 覆盖率；只保证整体不倒退。

主动升基线：确认覆盖率确实因新增测试或新增代码被覆盖而变高后，把基线改成新的实测值
（向下取整到 0.1 精度，不要四舍五入抬高），随代码一起提交。降低基线必须说明被替代的
义务（docs/engineering.md「需要先取得决定的变更」），不能只因门禁失败而改。

单独运行（需先有 pytest --cov 产生的数据）：

    COVERAGE_FILE=<scratch>/.coverage python scripts/check-coverage.py
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import coverage

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / "scripts" / "coverage-baseline.json"


def main() -> int:
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    floor = float(baseline["percent"])
    cov = coverage.Coverage()
    cov.load()
    actual = cov.report(file=io.StringIO(), show_missing=False)
    print(f"[覆盖率] 实测 {actual:.1f}%（基线 {floor:.1f}%）")
    if actual + 1e-9 < floor:
        print(
            f"[FAIL] 总覆盖率 {actual:.1f}% 低于基线 {floor:.1f}%："
            "改动代码缺少覆盖或删除了测试；确需降低基线时说明被替代的义务。",
        )
        return 1
    print("[PASS] 覆盖率不低于基线")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
