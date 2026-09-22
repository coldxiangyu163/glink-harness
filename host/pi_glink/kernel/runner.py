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
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import events
from ..config import ConfigError, HostConfig
from ..store import CANCELLED, FAILED, FINISHED, RunRecord, RunStore
from .agent_env import AgentRuntime, build_pi_argv, build_pi_env, prepare_agent_dir
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
    ) -> None:
        self.config = config
        self.bus = bus
        self.store = store
        self._client_factory = client_factory
        self._lock = threading.RLock()
        self._executions: dict[str, _Execution] = {}
        self._active_run_id: str | None = None
        self._usage: dict[str, dict[str, Any]] = {}

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

        A cancel that arrives while pi is still starting is honoured: the flag
        is set immediately and the executor checks it before prompting, so an
        operator does not have to race the kernel to stop their run.
        """
        with self._lock:
            execution = self._executions.get(run_id)
        if execution is None:
            return False
        execution.cancel_requested = True
        client = execution.client
        if client is None:
            return True
        try:
            client.request({"type": "abort"}, timeout=10.0)
        except (PiRpcError, OSError):
            logger.warning("abort request failed run=%s", run_id)
            return False
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
        status = FINISHED
        stop_reason: str | None = None
        error: str | None = None
        try:
            execution.workspace.mkdir(parents=True, exist_ok=True)
            self._emit(run_id, events.RUN_STARTED, {"prompt": execution.record.prompt})

            runtime = prepare_agent_dir(self.config)
            client = self._client_factory(
                self._argv_with_research_prompt(runtime, run_id),
                cwd=execution.workspace,
                env=build_pi_env(self.config, runtime),
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

            report_path = self._collect_report(execution.workspace)
            if status is FINISHED and report_path is None:
                stop_reason = stop_reason or "no_report"
            previous = self.store.get(run_id)
            session_id = previous.session_id if previous is not None else None
            session_file = previous.session_file if previous is not None else None
            record = self.store.update(
                run_id,
                status=status,
                finished_at=time.time(),
                report_path=report_path,
                error=error,
                stop_reason=stop_reason,
                usage=self._usage.pop(run_id, None),
            )
            self._emit(
                run_id,
                events.RUN_FAILED if status == FAILED else events.RUN_FINISHED,
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

    def _argv_with_research_prompt(self, runtime: AgentRuntime, run_id: str) -> list[str]:
        extra = ["--name", f"pi-glink-{run_id}"]
        text = self._research_prompt_text()
        if text:
            extra.extend(["--append-system-prompt", text])
        return build_pi_argv(self.config, runtime, extra=extra)

    def _research_prompt_text(self) -> str:
        path = self.config.agent_config_dir / RESEARCH_PROMPT_FILENAME
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()

    def _on_frame(self, execution: _Execution, frame: Mapping[str, Any]) -> None:
        run_id = execution.record.run_id
        for event_type, payload in project_event(frame):
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
        report = workspace / REPORT_FILENAME
        return str(report) if report.is_file() else None

    def _emit(
        self, run_id: str, event_type: str, payload: Mapping[str, Any] | None = None
    ) -> None:
        event = self.bus.publish(run_id, event_type, payload)
        self.store.append_event(run_id, event.as_dict())
