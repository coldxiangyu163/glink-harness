"""E2B execution face: pi runs inside a cloud sandbox.

**RPC 通道（GLINK-ARCH-3 收敛）。** 本面与其余两面说同一个协议：pi 以
``pi --mode rpc`` 作为沙箱 *background* 命令启动（``stdin=True``），宿主把
``CommandHandle`` 包装成 ``subprocess.Popen`` 形状的
:class:`~pi_glink.kernel.acp_e2b_env.SandboxCommandProcess` 交给共享的
``PiRpcClient``——只有载体变化，帧分发、投影与终态判定复用同一套代码
（ACP 面同构先例：``E2BAcpSandboxClient.spawn()``）。双向通道的可行性（
含运行中 abort 被受理、close_stdin 语义）由 spike 实测钉死，证据在
``docs/evidence/pi-e2b-rpc-stdin-spike.txt``。

**断流即失败（与旧 tee 路径的取舍）。** RPC 通道没有第二份事件副本：流即
权威，沙箱流断开（拿不到退出码）时 run 判 ``execution_error``——不补发、
不做 ``get_state`` 对账（响应帧走同一条已断的 stdout，且 SDK 重连是否重放
未规定）。旧路径把 stdout ``tee`` 进 ``events.jsonl`` 作完整性权威、断流可
补齐，那套机制（tee/marker/补读）已整体删除；换来的是双向通道：运行中
abort、``session_ready`` 前即知会话身份。架构文档 §5.2 记录这一取舍。

**Credential flow (recorded per the task contract).**

* The model credential is uploaded inside ``auth.json`` — pi's official credential
  store — so the sandbox kernel never receives it through its environment: a shell
  tool inside the sandbox cannot read it with ``env``. The file exists only for the
  sandbox lifetime (destroyed on ``kill()``).
* The pi-web-access credential is written to ``/home/user/config/web-search.json``
  inside the sandbox. That file lives for the sandbox lifetime only (destroyed on
  ``kill()``); the host keeps just its own local copy. This is deliberate: the
  plugin resolves that file from ``PI_CODING_AGENT_DIR`` and has no
  environment-variable form for it. A sandbox without the file still works
  (keyless Exa MCP search).

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
import shlex
import shutil
from pathlib import Path
from typing import Any, Callable, Mapping

from ..artifacts import ArtifactError, ArtifactTree
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


class SandboxError(RuntimeError):
    """Raised when the sandbox cannot be created or the run cannot start."""


class E2BSandboxClient:
    """Own one sandbox for one run.

    ``sandbox_factory`` is injectable so the runner can be exercised without a
    cloud dependency.
    """

    def __init__(
        self,
        config: HostConfig,
        runtime: AgentRuntime,
        *,
        run_id: str,
        workspace: Path,
        sandbox_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.runtime = runtime
        self.run_id = run_id
        self.workspace = workspace
        self._sandbox_factory = sandbox_factory
        self.sandbox: Any | None = None
        self.uploaded: list[str] = []
        #: ``(relative path, sha256)`` for everything delivered to the sandbox.
        self.upload_manifest: list[tuple[str, str]] = []
        self.sandbox_id: str = ""
        #: Lifetime the sandbox was created with; the kernel command must be
        #: allowed to run at least that long (see ``_command_timeout``).
        self.sandbox_timeout: int = 0

    # -- lifecycle ------------------------------------------------------------

    def start(self, *, timeout_seconds: int | None = None) -> None:
        if self.sandbox is not None:
            raise SandboxError("sandbox already started")
        sandbox, self.sandbox_timeout = _create_sandbox(
            self.config,
            template=self.config.e2b_template,
            timeout_seconds=timeout_seconds,
            sandbox_factory=self._sandbox_factory,
            envs=self._model_envs(),
        )
        self.sandbox = sandbox
        self.sandbox_id = str(getattr(sandbox, "sandbox_id", ""))

    def upload_config(self) -> None:
        """Upload the run's rendered configuration directory (see module docstring).

        Every file in the runtime directory is delivered with its relative path
        intact, so the sandboxed kernel reads the same configuration the local
        faces read. The manifest lets a test (or an operator) verify what was
        actually delivered.
        """
        sandbox = self._require_sandbox()
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
        sandbox = self._require_sandbox()
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
        sandbox = self._require_sandbox()
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
        sandbox = self._require_sandbox()
        destination.mkdir(parents=True, exist_ok=True)
        try:
            tree = ArtifactTree(destination)
        except ArtifactError as exc:
            return [], [f"子代理产物落点不可用: {exc}"]
        with tree:
            return _mirror_subagent_data(sandbox, tree, destination)

    def spawn_rpc(self, argv: list[str]) -> "SandboxCommandProcess":
        """以 RPC 模式在沙箱内启动 pi，返回 ``subprocess.Popen`` 形状的进程门面。

        命令链（ACP 面 ``command()`` 先例）：先验包守卫——模板早于宿主时以
        exit 78 失败（``kernel_exit``），否则守卫验包后 ``exec`` pi——再
        ``cd`` 到工作区并 ``exec`` 整条 argv。``shlex.join`` 负责
        ``--append-system-prompt`` 文本的引用；prompt 本体走 stdin 的
        ``prompt`` 帧，从不出现在命令行（旧的 base64 prompt/system 编码机制
        已随 ``--mode json`` 路径整体废除）。

        ``background=True, stdin=True`` 是双向 stdio 通道（spike 证据
        ``docs/evidence/pi-e2b-rpc-stdin-spike.txt``）。``timeout`` 必须与
        沙箱寿命同长：SDK 默认 60 秒的命令活动时限会在沙箱仍存活时杀掉每个
        研究 run。
        """
        sandbox = self._require_sandbox()
        command = (
            # 模板早于宿主时没有守卫脚本：带明确原因失败，而不是让 pi 在
            # 没有 web_search 的情况下起跑。
            f"test -x {SANDBOX_PACKAGE_GUARD} || {{ "
            f"echo 'pi-glink: 沙箱模板缺少预装包守卫 {SANDBOX_PACKAGE_GUARD}，"
            "请用 scripts/build-e2b-template.py 重建模板' >&2; exit 78; }"
            f" && cd {SANDBOX_WORKSPACE}"
            f" && exec {shlex.join([SANDBOX_PACKAGE_GUARD, *argv])}"
        )
        handle = sandbox.commands.run(
            command,
            background=True,
            stdin=True,
            envs=self._model_envs(),
            cwd=SANDBOX_WORKSPACE,
            timeout=self._command_timeout(),
        )
        # 延迟 import：acp_e2b_env 自本模块导入常量与助手，模块级互领会成环。
        from .acp_e2b_env import SandboxCommandProcess

        return SandboxCommandProcess(handle, sandbox_id=self.sandbox_id)

    def _command_timeout(self) -> int:
        """How long the sandboxed kernel command may run, in seconds.

        Matched to the sandbox's own lifetime: a shorter bound would kill the
        kernel (and discard its work) while the sandbox is still usable, and a
        longer one is meaningless because the sandbox dies first. The value is
        frozen from the same calculation that created the sandbox, so the two
        cannot drift apart.
        """
        if self.sandbox_timeout > 0:
            return self.sandbox_timeout
        return int(self.config.task_timeout_seconds) + 300

    def recover_artifacts(self, destination: Path) -> tuple[list[Path], list[str]]:
        """Pull the sandbox workspace back into the host run workspace.

        Only direct children with simple names are recovered, each one bounded;
        anything unreadable (a directory), oversized, or named with a path
        separator is recorded as a failure instead of being guessed at.
        """
        return _recover_workspace(self._require_sandbox(), destination)

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

    # -- internals ------------------------------------------------------------

    def _require_sandbox(self) -> Any:
        if self.sandbox is None:
            raise SandboxError("sandbox 未启动")
        return self.sandbox

    def _model_envs(self) -> dict[str, str]:
        """Environment for the sandboxed kernel — deliberately credential-free.

        The model credential travels in the uploaded ``auth.json`` (pi's own
        store), exactly like the in-process face, so ``env`` inside the sandbox
        cannot print it.
        """
        return {
            "PI_CODING_AGENT_DIR": SANDBOX_CONFIG,
            "PI_CODING_AGENT_SESSION_DIR": SANDBOX_SESSIONS,
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
        }


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


def _recover_workspace(sandbox: Any, destination: Path) -> tuple[list[Path], list[str]]:
    """Copy the sandbox workspace's direct file children into ``destination``.

    Only direct children with simple names are recovered, each one bounded;
    anything unreadable (a directory), oversized, or named with a path
    separator is recorded as a failure instead of being guessed at. Shared by
    the pi and ACP faces (both write their deliverable into the same sandbox
    workspace).
    """
    destination.mkdir(parents=True, exist_ok=True)
    try:
        entries = sandbox.files.list(SANDBOX_WORKSPACE)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller
        return [], [f"无法列出沙箱工作区: {type(exc).__name__}: {exc}"]
    try:
        tree = ArtifactTree(destination)
    except ArtifactError as exc:
        return [], [f"宿主工作区不可用: {exc}"]
    files: list[Path] = []
    failures: list[str] = []
    with tree:
        for entry in entries:
            name = str(getattr(entry, "name", None) or "")
            path = str(getattr(entry, "path", None) or f"{SANDBOX_WORKSPACE}/{name}")
            if not name:
                continue
            # Path semantics are checked before the hidden-file skip: a name
            # that tries to escape ("../x") starts with a dot and must be
            # reported, not silently ignored.
            if "/" in name or "\\" in name or name in {".", ".."}:
                failures.append(f"跳过非法产物名: {name!r}")
                continue
            kind = _entry_kind(entry)
            if kind == "dir":
                # Directories are not deliverables; only regular entries are
                # recovered (and an unreadable regular entry is reported).
                continue
            if kind == "symlink":
                # The host never follows links — the same rule ArtifactTree
                # enforces on every read and write. A linked artifact is
                # skipped rather than resolved and is not a run failure.
                continue
            if name.startswith("."):
                continue
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
            try:
                tree.replace(name, data, max_bytes=ARTIFACT_MAX_BYTES)
            except (ArtifactError, OSError) as exc:
                failures.append(f"产物写入失败: {name}: {exc}")
                continue
            files.append(destination / name)
    return files, failures


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
        info = get_info(path)
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
        return read(path, format="stream"), None
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
        content = sandbox.files.read(path)
    except Exception as exc:  # noqa: BLE001 - surfaced as a failure reason
        return None, f"{type(exc).__name__}: {exc}"
    data = content if isinstance(content, (bytes, bytearray)) else str(content).encode("utf-8")
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
        data, read_failure = _read_file_bounded(sandbox, path, ARTIFACT_MAX_BYTES)
        if read_failure is not None:
            return None, read_failure
        prefix = data or b""
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
    entries = sandbox.files.list(SANDBOX_SESSIONS)
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
    try:
        entries = sandbox.files.list(artifacts_root, depth=SUBAGENT_RECOVER_MAX_DEPTH)
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
    try:
        killed = kill(sandbox_id)
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


def sandbox_available() -> tuple[bool, str]:
    """Best-effort preflight used by the host before it accepts a run."""
    try:
        import e2b  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"e2b SDK 不可用: {exc}"
    if shutil.which("ssh") is None:  # pragma: no cover - ssh is ubiquitous
        return True, ""
    return True, ""
