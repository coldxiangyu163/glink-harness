"""The conversation-state archive channel: one tar stream per direction.

阶段 1 的归档传输通道（docs/adr/0018）。宿主持有对话级权威状态（durable
真相源），沙箱只是热缓存：状态根在两个方向上各走一条**单请求 tar 流**——

* ``POST /archive/state``（沙箱 → 宿主回收）：沙箱端把状态根打包成 tar
  （增量归档只携带每个追加式文件的 ``[base, size)`` 尾部与新增/变化文件的
  全量），宿主端流式落盘、按 manifest 校验、与上一版状态拼接后**整份成功
  才发布**（沿用 ``recovered`` 的 rename 纪律，docs/adr/0005）；
* ``GET /archive/state``（宿主 → 沙箱回灌）：服务端从 grant 推导源目录
  （不接受客户端路径），把权威状态根打包成 tar 流回给沙箱。

通道复用 ADR 0008 出口代理的 listener（同一 ``ThreadingHTTPServer``），但
token 独立（``pga_`` 前缀）、只授归档路由、不携带任何上游凭据字段：归档
token 泄露只暴露本 run 状态根的收发，没有凭据语义。凭据目标
（``credential_dests``）不出现在归档里——沙箱端打包器排除、宿主端解包时
再见即拒绝（双保险，ADR 0015/0016）。

传输格式是**裸 tar**：主要体量（dsh 会话文件）本身已是 zstd 压缩数据，
再压缩收益有限；沙箱模板镜像没有 zstd CLI，两端能力不一致时压缩反而让
通道失效。压缩层留给模板演进后再启用（见 ADR 0018 的残留）。

安全边界（回收方向，宿主端把沙箱产物当不可信输入）：

* 流式落盘受 :data:`ARCHIVE_MAX_BYTES` 上限，超限中止连接且**不发布**任何
  recovered 状态（fail-closed 不变：按阶段 1 裁决，这个量级属于异常检测，
  正常研究会话远达不到）；
* tar 解包只接受 ``manifest.json`` / ``files/<rel>`` / ``delta/<rel>`` 三类
  成员名（白名单），并交给 ``tarfile`` 的 ``data`` filter（拒绝绝对路径与
  ``..``）；
* 每个成员的 size 与 sha256 都按 manifest 校验；增量拼接要求宿主侧基线
  文件的长度恰为 ``base``，拼接结果整文件 sha256 复核——错位必然哈希不
  对，失败即整次回收失败，下一轮降级全量；
* 发布 = 在 staging 目录里组装完整新树后一次 ``rename``；中途失败或崩溃
  留下的是旧状态（或无状态），追问门禁按 manifest 的 ``sourceRunId`` 新鲜
  度校验拒收陈旧版本，不会呈现一个 agent 并不拥有的会话。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import tarfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("pi_glink.archive_channel")

#: Route segment shared by both directions (``/archive/state``).
ARCHIVE_KIND_STATE = "state"
#: Token prefix: archive grants never mix with the egress family (``pge_``).
ARCHIVE_TOKEN_PREFIX = "pga_"
#: One archive body's ceiling. This is the *anomaly-detection* bound of 阶段 1
#: (not a session-lifetime quota): a research session's own state is tens of MB
#: at most, so reaching this means something runaway — the connection is
#: aborted and no state is published (fail-closed), with the fact recorded in
#: the event stream by the caller.
ARCHIVE_MAX_BYTES = 256 * 1024 * 1024
#: Member-count ceiling of one archive (defence against a crafted tar that
#: expands into endless tiny members). Aligned with the inheritance file bound.
ARCHIVE_MAX_MEMBERS = 16384
#: Budget of the **reconstructed** state a manifest describes (not of one
#: transfer): incremental publishes append onto the previous tree, so each
#: small body can stay under :data:`ARCHIVE_MAX_BYTES` while the state keeps
#: growing. The values are the inheritance/files-reclaim bounds of
#: ``acp_env.INHERIT_MAX_*`` (kept equal by a test; this module is a leaf and
#: may not import the kernel's ACP modules), so all three state paths refuse
#: the same runaway session.
STATE_MAX_FILES = 16384
STATE_MAX_FILE_BYTES = 128 * 1024 * 1024
STATE_MAX_TOTAL_BYTES = 256 * 1024 * 1024
#: Seconds between bytes while receiving one archive body (slow-loris bound).
ARCHIVE_READ_TIMEOUT_SECONDS = 300
#: The manifest member every archive must carry exactly once, at the root.
MANIFEST_MEMBER = "manifest.json"
#: Recognised member prefixes inside the tar container.
_FILES_PREFIX = "files/"
_DELTA_PREFIX = "delta/"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: Fsync-backed write of one staged file (the whole publish is rename-based,
#: so the bytes only need to reach the filesystem before the rename).
_CHUNK = 1 << 16


class ArchiveError(RuntimeError):
    """One archive transfer or its content failed the host's checks.

    The message is safe for logs and events (no secret values, no paths beyond
    the relative names the manifest itself carries).
    """


@dataclass(frozen=True, slots=True)
class ArchiveEndpoint:
    """The archive routes as the sandbox reaches them, plus this run's token.

    The mirror of :class:`~pi_glink.kernel.agent_env.EgressEndpoint` for the
    archive capability: ``origin`` is the proxy base URL as the sandbox sees
    it (the public URL on E2B), ``token`` the ``pga_`` grant. Both directions
    live on ``<origin>/archive/state``.
    """

    origin: str
    token: str = field(repr=False)

    @property
    def url(self) -> str:
        return f"{self.origin.rstrip('/')}/archive/{ARCHIVE_KIND_STATE}"


@dataclass
class ArchiveGrant:
    """One run's archive capability: the token plus what it may move.

    Memory only (same lifetime discipline as an egress grant): the token binds
    the run and the conversation directory the run may publish into / read
    from. ``credential_dests`` are the run's credential destinations (relative
    to the state root) that must never appear inside an archive.
    """

    token: str = field(repr=False)
    run_id: str
    conversation_dir: Path
    credential_dests: tuple[str, ...] = ()
    #: Receipt of the last successful publish (bytes/files/duration), for the
    #: run's event stream; ``None`` until one POST succeeded.
    last_publish: dict[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def state_dir(self) -> Path:
        """The conversation's authoritative state root (publish target)."""
        return self.conversation_dir / "state"

    @property
    def manifest_path(self) -> Path:
        """The durable manifest published with the state root."""
        return self.state_dir / MANIFEST_MEMBER


def read_state_manifest(conversation_dir: Path) -> dict[str, Any] | None:
    """The published manifest of a conversation's state, or ``None``.

    ``None`` covers both "never published" and "unparsable" — the caller (the
    follow-up gate and the incremental base) treats an unreadable manifest as
    no base at all, falling back to a full archive or to the legacy layout.
    """
    path = conversation_dir / "state" / MANIFEST_MEMBER
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        # The swap crash window can leave state/ missing with the last good
        # tree parked as .state.retired (see :func:`_heal_swap_window`): heal
        # it here too — readers (the follow-up gate, the incremental base)
        # must not see a "never published" conversation that was.
        _heal_swap_window(conversation_dir)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return None
    files = raw.get("files")
    if not isinstance(files, dict):
        return None
    return raw


def incremental_base(conversation_dir: Path) -> dict[str, dict[str, Any]]:
    """The ``{relative: {size, sha256}}`` map a reclaim script anchors on.

    Derived from the published manifest; an absent or unusable manifest means
    "no base" and the first archive is simply a full one.
    """
    manifest = read_state_manifest(conversation_dir)
    if manifest is None:
        return {}
    base: dict[str, dict[str, Any]] = {}
    for relative, entry in manifest.get("files", {}).items():
        if not isinstance(relative, str) or not isinstance(entry, dict):
            continue
        size = entry.get("size")
        digest = entry.get("sha256")
        if isinstance(size, int) and isinstance(digest, str):
            base[relative] = {"size": size, "sha256": digest}
    return base


class ArchiveChannel:
    """The archive routes served on the egress proxy's listener.

    The :class:`~pi_glink.egress.EgressProxy` owns one channel and forwards
    ``/archive/*`` to it; the channel keeps its own grant table (``pga_``
    tokens) so the two capabilities can never be exchanged. Publishing is
    single-run-at-a-time by construction (the host admits one run globally),
    and the staging directory is per-grant anyway.
    """

    def __init__(self, *, max_bytes: int = ARCHIVE_MAX_BYTES) -> None:
        self.max_bytes = max_bytes
        self._grants: dict[str, ArchiveGrant] = {}
        self._lock = threading.Lock()

    # -- grants -----------------------------------------------------------------

    def issue(
        self,
        *,
        run_id: str,
        conversation_dir: Path,
        credential_dests: tuple[str, ...] = (),
    ) -> ArchiveGrant:
        grant = ArchiveGrant(
            token=ARCHIVE_TOKEN_PREFIX + secrets.token_urlsafe(32),
            run_id=run_id,
            conversation_dir=Path(conversation_dir),
            credential_dests=tuple(credential_dests),
        )
        with self._lock:
            self._grants[_digest(grant.token)] = grant
        return grant

    def revoke(self, token: str) -> ArchiveGrant | None:
        with self._lock:
            return self._grants.pop(_digest(token), None)

    def grant_for(self, token: str) -> ArchiveGrant | None:
        with self._lock:
            return self._grants.get(_digest(token))

    @property
    def live_grants(self) -> int:
        with self._lock:
            return len(self._grants)

    def close(self) -> None:
        with self._lock:
            self._grants.clear()

    # -- receive (sandbox → host) ------------------------------------------------

    def receive(
        self,
        grant: ArchiveGrant,
        body,
        *,
        length: int,
    ) -> dict[str, Any]:
        """Stream one archive body to staging, verify it, publish the state.

        ``body`` is the request's raw stream (``rfile``); ``length`` its
        declared ``Content-Length``. The whole receive-verify-publish is one
        transaction against the grant's staging area: any failure raises
        :class:`ArchiveError` with **nothing published** — the previous state
        (if any) stays exactly as it was, which is the fail-closed contract a
        follow-up's admission gate reads.
        """
        if length <= 0:
            raise ArchiveError("归档请求缺少正体长度")
        if length > self.max_bytes:
            raise ArchiveError(
                f"归档超过上限（{length} > {self.max_bytes} 字节）；已中止，未发布任何状态"
            )
        staging = grant.conversation_dir / ".archive-staging" / grant.run_id
        _reset_dir(staging)
        archive_path = staging / "archive.tar"
        started = time.monotonic()
        received = _copy_bounded(body, archive_path.open("wb"), length)
        if received != length:
            raise ArchiveError(f"归档传输中断（收到 {received}/{length} 字节）")
        facts = publish_archive(
            archive_path,
            grant.conversation_dir,
            source_run_id=grant.run_id,
            credential_dests=grant.credential_dests,
            max_members=ARCHIVE_MAX_MEMBERS,
        )
        facts["durationMs"] = round((time.monotonic() - started) * 1000, 1)
        with grant.lock:
            grant.last_publish = dict(facts)
        shutil.rmtree(staging, ignore_errors=True)
        return facts

    # -- serve (host → sandbox) --------------------------------------------------

    def serve(self, grant: ArchiveGrant, out) -> dict[str, Any]:
        """Stream the conversation's authoritative state as one tar body.

        ``out`` is the response stream (``wfile``). The tar members are the
        state root's relative paths; the manifest and every credential
        destination are skipped — the sandbox re-renders its own credentials
        (``upload_home``), so the backfill must never shadow them. Returns the
        served facts for the run's event stream.
        """
        if read_state_manifest(grant.conversation_dir) is None:
            raise ArchiveError("对话权威状态尚未发布，不能回灌空归档")
        state = grant.state_dir
        skip = {MANIFEST_MEMBER, *grant.credential_dests}
        entries = _walk_state(state, skip)
        started = time.monotonic()
        written = 0
        with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            for relative, path in entries:
                size = path.stat().st_size
                info = tarfile.TarInfo(relative)
                info.size = size
                info.mtime = int(path.stat().st_mtime)
                info.mode = 0o600
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
                written += size
        return {
            "files": len(entries),
            "bytes": written,
            "durationMs": round((time.monotonic() - started) * 1000, 1),
        }


# -- the archive container ------------------------------------------------------


def publish_archive(
    archive_path: Path,
    conversation_dir: Path,
    *,
    source_run_id: str,
    credential_dests: tuple[str, ...] = (),
    max_members: int = ARCHIVE_MAX_MEMBERS,
    max_state_files: int = STATE_MAX_FILES,
    max_file_bytes: int = STATE_MAX_FILE_BYTES,
    max_total_bytes: int = STATE_MAX_TOTAL_BYTES,
) -> dict[str, Any]:
    """Verify one staged archive and publish it as the conversation's state.

    The container's contract (see the module docstring): ``manifest.json`` plus
    ``files/<rel>`` full members and ``delta/<rel>`` tail members. Publishing
    means: build the complete new tree in ``.state.staging`` (the old tree
    where unchanged, full members where replaced, concatenated tails where
    appended), write the new manifest, then swap it in with one ``rename``
    per directory. Any check failure leaves the published state untouched.
    """
    manifest, extracted = _extract_archive(
        archive_path, max_members=max_members
    )
    try:
        entries = _manifest_entries(manifest, credential_dests)
        _check_state_budget(
            entries,
            max_files=max_state_files,
            max_file_bytes=max_file_bytes,
            max_total_bytes=max_total_bytes,
        )
        state = conversation_dir / "state"
        staging = conversation_dir / ".state.staging"
        _reset_dir(staging)
        try:
            if state.is_dir():
                _copy_state_base(state, staging)
            total = 0
            base_used = False
            for relative, entry in entries.items():
                target = staging / relative
                base = entry.get("base")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if isinstance(base, int):
                    base_used = True
                    _concatenate_delta(staging, extracted, relative, base, entry)
                else:
                    _write_member(extracted, "files", relative, entry, target)
                os.chmod(target, 0o600)
                total += entry["size"]
            _drop_unlisted(staging, entries)
            _verify_unchanged(staging, entries)
            mode = "incremental" if base_used else "full"
            _write_manifest(
                staging,
                entries=entries,
                source_run_id=source_run_id,
                total=total,
                mode=mode,
            )
            _swap_published(state, staging)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    finally:
        shutil.rmtree(extracted, ignore_errors=True)
    return {
        "files": len(entries),
        "bytes": total,
        "mode": mode,
        "sourceRunId": source_run_id,
    }


def _extract_archive(
    archive_path: Path, *, max_members: int
) -> tuple[dict[str, Any], Path]:
    """Open one staged tar into a scratch tree, enforcing the container rules.

    Returns ``(manifest, scratch_dir)`` where the scratch tree holds exactly
    the whitelisted members at their container-relative paths. Anything the
    whitelist or the tar ``data`` filter refuses fails the whole archive.
    """
    scratch = archive_path.parent / "extracted"
    _reset_dir(scratch)
    try:
        with tarfile.open(archive_path, mode="r:") as tar:
            members: list[tarfile.TarInfo] = []
            while True:
                member = tar.next()
                if member is None:
                    break
                if len(members) >= max_members:
                    raise ArchiveError(f"归档成员数超过上限 {max_members}")
                if member.isdir():
                    # Directory records (``./``, ``./files`` …) carry no bytes;
                    # the file members below decide the tree.
                    continue
                if not _valid_member_name(member.name):
                    raise ArchiveError(f"归档含非法成员名，已拒绝: {member.name[:200]!r}")
                if not member.isfile():
                    # Devices, fifos, symlinks: never extracted (data filter
                    # refuses them too — this is the explicit first check).
                    raise ArchiveError(
                        f"归档含非常规文件成员，已拒绝: {member.name[:200]!r}"
                    )
                members.append(member)
            manifest: dict[str, Any] | None = None
            for member in members:
                normalized = member.name
                while normalized.startswith("./"):
                    normalized = normalized[2:]
                if normalized == MANIFEST_MEMBER:
                    if manifest is not None:
                        raise ArchiveError("归档含多个 manifest 成员")
                    manifest = _read_member_json(tar, member)
                    continue
                tar.extract(member, scratch, filter="data")
            if manifest is None:
                raise ArchiveError("归档缺少 manifest.json")
        return manifest, scratch
    except tarfile.TarError as exc:
        raise ArchiveError(f"归档不是合法 tar: {type(exc).__name__}: {exc}") from exc


def _valid_member_name(name: str) -> bool:
    """A container member's name: the manifest itself, or a scoped relative.

    ``tar -C <dir> .`` prefixes every member with ``./`` on both GNU tar and
    bsdtar, so the leading form is normalized away before the whitelist.
    """
    while name.startswith("./"):
        name = name[2:]
    if name == MANIFEST_MEMBER:
        return True
    for prefix in (_FILES_PREFIX, _DELTA_PREFIX):
        if name.startswith(prefix):
            return _valid_relative(name[len(prefix):])
    return False


def _manifest_entries(
    manifest: dict[str, Any], credential_dests: tuple[str, ...]
) -> dict[str, dict[str, Any]]:
    """Validate the manifest into ``{relative: entry}``; reject credentials."""
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ArchiveError("归档 manifest 缺少 files 映射")
    blocked = set(credential_dests)
    entries: dict[str, dict[str, Any]] = {}
    for relative, entry in files.items():
        if not isinstance(relative, str) or not _valid_relative(relative):
            raise ArchiveError(f"归档 manifest 含非法路径，已拒绝: {relative[:200]!r}")
        if relative in blocked:
            raise ArchiveError(
                "归档包含本 run 的凭据目标路径，已整次拒绝"
                f"（{relative[:120]!r}）"
            )
        if not isinstance(entry, dict):
            raise ArchiveError(f"归档 manifest 条目形状非法: {relative[:200]!r}")
        size = entry.get("size")
        digest = entry.get("sha256")
        if not isinstance(size, int) or size < 0:
            raise ArchiveError(f"归档 manifest 条目 size 非法: {relative[:200]!r}")
        if not isinstance(digest, str) or not _SHA256_RE.match(digest):
            raise ArchiveError(f"归档 manifest 条目 sha256 非法: {relative[:200]!r}")
        base = entry.get("base")
        if base is not None and (not isinstance(base, int) or base < 0 or base > size):
            raise ArchiveError(f"归档 manifest 条目 base 非法: {relative[:200]!r}")
        entries[relative] = entry
    return entries


def _check_state_budget(
    entries: dict[str, dict[str, Any]],
    *,
    max_files: int,
    max_file_bytes: int,
    max_total_bytes: int,
) -> None:
    """Refuse a manifest whose complete state exceeds the state budget.

    Checked before anything is rebuilt: the manifest's sizes are the whole
    files (delta or not), and every member is later verified against them, so
    a manifest cannot understate what the published tree will hold.
    """
    if len(entries) > max_files:
        raise ArchiveError(
            f"归档描述的状态文件数 {len(entries)} 超过上限 {max_files}；未发布任何状态"
        )
    total = 0
    for relative, entry in entries.items():
        size = entry["size"]
        if size > max_file_bytes:
            raise ArchiveError(
                f"归档状态文件超过单文件上限 {max_file_bytes} 字节: {relative[:200]!r}；未发布任何状态"
            )
        total += size
    if total > max_total_bytes:
        raise ArchiveError(
            f"归档描述的状态总量 {total} 字节超过总量上限 {max_total_bytes}；未发布任何状态"
        )


def _valid_relative(relative: str) -> bool:
    if not relative or "\\" in relative or "\x00" in relative:
        return False
    parts = relative.split("/")
    return all(part not in ("", ".", "..") for part in parts)


#: Chunk size of the streamed member copy (bounded host memory per file).
_COPY_CHUNK = 1 << 20


def _write_member(
    extracted: Path, family: str, relative: str, entry: dict[str, Any], target: Path
) -> None:
    """Stream one full member into ``target``, checked against its manifest entry.

    Copied in bounded chunks with a running digest: a large state file never
    has to be held in host memory as a whole.
    """
    source = extracted / family / relative
    digest = hashlib.sha256()
    try:
        with source.open("rb") as reader, target.open("wb") as writer:
            written = _stream(reader, writer, digest)
    except OSError as exc:
        raise ArchiveError(f"归档成员读取失败: {relative[:200]!r}") from exc
    _check_entry(written, digest, entry, relative)


def _concatenate_delta(
    staging: Path, extracted: Path, relative: str, base: int, entry: dict[str, Any]
) -> None:
    """Apply one delta entry onto the staged (previous) prefix, in place.

    ``base == size`` marks an unchanged file (the packer anchors it, sends no
    member): the staged copy is verified against the manifest instead. Any
    other base appends the ``delta/<rel>`` member's bytes onto the prefix and
    verifies the whole result — a misaligned base can only produce a hash
    mismatch, which fails the whole archive. The prefix is hashed and the
    tail appended in bounded chunks (no whole-file ``bytes`` in memory).
    """
    target = staging / relative
    digest = hashlib.sha256()
    try:
        if base > 0:
            with target.open("rb") as prefix:
                prefix_length = _stream(prefix, None, digest)
        else:
            prefix_length = 0
            target.write_bytes(b"")
    except OSError as exc:
        raise ArchiveError(
            f"增量基线文件缺失或不可读，无法拼接: {relative[:200]!r}"
        ) from exc
    if prefix_length != base:
        raise ArchiveError(
            f"增量基线长度不符（宿主 {prefix_length} != base {base}），"
            f"拒绝拼接: {relative[:200]!r}"
        )
    written = prefix_length
    if base != entry["size"]:
        tail_path = extracted / "delta" / relative
        try:
            with tail_path.open("rb") as tail, target.open("ab") as writer:
                written += _stream(tail, writer, digest)
        except OSError as exc:
            raise ArchiveError(
                f"增量成员缺失或不可读: {relative[:200]!r}"
            ) from exc
    _check_entry(written, digest, entry, relative)


def _stream(reader, writer, digest) -> int:
    """Copy ``reader`` into ``writer`` (``None`` = hash only); returns the byte count."""
    count = 0
    while True:
        chunk = reader.read(_COPY_CHUNK)
        if not chunk:
            return count
        digest.update(chunk)
        if writer is not None:
            writer.write(chunk)
        count += len(chunk)


def _check_entry(size: int, digest, entry: dict[str, Any], relative: str) -> None:
    if size != entry["size"]:
        raise ArchiveError(
            f"归档成员长度与 manifest 不符: {relative[:200]!r}"
            f"（{size} != {entry['size']}）"
        )
    if digest.hexdigest() != entry["sha256"]:
        raise ArchiveError(f"归档成员 sha256 校验失败: {relative[:200]!r}")


def _copy_state_base(state: Path, staging: Path) -> None:
    """Copy the published tree into staging (the unchanged files' base)."""

    def copy(source: Path, target: Path) -> None:
        for child in sorted(source.iterdir()):
            child_target = target / child.name
            if child.is_dir() and not child.is_symlink():
                child_target.mkdir(mode=0o700)
                copy(child, child_target)
                continue
            if child.is_symlink() or not child.is_file():
                continue
            shutil.copyfile(child, child_target)
            os.chmod(child_target, 0o600)

    copy(state, staging)


def _drop_unlisted(staging: Path, entries: dict[str, dict[str, Any]]) -> None:
    """Remove staged files the manifest does not list (deleted in the sandbox)."""
    for relative in _walk_files(staging):
        if relative == MANIFEST_MEMBER:
            continue
        if relative not in entries:
            (staging / relative).unlink()


def _verify_unchanged(staging: Path, entries: dict[str, dict[str, Any]]) -> None:
    """Every manifest entry must now be present and matching in staging."""
    seen = set(_walk_files(staging))
    for relative in entries:
        if relative in seen:
            continue
        raise ArchiveError(f"发布前校验失败：状态文件缺失: {relative[:200]!r}")
        # size/sha of replaced files were checked on write; unchanged files
        # keep the base copy which the next manifest round re-anchors on.


def _write_manifest(
    staging: Path,
    *,
    entries: dict[str, dict[str, Any]],
    source_run_id: str,
    total: int,
    mode: str,
) -> None:
    """Write the durable manifest of the tree being published."""
    published: dict[str, Any] = {
        "version": 1,
        "sourceRunId": source_run_id,
        "mode": mode,
        "bytes": total,
        "files": {
            relative: {
                "size": entry["size"],
                "sha256": entry["sha256"],
            }
            for relative, entry in entries.items()
        },
    }
    path = staging / MANIFEST_MEMBER
    path.write_text(
        json.dumps(published, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    os.chmod(path, 0o600)


def _heal_swap_window(conversation_dir: Path) -> None:
    """Restore a published state lost in the swap crash window (cc 终审 S4).

    ``_swap_published`` moves the old tree aside before renaming the staged
    one in; a crash between the two renames leaves ``state/`` missing with
    ``.state.retired/`` holding the last good tree — without this heal the
    conversation looks unpublished and every follow-up is refused. The retired
    tree is moved back (still the old good manifest, still fail-closed on
    freshness); a stray ``staging`` remnant from the same crash is removed —
    it was never published.
    """
    state = Path(conversation_dir) / "state"
    retired = state.parent / ".state.retired"
    staging = state.parent / ".state.staging"
    if not state.exists() and retired.is_dir():
        try:
            os.rename(retired, state)
        except OSError:
            return
        # Only inside the crash window is a stray staging remnant garbage —
        # outside it the staging tree may be the one a publish is holding.
        shutil.rmtree(staging, ignore_errors=True)


def _swap_published(state: Path, staging: Path) -> None:
    """Publish the staged tree with per-directory renames.

    The old tree (if any) is moved aside first and removed after the swap;
    a crash inside that window leaves either the old or no published state —
    never a partial tree — and the follow-up gate's freshness check (manifest
    ``sourceRunId``) keeps a stale manifest unusable.
    """
    _heal_swap_window(state.parent)
    retired = state.parent / ".state.retired"
    shutil.rmtree(retired, ignore_errors=True)
    if state.exists():
        os.rename(state, retired)
    os.rename(staging, state)
    shutil.rmtree(retired, ignore_errors=True)


def _walk_files(root: Path) -> set[str]:
    """Every regular file below ``root``, as forward-slash relatives."""
    relatives: set[str] = set()
    if not root.is_dir():
        return relatives
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        relatives.add(path.relative_to(root).as_posix())
    return relatives


def _walk_state(state: Path, skip: set[str]) -> list[tuple[str, Path]]:
    """The served backfill entries: sorted, skipped, regular files only."""
    entries: list[tuple[str, Path]] = []
    if not state.is_dir():
        return entries
    for relative in sorted(_walk_files(state)):
        if relative in skip:
            continue
        entries.append((relative, state / relative))
    return entries


def _reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, mode=0o700)


def _copy_bounded(body, out, length: int) -> int:
    """Stream exactly ``length`` bytes from ``body`` into ``out``."""
    received = 0
    try:
        while received < length:
            chunk = body.read(min(_CHUNK, length - received))
            if not chunk:
                break
            out.write(chunk)
            received += len(chunk)
    finally:
        out.close()
    return received


def _read_member_json(tar: tarfile.TarFile, member: tarfile.TarInfo) -> dict[str, Any]:
    handle = tar.extractfile(member)
    if handle is None:  # pragma: no cover - isfile() was checked
        raise ArchiveError("manifest 成员不可读")
    try:
        document = json.loads(handle.read())
    except (ValueError, OSError) as exc:
        raise ArchiveError(f"manifest 不是合法 JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ArchiveError("manifest 不是 JSON 对象")
    return document


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
