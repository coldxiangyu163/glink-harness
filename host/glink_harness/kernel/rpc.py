"""Client for pi's documented headless RPC protocol.

pi runs as a child process (``pi --mode rpc``) and is spoken to over JSONL on
stdin/stdout. The host never imports pi and never links against it: a pi upgrade
is a version bump plus the protocol contract in this module. Process plumbing —
reader threads, LF-only framing, the close escalation, the stderr tail — is the
shared :class:`~glink_harness.kernel.rpc_transport.StdioRpcTransport`; what
this module adds is pi's vocabulary:

* ``response`` frames carry the ``id`` of the command that produced them;
* every other frame is an event and is delivered to ``on_event``.

Failures are explicit: an unknown command still produces a ``response`` frame
with ``success: false`` and an ``error`` string, and a child that dies before
answering surfaces as :class:`PiRpcClosed` instead of a silent hang.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .rpc_transport import StdioRpcTransport

logger = logging.getLogger("glink_harness.kernel.rpc")


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


class PiRpcClient(StdioRpcTransport):
    """Start pi once and talk to it until :meth:`close`.

    ``on_event`` is called for every non-response frame, in arrival order, on
    the reader thread. It must not block: the product side publishes into the
    run bus, which is a non-blocking fan-out.
    """

    whom = "pi"

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
        super().__init__(argv, cwd=cwd, env=env, on_stderr=on_stderr)
        self._on_event = on_event
        #: 进程工厂：默认 ``subprocess.Popen``；E2B 面注入一个返回
        #: ``SandboxCommandProcess`` 的 callable（沙箱内命令已在
        #: ``spawn_rpc`` 时启动，这里只把它的句柄交给客户端），协议驱动不变。
        self._popen = popen or subprocess.Popen
        self.event_count = 0

    # -- family hooks ----------------------------------------------------------

    def _launch_process(self) -> Any:
        logger.info("starting pi: %s", " ".join(self._argv))
        return self._popen(
            self._argv,
            cwd=str(self._cwd) if self._cwd is not None else None,
            env=self._env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

    def _dispatch_frame(self, frame: Mapping[str, Any]) -> None:
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

    def _closed_error(self, message: str) -> Exception:
        return PiRpcClosed(message)

    def _startup_error(self, message: str) -> Exception:
        return PiRpcError(message)

    def _fail_pending(self, error: Exception) -> None:
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

    def is_ready(self, *, timeout: float = 20.0) -> bool:
        """Ask the child for its state; used as a startup handshake."""
        return self.request({"type": "get_state"}, timeout=timeout).success

    # -- internals ------------------------------------------------------------

    def _next_id(self) -> str:
        with self._write_lock:
            self._id_counter += 1
            return f"req-{self._id_counter}"

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
