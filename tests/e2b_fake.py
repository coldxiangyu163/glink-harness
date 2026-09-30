"""stdin 版 E2B 假沙箱（pi RPC 面），供各测试模块共享。

仿照 tests/test_kernel_acp_e2b.py 的 ``FakeAgentHandle``：``commands.run``
以 ``background=True, stdin=True`` 收到 RPC 命令链时，从命令链里解析 argv
并以真实子进程跑 tests/fixtures/fake_pi.py（argv[0] 的字面量 ``"pi"`` 替换
为 ``sys.executable fake_pi.py``，镜像内的包守卫不模拟），handle 实现
``send_stdin``/``close_stdin``/``__iter__``/``wait``/``kill``——即
:class:`~pi_glink.kernel.e2b_env.SandboxCommandProcess` 依赖的
``CommandHandle`` 表面。

``FakeFiles`` 保持字典语义（凭据/upload/recover 的既有断言原样成立）：

* ``write`` 把文本记入 ``written``，同时把字节镜像进一棵真实目录树——
  子进程读到的配置（如 ``auth.json``）与宿主 upload 的是同一份字节；
* ``read``/``get_info`` 优先 ``readable``/``sizes`` 字典种子，其次真实树；
* ``list`` 是字典种子名（``session_files``/``artifact_files``/
  ``workspace_files``）与真实树子项的并集——fake pi 在子进程里写出的
  ``report.md`` 因而对 recover 路径可见。

``fake_pi_env`` 是脚本化 fake pi 行为的通道（``FAKE_PI_WRITE_REPORT``、
``FAKE_PI_SESSION_ID``、``FAKE_PI_ECHO_SECRET`` 等，见 fixtures/fake_pi.py
的说明）；``stream_break_marker`` 让 handle 在交付含该标记的块后抛错，
模拟「连接断开、拿不到退出码」的断流。
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Iterator

from pi_glink.kernel.e2b_env import SANDBOX_PACKAGE_GUARD, SANDBOX_WORKSPACE

FAKE_PI = Path(__file__).resolve().parent / "fixtures" / "fake_pi.py"

# 事实类型对齐真实 SDK（tests/test_kernel_e2b_env.py 的先例：宿主的 not-found
# 判定是类型化的，fake 必须抛同一类型才能穿透 is_file_not_found/_is_not_found）。
from e2b import FileNotFoundException as FakeFileNotFoundException  # noqa: E402
from e2b import SandboxNotFoundException  # noqa: E402


class FakeSandboxNotFound(SandboxNotFoundException):
    """Stand-in for the SDK's ``SandboxNotFoundException`` (by-id lookup miss)."""


def translate(sandbox_path: str, root: Path) -> Path:
    """把一个 ``/home/user/…`` 沙箱路径映射到假沙箱的真实目录树。"""
    if not sandbox_path.startswith("/home/user"):
        raise AssertionError(f"unexpected sandbox path: {sandbox_path}")
    return root / sandbox_path.lstrip("/")


class _Stream:
    """Stand-in for the SDK's ``format="stream"`` reader (chunks + close)."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.closed = False

    def __iter__(self) -> Any:
        return iter(self._chunks)

    def close(self) -> None:
        self.closed = True


class _Info:
    def __init__(self, size: int) -> None:
        self.size = size


class FakeFiles:
    """字典语义优先、真实目录树兜底的沙箱文件面。"""

    def __init__(self, sandbox: "FakeSandbox") -> None:
        self._sandbox = sandbox

    def _real(self, sandbox_path: str) -> Path:
        return translate(sandbox_path, self._sandbox.root)

    def make_dir(self, path: str) -> None:
        self._sandbox.dirs.append(path)
        self._real(path).mkdir(parents=True, exist_ok=True)

    def write(self, path: str, content: Any) -> None:
        data = content if isinstance(content, bytes) else str(content).encode("utf-8")
        self._sandbox.written[path] = data.decode("utf-8")
        real = self._real(path)
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_bytes(data)

    def remove(self, path: str, **_kwargs: Any) -> None:
        """The SDK's ``files.remove``: a missing path raises not-found."""
        real = self._real(path)
        if not real.exists():
            raise FakeFileNotFoundException(path)
        if real.is_dir() and not real.is_symlink():
            real.rmdir()
        else:
            real.unlink()

    def _bytes(self, path: str) -> bytes:
        if path in self._sandbox.readable:
            value = self._sandbox.readable[path]
            return value if isinstance(value, bytes) else value.encode("utf-8")
        return self._real(path).read_bytes()

    def read(
        self,
        path: str,
        format: str | None = None,
        request_timeout: float | None = None,
    ) -> Any:
        self._sandbox.reads.append(path)
        self._sandbox.read_timeouts.append(request_timeout)
        data = self._bytes(path)
        if format == "stream":
            return _Stream([data])
        return data

    def get_info(self, path: str, request_timeout: float | None = None) -> Any:
        """The SDK's entry metadata: the host bounds the read by ``size`` first."""
        self._sandbox.info_timeouts.append(request_timeout)
        sizes = self._sandbox.sizes
        if path in sizes:
            return _Info(sizes[path])
        if path in self._sandbox.readable:
            value = self._sandbox.readable[path]
            size = len(value) if isinstance(value, bytes) else len(value.encode("utf-8"))
            return _Info(size)
        return _Info(self._real(path).stat().st_size)

    def list(
        self,
        path: str,
        depth: int = 1,
        request_timeout: float | None = None,
        **_kwargs: Any,
    ) -> list[Any]:
        self._sandbox.list_timeouts.append(request_timeout)
        readable = self._sandbox.readable
        sizes = self._sandbox.sizes
        types = self._sandbox.entry_types

        class Entry:
            def __init__(self, name: str, entry_path: str, real: Path | None) -> None:
                self.name = name
                self.path = entry_path
                # The real SDK reports an EntryInfo.type (an enum); absent
                # means a regular file.
                if entry_path in types:
                    self.type = types[entry_path]
                elif real is not None:
                    self.type = "dir" if real.is_dir() else "file"
                else:
                    self.type = "file"
                if entry_path in sizes:
                    self.size = sizes[entry_path]
                    return
                content = readable.get(entry_path)
                if content is not None:
                    self.size = (
                        len(content)
                        if isinstance(content, bytes)
                        else len(content.encode("utf-8"))
                    )
                elif real is not None and real.is_file():
                    self.size = real.stat().st_size
                else:
                    self.size = 0

        names = (
            self._sandbox.artifact_files
            if path == "/home/user/sessions/subagent-artifacts"
            else self._sandbox.session_files
            if path == "/home/user/sessions"
            else self._sandbox.workspace_files
            if path == "/home/user/workspace"
            else ()
        )
        entries = [Entry(name, f"{path}/{name}", None) for name in names]
        seen = {entry.name for entry in entries}
        real = self._real(path)
        if real.is_dir():
            for child in sorted(real.iterdir()):
                if child.name not in seen:
                    entries.append(Entry(child.name, f"{path}/{child.name}", child))
        return entries


class _Result:
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code


class _ExitException(Exception):
    """Stand-in for the SDK's ``CommandExitException`` (carries the code)."""

    def __init__(self, code: int) -> None:
        super().__init__(f"command exited with {code}")
        self.exit_code = code


class FakeAgentHandle:
    """Stand-in for the SDK's ``CommandHandle`` over a real subprocess.

    Implements the surface the E2B face uses: ``send_stdin``/``close_stdin``
    for input, iteration for streaming stdout/stderr chunks, ``kill`` and
    ``wait``. Every stdin frame is parsed into ``sandbox.stdin_frames`` so a
    test can assert which RPC commands reached the kernel (prompt/abort). The
    chunks are deliberately small so the shared LF-only framer is exercised
    the way a real sandbox stream exercises it.
    """

    def __init__(self, process: subprocess.Popen[bytes], sandbox: "FakeSandbox", *, chunk_size: int = 64) -> None:
        self._process = process
        self._sandbox = sandbox
        self._chunk_size = chunk_size
        self.pid = process.pid

    def _chunks(self) -> Iterator[tuple[str | None, str | None, None]]:
        assert self._process.stdout is not None
        while True:
            chunk = self._process.stdout.read(self._chunk_size)
            if not chunk:
                break
            yield chunk.decode("utf-8", "replace"), None, None
        assert self._process.stderr is not None
        for chunk in iter(lambda: self._process.stderr.read(self._chunk_size), b""):
            if chunk:
                yield None, chunk.decode("utf-8", "replace"), None

    def __iter__(self) -> Any:
        yield from self._chunks()

    def send_stdin(self, data: Any, request_timeout: float | None = None) -> None:
        assert self._process.stdin is not None
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        for line in payload.splitlines():
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(frame, dict):
                self._sandbox.stdin_frames.append(frame)
        self._process.stdin.write(payload)
        self._process.stdin.flush()

    def close_stdin(self, request_timeout: float | None = None) -> None:
        assert self._process.stdin is not None
        self._process.stdin.close()

    def kill(self) -> bool:
        self._process.kill()
        return True

    def wait(self) -> _Result:
        code = self._process.wait()
        if code != 0:
            raise _ExitException(code)
        return _Result(code)


class BrokenStreamHandle(FakeAgentHandle):
    """A handle whose stream dies mid-run: the SDK raises, no end event.

    交付含 ``marker`` 的块之后抛错（模拟「连接断开」），``wait`` 随之以
    「拿不到 end event」失败——沙箱进程本身可能仍在跑，但宿主从此拿不到它
    的退出码。
    """

    def __init__(self, process: subprocess.Popen[bytes], sandbox: "FakeSandbox", *, marker: bytes) -> None:
        super().__init__(process, sandbox)
        self._marker = marker

    def __iter__(self) -> Any:
        seen = b""
        for chunk in self._chunks():
            yield chunk
            if chunk[0]:
                seen += chunk[0].encode("utf-8")
                if self._marker in seen:
                    raise RuntimeError("stream disconnected before the end event")

    def wait(self) -> _Result:
        raise RuntimeError("command ended without an end event")


class FakeCommands:
    def __init__(self, sandbox: "FakeSandbox") -> None:
        self._sandbox = sandbox

    def run(self, cmd: str, **kwargs: Any) -> Any:
        sandbox = self._sandbox
        sandbox.command = cmd
        sandbox.command_kwargs = dict(kwargs)
        sandbox.command_envs = dict(kwargs.get("envs") or {})
        sandbox.command_timeout = kwargs.get("timeout")
        if kwargs.get("background") and kwargs.get("stdin"):
            return self._spawn(cmd, kwargs)
        # The pi face runs no other sandbox commands (the ACP face's chmod
        # hardening is its own module's concern).
        return _Result(0)

    def _spawn(self, cmd: str, kwargs: dict[str, Any]) -> FakeAgentHandle:
        sandbox = self._sandbox
        if "&& exec " not in cmd:
            raise AssertionError(f"kernel command is not the documented shape: {cmd!r}")
        argv = shlex.split(cmd.split("&& exec ", 1)[1])
        if argv[0] != SANDBOX_PACKAGE_GUARD or len(argv) < 2 or argv[1] != "pi":
            raise AssertionError(f"kernel command chain is not guard + pi: {argv!r}")
        env = dict(os.environ)
        for name, value in (kwargs.get("envs") or {}).items():
            # Sandbox paths mean nothing to a host subprocess: map them onto
            # the fake tree so the agent reads the uploaded files for real.
            env[name] = (
                str(translate(value, sandbox.root))
                if isinstance(value, str) and value.startswith("/home/user")
                else value
            )
        env.update(sandbox.fake_pi_env)
        cwd = translate(kwargs.get("cwd") or SANDBOX_WORKSPACE, sandbox.root)
        cwd.mkdir(parents=True, exist_ok=True)
        # argv[0] is the literal "pi" of the worker image's PATH; the fake
        # runs the protocol stand-in instead. Quoting already round-tripped
        # through shlex.join/shlex.split, so the logical argv is intact.
        process = subprocess.Popen(
            [sys.executable, str(FAKE_PI), *argv[2:]],
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        sandbox.agent_argv = argv[1:]
        sandbox.agent_process = process
        if sandbox.stream_break_marker is not None:
            return BrokenStreamHandle(process, sandbox, marker=sandbox.stream_break_marker)
        return FakeAgentHandle(process, sandbox)


class FakeSandbox:
    """Minimal stand-in for ``e2b.Sandbox`` driven by the tests.

    阶段 2（docs/adr/0020）的表面：实例互相独立之外，还有一个**类级注册表**
    （``FakeSandbox.REGISTRY``，sandbox_id → 实例），让类方法形态的
    ``FakeSandbox.connect(sandbox_id, ...)`` 能找回暂停的沙箱——与真实
    SDK 的 ``Sandbox.connect`` 类方法变体同形：paused 自动恢复、killed
    报 not found、幂等（已 running 直接返回）；``pause(keep_memory=...)``
    记录调用事实（幂等：已暂停返回 False）。``files.remove`` 补齐删除面
    （凭据副本清理路径）。
    """

    #: Class-level registry: every constructed fake registers itself by id.
    REGISTRY: dict[str, "FakeSandbox"] = {}

    def __init__(
        self,
        root: Path | None = None,
        *,
        sandbox_id: str = "fake-sandbox",
        report: str | None = "# 报告\n\n来自沙箱。\n",
        workspace_files: tuple[str, ...] | None = None,
        fake_pi_env: dict[str, str] | None = None,
        stream_break_marker: bytes | None = None,
        metadata: dict[str, str] | None = None,
        register: bool = True,
    ) -> None:
        self.root = root if root is not None else Path(tempfile.mkdtemp(prefix="fake-e2b-"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.sandbox_id = sandbox_id
        #: The SDK's lifecycle states (``running`` / ``paused`` / ``killed``).
        self.state = "running"
        #: ``create``-time metadata (the conversation lease's label hook).
        self.metadata = dict(metadata or {})
        #: Every ``pause`` call's kwargs, for the keep_memory assertion.
        self.pause_calls: list[dict[str, Any]] = []
        #: Every ``connect`` call's kwargs (the on_resume contract).
        self.connect_calls: list[dict[str, Any]] = []
        if register:
            FakeSandbox.REGISTRY[sandbox_id] = self
        self.dirs: list[str] = []
        self.written: dict[str, str] = {}
        self.readable: dict[str, Any] = {}
        self.workspace_files: tuple[str, ...] = (
            ("report.md",) if report is not None else ()
        ) if workspace_files is None else workspace_files
        self.session_files: tuple[str, ...] = ()
        #: Relative names inside ``/home/user/sessions/subagent-artifacts``
        #: (nested names contain ``/``); kinds via ``entry_types``.
        self.artifact_files: tuple[str, ...] = ()
        self.command = ""
        self.command_kwargs: dict[str, Any] = {}
        self.command_envs: dict[str, str] = {}
        #: ``timeout`` the host passed to ``commands.run`` (``None`` when the
        #: caller left the SDK default in place).
        self.command_timeout: float | None = None
        self.killed = False
        #: Paths the host asked to read (the bounded-read regressions).
        self.reads: list[str] = []
        #: ``request_timeout`` values the host passed to ``files.list`` /
        #: ``files.read`` / ``files.get_info`` (``None`` when the caller left
        #: the SDK default).
        self.list_timeouts: list[float | None] = []
        self.read_timeouts: list[float | None] = []
        self.info_timeouts: list[float | None] = []
        #: Entry sizes the SDK reports instead of the real content length.
        self.sizes: dict[str, int] = {}
        #: Entry kinds the SDK reports (path -> "file"/"dir"/"symlink"/enum).
        self.entry_types: dict[str, Any] = {}
        #: Scripting for the fake pi subprocess (``FAKE_PI_*`` entries).
        self.fake_pi_env = dict(fake_pi_env or {})
        #: When set, the command handle's stream dies after this marker.
        self.stream_break_marker = stream_break_marker
        #: The logical pi argv parsed back out of the sandbox command chain.
        self.agent_argv: list[str] = []
        self.agent_process: subprocess.Popen[bytes] | None = None
        #: Every JSONL frame the host wrote to the kernel's stdin, in order.
        self.stdin_frames: list[dict[str, Any]] = []
        if report is not None:
            self.readable["/home/user/workspace/report.md"] = report
        self.files = FakeFiles(self)
        self.commands = FakeCommands(self)

    def pause(self, keep_memory: bool | None = None, **_opts: Any) -> bool:
        """The SDK's pause: ``True`` just paused, ``False`` already paused."""
        if self.killed:
            raise FakeSandboxNotFound(self.sandbox_id)
        self.pause_calls.append({"keep_memory": keep_memory})
        if self.state == "paused":
            return False
        self.state = "paused"
        return True

    @classmethod
    def connect(
        cls,
        sandbox_id: str,
        timeout: int | None = None,
        *,
        on_resume: str = "restore",
        **opts: Any,
    ) -> "FakeSandbox":
        """The SDK's class-method connect: resume a paused sandbox by id."""
        sandbox = cls.REGISTRY.get(sandbox_id)
        if sandbox is None or sandbox.killed:
            raise FakeSandboxNotFound(sandbox_id)
        sandbox.connect_calls.append(
            {"timeout": timeout, "on_resume": on_resume, "opts": dict(opts)}
        )
        if sandbox.state == "paused":
            # ``reboot`` cold-boots from disk: the fake keeps the same tree.
            sandbox.state = "running"
        return sandbox

    @classmethod
    def kill_by_id(cls, sandbox_id: str, **_opts: Any) -> bool:
        """The SDK's class-method kill: ``False`` when not found."""
        sandbox = cls.REGISTRY.get(sandbox_id)
        if sandbox is None or sandbox.killed:
            return False
        sandbox.kill()
        return True

    def kill(self) -> None:
        if self.agent_process is not None and self.agent_process.poll() is None:
            self.agent_process.kill()
        self.killed = True
        self.state = "killed"
        FakeSandbox.REGISTRY.pop(self.sandbox_id, None)

    def get_info(self) -> Any:
        """The SDK's ``SandboxInfo`` shape (state + metadata), by-id facts."""

        class _Info:
            def __init__(self, sandbox: "FakeSandbox") -> None:
                self.sandbox_id = sandbox.sandbox_id
                self.state = sandbox.state
                self.metadata = dict(sandbox.metadata)

        return _Info(self)


class IdleHandle:
    """A command handle that stays alive until told to stop (no output).

    ``blocked=False`` ends the stream immediately: the process behind it has
    already exited with ``exit_code``.
    """

    def __init__(self, exit_code: int = 0, *, blocked: bool = True) -> None:
        self.pid = 4321
        self._gate = threading.Event()
        self._exit_code = exit_code
        if not blocked:
            self._gate.set()

    def __iter__(self) -> Any:
        self._gate.wait(timeout=10)
        return
        yield  # pragma: no cover - generator shape only

    def send_stdin(self, data: Any, request_timeout: float | None = None) -> None:
        pass

    def close_stdin(self, request_timeout: float | None = None) -> None:
        self._gate.set()

    def kill(self) -> bool:
        self._gate.set()
        return True

    def wait(self) -> _Result:
        if self._exit_code != 0:
            raise _ExitException(self._exit_code)
        return _Result(self._exit_code)


class BrokenHandle:
    """A handle whose stream never delivers anything and never ends well."""

    pid = 4321

    def __iter__(self) -> Any:
        raise RuntimeError("connection dropped")

    def send_stdin(self, data: Any, request_timeout: float | None = None) -> None:
        pass

    def close_stdin(self, request_timeout: float | None = None) -> None:
        pass

    def kill(self) -> bool:
        return True

    def wait(self) -> _Result:
        raise RuntimeError("command ended without an end event")
