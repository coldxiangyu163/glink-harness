"""The workbench token layer must stay equal to the 百鉴 standard library's.

``web/styles.css`` reproduces the library's palette / semantic / scale /
typography / elevation / motion values; ``scripts/check-ui-tokens.py`` is the
guard. The library lives outside this repository and the delivery gate never
reads it, so the test runs only when its checkout is named explicitly through
``GLINK_UI_LIBRARY`` (the same variable the script itself reads):

    GLINK_UI_LIBRARY=/path/to/glink-ui-library \
        .venv/bin/python -m pytest tests/test_ui_tokens.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check-ui-tokens.py"


def _library() -> Path | None:
    env = os.environ.get("GLINK_UI_LIBRARY", "").strip()
    return Path(env) if env else None


def test_ui_tokens_match_standard_library() -> None:
    library = _library()
    if library is None or not library.is_dir():
        pytest.skip("未指定或不存在的百鉴标准库（GLINK_UI_LIBRARY），跳过 token 对齐校验")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--library", str(library)],
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
