"""Run lifecycle for the local execution face.

One run is one workspace plus one pi process plus one session file. The manager
keeps four promises:

* **Serial by construction.** Only one run is active at a time; a second start
  is refused with :class:`RunBusy` instead of silently interleaving. This
  matches FrontierAgent's ``inproc`` semantics and keeps the local face honest.
* **One stream, two sinks.** Every projected event is published to the bus *and*
  appended to the run's ``events.jsonl``, so a reconnect and a post-mortem see
  the same frames. ``meta.json`` is rewritten only at state transitions, never
  per delta.
* **Identity comes from pi.** The session id/file recorded on the run is what
  pi reported through ``get_state``, not a guess derived from file timestamps.
* **Terminal state from evidence.** A run is only ``finished`` after pi settled
  and the workspace was inspected for the report; a missing report is recorded
  as ``no_report`` and never papered over.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import events
from ..config import ConfigError, HostConfig
from ..redaction import SecretRedactor, redactor_from_config
from ..store import CANCELLED, FAILED, FINISHED, RunRecord, RunStore
from .agent_env import AgentRuntime, build_pi_argv, build_pi_env, prepare_agent_dir
from .docker_env import (
    build_plan as build_docker_plan,
    is_available as docker_available,
    stop_argv as docker_stop_argv,
)
from .projection import project_event
from .rpc import PiRpcClient, PiRpcError

logger = logging.getLogger("pi_glink.kernel.runner")

#: Artifact the research contract asks pi to produce.
REPORT_FILENAME = "report.md"
#: System prompt appended to every run (pi's own prompt stays untouched).
RESEARCH_PROMPT_FILENAME = "research-prompt.md"
#: Deadline for pi to accept and answer a command frame.
_REQUEST_TIMEOUT_SECONDS = 60.0


class RunBusy(RuntimeError):
    """Raised when a run is requested while another one is active."""

    def __init__(self, active_run_id: str) -> None:
        super().__init__(f"run {active_run_id} is still active")
        self.active_run_id = active_run_id


@dataclass
class _Execution:
    """Mutable per-run handles held for as long as the run is alive."""

    record: RunRecord
    workspace: Path
    settled: threading.Event = field(default_factory=threading.Event)
    client: PiRpcClient | None = None
    cancel_requested: bool = False
    container_name: str | None = None
    sandbox: Any | None = None


ClientFactory = Callable[..., PiRpcClient]


class RunManager:
    """Own the lifecycle of local runs."""

    def __init__(
        self,
        config: HostConfig,
        bus: Any,
        store: RunStore,
        *,
        client_factory: ClientFactory = PiRpcClient,
        sandbox_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.bus = bus
        self.store = store
        self._client_factory = client_factory
        self._sandbox_factory = sandbox_factory
        # The single redaction boundary: every event payload passes through it
        # before it reaches the bus (SSE + browser) or the event log.
        self.redactor: SecretRedactor = redactor_from_config(config)
        self._lock = threading.RLock()
        self._executions: dict[str, _Execution] = {}
        self._active_run_id: str | None = None
        self._usage: dict[str, dict[str, Any]] = {}
        self._report_redacted = False

    # -- public API -----------------------------------------------------------

    @property
    def active_run_id(self) -> str | None:
        with self._lock:
            return self._active_run_id

    def start(self, prompt: str) -> RunRecord:
        """Create a run and execute it in the background."""
        text = (prompt or "").strip()
        if not text:
            raise ValueError("prompt must not be empty")
        if not self.config.has_openai_key:
            raise ConfigError(
                "OPENAI_API_KEY is not configured; set it in .env before starting a run"
            )

        with self._lock:
            if self._active_run_id is not None:
                raise RunBusy(self._active_run_id)
            run_id = uuid.uuid4().hex[:12]
            workspace = self.config.workspace_root / run_id
            record = self.store.create(text, run_id=run_id, workspace=workspace)
            execution = _Execution(record=record, workspace=workspace)
            self._executions[run_id] = execution
            self._active_run_id = run_id

        threading.Thread(
            target=self._execute,
            args=(execution,),
            name=f"run-{run_id}",
            daemon=True,
        ).start()
        return record

    def stop(self, run_id: str) -> bool:
        """Request cancellation; returns False only when nothing is running.

        True means "the cancellation is accepted", not "pi acknowledged it".
        The flag is what ends the run (the executor checks it before prompting
        and after settling); the ``abort`` command only shortens the wait, so a
        failed abort is logged and does not turn the request into a rejection.
        """
        with self._lock:
            execution = self._executions.get(run_id)
        if execution is None:
            return False
        execution.cancel_requested = True
        if execution.sandbox is not None:
            try:
                execution.sandbox.close()
            except Exception:  # noqa: BLE001 - teardown is best effort
                logger.debug("killing sandbox failed", exc_info=True)
            return True
        client = execution.client
        if client is None:
            return True
        try:
            client.request({"type": "abort"}, timeout=10.0)
        except (PiRpcError, OSError) as exc:
            logger.warning(
                "abort request failed run=%s: %s: %s (cancellation still accepted)",
                run_id,
                type(exc).__name__,
                exc,
            )
        return True

    def wait(self, run_id: str, timeout: float = 60.0) -> RunRecord | None:
        """Block until the run leaves ``running`` (used by tests and smoke)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self.store.get(run_id)
            if record is not None and record.is_terminal:
                return record
            time.sleep(0.1)
        return self.store.get(run_id)

    # -- execution ------------------------------------------------------------

    def _execute(self, execution: _Execution) -> None:
        run_id = execution.record.run_id
        self.bus.open_run(run_id)
        self._report_redacted = False
        status = FINISHED
        stop_reason: str | None = None
        error: str | None = None
        try:
            execution.workspace.mkdir(parents=True, exist_ok=True)
            self._emit(run_id, events.RUN_STARTED, {"prompt": execution.record.prompt})

            runtime = prepare_agent_dir(self.config)
            # The search credential only exists in the rendered file, so it is
            # learned here (never logged, never returned).
            self.redactor.add_from_file(runtime.directory / "web-search.json")
            if self.config.executor == "e2b":
                self._run_in_sandbox(execution, runtime)
                settled = True
                accepted = None
            else:
                argv, env, cwd = self._execution_plan(runtime, run_id, execution.workspace)
                client = self._client_factory(
                    argv,
                    cwd=cwd,
                    env=env,
                    on_event=lambda frame: self._on_frame(execution, frame),
                )
                execution.client = client
                client.start()

                self._publish_session_ready(run_id, client)
                if execution.cancel_requested:
                    # Cancelled while pi was starting: never start the work.
                    status = CANCELLED
                    stop_reason = "abort"
                else:
                    accepted = client.request(
                        {"type": "prompt", "message": execution.record.prompt},
                        timeout=_REQUEST_TIMEOUT_SECONDS,
                    )
                    if not accepted.success:
                        raise PiRpcError(accepted.error or "pi rejected the prompt")

                    settled = execution.settled.wait(self.config.task_timeout_seconds)
                    if execution.cancel_requested:
                        status = CANCELLED
                        stop_reason = "abort"
                    elif not settled:
                        status = FAILED
                        stop_reason = "timeout"
                        error = (
                            f"run did not settle within {self.config.task_timeout_seconds}s"
                        )
                        try:
                            client.request({"type": "abort"}, timeout=10.0)
                        except (PiRpcError, OSError):
                            pass
        except Exception as exc:  # noqa: BLE001 - every run must end with a reason
            logger.exception("run failed run=%s", run_id)
            status = FAILED
            error = f"{type(exc).__name__}: {exc}"
        finally:
            client = execution.client
            if client is not None:
                try:
                    client.close()
                except Exception:  # pragma: no cover - best effort
                    logger.debug("closing pi client failed", exc_info=True)
            if execution.sandbox is not None:
                try:
                    execution.sandbox.close()
                except Exception:  # noqa: BLE001 - teardown is best effort
                    logger.debug("closing sandbox failed", exc_info=True)
            if execution.container_name:
                try:
                    subprocess.run(
                        docker_stop_argv(execution.container_name),
                        capture_output=True,
                        timeout=30,
                    )
                except Exception:  # pragma: no cover - best effort cleanup
                    logger.debug("removing container failed", exc_info=True)

            # report.md is not the only artifact a tool echo can reach, so the
            # credential boundary runs over the whole workspace, then pi's own
            # session log, and finally the report path itself.
            self._sanitize_workspace(execution.workspace)
            previous = self.store.get(run_id)
            session_id = previous.session_id if previous is not None else None
            session_file = previous.session_file if previous is not None else None
            self._sanitize_session_file(session_file)
            report_path = self._collect_report(execution.workspace)
            if status is FINISHED and report_path is None:
                stop_reason = stop_reason or "no_report"
            record = self.store.update(
                run_id,
                status=status,
                finished_at=time.time(),
                report_path=report_path,
                error=self.redactor.redact(error) if error else error,
                stop_reason=stop_reason,
                usage=self._usage.pop(run_id, None),
                redacted=self._report_redacted,
            )
            # The terminal frame must match the terminal state: a cancelled run
            # that reports ``run_finished`` makes the workbench (and anyone
            # reading the event log) show a stop as a success.
            terminal_type = (
                events.RUN_FAILED
                if status == FAILED
                else events.RUN_CANCELLED
                if status == CANCELLED
                else events.RUN_FINISHED
            )
            self._emit(
                run_id,
                terminal_type,
                {
                    "status": status,
                    "stopReason": stop_reason,
                    "error": error,
                    "reportPath": report_path,
                    "sessionId": session_id,
                    "sessionFile": session_file,
                    "usage": record.usage,
                    "durationSeconds": round(record.finished_at - record.created_at, 3),
                },
            )
            # The terminal frame is part of the log, so the count is written
            # after it: ``event_count`` must equal the lines in events.jsonl.
            self.store.update(run_id, event_count=self.bus.last_seq(run_id))
            self.bus.close(run_id)
            with self._lock:
                self._executions.pop(run_id, None)
                if self._active_run_id == run_id:
                    self._active_run_id = None
            logger.info(
                "run finished run=%s status=%s report=%s", run_id, status, report_path or "<none>"
            )

    # -- helpers --------------------------------------------------------------

    def _run_in_sandbox(self, execution: _Execution, runtime: AgentRuntime) -> None:
        """Execute this run inside an E2B sandbox (single-shot JSON stream).

        The sandbox face has no bidirectional pipe, so it uses ``pi --mode json``:
        the prompt is passed once and the events stream back line by line. The
        projection and the product vocabulary are the same as the RPC faces, so
        nothing downstream changes.
        """
        from .e2b_env import E2BSandboxClient, parse_json_stream_line

        run_id = execution.record.run_id
        client = E2BSandboxClient(
            self.config,
            runtime,
            run_id=run_id,
            workspace=execution.workspace,
            sandbox_factory=self._sandbox_factory,
        )
        execution.sandbox = client
        client.start()
        self._emit(
            run_id,
            events.SESSION_READY,
            {"sandboxId": client.sandbox_id, "template": self.config.e2b_template},
        )
        client.upload_config()

        exit_code, stderr_tail = client.run_prompt(
            execution.record.prompt,
            system_prompt=self._research_prompt_text(),
        )
        # The sandbox face replays the run's frames once it settles (the E2B SDK
        # provides no working stdout callback); ordering and vocabulary are the
        # same as the RPC faces.
        for line in client.fetch_events():
            frame = parse_json_stream_line(line)
            if frame is not None:
                self._on_frame(execution, frame)
        report = client.fetch_report()
        # A non-zero exit is a failure even when a report happens to exist: the
        # run contract is "pi finished successfully and produced its artifact",
        # not "a file with the right name showed up".
        if exit_code != 0:
            raise PiRpcError(
                f"sandbox pi exited with {exit_code}: {stderr_tail[-300:]}"
            )

    def _execution_plan(
        self, runtime: AgentRuntime, run_id: str, workspace: Path
    ) -> tuple[list[str], dict[str, str], Path | None]:
        """Where pi runs: in this process tree or inside a container.

        Both faces speak the same RPC protocol over stdio, so only the command
        line and the environment differ; the event projection does not.
        """
        extra = self._extra_argv(run_id)
        if self.config.executor == "docker":
            if not docker_available():
                raise ConfigError(
                    "LOCAL_WEB_EXECUTOR=docker requires a reachable Docker daemon"
                )
            plan = build_docker_plan(
                self.config, runtime, run_id=run_id, workspace=workspace, extra=extra
            )
            with self._lock:
                execution = self._executions.get(run_id)
                if execution is not None:
                    execution.container_name = plan.container_name
            return plan.argv, plan.env, None
        return (
            build_pi_argv(self.config, runtime, extra=extra),
            build_pi_env(self.config, runtime),
            workspace,
        )

    def _extra_argv(self, run_id: str) -> list[str]:
        extra = ["--name", f"pi-glink-{run_id}"]
        text = self._research_prompt_text()
        if text:
            extra.extend(["--append-system-prompt", text])
        return extra

    def _argv_with_research_prompt(self, runtime: AgentRuntime, run_id: str) -> list[str]:
        return build_pi_argv(self.config, runtime, extra=self._extra_argv(run_id))

    def _research_prompt_text(self) -> str:
        path = self.config.agent_config_dir / RESEARCH_PROMPT_FILENAME
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def _on_frame(self, execution: _Execution, frame: Mapping[str, Any]) -> None:
        run_id = execution.record.run_id
        for event_type, payload in project_event(frame, self.redactor):
            if event_type == events.USAGE_REPORTED:
                self._usage[run_id] = payload.get("usage", {})
            self._emit(run_id, event_type, payload)
        if frame.get("type") == "agent_settled":
            execution.settled.set()

    def _publish_session_ready(self, run_id: str, client: PiRpcClient) -> None:
        try:
            state = client.request({"type": "get_state"}, timeout=30.0)
        except PiRpcError as exc:
            logger.warning("get_state failed run=%s: %s", run_id, exc)
            return
        data = state.data
        model = data.get("model")
        session_id = data.get("sessionId")
        session_file = data.get("sessionFile")
        if isinstance(session_id, str) or isinstance(session_file, str):
            self.store.update(
                run_id,
                session_id=str(session_id) if session_id else None,
                session_file=str(session_file) if session_file else None,
            )
        self._emit(
            run_id,
            events.SESSION_READY,
            {
                "sessionId": session_id,
                "sessionFile": session_file,
                "model": model.get("id") if isinstance(model, Mapping) else None,
                "thinkingLevel": data.get("thinkingLevel"),
            },
        )

    def _collect_report(self, workspace: Path) -> str | None:
        """Return the report path, redacting credentials in place first.

        The report is written by pi, but it is a *product* artifact this host
        serves, and the objective requires every file the host handles to be free
        of credentials. If a tool echo (or a config-file read) reached the text,
        the value is masked here and the run records ``redacted=true`` so the
        sanitisation is visible rather than silent.
        """
        report = workspace / REPORT_FILENAME
        if not report.is_file():
            return None
        try:
            original = report.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return str(report)
        redacted = self.redactor.redact(original)
        if redacted != original:
            try:
                report.write_text(redacted, encoding="utf-8")
                self._report_redacted = True
                logger.warning(
                    "report contained credentials and was redacted in place: %s", report
                )
            except OSError:
                logger.error("failed to redact report in place: %s", report)
        return str(report)

    def _sanitize_session_file(self, session_file: str | None) -> None:
        """Mask credentials pi wrote into its own session log.

        The kernel owns that file, so redaction cannot happen on the way in; it
        happens here on the way out, and any change marks the run ``redacted``
        so the sanitisation is visible rather than silent.
        """
        if not session_file:
            return
        path = Path(session_file)
        if not path.is_file():
            return
        try:
            original = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        redacted = self.redactor.redact(original)
        if redacted == original:
            return
        try:
            path.write_text(redacted, encoding="utf-8")
            self._report_redacted = True
            logger.warning("session file contained credentials and was redacted: %s", path)
        except OSError:
            logger.error("failed to redact session file: %s", path)

    #: Text artifacts above this size are skipped by the workspace sweep.
    _SANITIZE_MAX_BYTES = 2 * 1024 * 1024

    def _sanitize_workspace(self, workspace: Path) -> None:
        """Redact credentials in every text artifact the run left behind.

        The report is not the only file a tool echo can reach; a ``write`` call
        can leak the same value into any artifact the operator later opens. The
        sweep is bounded (size cap, binary skip) and any change sets the
        run-level ``redacted`` marker, so the sanitisation is visible rather
        than silent.
        """
        if not workspace.is_dir():
            return
        for path in sorted(workspace.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                if path.stat().st_size > self._SANITIZE_MAX_BYTES:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:1024]:  # binary-ish
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            redacted = self.redactor.redact(text)
            if redacted == text:
                continue
            try:
                path.write_text(redacted, encoding="utf-8")
                self._report_redacted = True
                logger.warning("artifact contained credentials and was redacted: %s", path)
            except OSError:
                logger.error("failed to redact artifact: %s", path)

    def _emit(
        self, run_id: str, event_type: str, payload: Mapping[str, Any] | None = None
    ) -> None:
        # Belt-and-braces: projection already redacts, but host-owned events
        # (run_finished, kernel_status, …) are built here, so the same boundary
        # runs again on the way out. Nothing reaches the bus or the log unredacted.
        safe_payload = (
            self.redactor.redact_payload(dict(payload)) if payload is not None else None
        )
        event = self.bus.publish(run_id, event_type, safe_payload)
        self.store.append_event(run_id, event.as_dict())
