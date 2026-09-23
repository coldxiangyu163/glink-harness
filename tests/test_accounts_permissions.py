"""账号库权限边界：文件 0600、专用目录 0700、WAL 侧车检查、拒绝宽权限与符号链接。

验收复现：``AccountsStore.__init__`` 直接 ``sqlite3.connect(path)``，既不以受限
权限创建，也不校验已有文件；合成 fixture 的 ``accounts.db`` 因此是 0644（所在
data 目录 0755），同机其他本地用户可以读到邮箱、口令散列、CSRF 原文与邀请摘要。

这些回归在旧实现上必红：文件/目录权限、拒绝宽权限文件、拒绝符号链接、拒绝宽
权限父目录四条都会失败。宿主绝不自动修改已有文件/目录的权限：宽权限的已有库
一律 fail-closed，操作员自行迁移并 ``chmod 600``。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from pi_glink.accounts import AccountsError, AccountsStore

PASSWORD = "correct horse battery"


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def make_parent(tmp_path: Path, name: str = "accounts", mode: int = 0o700) -> Path:
    parent = tmp_path / "data" / name
    parent.mkdir(parents=True, exist_ok=True)
    os.chmod(parent, mode)
    return parent


def test_new_database_directory_and_sidecars_are_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "data" / "accounts" / "accounts.db"
    store = AccountsStore(path)
    try:
        # The file is created 0600 and the dedicated directory 0700 — before the
        # first byte of account data exists.
        assert mode_of(path) == 0o600
        assert mode_of(path.parent) == 0o700
        store.upsert_tenant("tenant-alpha", "组织甲")
        store.create_user(
            tenant_id="tenant-alpha",
            email="member@example.test",
            name="成员",
            role="member",
            password=PASSWORD,
        )
        # WAL/SHM sidecars carry the same owner-only rule; a widened one would
        # leak the committed pages that are not yet checkpointed.
        for suffix in ("-wal", "-shm", "-journal"):
            candidate = Path(f"{path}{suffix}")
            if candidate.exists():
                assert mode_of(candidate) & 0o077 == 0, f"{candidate.name} is not owner-only"
    finally:
        store.close()


def test_existing_permissive_database_is_refused_without_touching_it(tmp_path: Path) -> None:
    parent = make_parent(tmp_path)
    path = parent / "accounts.db"
    path.write_bytes(b"not a real account database")
    os.chmod(path, 0o644)
    before = path.read_bytes()

    with pytest.raises(AccountsError) as excinfo:
        AccountsStore(path)
    assert excinfo.value.code == "insecure_database"
    # Fail closed means fail *untouched*: no read, no migration, no chmod.
    assert path.read_bytes() == before
    assert mode_of(path) == 0o644
    assert not Path(f"{path}-wal").exists()


def test_a_database_owned_by_another_user_is_refused(tmp_path: Path) -> None:
    parent = make_parent(tmp_path)
    path = parent / "accounts.db"
    path.write_bytes(b"")
    os.chmod(path, 0o600)
    original = os.geteuid
    try:
        os.geteuid = lambda: original() + 1  # type: ignore[assignment]
        with pytest.raises(AccountsError) as excinfo:
            AccountsStore(path)
        assert excinfo.value.code == "insecure_database"
    finally:
        os.geteuid = original  # type: ignore[assignment]


def test_symlinked_database_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "shared-accounts.db"
    target.write_bytes(b"shared bytes")
    os.chmod(target, 0o600)
    parent = make_parent(tmp_path)
    link = parent / "accounts.db"
    link.symlink_to(target)

    with pytest.raises(AccountsError) as excinfo:
        AccountsStore(link)
    assert excinfo.value.code == "insecure_database"
    assert target.read_bytes() == b"shared bytes"
    assert link.is_symlink()


def test_permissive_parent_directory_is_refused_before_creating_anything(tmp_path: Path) -> None:
    parent = make_parent(tmp_path, mode=0o755)
    path = parent / "accounts.db"
    with pytest.raises(AccountsError) as excinfo:
        AccountsStore(path)
    assert excinfo.value.code == "insecure_database"
    assert not path.exists()


def test_a_legitimate_first_creation_and_wal_restart_still_work(tmp_path: Path) -> None:
    path = tmp_path / "data" / "accounts" / "accounts.db"
    first = AccountsStore(path)
    try:
        first.upsert_tenant("tenant-alpha", "组织甲")
        first.create_user(
            tenant_id="tenant-alpha",
            email="member@example.test",
            name="成员",
            role="member",
            password=PASSWORD,
        )
    finally:
        first.close()
    assert mode_of(path) == 0o600

    # Reopening (a WAL restart) validates the existing file and keeps working.
    again = AccountsStore(path)
    try:
        found = again.find_user_by_email("member@example.test")
        assert found is not None and found.tenant_id == "tenant-alpha"
        assert mode_of(path) == 0o600
    finally:
        again.close()


def test_a_widened_wal_sidecar_is_corrected_to_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "data" / "accounts" / "accounts.db"
    first = AccountsStore(path)
    first.upsert_tenant("tenant-alpha", "组织甲")
    first.create_user(
        tenant_id="tenant-alpha",
        email="member@example.test",
        name="成员",
        role="member",
        password=PASSWORD,
    )
    wal = Path(f"{path}-wal")
    if not wal.exists():  # pragma: no cover - SQLite always keeps a WAL open here
        first.close()
        pytest.skip("this SQLite build does not keep a WAL sidecar open")
    os.chmod(wal, 0o644)
    assert mode_of(wal) == 0o644
    second = AccountsStore(path)
    try:
        # The sidecar is a host-created derivative, not a user file: correcting
        # it is safe, and the reopened store proves the contents still work.
        assert mode_of(wal) & 0o077 == 0, "a widened WAL sidecar must be corrected"
        assert second.find_user_by_email("member@example.test") is not None
    finally:
        second.close()
        first.close()
