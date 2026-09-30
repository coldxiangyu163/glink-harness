"""Client for pi's documented headless RPC protocol.

pi runs as a child process (``pi --mode rpc``) and is spoken to over JSONL on
stdin/stdout. The host never imports pi and never links against it: a pi upgrade
is a version bump plus the protocol contract in this module.

Framing rules that matter (from pi's RPC documentation):

* records are separated by LF only; a trailing CR is tolerated;
* a generic line reader that also splits on U+2028/U+2029 is **not** protocol
  compliant, which is why stdout is read as bytes and split on ``b"\\n"`` here
  rather than through a locale-aware text wrapper;
* ``response`` frames carry the ``id`` of the command that produced them;
* every other frame is an event and is delivered to ``on_event``.

Failures are explicit: an unknown command still produces a ``response`` frame
with ``success: false`` and an ``error`` string, and a child that dies before
answering surfaces as :class:`PiRpcClosed` instead of a silent hang.
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .framing import (
    RecordTooLarge,
    check_pending_record,
    split_lf_records,
    strip_trailing_cr_bytes,
)

logger = logging.getLogger("pi_glink.kernel.rpc")

_STDERR_HISTORY = 200
_READ_CHUNK = 65536


class PiRpcError(RuntimeError):
    """Base class for protocol-level failures."""


class PiRpcClosed(PiRpcError):
    """The pi process is gone (or never started) and cannot answer."""


class PiRpcTimeout(PiRpcError):
    """No response frame arrived within the caller's deadline."""


@dataclass(frozen=True, slots=True)
class RpcResponse:
    """A ``response`` frame, normalised."""

    id: str | None
    command: str
    success: bool
    data: Mapping[str, Any]
    error: str | None
    raw: Mapping[str, Any]

    @classmethod
    def from_frame(cls, frame: Mapping[str, Any]) -> "RpcResponse":
        data = frame.get("data")
        return cls(
            id=frame.get("id"),
            command=str(frame.get("command") or ""),
            success=bool(frame.get("success")),
            data=data if isinstance(data, Mapping) else {},
            error=frame.get("error") if isinstance(frame.get("error"), str) else None,
            raw=frame,
        )


class PiRpcClient:
    """Start pi once and talk to it until :meth:`close`.

    ``on_event`` is called for every non-response frame, in arrival order, on
    the reader thread. It must not block: the product side publishes into the
    run bus, which is a non-blocking fan-out.
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        on_event: Callable[[Mapping[str, Any]], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        popen: Callable[..., Any] | None = None,
    ) -> None:
        self._argv = list(argv)
        self._cwd = cwd
        self._env = dict(env) if env is not None else None
        self._on_event = on_event
        self._on_stderr = on_stderr
        #: 进程工厂：默认 ``subprocess.Popen``；E2B 面注入一个返回
        #: ``SandboxCommandProcess`` 的 callable（沙箱内命令已在
        #: ``spawn_rpc`` 时启动，这里只把它的句柄交给客户端），协议驱动不变。
        self._popen = popen or subprocess.Popen
        self._process: subprocess.Popen[bytes] | None = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[str, queue.Queue[RpcResponse]] = {}
        self._stderr_history: list[str] = []
        self._stderr_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._closed = threading.Event()
        self._id_counter = 0
        self.event_count = 0
        self.invalid_frame_count = 0
        #: Set when the stream violated the framing bound; the process is killed.
        self.protocol_error: str | None = None

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._process is not None:
            raise PiRpcError("client already started")
        logger.info("starting pi: %s", " ".join(self._argv))
        self._process = self._popen(
            self._argv,
            cwd=str(self._cwd) if self._cwd is not None else None,
            env=self._env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._reader = threading.Thread(
            target=self._read_stdout, name="pi-rpc-stdout", daemon=True
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._read_stderr, name="pi-rpc-stderr", daemon=True
        )
        self._stderr_reader.start()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def exit_code(self) -> int | None:
        """The child's exit status once it has exited, else ``None``."""
        if self._process is None:
            return None
        return self._process.returncode

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def close(self, *, timeout: float = 5.0) -> int | None:
        """Stop pi: close stdin, then terminate if it does not exit in time."""
        process = self._process
        if process is None:
            return None
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("pi did not exit after stdin closed; terminating")
            process.terminate()
            try:
                return process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                return process.wait(timeout=timeout)
        finally:
            self._closed.set()
            self._fail_pending(PiRpcClosed("pi process closed"))

    # -- commands -------------------------------------------------------------

    def send(self, command: Mapping[str, Any]) -> str:
        """Write one command frame and return the id used for it."""
        request = dict(command)
        request_id = request.get("id") or self._next_id()
        request["id"] = request_id
        self._write(request)
        return str(request_id)

    def request(
        self, command: Mapping[str, Any], *, timeout: float = 30.0
    ) -> RpcResponse:
        """Send a command and wait for its ``response`` frame."""
        request = dict(command)
        request_id = str(request.get("id") or self._next_id())
        request["id"] = request_id
        waiter: queue.Queue[RpcResponse] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = waiter
        try:
            self._write(request)
            try:
                return waiter.get(timeout=timeout)
            except queue.Empty as exc:
                raise PiRpcTimeout(
                    f"no response for {request.get('type')!r} within {timeout}s"
                ) from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def stderr_tail(self, limit: int = 20) -> list[str]:
        with self._stderr_lock:
            return self._stderr_history[-limit:]

    def is_ready(self, *, timeout: float = 20.0) -> bool:
        """Ask the child for its state; used as a startup handshake."""
        return self.request({"type": "get_state"}, timeout=timeout).success

    # -- internals ------------------------------------------------------------

    def _next_id(self) -> str:
        with self._write_lock:
            self._id_counter += 1
            return f"req-{self._id_counter}"

    def _write(self, payload: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise PiRpcClosed("pi process is not running")
        if process.poll() is not None:
            raise PiRpcClosed(f"pi exited with code {process.returncode}")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                process.stdin.write(data)
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise PiRpcClosed("pi stdin is closed") from exc

    def _read_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        buffer = b""
        while True:
            try:
                chunk = process.stdout.read(_READ_CHUNK)
            except (ValueError, OSError):  # pragma: no cover - pipe torn down
                break
            if not chunk:
                break
            buffer += chunk
            # LF-only framing (docs/json.md): U+2028/U+2029 stay inside a frame.
            records, buffer = split_lf_records(buffer)
            for raw in records:
                self._handle_line(raw)
            try:
                # Only the unterminated tail is held: complete records already
                # left the buffer above, so a legitimate stream stays far below.
                check_pending_record(buffer)
            except RecordTooLarge as exc:
                self.protocol_error = str(exc)
                logger.error("pi stdout frame overflow: %s", exc)
                buffer = b""
                self._kill_for_protocol_error()
                break
        if buffer.strip():
            self._handle_line(buffer)
        self._closed.set()
        self._fail_pending(PiRpcClosed("pi stdout closed"))

    def _kill_for_protocol_error(self) -> None:
        """Stop a child whose stream broke the framing bound (never read further)."""
        process = self._process
        if process is None:
            return
        try:
            process.kill()
        except Exception:  # noqa: BLE001 - the process may already be gone
            logger.debug("kill after protocol error failed", exc_info=True)

    def _handle_line(self, raw: bytes) -> None:
        text = strip_trailing_cr_bytes(raw).decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            self.invalid_frame_count += 1
            logger.warning("ignoring non-JSON frame from pi: %.200s", text)
            return
        if not isinstance(frame, dict):
            self.invalid_frame_count += 1
            return
        if frame.get("type") == "response":
            self._dispatch_response(frame)
            return
        self.event_count += 1
        handler = self._on_event
        if handler is None:
            return
        try:
            handler(frame)
        except Exception:  # pragma: no cover - a broken consumer must not kill pi
            logger.exception("event consumer raised; frame type=%s", frame.get("type"))

    def _dispatch_response(self, frame: Mapping[str, Any]) -> None:
        response = RpcResponse.from_frame(frame)
        waiter: queue.Queue[RpcResponse] | None = None
        if response.id is not None:
            with self._pending_lock:
                waiter = self._pending.get(str(response.id))
        if waiter is None:
            logger.debug("unsolicited response frame: %s", response.command)
            return
        try:
            waiter.put_nowait(response)
        except queue.Full:  # pragma: no cover - one response per id
            pass

    def _fail_pending(self, error: PiRpcError) -> None:
        with self._pending_lock:
            waiters = list(self._pending.values())
        for waiter in waiters:
            try:
                waiter.put_nowait(RpcResponse(
                    id=None,
                    command="",
                    success=False,
                    data={},
                    error=str(error),
                    raw={"type": "response", "success": False, "error": str(error)},
                ))
            except queue.Full:  # pragma: no cover
                pass

    def _read_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        for raw in process.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if not line:
                continue
            with self._stderr_lock:
                self._stderr_history.append(line)
                if len(self._stderr_history) > _STDERR_HISTORY:
                    del self._stderr_history[:-_STDERR_HISTORY]
            if self._on_stderr is not None:
                try:
                    self._on_stderr(line)
                except Exception:  # pragma: no cover
                    logger.exception("stderr consumer raised")

    def __enter__(self) -> "PiRpcClient":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def wait_for_idle(
    client: PiRpcClient, *, timeout: float, poll: float = 0.25
) -> Mapping[str, Any] | None:
    """Poll ``get_state`` until pi reports it is no longer streaming.

    Returns the last state, or ``None`` when the deadline passes. Used by
    callers that need a settled session rather than a settled event stream.
    """
    deadline = time.monotonic() + timeout
    last: Mapping[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.request({"type": "get_state"}, timeout=min(10.0, timeout))
        if not response.success:
            return None
        last = response.data
        if not last.get("isStreaming") and not last.get("isCompacting"):
            return last
        time.sleep(poll)
    return last
