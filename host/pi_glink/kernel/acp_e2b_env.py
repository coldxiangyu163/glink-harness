"""The ACP harness's file transfer for the E2B face (:class:`AcpSandboxTransfer`).

**The ACP face shares the sandbox (M3b).** The same
:class:`~pi_glink.kernel.sandbox.E2BSandbox` resource (creation, lifetime,
workspace recovery, identity, ``kill()``) carries both families: the agent runs
as a *background* command with ``stdin=True`` and the host frames the
CommandHandle's stdout with the shared LF-only splitter, so the ACP client,
projection and verdict table are the same code as the in-process and Docker
faces. :class:`~pi_glink.kernel.e2b_env.SandboxCommandProcess` adapts that
handle to the ``subprocess.Popen`` shape ``AcpRpcClient`` is written against;
this module only adds what is ACP-specific — uploading the run's state root,
the continuation's workspace half, and recovering the state root; the measured framing facts
(ordering, byte fidelity, ≥64 KiB frames, ``close_stdin``) are in
``docs/evidence/acp-m3-e2b-framing.txt``.

**Credential and state boundary.** The run's host-rendered ACP state root
(:func:`~pi_glink.kernel.acp_env.prepare_acp_runtime`) is uploaded into the
sandbox with 0600 applied there, so the M2/M3a boundary (per-run HOME,
file-shaped credentials, no value in argv or environment) holds on this face
too. Before the sandbox is destroyed, its own state root is mirrored back to
the host (bounded), which is what makes a follow-up's session inheritance
possible.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..artifacts import ArtifactError, ArtifactTree
from .acp_env import (
    ACP_RECOVERED_DIRNAME,
    INHERIT_MAX_FILE_BYTES,
    INHERIT_MAX_FILES,
    INHERIT_MAX_TOTAL_BYTES,
    AcpRuntime,
)
from .archive_channel import (
    ArchiveEndpoint,
    incremental_base,
    read_state_manifest,
)
from ..redaction import credential_secrets_from_text
from .e2b_env import (
    REPORT_FILENAME,
    SANDBOX_HOME,
    SANDBOX_WORKSPACE,
    UPLOAD_MAX_BYTES,
    SandboxError,
    _entry_kind,
    _read_file_bounded,
    _runtime_manifest,
)

logger = logging.getLogger("pi_glink.kernel.acp_e2b_env")

#: The ACP family's per-run state root inside the sandbox. The host renders the
#: run's own HOME (``runs/<id>/acp-agent/home``: credentials injected 0600) and
#: uploads it here; the agent process gets ``HOME`` pointed at it, so ``~/.dsh``,
#: ``~/.codex`` and every cache stay inside this run's sandbox.
SANDBOX_ACP_HOME = f"{SANDBOX_HOME}/acp/home"
#: How deep the sandbox's ACP state root is walked when it is recovered to the
#: host. The measured state roots (dsh / codex / opencode) are a handful
#: of levels deep; a deeper tree is refused instead of walked without a bound.
ACP_STATE_MAX_DEPTH = 12

#: Where the reclaim packer script, its base manifest and the archive token
#: land inside the sandbox (short-lived scratch, never below the state root).
RECLAIM_SCRIPT_PATH = "/tmp/pi-glink-reclaim.mjs"
RECLAIM_BASE_PATH = "/tmp/pi-glink-reclaim-base.json"
RECLAIM_OUT_PATH = "/tmp/pi-glink-reclaim-out"
RECLAIM_TAR_PATH = "/tmp/pi-glink-archive.tar"
RECLAIM_TOKEN_PATH = "/tmp/pi-glink-archive-token"
#: Remove the run-scoped archive **token** right after a transfer (cc 终审
#: S2): the grant must not outlive its command — a later reader (or a parked
#: sandbox) finding it could mint state posts for this conversation. The
#: transport's other scratch files carry no grant and stay until the park-time
#: sweep below, so a transfer's own artifacts remain inspectable.
RECLAIM_TOKEN_CLEANUP_COMMAND = "rm -f /tmp/pi-glink-archive-token"
#: The park-time sweep (before pause): clears every scratch file the archive
#: paths ever left — nothing run-scoped may survive into a parked sandbox.
RECLAIM_CLEANUP_COMMAND = "rm -f /tmp/pi-glink-archive-token /tmp/pi-glink-reclaim.mjs /tmp/pi-glink-reclaim-base.json /tmp/pi-glink-reclaim-base.json.tmp /tmp/pi-glink-reclaim-out /tmp/pi-glink-archive.tar /tmp/pi-glink-archive.tar.zst"
BACKFILL_TAR_PATH = "/tmp/pi-glink-backfill.tar"
#: One reclaim/backfill command's ceiling (the curl inside gets slightly less,
#: so the command — not the transport — owns the deadline).
ARCHIVE_COMMAND_TIMEOUT_SECONDS = 600
ARCHIVE_CURL_MAX_TIME = ARCHIVE_COMMAND_TIMEOUT_SECONDS - 30

#: The sandbox-side reclaim packer, delivered by the host before every reclaim
#: (docs/adr/0018). The template image needs nothing new: node is already the
#: ACP agent's runtime and ``tar``/``curl`` ship with the base image. The packer
#: walks the state root, anchors every file against the host-provided base
#: manifest (path + size + prefix sha256 — V1's guard rails) and emits the
#: archive container: ``manifest.json`` plus ``files/<rel>`` full members and
#: ``delta/<rel>`` tail members. Credential destinations are excluded by the
#: caller's arguments; symlinks and other non-regular entries are never
#: followed.
RECLAIM_SCRIPT = """\
#!/usr/bin/env node
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const [stateRoot, baseJsonPath, outDir, ...excludes] = process.argv.slice(2);
if (!stateRoot || !baseJsonPath || !outDir) {
  console.error("usage: reclaim.mjs <state_root> <base_json> <out_dir> [exclude...]");
  process.exit(64);
}
const excluded = new Set(excludes);
let baseFiles = {};
try {
  baseFiles = JSON.parse(readFileSync(baseJsonPath, "utf8")).files ?? {};
} catch (error) {
  if (error.code !== "ENOENT") throw error;
}
const sha256 = (buffer) => createHash("sha256").update(buffer).digest("hex");
const segments = (relative) => relative.split("/");

function walk(directory, prefix, found) {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (excluded.has(relative)) continue;
    const path = join(directory, entry.name);
    if (entry.isDirectory()) walk(path, relative, found);
    else if (entry.isFile()) found.push(relative);
  }
  return found;
}

let relatives = [];
try {
  relatives = walk(stateRoot, "", []);
} catch (error) {
  if (error.code !== "ENOENT") throw error;
}
rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });
const files = {};
for (const relative of relatives) {
  const data = readFileSync(join(stateRoot, ...segments(relative)));
  const digest = sha256(data);
  const previous = baseFiles[relative];
  const write = (family, bytes) => {
    const target = join(outDir, family, ...segments(relative));
    mkdirSync(join(target, ".."), { recursive: true });
    writeFileSync(target, bytes);
  };
  if (
    previous &&
    Number.isInteger(previous.size) &&
    previous.size > 0 &&
    previous.size <= data.length &&
    sha256(data.subarray(0, previous.size)) === previous.sha256
  ) {
    if (previous.size === data.length && previous.sha256 === digest) {
      // 未变化：锚定它但不传任何成员（宿主端 base == size 即「沿用已发布副本」）。
      files[relative] = { size: data.length, sha256: digest, base: previous.size };
      continue;
    }
    write("delta", data.subarray(previous.size));
    files[relative] = { size: data.length, sha256: digest, base: previous.size };
  } else {
    write("files", data);
    files[relative] = { size: data.length, sha256: digest };
  }
}
writeFileSync(join(outDir, "manifest.json"), JSON.stringify({ version: 1, files }));
"""


@dataclass(frozen=True, slots=True)
class AcpStateRecovery:
    """What one bounded recovery of a sandbox's ACP state root achieved.

    ``ok`` is the whole-or-nothing verdict: a recovery that could not read or
    mirror every file (a bound violation, an unrepresentable path, a sandbox
    read error) is reported failed and leaves **no** published state for this
    run — a partial state root would present the next turn a conversation the
    agent does not actually have. ``files``/``bytes`` are the mirror's facts for
    the run record; ``secrets`` are the credential-shaped values discovered in
    the recovered text, which join this run's redaction boundary (the state
    files themselves are never rewritten — rewriting the agent's own session
    store would corrupt the session a resume is meant to continue; see
    :func:`~pi_glink.kernel.acp_env.inherit_acp_state`, which scans the same
    root again when a follow-up inherits it).

    ``mode`` says which path produced the recovery — the archive channel
    (``archive-incremental`` / ``archive-full``, docs/adr/0018) or the bounded
    files-API fallback (``files-fallback``, the pre-channel capability kept for
    a face the channel cannot serve) — and ``duration_ms`` how long it took;
    both go into the run's event stream (阶段 1 的观测义务).
    """

    ok: bool
    files: tuple[str, ...] = ()
    bytes: int = 0
    secrets: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    mode: str = ""
    duration_ms: float = 0.0


class AcpSandboxTransfer:
    """ACP 族在一个**已打开**沙箱上的状态交付与回收（harness 侧，不拥有沙箱）。

    沙箱的创建、命令、身份与销毁属于
    :class:`~pi_glink.kernel.sandbox.E2BSandbox`；本类只知道 ACP 的布局：
    ``runtime`` 是 run 的宿主渲染 ACP 状态根
    (:func:`~pi_glink.kernel.acp_env.prepare_acp_runtime`)，其中的凭据文件交到
    :data:`SANDBOX_ACP_HOME` 并在沙箱内收紧为 0600，使 M2/M3a 边界（per-run
    HOME、文件形凭据、值不进 argv 或环境）在本面同样成立。

    阶段 1（docs/adr/0018）起，宿主持有对话级权威状态，本类是三件事的
    承载者：凭据交付（:meth:`upload_home`，不变）、追问回灌（:meth:`backfill_state`，
    沙箱端经归档通道 ``GET /archive/state`` 拉取对话状态根——通道可用时追问
    run 的 HOME 只含凭据，状态不再经 files API 逐文件灌入）、以及沙箱销毁前的
    回收（:meth:`recover_state`，优先归档通道按 offset 增量，失败或无通道时
    降级 files 逐文件=现状能力）。回收发布到对话状态根
    （``<data_root>/conversations/<根run>/state/``），整份成功才可见。
    """

    def __init__(
        self,
        handle: Any,
        runtime: AcpRuntime,
        *,
        workspace: Path,
        archive: ArchiveEndpoint | None = None,
        conversation_dir: Path | None = None,
        run_id: str = "",
    ) -> None:
        self.handle = handle
        self.runtime = runtime
        self.workspace = Path(workspace)
        #: The archive channel as the sandbox reaches it (``None`` = the face
        #: degrades to the files-API paths for both directions).
        self.archive = archive
        #: The conversation's durable state directory on the host (``None``
        #: together with ``archive`` on a face that has no channel).
        self.conversation_dir = Path(conversation_dir) if conversation_dir else None
        #: This run's id (the manifest's ``sourceRunId`` freshness anchor).
        self.run_id = run_id
        #: Sandbox paths the credential upload wrote.
        self.uploaded: list[str] = []
        #: ``(relative path, sha256)`` for everything delivered to the sandbox.
        self.upload_manifest: list[tuple[str, str]] = []
        #: Backfill facts (``{files, bytes, durationMs}``) once a backfill ran.
        self.backfill_facts: dict[str, Any] | None = None

    def upload_home(self) -> None:
        """Deliver the run's rendered ACP HOME (credential files) to the sandbox.

        The tree keeps its relative paths under :data:`SANDBOX_ACP_HOME`. The
        boundary is applied in the order the transport allows: the state root is
        made 0700 *before* any file is written (so nothing lands in a
        traversable directory), and after the upload every created directory is
        0700 and every file 0600 — the upload API carries no mode, so this is
        the closest faithful application of the M2/M3a per-run HOME boundary on
        this face. The ``(relative, sha256)`` manifest makes delivery
        verifiable instead of assumed. A run with no credential files uploads
        nothing.

        A follow-up's HOME is not just credentials **on a face without the
        archive channel**: there the runner inherited the parent run's state
        into it and this upload is also the "灌回" step. With the channel
        (docs/adr/0018) the state travels sandbox-side first
        (:meth:`backfill_state`) and this HOME is credentials only. Either
        way the upload keeps the first turn's hardened order, and a failed
        upload fails the run (the caller never starts the agent against a
        partial state root).
        """
        sandbox = self.handle
        sandbox.files.make_dir(SANDBOX_ACP_HOME)
        self._chmod(0o700, [SANDBOX_ACP_HOME], "先收紧状态根权限")
        manifest = _runtime_manifest(self.runtime.home)
        if not manifest:
            return
        try:
            tree = ArtifactTree(self.runtime.home)
        except ArtifactError as exc:
            raise SandboxError(f"ACP 凭据交付失败: {exc}") from exc
        with tree:
            for relative, is_dir in manifest:
                sandbox_path = f"{SANDBOX_ACP_HOME}/{relative}"
                if is_dir:
                    sandbox.files.make_dir(sandbox_path)
                    continue
                try:
                    data = tree.read(relative, max_bytes=UPLOAD_MAX_BYTES)
                except ArtifactError as exc:
                    raise SandboxError(f"ACP 凭据交付失败: {relative}: {exc}") from exc
                sandbox.files.write(sandbox_path, data)
                self.uploaded.append(sandbox_path)
                self.upload_manifest.append((relative, hashlib.sha256(data).hexdigest()))
        self._harden_uploaded(manifest)

    def _harden_uploaded(self, manifest: list[tuple[str, bool]]) -> None:
        """Re-apply the per-run boundary in the sandbox: 0700 dirs, 0600 files.

        ``chmod`` is one sandbox command over the paths this run just uploaded
        (never a wildcard, never another run's tree); the credential set is a
        handful of files, so there is no argv-length concern.
        """
        directories = [
            f"{SANDBOX_ACP_HOME}/{relative}" for relative, is_dir in manifest if is_dir
        ]
        self._chmod(0o700, [SANDBOX_ACP_HOME, *directories], "收紧沙箱状态根权限")
        self._chmod(0o600, list(self.uploaded), "收紧沙箱凭据文件权限")

    def _chmod(self, mode: int, paths: list[str], what: str) -> None:
        """Run one ``chmod`` inside the sandbox over exactly ``paths``."""
        if not paths:
            return
        sandbox = self.handle
        command = shlex.join(["chmod", f"{mode:04o}", *paths])
        try:
            sandbox.commands.run(command, timeout=60)
        except Exception as exc:  # noqa: BLE001 - surfaced as a delivery failure
            raise SandboxError(f"{what}失败: {type(exc).__name__}: {exc}") from exc

    def upload_continuation(self) -> None:
        """Deliver the conversation's prior deliverable into the new sandbox.

        The pi face transfers the sanitized session snapshot plus the prior
        ``report.md`` (:meth:`~pi_glink.kernel.e2b_env.PiSandboxTransfer.upload_continuation`); the ACP
        session itself travels through the inherited run HOME, and this is the
        workspace half: a resumed agent remembers the conversation's workspace
        (M7), so the deliverable it produced there is restored rather than left
        as an empty directory the conversation never wrote. Only
        ``report.md`` is transferred — the product contract's file — read
        through :class:`~pi_glink.artifacts.ArtifactTree` and bounded; a run
        with no prior report (or an unreadable one) simply uploads nothing.
        """
        sandbox = self.handle
        try:
            with ArtifactTree(self.workspace) as tree:
                report = tree.read(REPORT_FILENAME, max_bytes=UPLOAD_MAX_BYTES)
        except ArtifactError:
            return
        sandbox.files.write(f"{SANDBOX_WORKSPACE}/{REPORT_FILENAME}", report)

    def backfill_state(self) -> None:
        """Pull the conversation's authoritative state into the new sandbox.

        The cold path's 灌回 step on the archive channel (docs/adr/0018): the
        sandbox curls ``GET /archive/state`` — the server derives the source
        directory from the grant, never from the client — and untars the stream
        below :data:`SANDBOX_ACP_HOME`. The tar carries no credential
        destinations (they are skipped server-side) and no manifest, so the
        follow-up's own credential upload (:meth:`upload_home`, which runs
        after this) is the only writer of those paths. A failed backfill fails
        the run: the follow-up was admitted against this state, and resuming
        against an empty root would present a session the agent does not have.
        """
        if self.archive is None or self.conversation_dir is None:
            return
        started = time.monotonic()
        sandbox = self.handle
        try:
            sandbox.files.write(RECLAIM_TOKEN_PATH, self.archive.token)
            sandbox.files.make_dir(SANDBOX_ACP_HOME)
            sandbox.commands.run(
                _backfill_command(self.archive.url),
                timeout=ARCHIVE_COMMAND_TIMEOUT_SECONDS,
            )
            # The grant must not outlive the transfer (cc 终审 S2).
            try:
                sandbox.commands.run(RECLAIM_TOKEN_CLEANUP_COMMAND, timeout=30)
            except Exception:  # noqa: BLE001 - cleanup never fails the backfill
                logger.warning("回灌后清理沙箱 scratch 失败（忽略）", exc_info=True)
        except Exception as exc:  # noqa: BLE001 - surfaced as a start failure
            raise SandboxError(
                f"追问状态回灌（归档通道）失败: {type(exc).__name__}: {exc}"
            ) from exc
        manifest = read_state_manifest(self.conversation_dir)
        self.backfill_facts = {
            "files": len(manifest.get("files", {})) if manifest else 0,
            "bytes": int(manifest.get("bytes") or 0) if manifest else 0,
            "durationMs": round((time.monotonic() - started) * 1000, 1),
        }

    def recover_state(self) -> AcpStateRecovery:
        """Mirror the sandbox's ACP state root into the conversation state.

        The sandbox destroys the agent's state (its sessions, credentials,
        caches) with it, so the host takes its own copy **before** ``close()``
        kills the sandbox: without it a follow-up would be admitted into a
        sandbox that can never find the session it is told to resume.

        阶段 1（docs/adr/0018）起有两条路，按序尝试：

        * **归档通道**（:meth:`_recover_via_archive`）：沙箱端打包器按宿主
          下发的 base manifest 做 offset 增量（dsh 会话文件是追加式 zstd 帧
          拼接，前缀哈希锚定不变即传输 ``[base, size)`` 尾部），整份 tar 流
          POST 到宿主的 ``/archive/state``，宿主端校验拼接后替换式发布到
          对话状态根。O(增量)，多轮长会话不再线性放大回收成本。
        * **files 逐文件降级**（:meth:`_recover_via_files`）：通道不可用
          （无归档端点）或通道失败时走的能力=现状（有界逐文件镜像），发布
          目标同样是对话状态根。降级事实进 ``mode``，供事件流观测。

        两条路的落点都是 ``<conversation>/state/``，整份成功才发布；发布
        manifest 的 ``sourceRunId`` 记录本次 run，追问门禁以它校验新鲜度，
        所以一次失败的回收不会留下一个看起来成功的事实（与 ADR 0005 的
        ``recovered`` 语义相同，只是位置从每 run 目录移到对话根）。
        """
        if self.archive is not None and self.conversation_dir is not None:
            recovery = self._recover_via_archive()
            if recovery is not None:
                return recovery
        return self._recover_via_files()

    def _recover_via_archive(self) -> AcpStateRecovery | None:
        """Reclaim through the archive channel; ``None`` = degrade to files.

        The host delivers the packer script, the incremental base (the last
        published manifest) and the run's archive token into the sandbox, then
        runs one command that packs, tars and POSTs the container. The host side
        of the channel receives, verifies and publishes inside the POST, so the
        command's success plus a manifest whose ``sourceRunId`` is this run is
        the durable "回收成功" fact. Any failure answers ``None`` and the caller
        degrades to the bounded files walk (the pre-channel capability).
        """
        started = time.monotonic()
        sandbox = self.handle
        archive = self.archive
        conversation = self.conversation_dir
        assert archive is not None and conversation is not None
        excludes = tuple(self.runtime.credential_dests)
        base = incremental_base(conversation)
        try:
            sandbox.files.write(RECLAIM_SCRIPT_PATH, RECLAIM_SCRIPT)
            sandbox.files.write(
                RECLAIM_BASE_PATH,
                json.dumps({"version": 1, "files": base}, sort_keys=True),
            )
            sandbox.files.write(RECLAIM_TOKEN_PATH, archive.token)
            command = _reclaim_command(archive.url, excludes=excludes)
            sandbox.commands.run(
                command, timeout=ARCHIVE_COMMAND_TIMEOUT_SECONDS
            )
            try:
                sandbox.commands.run(RECLAIM_TOKEN_CLEANUP_COMMAND, timeout=30)
            except Exception:  # noqa: BLE001 - cleanup never fails the reclaim
                logger.warning("回收后清理沙箱 token 失败（忽略）", exc_info=True)
        except Exception as exc:  # noqa: BLE001 - degrade, the failure is a fact
            logger.warning(
                "归档通道回收失败，降级 files 逐文件 run=%s: %s: %s",
                self.run_id,
                type(exc).__name__,
                exc,
            )
            return None
        manifest = read_state_manifest(conversation)
        if manifest is None or manifest.get("sourceRunId") != self.run_id:
            logger.warning(
                "归档通道回收后未见本 run 的发布 manifest，降级 files 逐文件 run=%s",
                self.run_id,
            )
            return None
        mode = "archive-full" if manifest.get("mode") == "full" else "archive-incremental"
        files = tuple(sorted(str(key) for key in manifest.get("files", {})))
        total = int(manifest.get("bytes") or 0)
        secrets = _scan_published_secrets(conversation, manifest, base, excludes)
        return AcpStateRecovery(
            ok=True,
            files=files,
            bytes=total,
            secrets=secrets,
            mode=mode,
            duration_ms=round((time.monotonic() - started) * 1000, 1),
        )

    def _recover_via_files(self) -> AcpStateRecovery:
        """The bounded files-API walk (the pre-channel recovery capability).

        Same discipline as before 阶段 1: the follow-up inheritance bounds
        (file count, one file, total bytes) refuse the whole recovery instead
        of truncating it; entries are read through :func:`_read_file_bounded`,
        only regular files are mirrored (symlinks are skipped, never followed)
        and every relative path is validated before it is written through
        :class:`~pi_glink.artifacts.ArtifactTree`. The mirror publishes to the
        conversation state root with a ``files-fallback`` manifest; a face
        without a conversation directory (none today) keeps the legacy
        ``acp-agent/recovered`` layout.
        """
        sandbox = self.handle
        max_files = INHERIT_MAX_FILES
        # A credential copy never enters the conversation state: the state is
        # durable across turns (nothing re-renders or sweeps it per run), so
        # the pre-阶段 1 behaviour of mirroring it into a per-run ``recovered``
        # root cannot carry over (docs/adr/0018).
        blocked = set(self.runtime.credential_dests)
        try:
            entries = sandbox.files.list(SANDBOX_ACP_HOME, depth=ACP_STATE_MAX_DEPTH)
        except Exception as exc:  # noqa: BLE001 - a failed recovery refuses the follow-up
            return AcpStateRecovery(
                ok=False,
                failures=(f"无法列出沙箱 ACP 状态根: {type(exc).__name__}: {exc}",),
            )
        collected: dict[str, bytes] = {}
        total = 0
        for entry in entries:
            path = str(getattr(entry, "path", None) or "")
            if path.rstrip("/") == SANDBOX_ACP_HOME:
                # A recursive listing that includes the root itself describes
                # nothing to mirror (robustness against SDK payload shapes).
                continue
            kind = _entry_kind(entry)
            if kind == "dir":
                continue
            if kind != "file":
                # Symlinks and non-regular entries are never followed or
                # imported (the same rule ArtifactTree and the pi face apply).
                continue
            relative = _state_relative(path)
            if relative is None:
                return AcpStateRecovery(
                    ok=False,
                    failures=(f"沙箱状态根含无法映射的路径，已拒绝回收: {path[:200]!r}",),
                )
            if relative in blocked:
                continue
            if len(collected) >= max_files:
                return AcpStateRecovery(
                    ok=False,
                    failures=(f"沙箱状态文件数超过上限 {max_files}",),
                )
            known = getattr(entry, "size", None)
            data, failure = _read_file_bounded(
                sandbox,
                path,
                INHERIT_MAX_FILE_BYTES,
                known_size=known if isinstance(known, int) else None,
            )
            if data is None:
                return AcpStateRecovery(
                    ok=False,
                    failures=(f"沙箱状态文件不可读取或超过上限: {relative}: {failure}",),
                )
            total += len(data)
            if total > INHERIT_MAX_TOTAL_BYTES:
                return AcpStateRecovery(
                    ok=False,
                    failures=(f"沙箱状态超过总量上限 {INHERIT_MAX_TOTAL_BYTES} 字节",),
                )
            collected[relative] = data
        started = time.monotonic()
        secrets: list[str] = []
        for relative in collected:
            data = collected[relative]
            if b"\x00" not in data[:1024]:
                secrets.extend(
                    credential_secrets_from_text(data.decode("utf-8", errors="replace"))
                )
        recovery = self._publish_recovered_state(collected, total, _stable(secrets))
        if recovery.ok:
            return dataclasses.replace(
                recovery,
                mode="files-fallback",
                duration_ms=round((time.monotonic() - started) * 1000, 1),
            )
        return recovery

    def _publish_recovered_state(
        self,
        collected: Mapping[str, bytes],
        total: int,
        secrets: tuple[str, ...],
    ) -> AcpStateRecovery:
        """Write the staged mirror and publish it with one rename.

        The mirror publishes to the conversation state root (with a
        ``files-fallback`` manifest naming this run) when the transfer knows
        one; without a conversation directory it falls back to the legacy
        ``acp-agent/recovered`` layout, whose existence remains the admission
        fact for runs recovered before 阶段 1. The staging directory is private
        to this run and this call; it is created 0700 and every file lands
        through :class:`ArtifactTree` (0600, parents 0700). Only a complete
        mirror is renamed into place, so a crash or write failure leaves no
        published root and the follow-up refusal holds.
        """
        if self.conversation_dir is not None:
            staging = self.conversation_dir / ".state.staging"
            manifest = {
                "version": 1,
                "sourceRunId": self.run_id,
                "mode": "files-fallback",
                "bytes": total,
                "files": {
                    relative: {
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                    for relative, data in collected.items()
                },
            }
            published = self.conversation_dir / "state"
        else:
            staging = self.runtime.directory / f".{ACP_RECOVERED_DIRNAME}.staging"
            published = self.runtime.directory / ACP_RECOVERED_DIRNAME
            manifest = None
        try:
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(mode=0o700)
            with ArtifactTree(staging) as tree:
                for relative in sorted(collected):
                    tree.replace(
                        relative,
                        collected[relative],
                        max_bytes=INHERIT_MAX_FILE_BYTES,
                        create_parents=True,
                    )
                if manifest is not None:
                    tree.replace(
                        "manifest.json",
                        json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode(
                            "utf-8"
                        ),
                        max_bytes=1 << 20,
                    )
            if published.exists():
                shutil.rmtree(published)
            os.rename(staging, published)
        except Exception as exc:  # noqa: BLE001 - a failed recovery refuses the follow-up
            shutil.rmtree(staging, ignore_errors=True)
            return AcpStateRecovery(
                ok=False,
                failures=(
                    f"沙箱 ACP 状态根写入宿主失败: {type(exc).__name__}: {exc}",
                ),
            )
        return AcpStateRecovery(
            ok=True, files=tuple(sorted(collected)), bytes=total, secrets=secrets
        )


def _reclaim_command(url: str, *, excludes: tuple[str, ...]) -> str:
    """The one sandbox command that packs, tars and POSTs the state root.

    Every argument is a fixed scratch path or a host-validated relative name,
    quoted with :func:`shlex.join`; the token never appears in the command
    text (the shell reads it from the delivered file), so no sandbox command
    log or ``ps`` view inside the sandbox carries the raw secret.
    """
    pack = shlex.join(
        [
            "node",
            RECLAIM_SCRIPT_PATH,
            SANDBOX_ACP_HOME,
            RECLAIM_BASE_PATH,
            RECLAIM_OUT_PATH,
            *excludes,
        ]
    )
    # COPYFILE_DISABLE keeps macOS bsdtar from emitting AppleDouble ``._``
    # metadata members into the container (the host's tarfile reader does not
    # hide them); GNU tar on the Linux sandbox ignores the variable.
    tar = (
        f"COPYFILE_DISABLE=1 tar -cf {shlex.quote(RECLAIM_TAR_PATH)} "
        f"-C {shlex.quote(RECLAIM_OUT_PATH)} ."
    )
    curl = (
        f'curl -sS -f --max-time {ARCHIVE_CURL_MAX_TIME} -X POST '
        f'-H "Authorization: Bearer $(cat {RECLAIM_TOKEN_PATH})" '
        f'-H "Content-Type: application/x-tar" '
        f"--data-binary @{RECLAIM_TAR_PATH} {shlex.quote(url)}"
    )
    return f"{pack} && {tar} && {curl}"


def _backfill_command(url: str) -> str:
    """The one sandbox command that GETs the state archive and untars it.

    ``tar -x`` below :data:`SANDBOX_ACP_HOME` with GNU/bsdtar's default
    handling already strips absolute paths and refuses ``..`` members; the
    served container only ever contains host-validated relative names anyway
    (the packer and the server both whitelist them).
    """
    curl = (
        f'curl -sS -f --max-time {ARCHIVE_CURL_MAX_TIME} '
        f'-H "Authorization: Bearer $(cat {RECLAIM_TOKEN_PATH})" '
        f"-o {BACKFILL_TAR_PATH} {shlex.quote(url)}"
    )
    untar = (
        f"COPYFILE_DISABLE=1 tar -xf {shlex.quote(BACKFILL_TAR_PATH)} "
        f"-C {shlex.quote(SANDBOX_ACP_HOME)}"
    )
    return f"mkdir -p {shlex.quote(SANDBOX_ACP_HOME)} && {curl} && {untar}"


def _scan_published_secrets(
    conversation_dir: Path,
    manifest: Mapping[str, Any],
    base: Mapping[str, Mapping[str, Any]],
    excludes: tuple[str, ...],
) -> tuple[str, ...]:
    """Learn credential-shaped values from the freshly published state.

    Only the files this recovery actually changed are scanned — a file whose
    size and sha256 still equal the previous published round's was scanned by
    the round that first carried it — under the same scan budget the
    inheritance path uses. The published bytes are never rewritten.
    """
    from .acp_env import INHERIT_SCAN_MAX_BYTES

    state = conversation_dir / "state"
    blocked = set(excludes)
    found: list[str] = []
    scanned = 0
    for relative, entry in manifest.get("files", {}).items():
        if relative in blocked or not isinstance(entry, Mapping):
            continue
        previous = base.get(relative)
        if (
            isinstance(previous, Mapping)
            and previous.get("size") == entry.get("size")
            and previous.get("sha256") == entry.get("sha256")
        ):
            continue
        if scanned >= INHERIT_SCAN_MAX_BYTES:
            break
        try:
            data = (state / str(relative)).read_bytes()
        except OSError:
            continue
        scanned += len(data)
        if b"\x00" not in data[:1024]:
            found.extend(
                credential_secrets_from_text(data.decode("utf-8", errors="replace"))
            )
    return _stable(found)


def _state_relative(path: str) -> str | None:
    """One sandbox state entry's path relative to the sandbox state root.

    Returns ``None`` for anything that cannot be mirrored faithfully (outside
    the root, an empty or traversing component, a separator the host path
    model would reinterpret): such an entry refuses the whole recovery rather
    than being silently dropped from the mirror. Sandbox content is untrusted
    input; the same path discipline is what keeps the mirror inside the run.
    """
    prefix = f"{SANDBOX_ACP_HOME}/"
    if not path.startswith(prefix):
        return None
    relative = path[len(prefix) :]
    if not relative or "\\" in relative or "\x00" in relative:
        return None
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return relative


def _stable(values: list[str]) -> tuple[str, ...]:
    """The distinct values, in first-seen order (redaction boundary input)."""
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return tuple(seen)
