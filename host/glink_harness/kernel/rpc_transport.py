"""Shared stdio transport for one JSON-lines child process.

Both harness families speak to their kernel as a child process over
newline-delimited JSON on stdin/stdout: pi through its documented headless RPC
mode, ACP through the JSON-RPC 2.0 stdio transport. The protocol vocabularies
differ (response/event frames versus JSON-RPC requests/notifications/responses);
the process plumbing does not. This module owns that plumbing once — starting
the child, the reader threads, LF-only framing with its record bound, the close
escalation (stdin close → terminate → kill), the bounded stderr tail and the
pending-waiter bookkeeping — and leaves frame interpretation
(:meth:`StdioRpcTransport._dispatch_frame`) and the family's error vocabulary
(:meth:`StdioRpcTransport._closed_error`) to the subclasses
(:class:`~glink_harness.kernel.rpc.PiRpcClient`,
:class:`~glink_harness.kernel.acp_rpc.AcpRpcClient`).

Framing rules shared by both protocols (pi's RPC documentation, the ACP stdio
transport spec): records are separated by LF only; a trailing CR is tolerated;
U+2028/U+2029 inside a JSON string are content, not frame boundaries, which is
why stdout is read as bytes and split on ``b"\\n"`` (via
:mod:`glink_harness.kernel.framing`) rather than through a locale-aware text
wrapper.

The exit-status and stderr-tail semantics here follow the stricter of the two
former implementations (the ACP one, pinned by the crash-race regressions in
``tests/test_kernel_acp_race.py``): :attr:`exit_code` reaps first so an exited
child is never reported as status-less, and :meth:`stderr_tail` gives the
stderr reader a bounded drain after the child was reaped.
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Mapping, Self, Sequence

from .framing import (
    RecordTooLarge,
    check_pending_record,
    split_lf_records,
    strip_trailing_cr_bytes,
)

logger = logging.getLogger("glink_harness.kernel.rpc_transport")

_STDERR_HISTORY = 200
_READ_CHUNK = 65536
#: How long :meth:`StdioRpcTransport.stderr_tail` waits for the reader thread
#: after the child was reaped. The pipe's last bytes can still be in flight;
#: the wait is bounded so a report never blocks on a pipe that stays open.
_STDERR_DRAIN_TIMEOUT = 0.5


class StdioRpcTransport(ABC):
    """One child process spoken to over LF-framed JSON until :meth:`close`.

    ``on_stderr`` runs on the stderr reader thread and must not block; the
    same holds for whatever the family's frame dispatch calls on the stdout
    reader thread (the adapters' hand-off into the run bus is a non-blocking
    fan-out).

    A subclass provides: how the child is started (:meth:`_launch_process`),
    how a parsed frame is classified (:meth:`_dispatch_frame`), and the
    family's error vocabulary (:meth:`_closed_error`,
    :meth:`_startup_error`, :meth:`_fail_pending`).
    """

    #: What the child is called in logs, thread names and error strings
    #: ("pi", "ACP agent").
    whom: str = "child"

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> None:
        self._argv = list(argv)
        self._cwd = cwd
        self._env = dict(env) if env is not None else None
        self._on_stderr = on_stderr
        self._process: Any | None = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        #: Outbound request id -> waiter; the value shape is the family's own
        #: (a response object for pi, a response frame or an error for ACP).
        #: Keys are strings because a wire id may be a string or a number and
        #: both must find the same waiter.
        self._pending: dict[str, queue.Queue[Any]] = {}
        self._stderr_history: list[str] = []
        self._stderr_lock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._closed = threading.Event()
        #: Whether teardown had to stop the child itself (terminate/kill)
        #: instead of observing its own exit. A status this teardown caused
        #: says nothing about the agent's own result.
        self._stopped_process = False
        self._id_counter = 0
        self.invalid_frame_count = 0
        #: Set when the stream violated the framing bound; the process is killed.
        self.protocol_error: str | None = None

    # -- family hooks ------------------------------------------------------------

    @abstractmethod
    def _launch_process(self) -> Any:
        """Start the child and return its ``subprocess.Popen``-shaped object."""

    @abstractmethod
    def _dispatch_frame(self, frame: Mapping[str, Any]) -> None:
        """Classify and deliver one parsed protocol frame."""

    @abstractmethod
    def _closed_error(self, message: str) -> Exception:
        """The family's error for "the child is gone and cannot answer"."""

    @abstractmethod
    def _startup_error(self, message: str) -> Exception:
        """The family's error for client misuse before the child exists."""

    @abstractmethod
    def _fail_pending(self, error: Exception) -> None:
        """Answer every waiter when the child can no longer respond."""

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        if self._process is not None:
            raise self._startup_error("client already started")
        self._process = self._launch_process()
        self._reader = threading.Thread(
            target=self._read_stdout, name=f"{self.whom}-rpc-stdout", daemon=True
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._read_stderr, name=f"{self.whom}-rpc-stderr", daemon=True
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
        """Whether :meth:`close` (or a protocol-error kill) stopped the child.

        A child that exits by itself after stdin closed keeps its own status;
        once this client terminates or kills it, the status that comes back is
        this teardown's doing and must not stand in for the agent's own.
        """
        return self._stopped_process

    def close(self, *, timeout: float = 5.0) -> int | None:
        """Stop the child: close stdin, then terminate if it does not exit.

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
            logger.warning("%s did not exit after stdin closed; terminating", self.whom)
            self._stopped_process = True
            process.terminate()
            try:
                return process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                return process.wait(timeout=timeout)
        finally:
            self._closed.set()
            self._fail_pending(self._closed_error(f"{self.whom} process closed"))

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

    # -- shared internals ----------------------------------------------------------

    def _write(self, payload: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise self._closed_error(f"{self.whom} process is not running")
        if process.poll() is not None:
            raise self._closed_error(f"{self.whom} exited with code {process.returncode}")
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                process.stdin.write(data)
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise self._closed_error(f"{self.whom} stdin is closed") from exc

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
                logger.error("%s stdout frame overflow: %s", self.whom, exc)
                buffer = b""
                self._kill_for_protocol_error()
                break
        if buffer.strip():
            self._handle_line(buffer)
        self._closed.set()
        self._fail_pending(self._closed_error(f"{self.whom} stdout closed"))

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
            logger.warning("ignoring non-JSON frame from %s: %.200s", self.whom, text)
            return
        if not isinstance(frame, dict):
            self.invalid_frame_count += 1
            return
        self._dispatch_frame(frame)

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

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
