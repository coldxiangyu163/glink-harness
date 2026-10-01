"""`scripts/check-no-secrets.py` must see every value the run database stores.

SQLite spills large records onto overflow pages (each page starts with a
4-byte page pointer) and the WAL is page-framed too, so scanning ``runs.db`` /
``runs.db-wal`` as text cuts a credential that straddles a page boundary in two
and reports zero hits. The scanner reads the stored columns through SQLite.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from glink_harness.store import DATABASE_NAME, _SCHEMA

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check-no-secrets.py"
#: Synthetic key the fake operator `.env` declares.
SECRET = "sk-SYNTHETICSECRET1234567890abcdef"


def _load_script():
    spec = importlib.util.spec_from_file_location("check_no_secrets", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def scanner():
    return _load_script()


def _repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env").write_text(
        f"OPENAI_API_KEY={SECRET}\nGLINK_HARNESSES_DIR={REPO_ROOT / 'harnesses'}\n",
        encoding="utf-8",
    )
    return root


def _database(data_root: Path) -> sqlite3.Connection:
    runs = data_root / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(runs / DATABASE_NAME, isolation_level=None)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")  # keep rows in the WAL
    connection.executescript(_SCHEMA)
    return connection


def _insert(connection: sqlite3.Connection, run_id: str, body: str) -> None:
    connection.execute(
        "INSERT INTO runs VALUES (?, 0, NULL, 'finished', 0, ?)", (run_id, "{}")
    )
    connection.execute("INSERT INTO events VALUES (?, 1, 'text_delta', ?)", (run_id, body))


def _raw_text_scan_misses(data_root: Path) -> bool:
    """The old mechanism: database files decoded as text, substring match."""
    runs = data_root / "runs"
    text = "".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (runs / DATABASE_NAME, runs / f"{DATABASE_NAME}-wal")
        if path.is_file()
    )
    return SECRET not in text


def _straddling_database(tmp_path: Path) -> tuple[Path, sqlite3.Connection]:
    """A data root whose only event body has the secret across a page boundary."""
    for offset in range(4000, 9000, 5):
        data_root = tmp_path / f"data-{offset}"
        connection = _database(data_root)
        _insert(connection, "r1", "a" * offset + SECRET + "b" * 20000)
        if _raw_text_scan_misses(data_root):
            return data_root, connection
        connection.close()
    pytest.fail("no offset put the secret across an overflow page boundary")


def test_secret_across_overflow_page_boundary_is_found(tmp_path, scanner, capsys) -> None:
    data_root, connection = _straddling_database(tmp_path)
    try:
        code = scanner.main(["--data-root", str(data_root), "--repo-root", str(_repo_root(tmp_path))])
    finally:
        connection.close()
    out = capsys.readouterr().out
    assert code == 1
    assert "LEAK[value]" in out
    assert "events.body r1#1" in out
    assert SECRET not in out, "the report must stay redacted"


def test_secret_in_run_record_is_found(tmp_path, scanner, capsys) -> None:
    data_root = tmp_path / "data"
    connection = _database(data_root)
    connection.execute(
        "INSERT INTO runs VALUES ('r1', 0, NULL, 'failed', 0, ?)",
        ('{"error": "' + "x" * 6000 + SECRET + '"}',),
    )
    try:
        code = scanner.main(["--data-root", str(data_root), "--repo-root", str(_repo_root(tmp_path))])
    finally:
        connection.close()
    out = capsys.readouterr().out
    assert code == 1
    assert "runs.record r1" in out


def test_clean_database_passes(tmp_path, scanner, capsys) -> None:
    data_root = tmp_path / "data"
    connection = _database(data_root)
    _insert(connection, "r1", "a" * 30000)
    try:
        code = scanner.main(["--data-root", str(data_root), "--repo-root", str(_repo_root(tmp_path))])
    finally:
        connection.close()
    out = capsys.readouterr().out
    assert code == 0
    assert "扫描文件 1 个" in out and "真实命中 0 处" in out


def test_unreadable_database_is_a_failure(tmp_path, scanner, capsys) -> None:
    data_root = tmp_path / "data"
    (data_root / "runs").mkdir(parents=True)
    (data_root / "runs" / DATABASE_NAME).write_bytes(b"not a database" * 100)
    code = scanner.main(["--data-root", str(data_root), "--repo-root", str(_repo_root(tmp_path))])
    assert code == 1
    assert "数据库读取失败" in capsys.readouterr().out
