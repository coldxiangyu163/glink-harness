"""E2B execution face for the ACP harness family: the agent runs in a cloud sandbox.

**The ACP face shares the sandbox (M3b).** The same ``Sandbox.create``
lifecycle, credential upload and artifact recovery as the pi face
(:mod:`pi_glink.kernel.e2b_env`) carry the ACP family: the agent runs as a
*background* command with ``stdin=True`` and the host frames the
CommandHandle's stdout with the shared LF-only splitter, so the ACP client,
projection and verdict table are the same code as the in-process and Docker
faces. ``SandboxCommandProcess`` adapts that handle to the ``subprocess.Popen``
shape ``AcpRpcClient`` is written against; the measured framing facts
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
import queue
import shlex
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from ..artifacts import ArtifactError, ArtifactTree
from ..config import HostConfig
from .acp_env import (
    ACP_RECOVERED_DIRNAME,
    INHERIT_MAX_FILE_BYTES,
    INHERIT_MAX_FILES,
    INHERIT_MAX_TOTAL_BYTES,
    AcpRuntime,
    build_acp_argv,
    credential_secrets_from_text,
)
from .e2b_env import (
    REPORT_FILENAME,
    SANDBOX_HOME,
    SANDBOX_WORKSPACE,
    UPLOAD_MAX_BYTES,
    SandboxError,
    _create_sandbox,
    _entry_kind,
    _read_file_bounded,
    _recover_workspace,
    _runtime_manifest,
)

logger = logging.getLogger("pi_glink.kernel.acp_e2b_env")

#: The ACP family's per-run state root inside the sandbox. The host renders the
#: run's own HOME (``runs/<id>/acp-agent/home``: credentials injected 0600) and
#: uploads it here; the agent process gets ``HOME`` pointed at it, so ``~/.dsh``,
#: ``~/.codex`` and every cache stay inside this run's sandbox.
SANDBOX_ACP_HOME = f"{SANDBOX_HOME}/acp/home"
#: How deep the sandbox's ACP state root is walked when it is recovered to the
#: host. The measured state roots (dsh / codex / claude / opencode) are a handful
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


class SandboxCommandProcess:
    """A ``subprocess.Popen``-shaped facade over one sandbox background command.

    The ACP client is written against ``subprocess``: a stdin it writes frames
    to, a stdout it reads bytes from, a stderr it iterates, and
    ``poll``/``wait``/``terminate``/``kill``. On this face the agent is an E2B
    background command started with ``stdin=True``, so this adapter maps those
    calls onto ``send_stdin`` / ``close_stdin`` / the ``CommandHandle`` stream /
    ``kill``. Only the carrier changes: the protocol, projection and lifecycle
    code is the same one the in-process and Docker faces use.

    The facts this adapter relies on were measured in a real sandbox *before* it
    was written (``docs/evidence/acp-m3-e2b-framing.txt``): ``send_stdin``
    writes arrive in call order and byte-exact; stdout chunks are **not**
    frame-aligned, so the client keeps using the shared LF-only framer;
    ``close_stdin`` signals EOF and the command then exits with a real code.
    """

    def __init__(self, handle: Any, *, sandbox_id: str = "") -> None:
        self._handle = handle
        self._stdout: queue.Queue[bytes | None] = queue.Queue()
        self._stderr: queue.Queue[bytes | None] = queue.Queue()
        self._exited = threading.Event()
        self._exit_code: int | None = None
        self._stream_error: str | None = None
        self.sandbox_id = sandbox_id
        self.pid = int(getattr(handle, "pid", 0) or 0)
        self.stdin = _HandleStdin(handle)
        self.stdout = _QueueReader(self._stdout)
        self.stderr = _QueueLines(self._stderr)
        self._reader = threading.Thread(target=self._pump, name="e2b-stdout", daemon=True)
        self._reader.start()

    @property
    def stream_error(self) -> str | None:
        return self._stream_error

    def _pump(self) -> None:
        try:
            for stdout, stderr, _pty in self._handle:
                if stdout:
                    self._stdout.put(str(stdout).encode("utf-8"))
                if stderr:
                    self._stderr.put(str(stderr).encode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - reported through the exit code
            self._stream_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._exit_code = self._resolve_exit_code()
            self._stdout.put(None)
            self._stderr.put(None)
            self._exited.set()

    def _resolve_exit_code(self) -> int | None:
        """The command's own exit code, or ``None`` when the stream was lost.

        The SDK's ``wait()`` answers from the end event the iterator already
        consumed; a non-zero exit arrives as ``CommandExitException``, whose
        ``exit_code`` is the command's result rather than a stream failure.
        """
        try:
            result = self._handle.wait()
        except Exception as exc:  # noqa: BLE001 - an exit code is still a result
            code = getattr(exc, "exit_code", None)
            if isinstance(code, int) and not isinstance(code, bool):
                return code
            if self._stream_error is None:
                self._stream_error = f"{type(exc).__name__}: {exc}"
            return None
        code = getattr(result, "exit_code", None)
        if isinstance(code, int) and not isinstance(code, bool):
            return code
        return None

    # -- the subprocess.Popen surface the ACP client uses ---------------------

    def poll(self) -> int | None:
        if not self._exited.is_set():
            return None
        # A command that ended without a usable exit code is no longer running;
        # ``-1`` keeps ``poll() is not None`` meaning "gone" — the only thing the
        # client reads it for — without inventing a real status.
        return self._exit_code if self._exit_code is not None else -1

    @property
    def returncode(self) -> int | None:
        """``subprocess.Popen`` 形状：退出后是状态（无码为 -1，同 poll()），否则 None。

        pi 的 RPC 客户端在 ``exit_code`` 与 ``_write`` 两处读这个属性（ACP
        客户端不读它）；自 GLINK-ARCH-3 收敛起由生产类直接提供，spike 的
        探针子类（``scripts/spikes/pi_e2b_rpc/rpc_channel_probe.py``）是它的
        前瞻。
        """
        return self.poll() if self._exited.is_set() else None

    def wait(self, timeout: float | None = None) -> int:
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired("e2b sandbox command", timeout)
        if self._exit_code is None:
            raise OSError(
                "sandbox command ended without an exit code"
                + (f": {self._stream_error}" if self._stream_error else "")
            )
        return self._exit_code

    def terminate(self) -> None:
        self.kill()

    def kill(self) -> None:
        try:
            self._handle.kill()
        except Exception:  # noqa: BLE001 - the sandbox is torn down anyway
            logger.debug("sandbox command kill failed", exc_info=True)


class _HandleStdin:
    """``process.stdin``: one ACP frame per acknowledged ``send_stdin`` RPC."""

    def __init__(self, handle: Any) -> None:
        self._handle = handle
        self._closed = False

    def write(self, data: bytes) -> int:
        payload = bytes(data)
        if self._closed:
            raise BrokenPipeError("sandbox stdin is closed")
        if payload:
            try:
                self._handle.send_stdin(payload)
            except Exception as exc:  # noqa: BLE001 - mapped to the pipe's failure mode
                raise BrokenPipeError(
                    f"sandbox stdin write failed: {type(exc).__name__}: {exc}"
                ) from exc
        return len(payload)

    def flush(self) -> None:
        """Nothing is buffered: every ``send_stdin`` call is an acknowledged RPC."""

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._handle.close_stdin(request_timeout=30)
        except Exception:  # noqa: BLE001 - an older envd; kill() still ends it
            logger.debug("sandbox close_stdin unsupported or failed", exc_info=True)


class _QueueReader:
    """A blocking byte reader over the stdout chunks the pump thread receives."""

    def __init__(self, chunks: "queue.Queue[bytes | None]") -> None:
        self._chunks = chunks

    def read(self, _size: int = -1) -> bytes:
        chunk = self._chunks.get()
        if chunk is None:
            self._chunks.put(None)  # keep EOF sticky for a later read
            return b""
        return chunk


class _QueueLines:
    """``process.stderr``: iterate byte lines until the stream ends."""

    def __init__(self, chunks: "queue.Queue[bytes | None]") -> None:
        self._chunks = chunks

    def __iter__(self) -> Iterator[bytes]:
        while True:
            chunk = self._chunks.get()
            if chunk is None:
                return
            yield from chunk.splitlines(keepends=True)


class E2BAcpSandboxClient:
    """Own one E2B sandbox for one ACP run.

    The lifecycle is the pi face's (create with the frozen credential and a
    bounded lifetime, destroy by ``kill()``, recover the workspace before the
    sandbox dies) and the artifact/identity contract is the same. The difference
    is the transport: the ACP agent runs as a background command with
    ``stdin=True`` and :meth:`spawn` wraps its handle for the shared ACP client
    (:class:`SandboxCommandProcess`), exactly like the Docker face hands
    ``docker run -i``'s stdio to the same client.

    ``runtime`` is the run's host-rendered ACP state root
    (:func:`~pi_glink.kernel.acp_env.prepare_acp_runtime`): the credential files
    it holds are uploaded into the sandbox state root with 0600 applied there,
    so the M2/M3a boundary (per-run HOME, file-shaped credentials, no value in
    argv or environment) holds on this face too. A follow-up's ``runtime`` HOME
    additionally holds the parent run's state, inherited by the runner before
    this client exists, so the same upload is what restores the session into
    the new sandbox; and :meth:`recover_state` brings the sandbox's own state
    root back to the host before :meth:`close` destroys it, which is what makes
    that inheritance possible in the first place.
    """

    def __init__(
        self,
        config: HostConfig,
        runtime: AcpRuntime,
        *,
        run_id: str,
        workspace: Path,
        sandbox_factory: Callable[..., Any] | None = None,
        template: str = "",
    ) -> None:
        self.config = config
        self.runtime = runtime
        self.run_id = run_id
        self.workspace = Path(workspace)
        self._sandbox_factory = sandbox_factory
        self._template = template or config.acp_e2b_template
        self.sandbox: Any | None = None
        self.sandbox_id: str = ""
        #: Lifetime the sandbox was created with; the agent command must be
        #: allowed to run at least that long (see ``_command_timeout``).
        self.sandbox_timeout: int = 0
        #: Sandbox paths the credential upload wrote.
        self.uploaded: list[str] = []
        #: ``(relative path, sha256)`` for everything delivered to the sandbox.
        self.upload_manifest: list[tuple[str, str]] = []

    @property
    def argv(self) -> list[str]:
        return build_acp_argv(self.config)

    @property
    def template(self) -> str:
        """The frozen E2B template this run's sandbox is created from."""
        return self._template

    # -- lifecycle ------------------------------------------------------------

    def start(self, *, timeout_seconds: int | None = None) -> None:
        if self.sandbox is not None:
            raise SandboxError("sandbox already started")
        sandbox, self.sandbox_timeout = _create_sandbox(
            self.config,
            template=self._template,
            timeout_seconds=timeout_seconds,
            sandbox_factory=self._sandbox_factory,
        )
        self.sandbox = sandbox
        self.sandbox_id = str(getattr(sandbox, "sandbox_id", ""))

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
        sandbox = self._require_sandbox()
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
        sandbox = self._require_sandbox()
        command = shlex.join(["chmod", f"{mode:04o}", *paths])
        try:
            sandbox.commands.run(command, timeout=60)
        except Exception as exc:  # noqa: BLE001 - surfaced as a delivery failure
            raise SandboxError(f"{what}失败: {type(exc).__name__}: {exc}") from exc

    def upload_continuation(self) -> None:
        """Deliver the conversation's prior deliverable into the new sandbox.

        The pi face transfers the sanitized session snapshot plus the prior
        ``report.md`` (:meth:`E2BSandboxClient.upload_continuation`); the ACP
        session itself travels through the inherited run HOME, and this is the
        workspace half: a resumed agent remembers the conversation's workspace
        (M7), so the deliverable it produced there is restored rather than left
        as an empty directory the conversation never wrote. Only
        ``report.md`` is transferred — the product contract's file — read
        through :class:`~pi_glink.artifacts.ArtifactTree` and bounded; a run
        with no prior report (or an unreadable one) simply uploads nothing.
        """
        sandbox = self._require_sandbox()
        try:
            with ArtifactTree(self.workspace) as tree:
                report = tree.read(REPORT_FILENAME, max_bytes=UPLOAD_MAX_BYTES)
        except ArtifactError:
            return
        sandbox.files.write(f"{SANDBOX_WORKSPACE}/{REPORT_FILENAME}", report)

    def command(self) -> str:
        """The sandbox command line: the frozen agent argv in the workspace.

        ``exec`` replaces the login shell so the agent owns stdin directly, and
        :func:`shlex.join` quotes every argument (the argv is frozen
        configuration, but a path with a space must not become two words).
        """
        return f"cd {shlex.quote(SANDBOX_WORKSPACE)} && exec {shlex.join(self.argv)}"

    def command_env(self) -> dict[str, str]:
        """The agent command's environment: per-run HOME plus ``PI_GLINK_ACP_ENV``.

        ``HOME`` is the sandbox state root :meth:`upload_home` filled (the
        counterpart of the Docker face's ``HOME=/home/acp``); the configured
        entries are this face's explicit process-environment channel, and the
        credential material itself stays in files. ``HOME`` can never be
        overridden by ``PI_GLINK_ACP_ENV``: the configuration refuses that name,
        and this dict is built with it first.
        """
        env = {"HOME": SANDBOX_ACP_HOME}
        for name, value in self.config.acp_env:
            env[name] = value
        return env

    def spawn(self) -> SandboxCommandProcess:
        """Start the agent as a background command and wrap it for the client."""
        sandbox = self._require_sandbox()
        handle = sandbox.commands.run(
            self.command(),
            background=True,
            stdin=True,
            envs=self.command_env(),
            cwd=SANDBOX_WORKSPACE,
            timeout=self._command_timeout(),
        )
        return SandboxCommandProcess(handle, sandbox_id=self.sandbox_id)

    def recover_artifacts(self, destination: Path) -> tuple[list[Path], list[str]]:
        """Pull the sandbox workspace back before the sandbox is destroyed."""
        return _recover_workspace(self._require_sandbox(), destination)

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
        sandbox = self._require_sandbox()
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

    def close(self) -> list[str]:
        """Destroy the sandbox. Returns the failures instead of hiding them."""
        sandbox = self.sandbox
        self.sandbox = None
        if sandbox is None:
            return []
        try:
            sandbox.kill()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            return [f"sandbox kill failed: {type(exc).__name__}: {exc}"]
        return []

    def _command_timeout(self) -> int:
        """How long the agent command may run, in seconds (same rule as pi)."""
        if self.sandbox_timeout > 0:
            return self.sandbox_timeout
        return int(self.config.task_timeout_seconds) + 300

    def _require_sandbox(self) -> Any:
        if self.sandbox is None:
            raise SandboxError("sandbox 未启动")
        return self.sandbox


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
