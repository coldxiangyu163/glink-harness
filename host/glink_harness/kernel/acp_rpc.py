"""Client for the Agent Client Protocol over stdio.

ACP is JSON-RPC 2.0 between a client (this host) and an agent process. Over the
documented stdio transport messages are newline-delimited and MUST NOT contain
embedded newlines; U+2028/U+2029 inside a JSON string are content, not frame
boundaries — the shared LF-only framing in
:class:`~glink_harness.kernel.rpc_transport.StdioRpcTransport` (the same
splitter the pi face uses) is what that requires, never a locale-aware text
reader.

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

import logging
import queue
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .rpc_transport import StdioRpcTransport

logger = logging.getLogger("glink_harness.kernel.acp_rpc")

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


class AcpRpcClient(StdioRpcTransport):
    """Start one ACP agent process and talk JSON-RPC to it until :meth:`close`.

    ``on_notification`` runs on the reader thread and must not block; the
    adapter's hand-off into the run bus is a non-blocking fan-out, the same as
    the pi face. ``on_request`` also runs on the reader thread and must answer
    synchronously (its return value is the JSON-RPC ``result``).
    """

    whom = "ACP agent"

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
        super().__init__(argv, cwd=cwd, env=env, on_stderr=on_stderr)
        self._on_notification = on_notification
        self._on_request = on_request
        #: How the agent process is started. ``None`` spawns ``argv`` on this
        #: host (the in-process and Docker faces). The E2B face hands in a
        #: factory for a sandbox background command, so the client speaks to the
        #: same wire shape without a second protocol implementation — the object
        #: it must return has the ``subprocess.Popen`` surface this class uses
        #: (stdin/stdout/stderr, poll/wait/terminate/kill).
        self._spawn = spawn
        self.notification_count = 0

    # -- family hooks ----------------------------------------------------------

    def _launch_process(self) -> Any:
        if self._spawn is not None:
            # The execution face owns the launch (a sandbox command); the argv is
            # kept for diagnostics in the run record.
            logger.info(
                "starting ACP agent in its execution face: %s", " ".join(self._argv)
            )
            return self._spawn()
        logger.info("starting ACP agent: %s", " ".join(self._argv))
        return subprocess.Popen(
            self._argv,
            cwd=str(self._cwd) if self._cwd is not None else None,
            env=self._env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

    def _dispatch_frame(self, frame: Mapping[str, Any]) -> None:
        if isinstance(frame.get("method"), str):
            self._handle_incoming(frame)
            return
        if "id" in frame:
            self._dispatch_response(frame)
            return
        self.invalid_frame_count += 1
        logger.warning("ignoring frame without method or id from ACP agent")

    def _closed_error(self, message: str) -> Exception:
        return AcpClosed(message)

    def _startup_error(self, message: str) -> Exception:
        return AcpError(message)

    def _fail_pending(self, error: Exception) -> None:
        with self._pending_lock:
            waiters = list(self._pending.values())
        for waiter in waiters:
            try:
                waiter.put_nowait(error)
            except queue.Full:  # pragma: no cover
                pass

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

    # -- internals ------------------------------------------------------------

    def _next_id(self) -> int:
        with self._write_lock:
            self._id_counter += 1
            return self._id_counter

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
