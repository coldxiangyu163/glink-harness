"""The workbench token layer must stay equal to the 百鉴 standard library's.

``web/styles.css`` reproduces the library's palette / semantic / scale /
typography / elevation / motion values; ``scripts/check-ui-tokens.py`` is the
guard. The library lives outside this repository, so the test skips when it is
not checked out next to the workbench — the guard is still runnable by hand:

    scripts/check-ui-tokens.py --library /path/to/glink-ui-library
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-ui-tokens.py"
DEFAULT_LIBRARY = Path("/Users/coldxiangyu/work/BR/glink-ui-library")


def test_ui_tokens_match_standard_library() -> None:
    if not DEFAULT_LIBRARY.is_dir():
        pytest.skip(f"百鉴标准库不在 {DEFAULT_LIBRARY}，跳过 token 对齐校验")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--library", str(DEFAULT_LIBRARY)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "[ok]" in result.stdout


def test_ui_token_check_skips_cleanly_without_the_library(tmp_path: Path) -> None:
    """A missing library is a skip, never a false pass or a hard failure."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--library", str(tmp_path / "absent")],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "[skip]" in result.stderr
