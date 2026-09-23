"""GLINK-AUTH-01 ownership regression: the account database is a data-root write.

Audit reproduction: ``create_server`` resolved the multi-tenant material — in
accounts mode that opens/creates ``accounts.db`` and runs the schema migration —
*before* acquiring the exclusive ``<data_root>/host.lock``. A second host on the
same data root could therefore create or modify the account database before its
ownership check refused it, violating "acquire the lock, then read/reconcile/
write".

The fix puts ownership first for both the serving path and the operator CLI, and
requires the account database to live inside the data root so two roots cannot
share one library while each believes it owns its own lock. These tests are the
guard: a refused second host (in-process and cross-process) leaves every byte of
``accounts.db`` (and its WAL sidecars) unchanged, while the first legitimate
initialization still works.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from pi_glink.app import create_server, main as app_main
from pi_glink.config import ConfigError, build_config
from pi_glink.ownership import DataRootBusy

REPO_ROOT = Path(__file__).resolve().parents[1]
HOST_ROOT = REPO_ROOT / "host"
PASSWORD = "correct horse battery"


def write_tenant_file(tmp_path: Path) -> Path:
    tenants = tmp_path / "tenants.json"
    tenants.write_text(
        json.dumps({"tenants": {"tenant-alpha": {"modelApiKey": "sk-alpha-account-ownership"}}}),
        encoding="utf-8",
    )
    tenants.chmod(0o600)
    return tenants


def config_values(tmp_path: Path, tenants: Path, **overrides: str) -> dict[str, str]:
    values = {
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent-web-search.json"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_PORT": "0",
        "LOCAL_WEB_EXECUTOR": "docker",
        "PI_GLINK_TENANCY": "multitenant",
        "PI_GLINK_AUTH_MODE": "accounts",
        "PI_GLINK_TENANT_CONFIG_FILE": str(tenants),
        "PI_GLINK_ACCOUNTS_DB": str(tmp_path / "data" / "accounts" / "accounts.db"),
        "PI_GLINK_COOKIE_SECURE": "0",
        "LOCAL_WEB_TASK_TIMEOUT_SECONDS": "60",
    }
    values.update(overrides)
    return values


def make_config(tmp_path: Path, tenants: Path, **overrides: str):
    return build_config(config_values(tmp_path, tenants, **overrides), REPO_ROOT)


def db_fingerprint(path: Path) -> dict[str, str]:
    """Byte identity of the database and its sidecars — without opening SQLite.

    Opening the file with ``sqlite3`` could itself create or touch ``-shm``;
    the ownership question is about bytes on disk, so hash the files directly.
    """
    fingerprint: dict[str, str] = {}
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.exists():
            fingerprint[candidate.name] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    return fingerprint


def seed_account(server) -> None:
    server.accounts.upsert_tenant("tenant-alpha", "组织甲")
    server.accounts.create_user(
        tenant_id="tenant-alpha",
        email="member@example.test",
        name="成员",
        role="member",
        password=PASSWORD,
    )


def test_first_initialization_happens_under_the_lock_and_a_second_host_changes_nothing(
    tmp_path: Path,
) -> None:
    tenants = write_tenant_file(tmp_path)
    config = make_config(tmp_path, tenants)
    first = create_server(config)
    try:
        # The first legitimate initialization created the database inside its
        # own data root (the config already refused any other placement).
        assert config.accounts_db == tmp_path / "data" / "accounts" / "accounts.db"
        assert config.accounts_db.is_file()
        seed_account(first)
        assert first.accounts.find_user_by_email("member@example.test") is not None
        before = db_fingerprint(config.accounts_db)
        assert before, "the account database and its WAL sidecar were created"

        # A second host on the same data root — another port, same account db —
        # is refused with the first host's run/store/account bytes untouched.
        second = make_config(tmp_path, tenants, LOCAL_WEB_PORT="1")
        with pytest.raises(DataRootBusy):
            create_server(second)
        assert db_fingerprint(config.accounts_db) == before
        assert first.accounts.find_user_by_email("member@example.test") is not None
    finally:
        first.server_close()


def test_a_second_process_is_refused_without_touching_the_account_database(
    tmp_path: Path,
) -> None:
    tenants = write_tenant_file(tmp_path)
    config = make_config(tmp_path, tenants)
    first = create_server(config)
    try:
        seed_account(first)
        before = db_fingerprint(config.accounts_db)

        probe = (
            "import json, sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(HOST_ROOT)!r})\n"
            "from pi_glink.app import create_server\n"
            "from pi_glink.config import build_config\n"
            f"config = build_config(json.loads(sys.argv[1]), Path({str(REPO_ROOT)!r}))\n"
            "try:\n"
            "    create_server(config)\n"
            "except Exception as exc:\n"
            "    print(type(exc).__name__, file=sys.stderr)\n"
            "    raise SystemExit(3)\n"
            "print('STARTED')\n"
        )
        child = subprocess.run(
            [sys.executable, "-c", probe, json.dumps(config_values(tmp_path, tenants))],
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
                "PYTHONPATH": str(HOST_ROOT),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert child.returncode == 3, child.stderr
        assert "DataRootBusy" in child.stderr
        assert "STARTED" not in child.stdout
        assert db_fingerprint(config.accounts_db) == before
        assert first.accounts.find_user_by_email("member@example.test") is not None
    finally:
        first.server_close()


def test_print_config_never_creates_or_modifies_the_account_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tenants = write_tenant_file(tmp_path)
    config = make_config(tmp_path, tenants)
    monkeypatch.setattr("pi_glink.app.load_config", lambda: config)

    # Validation only: no data root is created, no database appears.
    assert app_main(["--print-config"]) == 0
    capsys.readouterr()
    assert not config.accounts_db.exists()

    # With an existing database (a real host created it, then stopped), the
    # validation path leaves every byte alone and does not need the lock.
    host = create_server(config)
    seed_account(host)
    host.server_close()
    before = db_fingerprint(config.accounts_db)
    assert app_main(["--print-config"]) == 0
    capsys.readouterr()
    assert db_fingerprint(config.accounts_db) == before


def test_a_failed_bind_closes_the_account_store_and_releases_the_lock(tmp_path: Path) -> None:
    tenants = write_tenant_file(tmp_path)
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    port = holder.getsockname()[1]
    try:
        with pytest.raises(OSError):
            create_server(make_config(tmp_path, tenants, LOCAL_WEB_PORT=str(port)))
        # The lock was released and the SQLite connection closed: a retry on a
        # free port starts cleanly and can write.
        retry = create_server(make_config(tmp_path, tenants, LOCAL_WEB_PORT="0"))
        try:
            assert retry.accounts is not None
            seed_account(retry)
            assert retry.accounts.find_user_by_email("member@example.test") is not None
        finally:
            retry.server_close()
    finally:
        holder.close()


def test_accounts_db_must_live_inside_the_data_root(tmp_path: Path) -> None:
    tenants = write_tenant_file(tmp_path)
    # A sibling path outside the data root is refused: two hosts with different
    # data roots (and different locks) must not be able to share one library.
    with pytest.raises(ConfigError) as excinfo:
        make_config(tmp_path, tenants, PI_GLINK_ACCOUNTS_DB=str(tmp_path / "shared-accounts.db"))
    assert "LOCAL_WEB_DATA_ROOT" in str(excinfo.value)
    # The data root itself is a directory, not a database file.
    with pytest.raises(ConfigError):
        make_config(tmp_path, tenants, PI_GLINK_ACCOUNTS_DB=str(tmp_path / "data"))
    # A subdirectory inside the root is a legitimate placement.
    config = make_config(
        tmp_path, tenants, PI_GLINK_ACCOUNTS_DB=str(tmp_path / "data" / "auth" / "accounts.db")
    )
    assert config.accounts_db == tmp_path / "data" / "auth" / "accounts.db"


def test_accounts_db_symlink_cannot_escape_the_data_root(tmp_path: Path) -> None:
    tenants = write_tenant_file(tmp_path)
    (tmp_path / "data").mkdir()
    elsewhere = tmp_path / "elsewhere.db"
    elsewhere.write_bytes(b"")
    link = tmp_path / "data" / "linked.db"
    link.symlink_to(elsewhere)
    with pytest.raises(ConfigError):
        make_config(tmp_path, tenants, PI_GLINK_ACCOUNTS_DB=str(link))
