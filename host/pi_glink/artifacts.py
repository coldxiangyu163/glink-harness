"""Directory-anchored reads and writes for kernel-reachable run files.

The kernel (pi and its tools) writes into the run workspace, its session
directory and its runtime configuration directory, and the host has to read
some of those files back: the report it copies and serves, the session log pi
wrote, and any text artifact the credential sweep touches. Resolving a path
string and then opening it again by name is **not** a safe check: every
component can be swapped for a symbolic link between the check and the open,
and a directory link created inside a container is resolved against the
*host* root. The R1 audit reproduced exactly that: after a ``realpath``
containment check an ancestor directory was replaced with a link and the host
read an external file.

This module therefore never re-resolves a path. An :class:`ArtifactTree` holds
a directory file descriptor for one run-owned root and every operation walks
from it with ``openat``-style calls:

* the root's parent is host configuration (``runs/``, ``workspaces/``, the
  session root) and is trusted; the root itself and every component below it
  are opened with ``O_NOFOLLOW`` — a symbolic link anywhere below the parent is
  refused, never followed;
* only regular files are read; directories, devices, FIFOs (opened
  non-blocking) and sockets are refused; the identity observed before opening
  must match the opened descriptor;
* after the leaf is open, the chain of directory entries is re-verified from
  the root, so the bytes read belong to a file that was reachable from the
  root through real directories at that moment;
* reads and writes are bounded by a byte limit, enforced while reading;
* writes create a fresh temporary file inside the verified parent directory
  and publish it with ``renameat``: a link at the destination is replaced
  (its target untouched), a hard link is broken, and the write can never land
  in a directory outside the tree;
* a removal unlinks one named regular file through its verified parent
  directory; a link is refused without touching its target, and the directory
  chain is re-verified first.

Paths given to a tree are relative, or absolute but lexically inside the root;
``..`` components are refused. Callers never fall back to path-based I/O when
a tree operation refuses a file.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path, PurePosixPath
from types import TracebackType

_O_NOFOLLOW = os.O_NOFOLLOW
_O_CLOEXEC = os.O_CLOEXEC
_O_DIRECTORY = os.O_DIRECTORY
_O_NONBLOCK = os.O_NONBLOCK
_DIR_FLAGS = os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW | _O_CLOEXEC

#: Default ceiling for one artifact the host reads back (report, session, sweep).
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
_READ_CHUNK = 64 * 1024


class ArtifactError(RuntimeError):
    """A kernel-reachable file was refused (missing, linked, oversized, moved…)."""


class ArtifactMissing(ArtifactError):
    """The file (or the tree root) does not exist — absent, not refused."""


class ArtifactTree:
    """A verified handle on one run-owned directory tree.

    Use as a context manager; the directory descriptor is closed on exit.
    ``create=True`` creates the root (not its parent) when it is missing.
    """

    def __init__(self, root: str | Path, *, create: bool = False) -> None:
        self.root = Path(root)
        name = self.root.name
        if not name or name in {".", ".."}:
            raise ArtifactError(f"不是可用的运行目录: {self.root}")
        try:
            parent = os.open(self.root.parent, os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC)
        except OSError as exc:
            raise ArtifactError(f"运行目录不可用: {self.root}") from exc
        try:
            if create:
                try:
                    os.mkdir(name, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            self._fd = os.open(name, _DIR_FLAGS, dir_fd=parent)
        except FileNotFoundError as exc:
            raise ArtifactMissing(f"运行目录不存在: {self.root}") from exc
        except OSError as exc:
            raise ArtifactError(f"运行目录不可用或是链接: {self.root}") from exc
        finally:
            os.close(parent)

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> "ArtifactTree":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # -- queries --------------------------------------------------------------

    def read(self, path: str | Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
        """Read one bounded regular file inside the tree."""
        parts = self._parts(path)
        chain = self._walk(parts[:-1], create=False)
        try:
            descriptor = self._open_leaf(chain[-1], parts[-1], max_bytes)
            try:
                self._verify(chain, parts)
                return self._read_bounded(descriptor, max_bytes, path)
            finally:
                os.close(descriptor)
        finally:
            _close_all(chain)

    def size(self, path: str | Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> int:
        """Size of a regular file the tree would serve (no read, same refusals)."""
        parts = self._parts(path)
        chain = self._walk(parts[:-1], create=False)
        try:
            info = self._leaf_stat(chain[-1], parts[-1], path)
            if info.st_size > max_bytes:
                raise ArtifactError(f"文件超过大小上限 {max_bytes} 字节: {path}")
            return info.st_size
        finally:
            _close_all(chain)

    def files(self, *, max_files: int) -> list[str]:
        """Relative paths of regular files, walked without following links."""
        found: list[str] = []
        stack: list[tuple[tuple[str, ...], int]] = [((), os.dup(self._fd))]
        try:
            while stack and len(found) < max_files:
                prefix, descriptor = stack.pop()
                try:
                    with os.scandir(descriptor) as iterator:
                        entries = sorted(iterator, key=lambda item: item.name)
                except OSError:
                    entries = []
                for entry in entries:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        try:
                            child = os.open(entry.name, _DIR_FLAGS, dir_fd=descriptor)
                        except OSError:
                            continue
                        stack.append(((*prefix, entry.name), child))
                    elif entry.is_file(follow_symlinks=False):
                        found.append("/".join((*prefix, entry.name)))
                        if len(found) >= max_files:
                            break
                os.close(descriptor)
        finally:
            _close_all([descriptor for _prefix, descriptor in stack])
        return found

    # -- writes ---------------------------------------------------------------

    def remove(self, path: str | Path) -> bool:
        """Delete one regular file inside the tree without following any link.

        ``False`` means the leaf was already absent (nothing to delete). A
        symbolic link, a non-regular file, an outside path and an unusable
        directory chain all raise :class:`ArtifactError` — the same boundary
        the reads and writes use, so a link is never followed (its target is
        untouched) and only the named leaf is unlinked. Directories are never
        removed.
        """
        parts = self._parts(path)
        chain = self._walk(parts[:-1], create=False)
        try:
            try:
                self._leaf_stat(chain[-1], parts[-1], path)
            except ArtifactMissing:
                return False
            self._verify(chain, parts[:-1])
            try:
                os.unlink(parts[-1], dir_fd=chain[-1])
            except OSError as exc:
                raise ArtifactError(f"无法删除文件: {path}") from exc
            return True
        finally:
            _close_all(chain)

    def replace(
        self,
        path: str | Path,
        data: bytes,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        create_parents: bool = False,
    ) -> None:
        """Atomically publish ``data`` at ``path`` inside the tree."""
        if len(data) > max_bytes:
            raise ArtifactError(f"写入超过大小上限 {max_bytes} 字节: {path}")
        parts = self._parts(path)
        chain = self._walk(parts[:-1], create=create_parents)
        parent = chain[-1]
        leaf = parts[-1]
        temporary = f".{leaf}.{secrets.token_hex(8)}.tmp"
        try:
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC,
                    0o600,
                    dir_fd=parent,
                )
            except OSError as exc:
                raise ArtifactError(f"无法在运行目录内创建临时文件: {path}") from exc
            try:
                try:
                    view = memoryview(data)
                    while view:
                        written = os.write(descriptor, view)
                        view = view[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self._verify(chain, parts[:-1])
                os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
            except BaseException:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except OSError:
                    pass
                raise
        finally:
            _close_all(chain)

    # -- internals ------------------------------------------------------------

    def _parts(self, path: str | Path) -> tuple[str, ...]:
        candidate = Path(path)
        if candidate.is_absolute():
            normalised = PurePosixPath(os.path.normpath(candidate))
            base = PurePosixPath(os.path.normpath(self.root))
            try:
                relative = normalised.relative_to(base)
            except ValueError as exc:
                raise ArtifactError(f"路径不属于本次运行的允许范围: {path}") from exc
        else:
            relative = PurePosixPath(candidate.as_posix())
        parts = tuple(relative.parts)
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise ArtifactError(f"路径不属于本次运行的允许范围: {path}")
        return parts

    def _walk(self, directories: tuple[str, ...], *, create: bool) -> list[int]:
        """Open every directory component without following links.

        Returns the descriptor chain ``[root, dir1, …, parent-of-leaf]``; the
        caller closes it.
        """
        chain = [os.dup(self._fd)]
        try:
            for name in directories:
                if create:
                    try:
                        os.mkdir(name, 0o700, dir_fd=chain[-1])
                    except FileExistsError:
                        pass
                chain.append(os.open(name, _DIR_FLAGS, dir_fd=chain[-1]))
        except OSError as exc:
            _close_all(chain)
            raise ArtifactError(
                f"路径分量是链接、不是目录或不可用，已拒绝: {'/'.join(directories)}"
            ) from exc
        return chain

    @staticmethod
    def _leaf_stat(parent: int, leaf: str, shown: str | Path) -> os.stat_result:
        try:
            info = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise ArtifactMissing(f"文件不存在: {shown}") from exc
        except OSError as exc:
            raise ArtifactError(f"文件不可用: {shown}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ArtifactError(f"符号链接，已拒绝: {shown}")
        if not stat.S_ISREG(info.st_mode):
            raise ArtifactError(f"非普通文件，已拒绝: {shown}")
        return info

    def _open_leaf(self, parent: int, leaf: str, max_bytes: int) -> int:
        before = self._leaf_stat(parent, leaf, leaf)
        if before.st_size > max_bytes:
            raise ArtifactError(f"文件超过大小上限 {max_bytes} 字节: {leaf}")
        try:
            # Non-blocking so a FIFO swapped in after the stat cannot hang the host.
            descriptor = os.open(
                leaf, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC, dir_fd=parent
            )
        except OSError as exc:
            raise ArtifactError(f"无法安全打开文件（可能已被替换为链接）: {leaf}") from exc
        after = os.fstat(descriptor)
        if not stat.S_ISREG(after.st_mode) or (after.st_dev, after.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            os.close(descriptor)
            raise ArtifactError(f"文件在校验后被替换，已拒绝: {leaf}")
        return descriptor

    @staticmethod
    def _verify(chain: list[int], parts: tuple[str, ...]) -> None:
        """Re-check that every opened directory is still the entry it came from."""
        for index, name in enumerate(parts[: len(chain) - 1]):
            try:
                entry = os.stat(name, dir_fd=chain[index], follow_symlinks=False)
            except OSError as exc:
                raise ArtifactError(f"路径在校验过程中被移动，已拒绝: {name}") from exc
            opened = os.fstat(chain[index + 1])
            if (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino):
                raise ArtifactError(f"路径在校验过程中被替换，已拒绝: {name}")

    @staticmethod
    def _read_bounded(descriptor: int, max_bytes: int, shown: str | Path) -> bytes:
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(_READ_CHUNK, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise ArtifactError(f"读取过程中超过大小上限 {max_bytes} 字节: {shown}")
        return data


def _close_all(descriptors: list[int]) -> None:
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError:
            pass
