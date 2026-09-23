"""Safe reads and writes for host-handled run artifacts.

The kernel (pi and its tools) writes into the run workspace, and the host has to
read some of those files back: the report it serves to the browser, the session
log pi wrote, and any text artifact the credential sweep touches. A path plus
``Path.is_file()`` / ``read_text()`` is **not** a safe check: both follow
symbolic links, and a symlink planted in a workspace (or reported by a
compromised/buggy kernel) turns a read into an arbitrary-file read and an
in-place write into an arbitrary-file overwrite. The completion audit reproduced
exactly that: a ``report.md`` symlink pointing outside the workspace was
accepted, read, redacted and *rewritten through the link*.

This module is the single place where the host opens kernel-produced files. The
guarantees are:

* the final path component is never followed (``O_NOFOLLOW``), and the file
  identity is re-checked on the open file descriptor (``fstat``), so a
  swap between check and open is detected rather than followed;
* only regular files are read; directories, devices, FIFOs and sockets are
  refused;
* reads and writes are bounded by a byte limit, enforced while reading (not only
  from the file's stat size, which a growing file can outrun);
* an optional ``root`` constrains the resolved path to a host-owned directory
  (``realpath`` containment), so a kernel-reported path cannot point anywhere
  else on the host;
* writes go to a fresh temporary file and are published with ``os.replace``.
  Replacing the directory entry means a symlink is replaced instead of being
  written through, and a hard link is broken instead of mutating the other
  name's content.

The module never returns a path it has not opened; callers must not fall back to
``read_text`` when a call here refuses a file.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

try:  # POSIX (Linux, macOS) — the host runs on both.
    _O_NOFOLLOW = os.O_NOFOLLOW
    _O_CLOEXEC = os.O_CLOEXEC
except AttributeError:  # pragma: no cover - non-POSIX fallback
    _O_NOFOLLOW = 0
    _O_CLOEXEC = 0

#: Default ceiling for one artifact the host reads back (report, session, sweep).
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
_READ_CHUNK = 64 * 1024


class ArtifactError(RuntimeError):
    """A kernel-produced file was refused (missing, linked, oversized, moved…)."""


def is_within(path: Path, root: Path) -> bool:
    """True when ``path`` resolves inside ``root`` (both real paths)."""
    try:
        resolved = Path(os.path.realpath(path))
        base = Path(os.path.realpath(root))
    except OSError:  # pragma: no cover - realpath rarely fails
        return False
    return resolved == base or base in resolved.parents


def is_regular_file(path: Path) -> bool:
    """True for an existing regular file whose final component is not a link."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def read_regular_file(
    path: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    root: str | Path | None = None,
) -> bytes:
    """Read one bounded regular file without following links.

    Raises :class:`ArtifactError` for anything the host must not read: a
    symlink, a non-regular file, an oversized file, a path outside ``root``, or
    a file that changed identity between the check and the open.
    """
    target = Path(path)
    # The final component is examined *before* resolution: a symlink must be
    # refused even when it points at another file inside the allowed root.
    try:
        given = os.lstat(target)
    except OSError as exc:
        raise ArtifactError(f"文件不可用: {target}") from exc
    if stat.S_ISLNK(given.st_mode):
        raise ArtifactError(f"符号链接，已拒绝: {target}")
    if not stat.S_ISREG(given.st_mode):
        raise ArtifactError(f"非普通文件，已拒绝: {target}")
    # Parent-directory links still resolve; containment is checked on the real
    # path so a linked directory cannot smuggle the file outside the root.
    resolved = Path(os.path.realpath(target))
    if root is not None and not is_within(resolved, Path(root)):
        raise ArtifactError(f"路径不属于本次运行的允许范围: {target}")
    try:
        before = os.lstat(resolved)
    except OSError as exc:
        raise ArtifactError(f"文件不可用: {target}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise ArtifactError(f"非普通文件，已拒绝: {target}")
    if before.st_size > max_bytes:
        raise ArtifactError(f"文件超过大小上限 {max_bytes} 字节: {target}")

    flags = os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise ArtifactError(f"无法安全打开文件: {target}") from exc
    try:
        after = os.fstat(descriptor)
        if not stat.S_ISREG(after.st_mode):
            raise ArtifactError(f"打开后不是普通文件，已拒绝: {target}")
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise ArtifactError(f"文件在校验后被替换，已拒绝: {target}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(_READ_CHUNK, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(data) > max_bytes:
        raise ArtifactError(f"读取过程中超过大小上限 {max_bytes} 字节: {target}")
    return data


def read_text_utf8(
    path: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    root: str | Path | None = None,
    errors: str = "replace",
) -> str:
    """Decoded convenience wrapper around :func:`read_regular_file`."""
    return read_regular_file(path, max_bytes=max_bytes, root=root).decode(
        "utf-8", errors
    )


def replace_with_bytes(
    path: str | Path,
    data: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> None:
    """Atomically publish ``data`` at ``path`` without following links.

    The temporary file is created in the destination directory, so the replace
    stays on one filesystem. A symlink at ``path`` is replaced (the link target
    is never touched); an existing hard link is broken (the other name keeps its
    bytes).
    """
    if len(data) > max_bytes:
        raise ArtifactError(f"写入超过大小上限 {max_bytes} 字节: {path}")
    target = Path(path)
    directory = target.parent
    directory.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(directory), prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def replace_with_text(
    path: str | Path,
    text: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> None:
    replace_with_bytes(path, text.encode("utf-8"), max_bytes=max_bytes)


def copy_regular_file(
    source: str | Path,
    destination: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    root: str | Path | None = None,
) -> int:
    """Copy one validated regular file into a host-owned destination."""
    data = read_regular_file(source, max_bytes=max_bytes, root=root)
    replace_with_bytes(destination, data, max_bytes=max_bytes)
    return len(data)
