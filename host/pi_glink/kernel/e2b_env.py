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
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from ..artifacts import ArtifactError, ArtifactTree
from ..config import ConfigError, HostConfig
from .agent_env import IMAGE_PACKAGE_GUARD, AgentRuntime
from .framing import strip_trailing_cr

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
#: The sandbox tees its JSON event stream here (see module docstring) and
#: records the pipeline's exit code in the marker file once it finishes.
_EVENTS_FILE = f"{SANDBOX_HOME}/events.jsonl"
_STDERR_FILE = f"{SANDBOX_HOME}/pi-stderr.txt"
_EXIT_FILE = f"{SANDBOX_HOME}/pi-exit-code.txt"
#: One uploaded config file / recovered artifact may not exceed this.
UPLOAD_MAX_BYTES = 8 * 1024 * 1024
ARTIFACT_MAX_BYTES = 32 * 1024 * 1024


class SandboxError(RuntimeError):
    """Raised when the sandbox cannot be created or the run cannot start."""


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
        template = self.config.e2b_template
        if not template:
            raise ConfigError("LOCAL_WEB_E2B_TEMPLATE 未配置")
        if not self.config.has_e2b_key:
            raise ConfigError("E2B_API_KEY 未配置")
        factory = self._sandbox_factory or _default_sandbox_factory()
        # ``Sandbox.create`` takes the credential through ``**opts`` (ApiParams),
        # so an operator who only filled ``.env`` still authenticates: the host
        # passes the key it read at startup instead of relying on the SDK having
        # inherited E2B_API_KEY from the launching shell.
        # ``timeout_seconds`` is the adapter's bound (task timeout + teardown
        # headroom); the frozen configuration may set a tighter ceiling.  E2B
        # does not expose CPU/memory controls through this SDK path, so the host
        # can only bound lifetime — it never claims a resource limit the cloud
        # provider was not asked to enforce.
        adapter_bound = int(timeout_seconds or 0)
        ceiling = int(
            self.config.e2b_sandbox_timeout_seconds
            or (self.config.task_timeout_seconds + 300)
        )
        timeout = min(adapter_bound, ceiling) if adapter_bound > 0 else ceiling
        self.sandbox_timeout = int(timeout)
        create_kwargs: dict[str, Any] = {
            "template": template,
            "timeout": timeout,
            "envs": self._model_envs(),
        }
        if self.config.e2b_api_key:
            create_kwargs["api_key"] = self.config.e2b_api_key
        try:
            self.sandbox = factory(**create_kwargs)
        except Exception as exc:  # noqa: BLE001 - surfaced to the run record
            raise SandboxError(f"sandbox 创建失败: {type(exc).__name__}: {exc}") from exc
        self.sandbox_id = str(getattr(self.sandbox, "sandbox_id", ""))

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
            for raw in self.fetch_event_lines(skip=delivered):
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

    def fetch_event_lines(self, *, skip: int = 0) -> list[str]:
        """Every LF-framed record after ``skip`` complete lines.

        Framing is LF-only (``pi`` documents strict JSONL): ``splitlines`` would
        also split on U+2028/U+2029, tearing a valid JSON string in two and
        losing both halves. Blank lines are kept, so an index into this list is
        the same unit the stream framer counts. A final record without LF is
        still a record: the live stream never terminated it, and this file is
        the one place that can.
        """
        sandbox = self._require_sandbox()
        try:
            content = sandbox.files.read(_EVENTS_FILE)
        except Exception:  # noqa: BLE001 - absent stream means no frames
            return []
        text = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
        lines = text.split("\n")
        if lines and lines[-1] == "":
            # The final LF terminates the previous record; it is not a record.
            lines.pop()
        return [strip_trailing_cr(line) for line in lines[max(skip, 0):]]

    def fetch_stderr_tail(self, limit: int = 20) -> str:
        sandbox = self._require_sandbox()
        try:
            content = sandbox.files.read(_STDERR_FILE)
        except Exception:  # noqa: BLE001
            return ""
        text = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
        return "\n".join(text.split("\n")[-limit:])

    def recover_artifacts(self, destination: Path) -> tuple[list[Path], list[str]]:
        """Pull the sandbox workspace back into the host run workspace.

        Only direct children with simple names are recovered, each one bounded;
        anything unreadable (a directory), oversized, or named with a path
        separator is recorded as a failure instead of being guessed at.
        """
        sandbox = self._require_sandbox()
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
                if name.startswith("."):
                    continue
                try:
                    content = sandbox.files.read(path)
                except Exception:  # noqa: BLE001 - directories and unreadable entries
                    continue
                data = content if isinstance(content, bytes) else str(content).encode("utf-8")
                if len(data) > ARTIFACT_MAX_BYTES:
                    failures.append(f"产物超过上限: {name}")
                    continue
                try:
                    tree.replace(name, data, max_bytes=ARTIFACT_MAX_BYTES)
                except (ArtifactError, OSError) as exc:
                    failures.append(f"产物写入失败: {name}: {exc}")
                    continue
                files.append(destination / name)
        return files, failures

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
