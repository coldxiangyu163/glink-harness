"""Narrow compatibility for retired, host-owned dsh audit events (ACP-15).

Only a continuation's target HOME is writable here, before its new agent is
spawned. Parent roots and archive manifests are never inputs to a write. A
rebooted lease sandbox is a disposable target too: the prior agent was stopped
before pause and reboot cannot revive a writer. Locks are deliberately untouched.

The codec uses the already-required Node runtime's public Zstandard API, not a
new Python dependency or a private dsh import. It sees bytes on stdin/stdout only;
filesystem ownership, bounds and publication remain in the host's transfer seam.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import uuid
from pathlib import Path
from typing import Any

from ..artifacts import ArtifactError, ArtifactTree
from .acp_env import INHERIT_MAX_FILE_BYTES, INHERIT_MAX_FILES, INHERIT_MAX_TOTAL_BYTES
from .e2b_env import E2B_REQUEST_TIMEOUT_SECONDS, _entry_kind, _read_file_bounded

# docs/acp-integration-notes.md §19.6: released dsh freezes
# KNOWN_SESSION_EVENT_TYPES; session.append cannot set envelope ignorable.
# This retired host-owned audit has no replay semantics. No heuristic/prefix
# matching: all other unknown required types still reach dsh's fail-closed gate.
RETIRED_AUDIT_TYPES = frozenset({"web/jina-fetch-request"})
V3_LOG_FILENAME = "session.v3.jsonl.zstd"
# A compressed log may expand; bound decoded data separately, also across files.
DECODE_MAX_BYTES = INHERIT_MAX_TOTAL_BYTES
CODEC_TIMEOUT_SECONDS = 600

_NODE_CODEC = r"""
const fs = require('node:fs');
const z = require('node:zlib');
// Zstandard's public frame layout: descriptor, optional window/dictionary/
// content-size fields, 3-byte block headers and optional 4-byte checksum.
// Node accepts a torn final frame, so validate its physical extent ourselves
// before decoding. Never recover/rewrite a partial durable append here.
function frameEnd(input, start) {
  let p = start;
  const need = n => {if (p + n > input.length) throw new Error();};
  need(5);
  if (input.readUInt32LE(p) !== 0xfd2fb528) throw new Error();
  p += 4;
  const descriptor = input[p++];
  if (descriptor & 0x18) throw new Error();
  const single = Boolean(descriptor & 0x20);
  const sizeFlag = descriptor >>> 6;
  const fields = (single ? 0 : 1) + [0, 1, 2, 4][descriptor & 3]
    + (sizeFlag ? [0, 2, 4, 8][sizeFlag] : (single ? 1 : 0));
  need(fields);
  p += fields;
  let last = false;
  while (!last) {
    need(3);
    const block = input.readUIntLE(p, 3);
    p += 3;
    last = Boolean(block & 1);
    const type = (block >>> 1) & 3;
    if (type === 3) throw new Error();
    const length = type === 1 ? 1 : block >>> 3;
    need(length);
    p += length;
  }
  if (descriptor & 4) {need(4); p += 4;}
  return p;
}
try {
  const [mode, limit] = process.argv.slice(1);
  const input = fs.readFileSync(0);
  if (mode === 'decode') {
    // Node decodes only one frame per call. info.engine.bytesWritten is the
    // consumed compressed prefix; iterate the entire durable append stream.
    const chunks = [];
    let offset = 0, size = 0;
    while (offset < input.length) {
      if (size >= Number(limit)) throw new Error();
      const end = frameEnd(input, offset);
      const result = z.zstdDecompressSync(input.subarray(offset, end), {
        info: true, maxOutputLength: Number(limit) - size,
      });
      const used = result.engine.bytesWritten;
      if (!used || used !== end - offset) throw new Error();
      if (!offset && (!result.buffer.length
        || result.buffer.indexOf(10) !== result.buffer.length - 1)) throw new Error();
      offset += used;
      size += result.buffer.length;
      chunks.push(result.buffer);
    }
    process.stdout.write(Buffer.concat(chunks, size));
  } else {
    // dsh requires exactly one header line in the first frame. Remaining
    // events may share a batch frame, as in its normal append path.
    const end = input.indexOf(10) + 1;
    if (!end) throw new Error();
    const options = {params: {[z.constants.ZSTD_c_checksumFlag]: 1}};
    process.stdout.write(z.zstdCompressSync(input.subarray(0, end), options));
    if (end < input.length) process.stdout.write(z.zstdCompressSync(input.subarray(end), options));
  }
} catch {
  // Never print parser input, session text, URLs or native exception strings.
  process.stderr.write('ACP state codec failed\n');
  process.exitCode = 1;
}
"""


class AcpStateCompatError(RuntimeError):
    """The target copy could not be safely prepared; no agent may start."""


def _codec(data: bytes, mode: str, limit: int) -> bytes:
    try:
        result = subprocess.run(
            ["node", "-e", _NODE_CODEC, mode, str(limit)], input=data,
            capture_output=True, timeout=CODEC_TIMEOUT_SECONDS,
            env={"PATH": os.environ.get("PATH", "")},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AcpStateCompatError("ACP 续接副本编解码不可用") from exc
    if result.returncode or len(result.stdout) > limit:
        raise AcpStateCompatError("ACP 续接副本编解码失败或超过上限")
    return result.stdout


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def annotate_v3_log(data: bytes, *, decode_limit: int = DECODE_MAX_BYTES) -> tuple[bytes, int]:
    """Return original bytes unless a whitelisted envelope needs the one flag.

    Preserve raw JSON lines, including large numbers, whitespace and payloads;
    reserializing JSON through a JS Number would lose >2**53 integers. Only
    insert a top-level member immediately after the event's opening brace.
    Header/event count, seq and references never move. Existing true flags and
    unrelated types are byte-idempotent. Corrupt/ambiguous envelopes are refused
    without exposing the input in the error message.
    """
    if len(data) > INHERIT_MAX_FILE_BYTES:
        raise AcpStateCompatError("ACP 续接副本压缩日志超过上限")
    raw = _codec(data, "decode", decode_limit)
    if not raw.endswith(b"\n"):
        raise AcpStateCompatError("ACP 续接副本日志未完整结束")
    lines = raw.splitlines(keepends=True)
    changed = False
    try:
        header = json.loads(lines[0], object_pairs_hook=_unique_object)
        if not isinstance(header, dict) or header.get("type") != "session":
            raise ValueError("invalid header")
        if header.get("version") != 3:
            return data, len(raw)  # No historical format migration.
        for index in range(1, len(lines)):
            event = json.loads(lines[index], object_pairs_hook=_unique_object)
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise ValueError("invalid envelope")
            if event["type"] not in RETIRED_AUDIT_TYPES or event.get("ignorable") is True:
                continue
            if "ignorable" in event:
                raise ValueError("invalid ignorable flag")
            start = len(lines[index]) - len(lines[index].lstrip())
            if lines[index][start:start + 1] != b"{":
                raise ValueError("invalid object")
            lines[index] = (lines[index][:start + 1] + b'"ignorable":true,'
                            + lines[index][start + 1:])
            changed = True
    except (ValueError, IndexError) as exc:
        raise AcpStateCompatError("ACP 续接副本日志结构不可安全修补") from exc
    if not changed:
        return data, len(raw)
    repaired = b"".join(lines)
    if len(repaired) > decode_limit:
        raise AcpStateCompatError("ACP 续接副本解压日志超过上限")
    return _codec(repaired, "encode", INHERIT_MAX_FILE_BYTES), len(repaired)


def _candidate(relative: str) -> bool:
    parts = relative.split("/")
    return (len(parts) == 5 and parts[:2] == [".dsh", "sessions"]
            and parts[-1] == V3_LOG_FILENAME
            and all(part not in ("", ".", "..") for part in parts))


def repair_resume_home(home: Path | str, *, handle: Any = None) -> None:
    """Prepare one inactive target, local/mounted or remote after all uploads.

    The caller restricts this to dsh continuations before sandbox.spawn.
    Local/Docker roots use ArtifactTree's anchored no-follow read/atomic replace.
    Remote roots use SDK entry kinds to verify every ancestor, bounded reads and
    a temporary file + guarded rename. No process is alive in these targets, so
    entries cannot be switched by a leftover agent during SDK calls/publication.
    Remote failures surface only a fixed summary, not SDK output or file content.
    """
    if handle is not None:
        _repair_remote(str(home), handle)
        return
    try:
        with ArtifactTree(Path(home)) as tree:
            relatives = tree.files(max_files=INHERIT_MAX_FILES + 1)
            if len(relatives) > INHERIT_MAX_FILES:
                raise AcpStateCompatError("ACP 续接副本文件数超过上限")
            total = 0
            for relative in relatives:
                if not _candidate(relative):
                    continue
                data = tree.read(relative, max_bytes=INHERIT_MAX_FILE_BYTES)
                repaired, size = annotate_v3_log(data, decode_limit=DECODE_MAX_BYTES - total)
                total += size
                if repaired != data:
                    tree.replace(relative, repaired, max_bytes=INHERIT_MAX_FILE_BYTES)
    except (ArtifactError, OSError) as exc:
        raise AcpStateCompatError("ACP 续接副本路径不可安全修补") from exc


def validate_remote_home(home: str, handle: Any, *, create: bool = False) -> list[str]:
    """Walk from the trusted sandbox '/' anchor using immediate entry kinds.

    Only list a parent after that exact directory was verified. A symlink,
    missing/ambiguous entry or failed lookup refuses traversal, before any
    recursive listing, payload read or scratch write under HOME. Cold starts
    may create missing directories one at a time, then verify the new entry;
    rebooted hot targets must already exist. No realpath/stat call follows links.
    """
    parts = home.split("/")
    if parts[0] != "" or any(part in ("", ".", "..") for part in parts[1:]):
        raise AcpStateCompatError("ACP 续接副本状态根路径无效")
    parent = "/"
    directories = []

    def matches(parent: str, child: str) -> list[Any]:
        entries = handle.files.list(
            parent, depth=1, request_timeout=E2B_REQUEST_TIMEOUT_SECONDS,
        )
        return [entry for entry in entries if str(entry.path) == child]

    try:
        for part in parts[1:]:
            child = parent.rstrip("/") + "/" + part
            entries = matches(parent, child)
            if not entries and create:
                handle.files.make_dir(child, request_timeout=E2B_REQUEST_TIMEOUT_SECONDS)
                entries = matches(parent, child)
            if len(entries) != 1 or _entry_kind(entries[0]) != "dir":
                raise AcpStateCompatError("ACP 续接副本状态根含链接或未经核验的目录")
            directories.append(child)
            parent = child
    except AcpStateCompatError:
        raise
    except Exception as exc:
        raise AcpStateCompatError("ACP 续接副本状态根目录核验失败") from exc
    return directories


def _repair_remote(home: str, handle: Any) -> None:
    try:
        validate_remote_home(home, handle)
        entries = handle.files.list(home, depth=5, request_timeout=E2B_REQUEST_TIMEOUT_SECONDS)
        if len(entries) > INHERIT_MAX_FILES * 5:
            raise AcpStateCompatError("ACP 续接副本条目数超过上限")
        kinds = {str(entry.path): _entry_kind(entry) for entry in entries}
        candidates = []
        for entry in entries:
            path = str(entry.path)
            if not path.startswith(home + "/") or kinds[path] != "file":
                continue
            relative = path[len(home) + 1:]
            if not _candidate(relative):
                continue
            parts = relative.split("/")
            ancestors = [home + "/" + "/".join(parts[:i]) for i in range(1, 5)]
            if not all(kinds.get(parent) == "dir" for parent in ancestors):
                continue  # Includes children advertised below a directory symlink.
            candidates.append((path, entry, ancestors))
        if len(candidates) > INHERIT_MAX_FILES:
            raise AcpStateCompatError("ACP 续接副本文件数超过上限")
        total = 0
        for path, entry, ancestors in candidates:
            data, _failure = _read_file_bounded(
                handle, path, INHERIT_MAX_FILE_BYTES, known_size=getattr(entry, "size", None),
            )
            if data is None:
                raise AcpStateCompatError("ACP 续接副本不可读取或超过上限")
            repaired, size = annotate_v3_log(data, decode_limit=DECODE_MAX_BYTES - total)
            total += size
            if repaired == data:
                continue
            _publish_remote(handle, home, path, ancestors, repaired)
    except AcpStateCompatError:
        raise
    except Exception as exc:  # SDK failures must not echo their stdout or session bytes.
        raise AcpStateCompatError("ACP 续接副本沙箱修补失败") from exc


def _publish_remote(handle: Any, home: str, path: str, ancestors: list[str], data: bytes) -> None:
    # Recheck the complete root chain before scratch I/O too; rename-time
    # shell guards are a final fence, never permission for an earlier write.
    roots = validate_remote_home(home, handle)
    temporary = path + ".acp15-" + uuid.uuid4().hex
    try:
        handle.files.write(temporary, data, request_timeout=E2B_REQUEST_TIMEOUT_SECONDS)
        checks = [f"test ! -L {shlex.quote(p)}" for p in [*roots, *ancestors, path, temporary]]
        directories = [f"test -d {shlex.quote(p)}" for p in [*roots, *ancestors]]
        files = [f"test -f {shlex.quote(p)}" for p in [path, temporary]]
        command = " && ".join([
            *checks, *directories, *files, shlex.join(["chmod", "0600", temporary]),
            shlex.join(["mv", "-f", "--", temporary, path]),
        ])
        result = handle.commands.run(command, timeout=60)
        if getattr(result, "exit_code", None) != 0:
            raise AcpStateCompatError("ACP 续接副本沙箱发布失败")
    except BaseException:
        try:
            handle.files.remove(temporary, request_timeout=E2B_REQUEST_TIMEOUT_SECONDS)
        except Exception:
            pass  # Best effort scratch cleanup never masks the original failure.
        raise
