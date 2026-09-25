"""E2B execution face: pi runs inside a cloud sandbox.

**Why this face speaks a different protocol.** The E2B SDK exposes one-shot
command execution with streamed stdout, not a bidirectional pipe, so this face
uses pi's single-shot JSON event stream (``pi --mode json "<prompt>"``) instead
of the RPC protocol used by the in-process and Docker faces. That is the
documented protocol fallback in the objective, and the event vocabulary and
projection are identical, so the browser contract does not change.

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
``settings.json``, ``extensions/``, ``skills/``, ``prompts/``), with the same
relative paths the in-process and Docker faces use. The upload records a manifest
of ``(relative path, sha256)`` so the delivery is verifiable instead of "the
current directory happened to be empty". ``settings.json`` renders the prebaked
pi packages as image-local paths (``/opt/pi-packages/node_modules/…``); the
sandbox command runs the image's package guard before pi, because pi silently
ignores a missing local package and a research run without web_search must fail
loudly instead.

**The ACP face shares the sandbox (M3b).** The same ``Sandbox.create``
lifecycle, credential upload and artifact recovery carry the ACP family: the
agent runs as a *background* command with ``stdin=True`` and the host frames the
CommandHandle's stdout with the shared LF-only splitter, so the ACP client,
projection and verdict table are the same code as the in-process and Docker
faces. ``SandboxCommandProcess`` adapts that handle to the ``subprocess.Popen``
shape ``AcpRpcClient`` is written against; the measured framing facts
(ordering, byte fidelity, ≥64 KiB frames, ``close_stdin``) are in
``docs/evidence/acp-m3-e2b-framing.txt``.

**Live event stream (corrected by GLINK-E2B-LIVE-01).** The SDK's streaming
interface for a background command is the ``CommandHandle`` returned by
``commands.run(background=True, …)``: iterating it yields ``(stdout, stderr,
pty)`` chunks while the command runs, and ``CommandHandle.wait(on_stdout=…)``
delivers them to a callback. ``background=True`` ignores callbacks passed to
``run()`` itself, which is why the earlier face concluded no callback fires and
replayed a file after the command settled — a usage misjudgment, since stdout
was also redirected into that file and there was nothing to stream. The sandbox
command now pipes pi's stdout through ``tee`` into ``/home/user/events.jsonl``:
the same bytes reach the host live *and* remain a complete file. The host
projects every complete LF-framed record as it arrives, and when the stream
drops before an end event it waits for the command to finish (the command
writes its exit code to ``/home/user/pi-exit-code.txt`` after the pipeline) and
delivers only the records the stream had not handed over — dropping neither a
frame nor a duplicate. Host-side streaming and recovery are covered by the
regressions; real-sandbox live timing is not yet validated. See
``docs/architecture.md`` §5 and §5.2.
"""

from __future__ import annotations

import base64
import codecs
import hashlib
import json
import logging
import os
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from ..artifacts import ArtifactError, ArtifactTree
from ..config import ConfigError, HostConfig
from .acp_env import (
    ACP_RECOVERED_DIRNAME,
    INHERIT_MAX_FILE_BYTES,
    INHERIT_MAX_FILES,
    INHERIT_MAX_TOTAL_BYTES,
    AcpRuntime,
    build_acp_argv,
    credential_secrets_from_text,
)
from .agent_env import IMAGE_PACKAGE_GUARD, AgentRuntime
from .framing import strip_trailing_cr

logger = logging.getLogger("pi_glink.kernel.e2b_env")

#: Sandbox paths (E2B runs as a non-root user with /home/user as home).
SANDBOX_HOME = "/home/user"
SANDBOX_CONFIG = f"{SANDBOX_HOME}/config"
SANDBOX_WORKSPACE = f"{SANDBOX_HOME}/workspace"
SANDBOX_SESSIONS = f"{SANDBOX_HOME}/sessions"
#: The ACP family's per-run state root inside the sandbox. The host renders the
#: run's own HOME (``runs/<id>/acp-agent/home``: credentials injected 0600) and
#: uploads it here; the agent process gets ``HOME`` pointed at it, so ``~/.dsh``,
#: ``~/.codex`` and every cache stay inside this run's sandbox.
SANDBOX_ACP_HOME = f"{SANDBOX_HOME}/acp/home"
#: How deep the sandbox's ACP state root is walked when it is recovered to the
#: host. The measured state roots (dsh / codex / claude / opencode) are a handful
#: of levels deep; a deeper tree is refused instead of walked without a bound.
ACP_STATE_MAX_DEPTH = 12
REPORT_FILENAME = "report.md"
#: Image-local guard script (same bytes in both worker images, see
#: :data:`agent_env.IMAGE_PACKAGE_GUARD`). The sandbox has no ``--entrypoint``
#: equivalent, so the command chain verifies it before starting pi. pi silently
#: skips a local package path that does not exist, so without the guard a
#: template that predates the host (or lost the package) would run the whole
#: research task without web_search.
SANDBOX_PACKAGE_GUARD = IMAGE_PACKAGE_GUARD
#: The sandbox tees its JSON event stream here (see module docstring) and
#: records the pipeline's exit code in the marker file once it finishes.
_EVENTS_FILE = f"{SANDBOX_HOME}/events.jsonl"
_STDERR_FILE = f"{SANDBOX_HOME}/pi-stderr.txt"
_EXIT_FILE = f"{SANDBOX_HOME}/pi-exit-code.txt"
#: One uploaded config file / recovered artifact may not exceed this.
UPLOAD_MAX_BYTES = 8 * 1024 * 1024
ARTIFACT_MAX_BYTES = 32 * 1024 * 1024
#: Ceiling for the sandbox's own event file. The file is only read when the
#: live stream could not prove completeness, and it is sandbox content: the
#: read is bounded by the SDK's entry metadata (or a streamed read) before a
#: byte is loaded. A real run's timeline is far below this.
EVENTS_MAX_BYTES = 32 * 1024 * 1024
#: Ceiling for the diagnostic stderr copy (a tail, not a deliverable).
STDERR_MAX_BYTES = 1024 * 1024


class SandboxError(RuntimeError):
    """Raised when the sandbox cannot be created or the run cannot start."""


class EventCatchUpFailed(SandboxError):
    """The event file could not prove what the run produced.

    Raised when the live stream dropped (or never existed) and the bounded
    file read that is the completeness authority failed: unreadable, absent or
    over the documented ceiling. The run is then unresolved, never finished.
    """


class LFLineFramer:
    """Incremental LF-only framing for the streamed event records.

    pi writes strict JSONL: records end with LF (optionally preceded by CR),
    while U+2028/U+2029 are ordinary characters *inside* a JSON string and must
    not split a record (``kernel/framing.py``). Chunk boundaries are arbitrary
    and may fall inside a multi-byte UTF-8 character, so bytes run through one
    incremental decoder and the trailing partial record is kept for the next
    chunk. That trailing piece is finally supplied by the events file (see
    :meth:`E2BSandboxClient.fetch_event_lines`), never flushed here: that is why
    a record can be delivered exactly once even when the stream is cut short.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._pending = ""

    def feed(self, chunk: str | bytes) -> list[str]:
        """Complete records unlocked by ``chunk`` (LF-split, trailing CR kept)."""
        if isinstance(chunk, (bytes, bytearray, memoryview)):
            self._pending += self._decoder.decode(bytes(chunk))
        else:
            self._pending += chunk
        parts = self._pending.split("\n")
        self._pending = parts.pop()
        return parts


@dataclass(frozen=True, slots=True)
class SandboxRunResult:
    """Facts about one sandboxed kernel command.

    ``delivered_lines`` counts the complete LF-framed records handed to
    ``on_line`` — the live stream first, then the file catch-up — and is the
    exact-once unit the regressions assert. ``stream_error`` is set when the
    stdout stream ended without an end event; the records are still completed
    from the file and the exit code from the sandbox marker, so a dropped
    stream is not a run failure. ``exit_code`` is ``None`` only when the host
    cancelled the run or the command's ending could not be observed at all.
    """

    exit_code: int | None
    stderr_tail: str
    delivered_lines: int
    stream_error: str | None = None


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


class _RunCancelled(Exception):
    """Internal: the host cancelled the run while the sandbox was streaming."""


class _SinkFailure(Exception):
    """Internal: the host frame sink raised; not a recoverable sandbox problem."""

    def __init__(self, original: BaseException) -> None:
        super().__init__(f"{type(original).__name__}: {original}")
        self.original = original


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
        """Recover a single session file before the sandbox is destroyed."""
        sandbox = self._require_sandbox()
        try:
            entries = sandbox.files.list(SANDBOX_SESSIONS)
            names = [str(getattr(entry, "name", "")) for entry in entries]
            if len(names) != 1 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.jsonl", names[0]):
                return None, "沙箱会话文件不可续接"
            name = names[0]
            data = sandbox.files.read(f"{SANDBOX_SESSIONS}/{name}")
            content = data if isinstance(data, bytes) else str(data).encode("utf-8")
            if not content or len(content) > ARTIFACT_MAX_BYTES:
                return None, "沙箱会话文件不可续接"
            with ArtifactTree(self.runtime.session_dir) as tree:
                tree.replace(name, content, max_bytes=ARTIFACT_MAX_BYTES)
            return self.runtime.session_dir / name, None
        except Exception as exc:  # noqa: BLE001 - sandbox SDK failures must not hide the verdict
            return None, f"沙箱会话回收失败: {type(exc).__name__}"

    def run_prompt(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        resume_session: str | None = None,
        on_line: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> SandboxRunResult:
        """Run ``pi --mode json`` once inside the sandbox and stream its records.

        The command pipes pi's stdout through ``tee`` into ``events.jsonl``, so
        the same bytes arrive on the SDK's stdout stream while the command runs
        and stay available as a complete file. ``on_line`` receives every
        complete LF-framed record as soon as its bytes arrive (the handle is the
        SDK's streaming interface — see the module docstring), and
        ``delivered_lines`` counts exactly those records.

        The file is the completeness authority, not a second protocol. When the
        stream drops before an end event, this method does not fail the run: it
        waits for the command to finish — the command writes its exit code to
        ``/home/user/pi-exit-code.txt`` after the ``tee`` pipeline — and then
        delivers ``fetch_event_lines(skip=delivered_lines)``. Records the stream
        already handed over are never taken from the file again. The SDK's
        ``commands.connect`` is deliberately not used for that gap: whether a
        reconnected stream replays output the host already consumed is not
        specified by the SDK, and a replay would break exact-once delivery; the
        marker plus the file answer both questions (fell over, and with which
        exit code) without that ambiguity.

        ``timeout`` is passed explicitly: the SDK default is 60 seconds for a
        command's total active time, which would kill every research run while
        the sandbox itself is still alive (a real run does minutes of work).
        """
        sandbox = self._require_sandbox()
        command = self._command(prompt, system_prompt, resume_session=resume_session)
        handle = sandbox.commands.run(
            command,
            background=True,
            envs=self._model_envs(),
            timeout=self._command_timeout(),
        )
        framer = LFLineFramer()
        delivered = 0

        def cancelled() -> bool:
            return is_cancelled is not None and is_cancelled()

        def record(raw: str) -> None:
            """Hand one complete wire record to the sink, CR stripped."""
            nonlocal delivered
            delivered += 1
            if on_line is None:
                return
            try:
                on_line(strip_trailing_cr(raw))
            except Exception as exc:  # noqa: BLE001 - the sink's failure is fatal
                raise _SinkFailure(exc) from exc

        def consume(chunk: str | bytes) -> None:
            for raw in framer.feed(chunk):
                record(raw)

        try:
            stream_error: str | None = None
            iterator = _handle_iterator(handle)
            if iterator is not None:
                try:
                    for stdout, _stderr, _pty in iterator:
                        if cancelled():
                            raise _RunCancelled()
                        if stdout:
                            consume(stdout)
                except (_RunCancelled, _SinkFailure):
                    raise
                except Exception as exc:  # noqa: BLE001 - recoverable: the file keeps the records
                    stream_error = f"{type(exc).__name__}: {exc}"

            exit_code: int | None = None
            if stream_error is None:
                exit_code, reason = self._wait_for_exit(handle)
                if exit_code is None:
                    stream_error = reason
            if exit_code is None:
                exit_code = self._wait_for_exit_code(cancelled)

            if cancelled():
                raise _RunCancelled()

            # File catch-up: exactly the records the live stream did not hand
            # over (normally none; after a drop, the rest of the run).
            # ``required`` is the fail-closed boundary: a stream that ended on
            # its own delivered the whole timeline and the exit marker, so the
            # file is redundant; a dropped stream (or no stream interface at
            # all) leaves the file as the only completeness authority, and an
            # unreadable/oversized file must fail the run instead of producing
            # a conclusion nobody can prove.
            proved_by_stream = stream_error is None and iterator is not None
            for raw in self.fetch_event_lines(skip=delivered, required=not proved_by_stream):
                record(raw)
        except _RunCancelled:
            return SandboxRunResult(exit_code=None, stderr_tail="", delivered_lines=delivered)
        except _SinkFailure as failure:
            raise failure.original from None

        return SandboxRunResult(
            exit_code=exit_code,
            stderr_tail=self.fetch_stderr_tail(),
            delivered_lines=delivered,
            stream_error=stream_error,
        )

    @staticmethod
    def _wait_for_exit(handle: Any) -> tuple[int | None, str | None]:
        """The handle's exit code, or ``(None, reason)`` when the stream ended early.

        The SDK raises ``CommandExitException`` for a non-zero exit code; that
        is the command's own result, not a stream failure, so it is returned as
        an exit code and mapped to ``kernel_exit`` by the adapter.
        """
        try:
            result = handle.wait()
        except Exception as exc:  # noqa: BLE001 - an exit code is still a result
            code = getattr(exc, "exit_code", None)
            if isinstance(code, int) and not isinstance(code, bool):
                return code, None
            return None, f"{type(exc).__name__}: {exc}"
        code = getattr(result, "exit_code", None)
        if isinstance(code, int) and not isinstance(code, bool):
            return code, None
        return None, "sandbox command returned no exit code"

    def _wait_for_exit_code(self, cancelled: Callable[[], bool]) -> int | None:
        """Wait for the command's exit-code marker after its stream was lost.

        The command writes the marker after the ``tee`` pipeline finishes, so
        its presence is exactly "the command ended and ``events.jsonl`` is
        complete". The SDK's per-command timeout bounds a command's lifetime, so
        it bounds this wait too; cancellation ends it immediately because the
        sandbox is being destroyed and no marker will appear.

        Returns ``None`` when the marker never appeared or is unreadable.
        """
        sandbox = self._require_sandbox()
        deadline = time.monotonic() + max(self._command_timeout(), 1) + 5.0
        delay = 0.05
        while time.monotonic() < deadline:
            if cancelled():
                return None
            try:
                content = sandbox.files.read(_EXIT_FILE)
            except Exception:  # noqa: BLE001 - not written yet, or sandbox gone
                content = None
            if content is not None:
                text = (
                    content.decode("utf-8", "replace")
                    if isinstance(content, bytes)
                    else str(content)
                ).strip()
                if text:
                    try:
                        return int(text)
                    except ValueError:
                        return None
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
        return None

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

    def fetch_events(self) -> list[str]:
        """Return the non-empty JSONL records the sandbox wrote for this run.

        Kept for the replay/regression path; a live run streams through
        :meth:`run_prompt` and completes through :meth:`fetch_event_lines` with
        the stream's delivered-line offset.
        """
        return [line for line in self.fetch_event_lines() if line.strip()]

    def fetch_event_lines(self, *, skip: int = 0, required: bool = False) -> list[str]:
        """Every LF-framed record after ``skip`` complete lines.

        Framing is LF-only (``pi`` documents strict JSONL): ``splitlines`` would
        also split on U+2028/U+2029, tearing a valid JSON string in two and
        losing both halves. Blank lines are kept, so an index into this list is
        the same unit the stream framer counts. A final record without LF is
        still a record: the live stream never terminated it, and this file is
        the one place that can.

        The read is bounded before a byte is loaded (:data:`EVENTS_MAX_BYTES`).
        ``required=True`` means the file is the completeness authority: a
        failure to read it raises :class:`EventCatchUpFailed` instead of
        quietly returning an empty timeline.
        """
        sandbox = self._require_sandbox()
        data, failure = _read_file_bounded(sandbox, _EVENTS_FILE, EVENTS_MAX_BYTES)
        if data is None:
            if required:
                raise EventCatchUpFailed(f"事件日志补读失败: {failure}")
            logger.warning(
                "事件日志未读取（%s）；实时流已完整交付并取得退出标记，结论不受影响", failure
            )
            return []
        text = data.decode("utf-8", "replace")
        lines = text.split("\n")
        if lines and lines[-1] == "":
            # The final LF terminates the previous record; it is not a record.
            lines.pop()
        return [strip_trailing_cr(line) for line in lines[max(skip, 0):]]

    def fetch_stderr_tail(self, limit: int = 20) -> str:
        """The last stderr lines, bounded before the read; empty on any failure."""
        sandbox = self._require_sandbox()
        data, failure = _read_file_bounded(sandbox, _STDERR_FILE, STDERR_MAX_BYTES)
        if data is None:
            logger.debug("stderr 尾部未读取: %s", failure)
            return ""
        text = data.decode("utf-8", "replace")
        return "\n".join(text.split("\n")[-limit:])

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

    def _command(self, prompt: str, system_prompt: str, *, resume_session: str | None = None) -> str:
        """Compose the sandbox command.

        Both the prompt and the research contract are base64-encoded before they
        are interpolated into the shell line, so quoting, newlines, backticks,
        ``$(…)`` and non-ASCII text cannot break the command or execute anything:
        the shell only ever sees a base64 alphabet. The command is asserted by
        the adapter tests against exactly those inputs.

        The kernel's stdout is piped through ``tee`` so it reaches the host live
        *and* lands in ``events.jsonl``; ``stderr`` still goes to its own file.
        ``set -o pipefail`` keeps a non-zero kernel exit from being masked by
        ``tee``, and the shell records the pipeline's status in the exit-code
        marker before exiting with it, so a run whose stream was lost can still
        be classified (``kernel_exit``) from the sandbox alone.

        The command chain also carries the image-local package guard: the first
        step fails with exit 78 when the template predates the host (no guard
        script), and the guard itself verifies the packages the rendered
        ``settings.json`` points at before ``exec``ing pi. Both failures reach
        the run as ``kernel_exit`` with the message in the stderr tail.
        """
        encoded_prompt = base64.b64encode(prompt.encode("utf-8")).decode("ascii")
        parts = [
            # Without pipefail the pipeline status is tee's, turning a failed
            # kernel into a successful run.
            "set -o pipefail",
            f"mkdir -p {SANDBOX_CONFIG} {SANDBOX_WORKSPACE} {SANDBOX_SESSIONS}",
            f"cd {SANDBOX_WORKSPACE}",
            f"PROMPT=$(printf '%s' '{encoded_prompt}' | base64 -d)",
            # A template built before this host has no guard script: fail with a
            # clear reason here instead of letting pi start without web_search.
            (
                f"test -x {SANDBOX_PACKAGE_GUARD} || {{ "
                f"echo 'pi-glink: 沙箱模板缺少预装包守卫 {SANDBOX_PACKAGE_GUARD}，"
                "请用 scripts/build-e2b-template.py 重建模板' >&2; exit 78; }"
            ),
        ]
        pi = [
            # The guard verifies every package the rendered settings point at,
            # then ``exec``s pi with these same arguments (exit 78 when one is
            # missing).
            SANDBOX_PACKAGE_GUARD,
            "pi",
            "--mode", "json",
            "--provider", self.runtime.provider_id,
            "--model", self.runtime.model_id,
            "--name", f"pi-glink-{self.run_id}",
        ]
        if resume_session:
            if resume_session != "continued.jsonl":
                raise SandboxError("invalid continuation session")
            pi.extend(["--session", f"{SANDBOX_SESSIONS}/{resume_session}"])
        if system_prompt:
            encoded_system = base64.b64encode(system_prompt.encode("utf-8")).decode("ascii")
            parts.append(f"SYSTEM=$(printf '%s' '{encoded_system}' | base64 -d)")
            pi.extend(["--append-system-prompt", '"$SYSTEM"'])
        parts.append(
            "{ "
            + f"{' '.join(pi)} \"$PROMPT\" 2> {_STDERR_FILE} | tee {_EVENTS_FILE}; "
            + f"rc=$?; printf '%s' \"$rc\" > {_EXIT_FILE}; exit \"$rc\"; }}"
        )
        return " && ".join(parts)


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


def _handle_iterator(handle: Any) -> Iterator[Any] | None:
    """The SDK's streaming iterator for a background handle, or ``None``.

    ``CommandHandle.__iter__`` yields ``(stdout, stderr, pty)`` chunks while
    the command runs. A handle without it (an older SDK, or a test double that
    only models ``wait()``) falls back to ``wait()`` plus the file catch-up
    instead of failing.
    """
    try:
        return iter(handle)
    except TypeError:
        return None


def parse_json_stream_line(line: str) -> Mapping[str, Any] | None:
    """Parse one ``--mode json`` frame; returns None for non-JSON output."""
    text = line.strip()
    if not text or not text.startswith("{"):
        return None
    try:
        frame = json.loads(text)
    except json.JSONDecodeError:
        return None
    return frame if isinstance(frame, dict) else None


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
