"""E2B sandbox plumbing and pi's file transfer for the E2B face.

This module holds what the E2B face needs below the
:class:`~pi_glink.kernel.sandbox.E2BSandbox` resource (sandbox creation,
workspace recovery, the ``subprocess.Popen``-shaped
:class:`SandboxCommandProcess` over a background command) plus the pi
harness's own transfer (:class:`PiSandboxTransfer`: configuration upload,
continuation upload, session and subagent-artifact recovery).

**RPC 通道（GLINK-ARCH-3 收敛）。** 本面与其余两面说同一个协议：pi 以
``pi --mode rpc`` 作为沙箱 *background* 命令启动（``stdin=True``），
``E2BSandbox.spawn()`` 把 ``CommandHandle`` 包装成 :class:`SandboxCommandProcess`
交给共享的 ``PiRpcClient``——只有载体变化，帧分发、投影与终态判定复用同一套
代码（ACP 族走同一个 ``E2BSandbox.spawn()``）。双向通道的可行性（
含运行中 abort 被受理、close_stdin 语义）由 spike 实测钉死，证据在
``docs/evidence/pi-e2b-rpc-stdin-spike.txt``。

**断流即失败。** RPC 通道没有第二份事件副本：流即权威，沙箱流断开（拿不到
退出码）时 run 判 ``execution_error``——不补发、不做 ``get_state`` 对账
（响应帧走同一条已断的 stdout，且 SDK 重连是否重放未规定）。换来的是双向
通道：运行中 abort、``session_ready`` 前即知会话身份（docs/adr/0004）。

**Credential flow (recorded per the task contract).**

* No real model or search key is uploaded (docs/adr/0008). ``auth.json`` and
  ``web-search.json`` carry only the run's short-lived egress token, and
  ``models.json`` / ``exaBaseUrl`` point at the host's credential egress proxy
  through ``PI_GLINK_EGRESS_PUBLIC_URL`` — without that URL an E2B pi run is
  refused (fail closed), never given the real key. The token is still kept out
  of the sandbox environment (a shell tool cannot read it with ``env``) and is
  revoked when the run's verdict is committed. A run without a search grant has
  no ``web-search.json`` (keyless Exa MCP search).

**Configuration delivery.** The sandbox receives the *whole* per-run runtime
directory (generated files plus the reviewed template entries: ``AGENTS.md``,
``settings.json``, ``extensions/``, ``skills/``), with the same
relative paths the in-process and Docker faces use. The upload records a manifest
of ``(relative path, sha256)`` so the delivery is verifiable instead of "the
current directory happened to be empty". ``settings.json`` renders the prebaked
pi packages as image-local paths (``/opt/pi-packages/node_modules/…``); the
sandbox command runs the image's package guard before pi, because pi silently
ignores a missing local package and a research run without web_search must fail
loudly instead.

**The ACP face shares the sandbox (M3b), and lives in its own module.** The
same ``Sandbox.create`` lifecycle, credential upload and artifact recovery carry
the ACP family; its client and the ``subprocess.Popen``-shaped transport bridge
over a background command's handle are in
:mod:`pi_glink.kernel.acp_e2b_env`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import queue
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from ..artifacts import ArtifactError, ArtifactTree, deliverable_path
from ..config import ConfigError, HostConfig
from .agent_env import IMAGE_PACKAGE_GUARD, AgentRuntime

logger = logging.getLogger("pi_glink.kernel.e2b_env")

#: Sandbox paths (E2B runs as a non-root user with /home/user as home).
SANDBOX_HOME = "/home/user"
SANDBOX_CONFIG = f"{SANDBOX_HOME}/config"
SANDBOX_WORKSPACE = f"{SANDBOX_HOME}/workspace"
SANDBOX_SESSIONS = f"{SANDBOX_HOME}/sessions"
REPORT_FILENAME = "report.md"
#: Image-local guard script (same bytes in both worker images, see
#: :data:`agent_env.IMAGE_PACKAGE_GUARD`). The sandbox has no ``--entrypoint``
#: equivalent, so the command chain verifies it before starting pi. pi silently
#: skips a local package path that does not exist, so without the guard a
#: template that predates the host (or lost the package) would run the whole
#: research task without web_search.
SANDBOX_PACKAGE_GUARD = IMAGE_PACKAGE_GUARD
#: One uploaded config file / recovered artifact may not exceed this.
UPLOAD_MAX_BYTES = 8 * 1024 * 1024
ARTIFACT_MAX_BYTES = 32 * 1024 * 1024
#: Host-side deadline for one E2B *files* request on the close-out path
#: (listing, metadata, streamed or whole reads). The SDK default is 60s, so a
#: single stuck call could stretch the recovery minute by minute and defer the
#: terminal commit; a stalled call is recorded as an artifact failure instead.
#: ``child_progress`` polls its own ``files`` reads under the same value.
E2B_REQUEST_TIMEOUT_SECONDS = 15.0
#: Host-side deadline for one kill call in the teardown path. The SDK's
#: ``CommandHandle.kill()`` accepts no request timeout, and a wedged transport
#: must not hold the close-out open, so the kill runs on a daemon thread and
#: the host gives up after this bound. The timeout is a *recorded* failure
#: (``CleanupReport.failures`` / the ledger's hold reason), never a silent loss
#: and never an unbounded stall before the terminal commit.
E2B_KILL_TIMEOUT_SECONDS = 15.0
#: Bounds for the run workspace recovery (direct file children; a single read is
#: already bounded by ``ARTIFACT_MAX_BYTES``). A runaway agent cannot turn the
#: close-out into an unbounded copy: going over either bound records a failure in
#: the run's ``artifactFailures`` and stops, never blocks the verdict.
WORKSPACE_RECOVER_MAX_FILES = 64
WORKSPACE_RECOVER_MAX_TOTAL_BYTES = 64 * 1024 * 1024


class SandboxError(RuntimeError):
    """Raised when the sandbox cannot be created or the run cannot start."""


def kill_within_budget(
    kill: Callable[[], Any],
    *,
    what: str,
    timeout: float | None = None,
) -> Any:
    """Run one kill call under a host-side deadline.

    Returns the call's result when it returned within the bound, re-raises what
    it raised when it failed, and raises :class:`SandboxError` when the host
    gives up waiting. The call runs on a daemon thread so a wedged
    SDK/transport call can neither keep the host process alive nor hold the
    terminal commit open; every caller records the raised failure in its own
    failure list (the sandbox's :class:`CleanupReport` or the resource ledger's
    hold reason).
    """
    bound = E2B_KILL_TIMEOUT_SECONDS if timeout is None else timeout
    done = threading.Event()
    result: list[Any] = []
    failure: list[BaseException] = []

    def _run() -> None:
        try:
            result.append(kill())
        except Exception as exc:  # noqa: BLE001 - re-raised to the caller
            failure.append(exc)
        finally:
            done.set()

    threading.Thread(target=_run, name="e2b-kill", daemon=True).start()
    if not done.wait(bound):
        raise SandboxError(
            f"{what} kill 未在 {bound:g}s 内返回（宿主放弃等待，销毁结果不确定）"
        )
    if failure:
        raise failure[0]
    return result[0] if result else None


class PiSandboxTransfer:
    """pi 族在一个**已打开**沙箱上的文件交付与回收（harness 侧，不拥有沙箱）。

    沙箱的创建、命令、身份与销毁属于
    :class:`~pi_glink.kernel.sandbox.E2BSandbox`（资源与传输）；本类只知道
    pi 的布局：渲染好的配置目录交到 :data:`SANDBOX_CONFIG`、续接会话快照交到
    :data:`SANDBOX_SESSIONS`、主会话与子代理产物在沙箱销毁前拉回宿主。
    ``handle`` 是 E2B SDK 的沙箱对象（``files``/``commands``）。
    """

    def __init__(self, handle: Any, runtime: AgentRuntime) -> None:
        self.handle = handle
        self.runtime = runtime
        self.uploaded: list[str] = []
        #: ``(relative path, sha256)`` for everything delivered to the sandbox.
        self.upload_manifest: list[tuple[str, str]] = []

    def upload_config(self) -> None:
        """Upload the run's rendered configuration directory (see module docstring).

        Every file in the runtime directory is delivered with its relative path
        intact, so the sandboxed kernel reads the same configuration the local
        faces read. The manifest lets a test (or an operator) verify what was
        actually delivered.
        """
        sandbox = self.handle
        sandbox.files.make_dir(SANDBOX_CONFIG)
        sandbox.files.make_dir(SANDBOX_WORKSPACE)
        sandbox.files.make_dir(SANDBOX_SESSIONS)
        manifest = _runtime_manifest(self.runtime.directory)
        if not manifest:
            return
        try:
            tree = ArtifactTree(self.runtime.directory)
        except ArtifactError as exc:
            raise SandboxError(f"配置交付失败: {exc}") from exc
        with tree:
            for relative, is_dir in manifest:
                sandbox_path = f"{SANDBOX_CONFIG}/{relative}"
                if is_dir:
                    sandbox.files.make_dir(sandbox_path)
                    continue
                try:
                    data = tree.read(relative, max_bytes=UPLOAD_MAX_BYTES)
                except ArtifactError as exc:
                    raise SandboxError(f"配置交付失败: {relative}: {exc}") from exc
                sandbox.files.write(sandbox_path, data)
                self.uploaded.append(sandbox_path)
                self.upload_manifest.append((relative, hashlib.sha256(data).hexdigest()))

    def upload_continuation(self, session_name: str, workspace: Path) -> None:
        """Transfer only the host-owned session snapshot and prior report."""
        if session_name != "continued.jsonl":
            raise SandboxError("invalid continuation session")
        sandbox = self.handle
        with ArtifactTree(self.runtime.session_dir) as tree:
            data = tree.read(session_name, max_bytes=ARTIFACT_MAX_BYTES)
        sandbox.files.write(f"{SANDBOX_SESSIONS}/{session_name}", data)
        try:
            with ArtifactTree(workspace) as tree:
                report = tree.read(REPORT_FILENAME, max_bytes=ARTIFACT_MAX_BYTES)
        except ArtifactError:
            return
        sandbox.files.write(f"{SANDBOX_WORKSPACE}/{REPORT_FILENAME}", report)

    def recover_session(self) -> tuple[Path | None, str | None]:
        """Recover the run's main session file before the sandbox is destroyed.

        Subagent forks (pi-subagents ``createBranchedSession``) share this
        directory with the main session: they are timestamped ``.jsonl`` files
        with the same naming, told apart only by the header — a branched
        header carries ``parentSession``, the main session's does not (a
        continued session is opened in place and keeps its original header).
        Subdirectories (``subagent-artifacts/``) never compete. The main
        session must be *uniquely* determined; anything ambiguous refuses the
        follow-up instead of guessing.
        """
        sandbox = self.handle
        try:
            main, _others, failure = _session_candidates(sandbox)
            if failure is not None or main is None:
                return None, f"沙箱会话文件不可续接: {failure or '主会话无法唯一确定'}"
            data, read_failure = _read_file_bounded(
                sandbox, f"{SANDBOX_SESSIONS}/{main}", ARTIFACT_MAX_BYTES
            )
            if read_failure is not None or not data:
                return None, f"沙箱会话文件不可续接: {read_failure or '空文件'}"
            with ArtifactTree(self.runtime.session_dir) as tree:
                tree.replace(main, data, max_bytes=ARTIFACT_MAX_BYTES)
            return self.runtime.session_dir / main, None
        except Exception as exc:  # noqa: BLE001 - sandbox SDK failures must not hide the verdict
            return None, f"沙箱会话回收失败: {type(exc).__name__}"

    def recover_subagent_files(
        self, destination: Path
    ) -> tuple[list[Path], list[str]]:
        """Mirror subagent sessions and artifacts out of the dying sandbox.

        Branched sessions land under ``sessions/`` and the ``subagent-artifacts``
        subtree under ``artifacts/``; the main session is left to
        :meth:`recover_session`. Best-effort per file: directories and links
        are never followed, unreadable or oversized regular files are recorded
        as failures (they are not load-bearing for the run's continuation), and
        every write lands through the destination's directory handles.
        """
        destination.mkdir(parents=True, exist_ok=True)
        try:
            tree = ArtifactTree(destination)
        except ArtifactError as exc:
            return [], [f"子代理产物落点不可用: {exc}"]
        with tree:
            return _mirror_subagent_data(self.handle, tree, destination)


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
        客户端不读它）。
        """
        return self.poll() if self._exited.is_set() else None

    def wait(self, timeout: float | None = None) -> int:
        if not self._exited.wait(timeout):
            raise subprocess.TimeoutExpired("e2b sandbox command", timeout or 0.0)
        if self._exit_code is None:
            raise OSError(
                "sandbox command ended without an exit code"
                + (f": {self._stream_error}" if self._stream_error else "")
            )
        return self._exit_code

    def terminate(self) -> None:
        self.kill()

    def kill(self) -> None:
        """Kill the background command under the host's kill deadline.

        ``CommandHandle.kill()`` takes no request timeout: without the bound a
        wedged transport would hold ``PiRpcClient.close`` (and with it the whole
        close-out) open forever. A kill that does not return in time is logged;
        the caller's following ``wait`` then reports the unobserved end as the
        usual failure, so the run records an unreleased resource instead of
        hanging. The sandbox teardown (``E2BSandbox.close``) is the release
        authority and applies the same bound.
        """
        try:
            kill_within_budget(self._handle.kill, what="沙箱命令")
        except Exception as exc:  # noqa: BLE001 - the sandbox is torn down anyway
            logger.warning("sandbox command kill did not complete: %s", exc)


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


def _create_sandbox(
    config: HostConfig,
    *,
    template: str,
    timeout_seconds: int | None,
    sandbox_factory: Callable[..., Any] | None,
    envs: Mapping[str, str] | None = None,
) -> tuple[Any, int]:
    """Create one sandbox from the frozen configuration; returns ``(sandbox, lifetime)``.

    Shared by the pi and ACP faces so both create the same kind of resource with
    the same credential flow. ``Sandbox.create`` takes the credential through
    ``**opts`` (ApiParams), so an operator who only filled ``.env`` still
    authenticates: the host passes the key it read at startup instead of
    relying on the SDK having inherited ``E2B_API_KEY`` from the launching
    shell. ``timeout_seconds`` is the adapter's bound (task timeout + teardown
    headroom); the frozen configuration may set a tighter ceiling. E2B does not
    expose CPU/memory controls through this SDK path, so the host can only bound
    lifetime — it never claims a resource limit the cloud provider was not asked
    to enforce.
    """
    if not template:
        raise ConfigError("E2B 沙箱模板未配置")
    if not config.has_e2b_key:
        raise ConfigError("E2B_API_KEY 未配置")
    factory = sandbox_factory or _default_sandbox_factory()
    adapter_bound = int(timeout_seconds or 0)
    ceiling = int(
        config.e2b_sandbox_timeout_seconds or (config.task_timeout_seconds + 300)
    )
    timeout = min(adapter_bound, ceiling) if adapter_bound > 0 else ceiling
    create_kwargs: dict[str, Any] = {
        "template": template,
        "timeout": timeout,
        "envs": dict(envs or {}),
    }
    if config.e2b_api_key:
        create_kwargs["api_key"] = config.e2b_api_key
    try:
        return factory(**create_kwargs), int(timeout)
    except Exception as exc:  # noqa: BLE001 - surfaced to the run record
        raise SandboxError(f"sandbox 创建失败: {type(exc).__name__}: {exc}") from exc


def _recover_workspace(
    sandbox: Any, destination: Path, *, declared: tuple[str, ...] = ()
) -> tuple[list[Path], list[str]]:
    """Copy direct files plus declared nested files within one shared budget.

    Main report and declarations get priority; every nested ancestor is listed
    and checked before the bounded read. Shared by both harness families.
    """
    destination.mkdir(parents=True, exist_ok=True)
    try:
        entries = sandbox.files.list(
            SANDBOX_WORKSPACE, request_timeout=E2B_REQUEST_TIMEOUT_SECONDS
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller
        return [], [f"无法列出沙箱工作区: {type(exc).__name__}: {exc}"]
    try:
        tree = ArtifactTree(destination)
    except ArtifactError as exc:
        return [], [f"宿主工作区不可用: {exc}"]
    files: list[Path] = []
    failures: list[str] = []
    total = 0
    direct = {str(getattr(item, "name", "")): item for item in entries}
    # report and declarations consume the existing shared count/byte budget first;
    # an unrelated pile of workspace files cannot crowd out a declared delivery.
    names = list(dict.fromkeys((REPORT_FILENAME, *declared, *direct)))
    with tree:
        for name in names:
            # Reject malformed SDK listing names even when no usable EntryInfo
            # could be resolved. Never let the missing-entry branch hide them.
            if (name in direct and "/" in name and name not in declared) or (
                name in direct and ("\\" in name or name in {".", ".."})
            ):
                failures.append(f"跳过非法产物名: {name!r}")
                continue
            entry = direct.get(name) if "/" not in name else None
            if entry is None and "/" in name and name in declared:
                entry, failure = _declared_entry(sandbox, name, direct)
                if failure:
                    failures.append(f"交付文件不可回收: {name}: {failure}")
                    continue
            if entry is None:
                if name in declared:
                    failures.append(f"交付文件不存在: {name}")
                continue
            path = f"{SANDBOX_WORKSPACE}/{name}"
            # Path semantics are checked before the hidden-file skip: a name
            # that tries to escape ("../x") starts with a dot and must be
            # reported, not silently ignored.
            if ("/" in name and name not in declared) or "\\" in name or name in {".", ".."}:
                failures.append(f"跳过非法产物名: {name!r}")
                continue
            kind = _entry_kind(entry)
            if kind == "dir":
                # Directories are not deliverables; only regular entries are
                # recovered (and an unreadable regular entry is reported).
                continue
            if kind == "symlink":
                # A declared link must produce a visible failure, not a phantom card.
                if name in declared:
                    failures.append(f"交付文件是符号链接: {name}")
                continue
            if name.startswith(".") and name not in declared:
                continue
            # Same triple bound as the subagent mirror: the file count is
            # checked before reading, the total after reading and before
            # writing. A breach is a recorded failure and stops the copy; the
            # verdict is never blocked by a runaway workspace.
            if name in declared:
                try:
                    deliverable_path(name)
                except ArtifactError as exc:
                    failures.append(f"交付文件路径拒绝: {name}: {exc}")
                    continue
            if len(files) >= WORKSPACE_RECOVER_MAX_FILES:
                failures.append(
                    f"工作区产物超过文件数上限（{WORKSPACE_RECOVER_MAX_FILES}）"
                )
                break
            # Bound the read with the listing's own metadata before loading
            # the entry; an entry whose size the SDK does not report falls
            # back to a streamed, bounded read.
            known = getattr(entry, "size", None)
            data, failure = _read_file_bounded(
                sandbox,
                path,
                ARTIFACT_MAX_BYTES,
                known_size=known if isinstance(known, int) else None,
            )
            if data is None:
                failures.append(f"产物不可读取或超过上限: {name}: {failure}")
                continue
            total += len(data)
            if total > WORKSPACE_RECOVER_MAX_TOTAL_BYTES:
                failures.append(
                    f"工作区产物超过总量上限（{WORKSPACE_RECOVER_MAX_TOTAL_BYTES} 字节）"
                )
                break
            try:
                tree.replace(name, data, max_bytes=ARTIFACT_MAX_BYTES, create_parents=True)
            except (ArtifactError, OSError) as exc:
                failures.append(f"产物写入失败: {name}: {exc}")
                continue
            files.append(destination / name)
    return files, failures


def _declared_entry(
    sandbox: Any, name: str, direct: dict[str, Any]
) -> tuple[Any | None, str | None]:
    """Walk remote list metadata one directory at a time; never list through a link."""
    try:
        deliverable_path(name)
    except ArtifactError as exc:
        return None, str(exc)
    parts = name.split("/")
    entries = direct
    for index, part in enumerate(parts):
        entry = entries.get(part)
        if entry is None:
            return None, "路径不存在"
        kind = _entry_kind(entry)
        if index == len(parts) - 1:
            return (entry, None) if kind == "file" else (None, "非普通文件或符号链接")
        if kind != "dir":
            return None, "祖先不是普通目录或是符号链接"
        parent = f"{SANDBOX_WORKSPACE}/{'/'.join(parts[:index + 1])}"
        try:
            children = sandbox.files.list(parent, request_timeout=E2B_REQUEST_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 - bounded remote request, report the failure
            return None, f"目录不可读取: {type(exc).__name__}"
        entries = {str(getattr(child, "name", "")): child for child in children}
    return None, "路径不存在"


def _runtime_manifest(runtime_dir: Path) -> list[tuple[str, bool]]:
    """Every file/directory to deliver, as ``(relative, is_dir)``.

    The runtime directory is host-rendered before the sandbox exists; the bytes
    are still read through :class:`~pi_glink.artifacts.ArtifactTree`.
    """
    manifest: list[tuple[str, bool]] = []
    if not runtime_dir.is_dir():
        return manifest
    for child in sorted(runtime_dir.rglob("*")):
        if child.is_symlink():
            continue
        manifest.append((child.relative_to(runtime_dir).as_posix(), child.is_dir()))
    return manifest


def _default_sandbox_factory() -> Callable[..., Any]:
    def factory(**kwargs: Any) -> Any:
        from e2b import Sandbox

        return Sandbox.create(**kwargs)

    return factory


def _default_resume_factory() -> Callable[..., Any]:
    def factory(**kwargs: Any) -> Any:
        from e2b import Sandbox

        # The SDK's class-method variant: connect by sandbox id, resuming a
        # paused sandbox on the way in (docs/adr/0020's hot path).
        return Sandbox.connect(**kwargs)

    return factory


def resume_sandbox(
    config: HostConfig,
    *,
    sandbox_id: str,
    timeout_seconds: int | None,
    resume_factory: Callable[..., Any] | None = None,
) -> tuple[Any, int]:
    """Reconnect to a paused sandbox as the conversation's hot cache.

    The mirror of :func:`_create_sandbox` for the hot path (docs/adr/0020):
    same credential flow (the frozen key over ``**opts``), same lifetime
    ceiling, one difference — ``on_resume="reboot"`` cold-boots from the
    persisted filesystem instead of restoring a memory snapshot. The parked
    sandbox was paused with ``keep_memory=False`` and its agent process was
    already stopped by the host, so there is no memory state worth restoring
    and no process the host could ever reattach to; a disk boot is the only
    resume that cannot resurrect a second writer. A control plane that rejects
    the option or cannot resume raises, and the caller degrades to the cold
    path (a fresh sandbox + the 阶段 1 backfill).
    """
    if not sandbox_id:
        raise SandboxError("热路径缺少 sandbox_id")
    if not config.has_e2b_key:
        raise ConfigError("E2B_API_KEY 未配置")
    factory = resume_factory or _default_resume_factory()
    adapter_bound = int(timeout_seconds or 0)
    ceiling = int(
        config.e2b_sandbox_timeout_seconds or (config.task_timeout_seconds + 300)
    )
    timeout = min(adapter_bound, ceiling) if adapter_bound > 0 else ceiling
    connect_kwargs: dict[str, Any] = {
        "sandbox_id": sandbox_id,
        "timeout": timeout,
        "on_resume": "reboot",
    }
    if config.e2b_api_key:
        connect_kwargs["api_key"] = config.e2b_api_key
    try:
        return factory(**connect_kwargs), int(timeout)
    except Exception as exc:  # noqa: BLE001 - the caller degrades to cold path
        raise SandboxError(
            f"热路径沙箱恢复失败: {type(exc).__name__}: {exc}"
        ) from exc


def kill_sandbox_by_id(
    sandbox_id: str, *, config: HostConfig | None = None, api_key: str = ""
) -> tuple[bool, str]:
    """Destroy one sandbox by id (the lease sweep's fail-closed hammer).

    Reuses the ledger's kill-based probe contract — only the SDK's own
    not-found types confirm a sandbox is gone — so an expired lease's sandbox
    is either confirmed destroyed or honestly left on the books for the next
    tick. ``config`` and ``api_key`` mirror :func:`confirm_sandbox_absent`'s
    two credential sources (the frozen configuration in production, a bare key
    in tests).
    """
    key = api_key or (config.e2b_api_key if config is not None else "")
    return confirm_sandbox_absent(sandbox_id, api_key=key)


def _entry_kind(entry: Any) -> str:
    """The SDK's entry kind as a plain lowercase string.

    ``EntryInfo.type`` is :class:`e2b.sandbox.filesystem.filesystem.FileType`, an
    ``Enum`` whose string form is ``"FileType.DIR"``; an older payload or a test
    double may hand back the plain value instead. Both shapes resolve to the same
    kind, and an absent type means a regular file (the listing's default).
    """
    raw = getattr(entry, "type", None)
    value = getattr(raw, "value", raw)
    if isinstance(value, str) and value:
        return value.strip().lower()
    return "file"


def _entry_size(sandbox: Any, path: str) -> int | None:
    """The SDK-reported size of one sandbox entry, when the SDK exposes it."""
    files = getattr(sandbox, "files", None)
    get_info = getattr(files, "get_info", None)
    if not callable(get_info):
        return None
    try:
        info = get_info(path, request_timeout=E2B_REQUEST_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - absence is the caller's decision
        return None
    size = getattr(info, "size", None)
    return int(size) if isinstance(size, int) else None


def _stream_reader(sandbox: Any, path: str) -> tuple[Any, str | None]:
    """``(reader, failure)`` for the SDK's chunked read of one file.

    ``(None, None)`` means the SDK has no streaming overload; a real read error
    (missing file, transport failure) is returned as a failure reason instead of
    being mistaken for unsupported behaviour.
    """
    files = getattr(sandbox, "files", None)
    read = getattr(files, "read", None)
    if not callable(read):
        return None, None
    try:
        stream = read(path, format="stream", request_timeout=E2B_REQUEST_TIMEOUT_SECONDS)
        return stream, None
    except TypeError:
        # An SDK (or test double) without the streaming overload: the caller
        # falls back to the size metadata as its only pre-read bound.
        return None, None
    except Exception as exc:  # noqa: BLE001 - surfaced as a failure reason
        return None, f"{type(exc).__name__}: {exc}"


def _read_file_bounded(
    sandbox: Any,
    path: str,
    max_bytes: int,
    *,
    known_size: int | None = None,
) -> tuple[bytes | None, str | None]:
    """Read one sandbox file without ever loading more than ``max_bytes``.

    Sandbox content is untrusted input, so the ceiling is enforced *before* the
    bytes are loaded: entry metadata (``files.get_info`` / a listing's ``size``)
    is consulted first, and a streamed read is abandoned as soon as the ceiling
    is crossed. When the SDK exposes neither, the whole-file read is refused
    instead of guessing. Returns ``(data, None)``, or ``(None, reason)``.

    (The metadata/streaming contract is pinned against the fake SDK in the
    regressions; the real E2B SDK exposes ``EntryInfo.size`` and a
    ``format="stream"`` reader, which still needs live verification.)
    """
    size = known_size if known_size is not None else _entry_size(sandbox, path)
    if size is not None and size > max_bytes:
        return None, f"文件超过上限（{size} > {max_bytes} 字节）"
    stream, failure = _stream_reader(sandbox, path)
    if failure is not None:
        return None, failure
    if stream is not None:
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in stream:
                data = (
                    bytes(chunk)
                    if isinstance(chunk, (bytes, bytearray))
                    else str(chunk).encode("utf-8")
                )
                total += len(data)
                if total > max_bytes:
                    return None, f"流式读取超过上限（> {max_bytes} 字节）"
                chunks.append(data)
        except Exception as exc:  # noqa: BLE001 - surfaced as a failure reason
            return None, f"{type(exc).__name__}: {exc}"
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - cleanup must not mask the result
                    pass
        return b"".join(chunks), None
    if size is None:
        return None, "SDK 未提供文件大小且不支持流式读取，拒绝整份载入"
    try:
        content = sandbox.files.read(path, request_timeout=E2B_REQUEST_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - surfaced as a failure reason
        return None, f"{type(exc).__name__}: {exc}"
    data = bytes(content) if isinstance(content, (bytes, bytearray)) else str(content).encode("utf-8")
    if len(data) > max_bytes:
        return None, f"文件超过上限（{len(data)} > {max_bytes} 字节）"
    return bytes(data), None


#: A pi session header is one compact JSON line; this prefix is far more than
#: it needs and far less than a session file, so sniffing never loads the body.
_HEADER_SNIFF_BYTES = 16 * 1024


def _session_header(sandbox: Any, path: str) -> tuple[Mapping[str, Any] | None, str | None]:
    """The first-line header of a candidate session file, read bounded.

    Returns ``(header, None)`` for a pi session file, ``(None, None)`` when the
    first line is not a session header (the file simply does not compete for
    the main-session slot), and ``(None, reason)`` when the file cannot even
    be read — an unreadable candidate refuses the follow-up rather than being
    silently ignored.
    """
    stream, failure = _stream_reader(sandbox, path)
    if failure is not None:
        return None, failure
    prefix = b""
    if stream is not None:
        try:
            for chunk in stream:
                data = (
                    bytes(chunk)
                    if isinstance(chunk, (bytes, bytearray))
                    else str(chunk).encode("utf-8")
                )
                prefix += data
                if b"\n" in data or len(prefix) >= _HEADER_SNIFF_BYTES:
                    break
        except Exception as exc:  # noqa: BLE001 - surfaced as a failure reason
            return None, f"{type(exc).__name__}: {exc}"
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - cleanup must not mask the result
                    pass
    else:
        loaded, read_failure = _read_file_bounded(sandbox, path, ARTIFACT_MAX_BYTES)
        if read_failure is not None:
            return None, read_failure
        prefix = loaded if loaded is not None else b""
    line = prefix.split(b"\n", 1)[0]
    try:
        header = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(header, dict) or header.get("type") != "session":
        return None, None
    return header, None


def _session_candidates(
    sandbox: Any,
) -> tuple[str | None, list[str], str | None]:
    """``(main, others, failure)`` among the sandbox session dir's top level.

    The main session is the only ``.jsonl`` whose header has no
    ``parentSession``; branched (subagent fork) sessions carry one, and
    subdirectories never compete. ``failure`` is set when a listed candidate
    cannot even be read or two header-mains appear — the layout is then
    unexplained and neither continuation nor recovery may guess.
    """
    entries = sandbox.files.list(
        SANDBOX_SESSIONS, request_timeout=E2B_REQUEST_TIMEOUT_SECONDS
    )
    candidates: list[str] = []
    for entry in entries:
        if _entry_kind(entry) != "file":
            continue
        name = str(getattr(entry, "name", ""))
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.jsonl", name):
            candidates.append(name)
    main: str | None = None
    others: list[str] = []
    for name in sorted(candidates):
        header, failure = _session_header(sandbox, f"{SANDBOX_SESSIONS}/{name}")
        if failure is not None:
            return None, [], f"{name}: {failure}"
        if header is None or header.get("parentSession"):
            others.append(name)
            continue
        if main is not None:
            return None, [], "multiple main-session candidates"
        main = name
    return main, others, None


#: Bounds for the subagent mirror (branched sessions + ``subagent-artifacts``).
#: The mirror is not load-bearing for the run's continuation, so these sit
#: below the ACP state root's; per-file breaches are recorded, not hidden.
SUBAGENT_RECOVER_MAX_DEPTH = 3
SUBAGENT_RECOVER_MAX_FILES = 64
SUBAGENT_RECOVER_MAX_FILE_BYTES = UPLOAD_MAX_BYTES
SUBAGENT_RECOVER_MAX_TOTAL_BYTES = 32 * 1024 * 1024
SUBAGENT_ARTIFACTS_DIRNAME = "subagent-artifacts"


def _subtree_relative(root: str, path: str) -> str | None:
    """``path`` relative to ``root``, or ``None`` when it escapes it."""
    prefix = root.rstrip("/") + "/"
    if not path.startswith(prefix):
        return None
    relative = path[len(prefix):]
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return relative


def _mirror_subagent_data(
    sandbox: Any, tree: ArtifactTree, destination: Path
) -> tuple[list[Path], list[str]]:
    """Copy every subagent file the sandbox holds into the host mirror."""
    files: list[Path] = []
    failures: list[str] = []
    total = 0

    def mirror(remote_path: str, relative: str, known_size: int | None = None) -> bool:
        """Mirror one bounded file; ``False`` means a global cap tripped (stop)."""
        nonlocal total
        if len(files) >= SUBAGENT_RECOVER_MAX_FILES:
            failures.append(f"子代理产物超过文件数上限（{SUBAGENT_RECOVER_MAX_FILES}）")
            return False
        data, failure = _read_file_bounded(
            sandbox, remote_path, SUBAGENT_RECOVER_MAX_FILE_BYTES, known_size=known_size
        )
        if data is None:
            failures.append(f"子代理产物不可回收: {relative}: {failure}")
            return True
        total += len(data)
        if total > SUBAGENT_RECOVER_MAX_TOTAL_BYTES:
            failures.append(f"子代理产物超过总量上限（{SUBAGENT_RECOVER_MAX_TOTAL_BYTES} 字节）")
            return False
        try:
            tree.replace(
                relative,
                data,
                max_bytes=SUBAGENT_RECOVER_MAX_FILE_BYTES,
                create_parents=True,
            )
        except ArtifactError as exc:
            failures.append(f"子代理产物落盘被拒: {relative}: {exc}")
            return True
        files.append(destination / relative)
        return True

    main, others, failure = _session_candidates(sandbox)
    if failure is not None:
        failures.append(f"子代理会话布局无法判定: {failure}")
    else:
        for name in others:
            if not mirror(f"{SANDBOX_SESSIONS}/{name}", f"sessions/{name}"):
                return files, failures

    artifacts_root = f"{SANDBOX_SESSIONS}/{SUBAGENT_ARTIFACTS_DIRNAME}"
    from e2b import FileNotFoundException

    try:
        entries = sandbox.files.list(
            artifacts_root,
            depth=SUBAGENT_RECOVER_MAX_DEPTH,
            request_timeout=E2B_REQUEST_TIMEOUT_SECONDS,
        )
    except FileNotFoundException:
        # pi-subagents never creates this directory when no child wrote an
        # artifact. The SDK reports its absence as an error, not an empty list.
        return files, failures
    except Exception as exc:  # noqa: BLE001 - surfaced as a failure reason
        failures.append(f"无法列出子代理产物目录: {type(exc).__name__}: {exc}")
        return files, failures
    for entry in entries:
        if _entry_kind(entry) != "file":
            # Directories shape the walk; links are never followed (the same
            # rule the workspace recovery and ArtifactTree enforce).
            continue
        path = str(getattr(entry, "path", "") or "")
        relative = _subtree_relative(artifacts_root, path)
        if relative is None:
            failures.append(f"子代理产物路径无法映射，已跳过: {path[:200]!r}")
            continue
        known = getattr(entry, "size", None)
        if not mirror(
            path,
            f"artifacts/{relative}",
            known_size=known if isinstance(known, int) else None,
        ):
            break
    return files, failures


def _default_kill(api_key: str) -> Callable[[str], Any]:
    def kill(sandbox_id: str) -> Any:
        from e2b import Sandbox

        # The credential comes from the frozen host configuration (the same
        # source ``Sandbox.create`` receives), never from the process
        # environment: a deployment that only filled ``.env`` must be able to
        # authenticate the probe.
        return Sandbox.kill(sandbox_id, api_key=api_key)

    return kill


def confirm_sandbox_absent(
    sandbox_id: str,
    *,
    api_key: str = "",
    kill_factory: Callable[[str], Any] | None = None,
) -> tuple[bool, str]:
    """Confirm by the persisted sandbox id that the sandbox is gone.

    ``Sandbox.kill(sandbox_id)`` is the SDK's single-id operation: it returns
    ``True`` when the sandbox existed and is now terminated, and ``False`` when
    it was not found. Only the persisted identity is ever addressed — no
    listing, no name pattern, no other sandbox.

    Release is confirmed **only** by the SDK's own contract: a ``False`` return,
    or a not-found exception *type*. Every other failure — authentication, a
    gateway, a timeout, an unparseable answer — is uncertainty, so the hold
    stands; no exception message is ever read as evidence of release.
    """
    if not sandbox_id:
        return False, "缺少持久化的 sandbox_id"
    kill = kill_factory or _default_kill(api_key)
    # The SDK's single-id kill has no host deadline: a wedged call must surface
    # as an unconfirmed hold (uncertainty keeps the hold) instead of blocking
    # the reconciliation forever. The not-found contract is unchanged: only the
    # SDK's own not-found *types* confirm release.
    try:
        killed = kill_within_budget(lambda: kill(sandbox_id), what="沙箱")
    except Exception as exc:  # noqa: BLE001 - uncertainty keeps the hold
        if _is_not_found(exc):
            return True, f"SDK 确认沙箱不存在: {type(exc).__name__}"
        return False, f"沙箱查询失败: {type(exc).__name__}: {exc}"
    if killed is False:
        return True, "SDK 确认沙箱不存在"
    if killed is True:
        return True, "已按 sandbox_id 终止沙箱"
    return False, f"SDK 返回无法判定的结果: {killed!r}"


def _not_found_types() -> tuple[type[BaseException], ...]:
    """The SDK's not-found exception classes (empty when the SDK is unavailable)."""
    try:
        from e2b import NotFoundException, SandboxNotFoundException
    except Exception:  # noqa: BLE001 - the SDK is an optional dependency
        return ()
    return (SandboxNotFoundException, NotFoundException)


def _is_not_found(exc: BaseException) -> bool:
    """True only for the SDK's not-found exception types.

    Deliberately type-based: an authentication or gateway error whose text
    happens to mention "404" or "not found" must never confirm a release of a
    sandbox that may still be running and billing.
    """
    not_found = _not_found_types()
    return bool(not_found) and isinstance(exc, not_found)


def is_file_not_found(exc: BaseException) -> bool:
    """True only for the SDK's file-not-found exception type.

    The same type-based discipline as :func:`_is_not_found`, for the files
    API's ``remove``: a path that is already gone is a successful delete,
    while a transport error whose text mentions "not found" is a failure.
    """
    try:
        from e2b import FileNotFoundException
    except Exception:  # noqa: BLE001 - the SDK is an optional dependency
        return False
    return isinstance(exc, FileNotFoundException)


def sandbox_available() -> tuple[bool, str]:
    """Best-effort preflight used by the host before it accepts a run."""
    try:
        import e2b  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"e2b SDK 不可用: {exc}"
    if shutil.which("ssh") is None:  # pragma: no cover - ssh is ubiquitous
        return True, ""
    return True, ""
