"""Client for the Agent Client Protocol over stdio.

ACP is JSON-RPC 2.0 between a client (this host) and an agent process. Over the
documented stdio transport messages are newline-delimited and MUST NOT contain
embedded newlines; U+2028/U+2029 inside a JSON string are content, not frame
boundaries. Framing therefore reuses :mod:`glink_harness.kernel.framing` — the same
LF-only splitter the pi face uses — instead of any locale-aware text reader.

Three frame kinds arrive on stdout:

* a **response** carries the ``id`` of a request this client sent;
* a **notification** (``method`` without ``id``) is delivered to
  ``on_notification`` in arrival order — ``session/update`` lives here;
* a **request** (``method`` with ``id``) is answered through ``on_request``.
  The handler is fail-closed: an unhandled method becomes a JSON-RPC
  "method not found" answer, and a handler that raises becomes an internal
  error, so an agent request is never left hanging on this side.

The client only speaks the wire protocol. Which methods to call, what to
answer, and how either maps onto product events belong to the ACP adapter and
projector.
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .framing import (
    RecordTooLarge,
    check_pending_record,
    split_lf_records,
    strip_trailing_cr_bytes,
)

logger = logging.getLogger("glink_harness.kernel.acp_rpc")

_STDERR_HISTORY = 200
_READ_CHUNK = 65536
#: How long :meth:`AcpRpcClient.stderr_tail` waits for the reader thread after
#: the child was reaped. The pipe's last bytes can still be in flight; the wait
#: is bounded so a report never blocks on a pipe that stays open.
_STDERR_DRAIN_TIMEOUT = 0.5

#: JSON-RPC error codes this client produces (the spec's reserved codes).
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603

#: Deadline for a command that must answer promptly (``initialize``,
#: ``session/new``). ``session/prompt`` answers only when the turn ends, so the
#: adapter waits on it without a client-side deadline.
REQUEST_TIMEOUT_SECONDS = 60.0


class AcpError(RuntimeError):
    """Base class for ACP protocol-level failures."""


class AcpClosed(AcpError):
    """The agent process is gone (or never started) and cannot answer."""


class AcpTimeout(AcpError):
    """No response frame arrived within the caller's deadline."""


class AcpProtocolError(AcpError):
    """A frame could not be used as the protocol requires."""


class AcpRequestError(AcpError):
    """The agent answered a request with a JSON-RPC error object.

    An agent may put the real cause in ``error.data`` and answer with a generic
    ``-32603 Internal error`` (dsh does, for a refused ``session/resume``). The
    message therefore carries :attr:`detail` — only a string ``data.details`` or
    ``data.message``, whitespace-collapsed and clipped to
    :data:`REQUEST_ERROR_DETAIL_CHARS` — never the raw ``data`` object. The text
    is agent-controlled: whoever shows it applies the run's redaction boundary.
    """

    def __init__(self, method: str, error: Mapping[str, Any]) -> None:
        code = error.get("code")
        message = error.get("message")
        data = error.get("data")
        detail = _request_error_detail(data)
        text = f"{method} failed: {code}: {message}"
        super().__init__(f"{text} ({detail})" if detail else text)
        self.method = method
        self.code = code if isinstance(code, int) else None
        self.message = message if isinstance(message, str) else None
        self.data = data
        self.detail = detail


#: Upper bound on the ``error.data`` text one request error carries.
REQUEST_ERROR_DETAIL_CHARS = 1000


def _request_error_detail(data: Any) -> str | None:
    """The bounded human-readable cause inside a JSON-RPC ``error.data``."""
    if not isinstance(data, Mapping):
        return None
    for key in ("details", "message"):
        value = data.get(key)
        if isinstance(value, str):
            text = " ".join(value.split())
            if text:
                if len(text) > REQUEST_ERROR_DETAIL_CHARS:
                    text = text[: REQUEST_ERROR_DETAIL_CHARS - 1] + "…"
                return text
    return None


class AcpMethodNotFound(AcpError):
    """Raised by an ``on_request`` handler to answer with ``-32601``.

    The host advertises no client capabilities (no ``fs/*``, no ``terminal/*``),
    so a compliant agent never sends those requests. A non-compliant or future
    one gets an explicit refusal instead of a hang.
    """

    def __init__(self, method: str) -> None:
        super().__init__(f"method not supported by this client: {method}")
        self.method = method


NotificationHandler = Callable[[Mapping[str, Any]], None]
RequestHandler = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


class AcpRpcClient:
    """Start one ACP agent process and talk JSON-RPC to it until :meth:`close`.

    ``on_notification`` runs on the reader thread and must not block; the
    adapter's hand-off into the run bus is a non-blocking fan-out, the same as
    the pi face. ``on_request`` also runs on the reader thread and must answer
    synchronously (its return value is the JSON-RPC ``result``).
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        on_notification: NotificationHandler | None = None,
        on_request: RequestHandler | None = None,
        on_stderr: Callable[[str], None] | None = None,
        spawn: Callable[[], Any] | None = None,
    ) -> None:
        self._argv = list(argv)
        self._cwd = cwd
        self._env = dict(env) if env is not None else None
        self._on_notification = on_notification
        self._on_request = on_request
        self._on_stderr = on_stderr
        #: How the agent process is started. ``None`` spawns ``argv`` on this
        #: host (the in-process and Docker faces). The E2B face hands in a
        #: factory for a sandbox background command, so the client speaks to the
        #: same wire shape without a second protocol implementation — the object
        #: it must return has the ``subprocess.Popen`` surface this class uses
        #: (stdin/stdout/stderr, poll/wait/terminate/kill).
        self._spawn = spawn
        self._process: Any | None = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        #: Outbound request id -> waiter. Keys are strings because a JSON-RPC id
        #: may be a string or a number and both must find the same waiter.
        self._pending: dict[str, queue.Queue[Any]] = {}
        self._stderr_history: list[str] = []
        self._stderr_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._closed = threading.Event()
        #: Whether :meth:`close` had to stop the agent itself (terminate/kill)
        #: instead of observing its own exit. A status this teardown caused says
        #: nothing about the agent's own result.
        self._stopped_process = False
        self._id_counter = 0
        self.notification_count = 0
        self.invalid_frame_count = 0
        #: Set when the stream violated the framing bound; the process is killed.
        self.protocol_error: str | None = None

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._process is not None:
            raise AcpError("client already started")
        if self._spawn is not None:
            # The execution face owns the launch (a sandbox command); the argv is
            # kept for diagnostics in the run record.
            logger.info("starting ACP agent in its execution face: %s", " ".join(self._argv))
            self._process = self._spawn()
        else:
            logger.info("starting ACP agent: %s", " ".join(self._argv))
            self._process = subprocess.Popen(
                self._argv,
                cwd=str(self._cwd) if self._cwd is not None else None,
                env=self._env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        self._reader = threading.Thread(
            target=self._read_stdout, name="acp-stdout", daemon=True
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._read_stderr, name="acp-stderr", daemon=True
        )
        self._stderr_reader.start()

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def exit_code(self) -> int | None:
        """The child's exit status once it has exited, else ``None``.

        ``poll()`` first, so an exited child is reaped here: the reader thread
        can observe the stdout EOF a moment before anything else calls
        ``wait()``, and a run that must report ``kernel_exit`` needs the real
        code rather than ``None``. A caller must not read this sample at the
        stream's end and call it the death's status: the child can close its
        stdout (the reader sees EOF) before it is reapable, so the sample can
        predate the exit. The status a report carries comes from reaping the
        child -- :meth:`close` does that and :attr:`stopped_process` says
        whether the status is the agent's own.
        """
        if self._process is None:
            return None
        self._process.poll()
        return self._process.returncode

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def stopped_process(self) -> bool:
        """Whether :meth:`close` had to terminate/kill the agent.

        A child that exits by itself after stdin closed keeps its own status;
        once this client terminates or kills it, the status that comes back is
        this teardown's doing and must not stand in for the agent's own.
        """
        return self._stopped_process

    def close(self, *, timeout: float = 5.0) -> int | None:
        """Stop the agent: close stdin, then terminate if it does not exit.

        The returned status is the child's own when it exited by itself; after
        a terminate/kill it is this teardown's doing, which
        :attr:`stopped_process` records so the caller can tell the two apart.
        """
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
            logger.warning("ACP agent did not exit after stdin closed; terminating")
            self._stopped_process = True
            process.terminate()
            try:
                return process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                return process.wait(timeout=timeout)
        finally:
            self._closed.set()
            self._fail_pending(AcpClosed("ACP agent process closed"))

    # -- commands -------------------------------------------------------------

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        """Send one JSON-RPC notification (no ``id``, no response)."""
        self._write(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": dict(params) if params is not None else {},
            }
        )

    def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = REQUEST_TIMEOUT_SECONDS,
    ) -> Mapping[str, Any]:
        """Send one request and wait for its response object.

        ``timeout=None`` waits until the agent answers or the process closes —
        the shape ``session/prompt`` needs, since it answers only when the turn
        ends and the caller owns the run deadline.
        """
        request_id = self._next_id()
        waiter: queue.Queue[Any] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[str(request_id)] = waiter
        try:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": dict(params) if params is not None else {},
                }
            )
            try:
                item = waiter.get() if timeout is None else waiter.get(timeout=timeout)
            except queue.Empty as exc:
                raise AcpTimeout(f"no response for {method!r} within {timeout}s") from exc
            if isinstance(item, AcpError):
                raise item
            error = item.get("error")
            if isinstance(error, Mapping):
                raise AcpRequestError(method, error)
            result = item.get("result")
            if not isinstance(result, Mapping):
                raise AcpProtocolError(f"{method} response is not an object")
            return result
        finally:
            with self._pending_lock:
                self._pending.pop(str(request_id), None)

    def stderr_tail(self, limit: int = 20) -> list[str]:
        """The last ``limit`` stderr lines, after a bounded drain of the pipe.

        Called after ``close()`` reaped the child: the reader thread may still
        be flushing the pipe's final bytes, and a crash report without them is
        the very diagnostic gap this tail exists for.
        """
        reader = self._stderr_reader
        if reader is not None and reader.is_alive():
            reader.join(timeout=_STDERR_DRAIN_TIMEOUT)
        with self._stderr_lock:
            return self._stderr_history[-limit:]

    # -- internals ------------------------------------------------------------

    def _next_id(self) -> int:
        with self._write_lock:
            self._id_counter += 1
            return self._id_counter

    def _write(self, payload: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise AcpClosed("ACP agent process is not running")
        if process.poll() is not None:
            raise AcpClosed(f"ACP agent exited with code {process.returncode}")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                process.stdin.write(data)
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise AcpClosed("ACP agent stdin is closed") from exc

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
            # LF-only framing: U+2028/U+2029 stay inside a frame.
            records, buffer = split_lf_records(buffer)
            for raw in records:
                self._handle_line(raw)
            try:
                # Only the unterminated tail is held: complete records already
                # left the buffer above, so a legitimate stream stays far below.
                check_pending_record(buffer)
            except RecordTooLarge as exc:
                self.protocol_error = str(exc)
                logger.error("ACP agent stdout frame overflow: %s", exc)
                buffer = b""
                self._kill_for_protocol_error()
                break
        if buffer.strip():
            self._handle_line(buffer)
        self._closed.set()
        self._fail_pending(AcpClosed("ACP agent stdout closed"))

    def _kill_for_protocol_error(self) -> None:
        """Stop a child whose stream broke the framing bound (never read further)."""
        process = self._process
        if process is None:
            return
        self._stopped_process = True
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
            logger.warning("ignoring non-JSON frame from ACP agent: %.200s", text)
            return
        if not isinstance(frame, dict):
            self.invalid_frame_count += 1
            return
        if isinstance(frame.get("method"), str):
            self._handle_incoming(frame)
            return
        if "id" in frame:
            self._dispatch_response(frame)
            return
        self.invalid_frame_count += 1
        logger.warning("ignoring frame without method or id from ACP agent")

    def _handle_incoming(self, frame: Mapping[str, Any]) -> None:
        method = str(frame.get("method"))
        params = frame.get("params")
        request_id = frame.get("id")
        if request_id is None:
            # A notification (``session/update`` and friends). It has no
            # response, so a broken consumer must not take the reader down.
            self.notification_count += 1
            handler = self._on_notification
            if handler is None:
                return
            try:
                handler(frame)
            except Exception:  # pragma: no cover - a broken consumer
                logger.exception("notification consumer raised; method=%s", method)
            return
        # A request from the agent. Answer exactly once, whatever happens.
        payload = params if isinstance(params, Mapping) else {}
        try:
            if self._on_request is None:
                raise AcpMethodNotFound(method)
            result = self._on_request(method, payload)
        except AcpMethodNotFound as exc:
            self._respond_error(request_id, METHOD_NOT_FOUND, str(exc))
        except Exception as exc:  # noqa: BLE001 - answered, never swallowed
            logger.exception("client request handler failed; method=%s", method)
            self._respond_error(
                request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"
            )
        else:
            try:
                self._write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": dict(result) if isinstance(result, Mapping) else {},
                    }
                )
            except AcpClosed:
                logger.warning(
                    "agent is gone before %s could be answered", method
                )

    def _respond_error(self, request_id: Any, code: int, message: str) -> None:
        try:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": code, "message": message},
                }
            )
        except AcpClosed:
            logger.warning("agent is gone before an error answer could be sent")

    def _dispatch_response(self, frame: Mapping[str, Any]) -> None:
        waiter: queue.Queue[Any] | None = None
        with self._pending_lock:
            waiter = self._pending.get(str(frame.get("id")))
        if waiter is None:
            logger.debug("unsolicited response frame from ACP agent")
            return
        try:
            waiter.put_nowait(frame)
        except queue.Full:  # pragma: no cover - one response per id
            pass

    def _fail_pending(self, error: AcpError) -> None:
        with self._pending_lock:
            waiters = list(self._pending.values())
        for waiter in waiters:
            try:
                waiter.put_nowait(error)
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

    def __enter__(self) -> "AcpRpcClient":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
