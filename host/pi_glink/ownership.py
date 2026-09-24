"""Exclusive execution ownership of one data root (single host).

Restart reconciliation rewrites every run a previous host left ``running`` as
interrupted. That is only honest when the previous owner is gone: a second
host started against the same data root (on the same or another port) found an
empty in-memory run table and "reconciled" a run the first host was still
executing (R1 audit, R3).

Ownership is an exclusive ``flock`` on ``<data_root>/host.lock`` held for the
host process lifetime. The kernel releases it when the owning process exits for
any reason, so acquiring it *proves* that no live process owns the data root —
the property reconciliation needs. A lock is per open file description, so two
owners inside one process are refused as well.

Scope: one machine and a local filesystem. ``flock`` is not a distributed lease
(network filesystems may not honour it); multi-host ownership is the lease
design in ``docs/multitenancy-plan.md`` §3.2 and is not implemented here.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import time
from pathlib import Path

from .config import ConfigError

LOCK_NAME = "host.lock"

#: Permission bits that make the data root unusable: group or other write access
#: means another account could add runs, replace the lock target or rewrite the
#: event logs the ownership decision rests on.
_UNSAFE_MODE_BITS = 0o022


class DataRootUnsafe(ConfigError):
    """The data root exists but is not safe to use (symlink, wide permissions)."""


class DataRootBusy(RuntimeError):
    """Another live process owns this data root."""

    def __init__(self, data_root: Path, owner: str) -> None:
        detail = f"（当前持有者：{owner}）" if owner else ""
        super().__init__(f"数据目录 {data_root} 已被另一个宿主进程占用{detail}")
        self.data_root = data_root
        self.owner = owner


class OwnershipRequired(RuntimeError):
    """A destructive recovery step was attempted without owning the data root."""


class DataRootOwnership:
    """Proof that this process exclusively owns one data root."""

    def __init__(self, data_root: Path, descriptor: int) -> None:
        self.data_root = data_root
        self._descriptor = descriptor

    @classmethod
    def acquire(cls, data_root: Path) -> "DataRootOwnership":
        data_root = Path(data_root)
        _ensure_data_root(data_root)
        path = data_root / LOCK_NAME
        descriptor = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            owner = _read_owner(descriptor)
            os.close(descriptor)
            raise DataRootBusy(data_root, owner) from None
        except BaseException:
            os.close(descriptor)
            raise
        # Diagnostics only: decisions rest on the lock, never on this content.
        os.ftruncate(descriptor, 0)
        os.pwrite(
            descriptor,
            json.dumps({"pid": os.getpid(), "since": time.time()}).encode("utf-8"),
            0,
        )
        return cls(data_root, descriptor)

    @property
    def held(self) -> bool:
        return self._descriptor >= 0

    def require(self, data_root: Path) -> None:
        """Raise unless this token currently owns ``data_root``."""
        if not self.held or Path(data_root) != self.data_root:
            raise OwnershipRequired(f"未持有数据目录 {data_root} 的执行所有权")

    def release(self) -> None:
        if self._descriptor < 0:
            return
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self._descriptor)
            self._descriptor = -1


def _ensure_data_root(data_root: Path) -> None:
    """Create the data root private (0700) or refuse to use an unsafe one.

    Same fail-closed rule as the account database: an existing root that is a
    symlink, not a directory, or writable by group/other accounts is reported to
    the operator instead of being chmod'ed behind their back — the host cannot
    know who created it or what points at it.
    """
    if data_root.is_symlink():
        raise DataRootUnsafe(
            f"数据目录 {data_root} 是符号链接；请改用真实目录（宿主不跟随链接）"
        )
    if not data_root.exists():
        data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if data_root.is_symlink():  # lost a race with a link creator: refuse
        raise DataRootUnsafe(f"数据目录 {data_root} 在创建时被换成了符号链接")
    if not data_root.is_dir():
        raise DataRootUnsafe(f"数据目录 {data_root} 不是目录")
    mode = stat.S_IMODE(data_root.stat().st_mode)
    if mode & _UNSAFE_MODE_BITS:
        raise DataRootUnsafe(
            f"数据目录 {data_root} 权限过宽（{mode:04o}）：组或其他账户可写。"
            "请停止宿主后自行 chmod 700 并重试（宿主不自动修改已有目录权限）"
        )


def _read_owner(descriptor: int) -> str:
    try:
        raw = os.pread(descriptor, 4096, 0).decode("utf-8", "replace").strip()
    except OSError:
        return ""
    return raw
