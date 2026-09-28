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

import hashlib
import logging
import os
import shlex
import shutil
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


@dataclass(frozen=True, slots=True)
class AcpStateRecovery:
    """What one bounded recovery of a sandbox's ACP state root achieved.

    ``ok`` is the whole-or-nothing verdict: a recovery that could not read or
    mirror every file (a bound violation, an unrepresentable path, a sandbox
    read error) is reported failed and leaves **no** recovered root behind — a
    partial state root would present the next turn a conversation the agent
    does not actually have. ``files``/``bytes`` are the mirror's facts for the
    run record; ``secrets`` are the credential-shaped values discovered in the
    recovered text, which join this run's redaction boundary (the state files
    themselves are never rewritten — rewriting the agent's own session store
    would corrupt the session a resume is meant to continue; see
    :func:`~pi_glink.kernel.acp_env.inherit_acp_state`, which scans the same
    root again when a follow-up inherits it).
    """

    ok: bool
    files: tuple[str, ...] = ()
    bytes: int = 0
    secrets: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()


class AcpSandboxTransfer:
    """ACP 族在一个**已打开**沙箱上的状态交付与回收（harness 侧，不拥有沙箱）。

    沙箱的创建、命令、身份与销毁属于
    :class:`~pi_glink.kernel.sandbox.E2BSandbox`；本类只知道 ACP 的布局：
    ``runtime`` 是 run 的宿主渲染 ACP 状态根
    (:func:`~pi_glink.kernel.acp_env.prepare_acp_runtime`)，其中的凭据文件交到
    :data:`SANDBOX_ACP_HOME` 并在沙箱内收紧为 0600，使 M2/M3a 边界（per-run
    HOME、文件形凭据、值不进 argv 或环境）在本面同样成立。追问 run 的 HOME
    已含 runner 继承的父 run 状态，同一次上传就是把会话灌回新沙箱；
    :meth:`recover_state` 在沙箱销毁前把状态根拉回宿主，这正是继承之所以
    可能的前提。
    """

    def __init__(self, handle: Any, runtime: AcpRuntime, *, workspace: Path) -> None:
        self.handle = handle
        self.runtime = runtime
        self.workspace = Path(workspace)
        #: Sandbox paths the credential upload wrote.
        self.uploaded: list[str] = []
        #: ``(relative path, sha256)`` for everything delivered to the sandbox.
        self.upload_manifest: list[tuple[str, str]] = []

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

        A follow-up's HOME is not just credentials: the runner inherited the
        parent run's recovered state into it, so this upload is also the
        "灌回" step — the new sandbox gets the state the resume needs, in the
        same hardened order as the first turn. A failed upload fails the run
        (the caller never starts the agent against a partial state root).
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

    def recover_state(self) -> AcpStateRecovery:
        """Mirror the sandbox's ACP state root onto the host (bounded).

        The sandbox destroys the agent's state (its sessions, credentials,
        caches) with it, so the host takes its own copy **before** ``close()``
        kills the sandbox: without it a follow-up would be admitted into a
        sandbox that can never find the session it is told to resume. The copy
        lands in a freshly staged directory under the run's ACP runtime dir and
        is published by one ``rename`` to ``acp-agent/recovered``; the
        admission gate reads the published directory's existence as the durable
        "父状态根回收成功" fact, so nothing is ever inherited from a partial or
        failed copy.

        The bounds are the follow-up inheritance bounds (file count, one file,
        total bytes) — the same limits M7 applies when the child inherits the
        root — and a violation refuses the whole recovery instead of truncating
        it. Entries are read through :func:`_read_file_bounded` (size metadata
        or a streamed, aborted-early read), only regular files are mirrored
        (symbolic links are skipped, never followed: the host must not import a
        file from outside the state root), and every relative path is validated
        before it is written through :class:`~pi_glink.artifacts.ArtifactTree`,
        so the mirror can never land outside the run's own directory. The
        recovered text is scanned for credential-shaped values, which are
        returned for the run's redaction boundary; the files are not rewritten.
        """
        sandbox = self.handle
        max_files = INHERIT_MAX_FILES
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
        secrets: list[str] = []
        for relative in collected:
            data = collected[relative]
            if b"\x00" not in data[:1024]:
                secrets.extend(
                    credential_secrets_from_text(data.decode("utf-8", errors="replace"))
                )
        return self._publish_recovered_state(collected, total, _stable(secrets))

    def _publish_recovered_state(
        self,
        collected: Mapping[str, bytes],
        total: int,
        secrets: tuple[str, ...],
    ) -> AcpStateRecovery:
        """Write the staged mirror and publish it with one rename.

        The staging directory is private to this run and this call; it is
        created 0700 and every file lands through :class:`ArtifactTree` (0600,
        parents 0700). Only a complete mirror is renamed into place, so a
        crash or write failure leaves no recovered root and the follow-up
        refusal holds.
        """
        staging = self.runtime.directory / f".{ACP_RECOVERED_DIRNAME}.staging"
        published = self.runtime.directory / ACP_RECOVERED_DIRNAME
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
