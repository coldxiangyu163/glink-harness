"""Run lifecycle for the local execution faces.

One run is one workspace plus one kernel process/sandbox plus one session file.
The manager keeps five promises:

* **Serial by construction.** Only one run is active at a time; a second start
  is refused with :class:`RunBusy` instead of silently interleaving, and the
  in-process face stays a single-agent debug entry point.
* **Commit before publish.** Every projected event is persisted through
  :meth:`EventBus.commit` and only then shown live, so the durable log is the
  replay source of truth and a failed append can never produce a frame that
  exists only in the browser.
* **Identity comes from pi.** The session id/file recorded on the run is what
  pi reported through ``get_state``, mapped back to the host path when the
  kernel ran inside a container.
* **Result, execution and artifact are separate facts.** ``agent_settled`` only
  means "pi will not continue automatically". The run is only ``finished`` once
  the execution settled *and* the final model result is not an error, while a
  missing or rejected report is recorded as such (``no_report`` /
  ``report_rejected``) instead of being papered over.
* **Bounded, honest teardown.** The run result is persisted independently of
  resource cleanup; a cleanup failure is reported in the record instead of
  pretending the resource was released, and the active slot is always freed.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import events
from ..artifacts import ArtifactError, read_regular_file, replace_with_text
from ..config import ConfigError, HostConfig
from ..redaction import SecretRedactor, StreamRedactor, redactor_from_config
from ..store import (
    CANCELLED,
    FAILED,
    FINISHED,
    RUNNING,
    RunRecord,
    RunStore,
)
from .agent_env import AgentRuntime, build_pi_argv, build_pi_env, prepare_agent_dir
from .docker_env import (
    build_plan as build_docker_plan,
    host_session_path as docker_host_session_path,
    is_available as docker_available,
    stop_argv as docker_stop_argv,
)
from .execution import (
    ArtifactRecovery,
    CleanupReport,
    ExecutionAdapter,
    ExecutionOutcome,
    ExecutionSpec,
    SessionInfo,
    build_adapter,
)
from .projection import project_event
from .rpc import PiRpcClient

logger = logging.getLogger("pi_glink.kernel.runner")

#: Artifact the research contract asks pi to produce.
REPORT_FILENAME = "report.md"
#: Host-owned artifact directory inside the run directory.
ARTIFACTS_DIRNAME = "artifacts"
#: Per-run pi configuration directory inside the run directory.
RUNTIME_DIRNAME = "pi-agent"
#: System prompt appended to every run (pi's own prompt stays untouched).
RESEARCH_PROMPT_FILENAME = "research-prompt.md"
#: Ceiling for artifacts the host reads back and serves.
REPORT_MAX_BYTES = 32 * 1024 * 1024
#: Ceiling for one file touched by the workspace credential sweep.
SWEEP_MAX_BYTES = 2 * 1024 * 1024
#: Bound on how many files one sweep may visit (a runaway workspace must not
#: turn run teardown into an unbounded walk).
SWEEP_MAX_FILES = 2000
#: Delta events whose split-credential protection needs stream state.
_DELTA_EVENTS = frozenset({events.ASSISTANT_DELTA, events.THINKING_DELTA})
#: Frames that end a streaming block; the withheld redaction tail is flushed
#: before their own product events so it still belongs to the block it came from.
_STREAM_BOUNDARY_FRAMES = frozenset(
    {"turn_end", "message_end", "agent_end", "agent_settled"}
)
_USAGE_FIELDS = ("input", "output", "cacheRead", "cacheWrite", "totalTokens")


class RunBusy(RuntimeError):
    """Raised when a run is requested while another one is active."""

    def __init__(self, active_run_id: str) -> None:
        super().__init__(f"run {active_run_id} is still active")
        self.active_run_id = active_run_id


class EventLogError(RuntimeError):
    """The durable event log could not be written; the frame must stay hidden."""


@dataclass
class _Execution:
    """Mutable per-run handles held for as long as the run is alive."""

    record: RunRecord
    workspace: Path
    #: The instruction as the user wrote it. ``record.prompt`` is the redacted
    #: copy that may be persisted and shown; the kernel receives this one.
    kernel_prompt: str = ""
    cancel_requested: bool = False
    adapter: ExecutionAdapter | None = None
    runtime: AgentRuntime | None = None
    stream: StreamRedactor | None = None
    #: Set once the execution thread finished its teardown (shutdown waits on it).
    done: threading.Event = field(default_factory=threading.Event)
    #: Run-level redaction marker (per run, never shared between runs).
    redacted: bool = False
    report_path: Path | None = None
    report_status: str = "none"
    model_stop_reason: str | None = None
    model_error: str | None = None
    event_log_error: str | None = None
    usage_total: dict[str, Any] = field(default_factory=dict)
    usage_keys: set[str] = field(default_factory=set)
    cleanup: CleanupReport | None = None
    artifact_failures: list[str] = field(default_factory=list)


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
            # A user can paste a credential into the prompt; the stored and
            # displayed copy is masked, while the kernel still receives the
            # original instruction.
            record = self.store.create(
                self.redactor.redact(text), run_id=run_id, workspace=workspace
            )
            execution = _Execution(
                record=record, workspace=workspace, kernel_prompt=text
            )
            self._executions[run_id] = execution
            self._active_run_id = run_id
            # Register the run before the caller gets the id back: a browser
            # that opens the event stream immediately must see an open run, not
            # a 404 race with the execution thread's first line.
            self.bus.open_run(run_id)

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
        The flag is what ends the run (the execution loop checks it around every
        blocking step); the face's own cancel only shortens the wait, so a
        failed abort is logged and does not turn the request into a rejection.
        """
        with self._lock:
            execution = self._executions.get(run_id)
        if execution is None:
            return False
        execution.cancel_requested = True
        adapter = execution.adapter
        if adapter is not None:
            try:
                adapter.cancel()
            except Exception:  # noqa: BLE001 - teardown is best effort here
                logger.debug("adapter cancel failed run=%s", run_id, exc_info=True)
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

    def shutdown(self, timeout: float = 15.0) -> list[str]:
        """Cancel and wait for every run owned by this process.

        Returns the run ids that did **not** finish releasing within the bound:
        a cleanup failure is reported, not hidden behind a shorter timeout. The
        host calls this on exit; tests call it with a synthetic run.
        """
        with self._lock:
            executions = list(self._executions.values())
        if not executions:
            return []
        for execution in executions:
            execution.cancel_requested = True
            adapter = execution.adapter
            if adapter is not None:
                try:
                    adapter.cancel()
                except Exception:  # noqa: BLE001 - the wait below is the bound
                    logger.debug(
                        "adapter cancel failed run=%s",
                        execution.record.run_id,
                        exc_info=True,
                    )
        deadline = time.monotonic() + max(0.0, timeout)
        unreleased: list[str] = []
        for execution in executions:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                execution.done.wait(remaining)
            if not execution.done.is_set():
                unreleased.append(execution.record.run_id)
        if unreleased:
            logger.error(
                "宿主退出前仍有 run 未在 %.1fs 内释放: %s",
                timeout,
                ", ".join(unreleased),
            )
        return unreleased

    # -- restart reconciliation ----------------------------------------------

    def reconcile(self) -> list[str]:
        """Close out runs left ``running`` by a previous host process.

        A run whose process is gone cannot continue; it becomes ``failed`` with
        ``stop_reason=interrupted`` and a terminal frame appended to its log, so
        the history is honest instead of showing a run that never ends. External
        resources (container/sandbox) are **not** touched: their durable identity
        is recorded in the run record and reported by
        :meth:`orphaned_resources` for a bounded operator decision.
        """
        reconciled: list[str] = []
        with self._lock:
            active = set(self._executions)
        for record in self.store.list():
            if record.status != RUNNING or record.run_id in active:
                continue
            self._recover_abandoned_run(record)
            reconciled.append(record.run_id)
        if reconciled:
            logger.warning(
                "识别到 %d 个上次进程遗留的 running run，已按中断收口: %s",
                len(reconciled),
                ", ".join(reconciled),
            )
        return reconciled

    def _recover_abandoned_run(self, record: RunRecord) -> None:
        run_id = record.run_id
        external = {
            key: value
            for key, value in (
                ("container", record.container_name),
                ("sandbox", record.sandbox_id),
            )
            if value
        }
        payload = {
            "status": FAILED,
            "stopReason": "interrupted",
            "error": "宿主进程重启时该 run 仍在运行，已按中断收口",
            "reportStatus": "none",
            "external": external,
            "cleanup": {
                "released": False,
                "failures": ["宿主进程重启，未自动回收外部资源"],
                "external": external,
            },
        }
        try:
            seq = self._last_persisted_seq(run_id) + 1
            self.store.append_event(
                run_id,
                {
                    "type": events.RUN_FAILED,
                    "runId": run_id,
                    "seq": seq,
                    "ts": time.time(),
                    "payload": payload,
                },
            )
        except OSError as exc:
            logger.error("遗留 run 的终态事件写入失败 run=%s: %s", run_id, exc)
        try:
            self.store.update(
                run_id,
                status=FAILED,
                finished_at=time.time(),
                stop_reason="interrupted",
                error=payload["error"],
                report_status="none",
                cleanup=payload["cleanup"],
                event_count=self.store.count_events(run_id),
            )
        except Exception:  # noqa: BLE001 - one broken record must not stop startup
            logger.exception("遗留 run 的元数据收口失败 run=%s", run_id)

    def _last_persisted_seq(self, run_id: str) -> int:
        last = 0
        for event in self.store.iter_events(run_id):
            seq = event.get("seq")
            if isinstance(seq, int):
                last = max(last, seq)
        return last

    def orphaned_resources(self) -> list[dict[str, Any]]:
        """Runs whose external resources may still exist, for operator review."""
        with self._lock:
            active = set(self._executions)
        resources: list[dict[str, Any]] = []
        for record in self.store.list():
            if record.run_id in active:
                continue
            if not (record.container_name or record.sandbox_id):
                continue
            resources.append(
                {
                    "runId": record.run_id,
                    "status": record.status,
                    "stopReason": record.stop_reason,
                    "container": record.container_name,
                    "sandbox": record.sandbox_id,
                    "cleanup": record.cleanup,
                }
            )
        return resources

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

            runtime = self._prepare_runtime(execution)
            adapter = self._build_adapter(execution, runtime)
            execution.adapter = adapter
            adapter.start()
            self._store_external_identity(execution, adapter)

            session = adapter.session_info()
            if session is not None:
                self._publish_session_ready(execution, session)

            if execution.cancel_requested:
                # Cancelled while the kernel was starting: never start the work.
                status, stop_reason = CANCELLED, "abort"
            else:
                adapter.deliver_input()
                deadline = time.monotonic() + self.config.task_timeout_seconds
                outcome = adapter.wait(
                    cancel=lambda: execution.cancel_requested, deadline=deadline
                )
                status, stop_reason, error = self._resolve_status(execution, outcome)
        except Exception as exc:  # noqa: BLE001 - every run must end with a reason
            if execution.cancel_requested:
                # Stopping a run closes the sandbox (or aborts pi); whatever
                # error that surfaces is the cancellation, not a failure.
                status = CANCELLED
                stop_reason = "abort"
                error = None
                logger.info("run cancelled run=%s (%s)", run_id, type(exc).__name__)
            elif isinstance(exc, EventLogError):
                logger.error("run failed run=%s: %s", run_id, exc)
                status = FAILED
                stop_reason = "event_log_failed"
                error = str(exc)
            else:
                logger.exception("run failed run=%s", run_id)
                status = FAILED
                stop_reason = "execution_error"
                error = f"{type(exc).__name__}: {exc}"
        finally:
            self._finish(execution, status, stop_reason, error)

    def _finish(
        self,
        execution: _Execution,
        status: str,
        stop_reason: str | None,
        error: str | None,
    ) -> None:
        """Land the result, recover artifacts, then release resources.

        Each step is guarded on its own: a cleanup error cannot overwrite the
        run's result, and a persistence error cannot leave the active slot held.
        """
        run_id = execution.record.run_id
        try:
            self._flush_streams(execution)
        except Exception:  # noqa: BLE001 - a flush must not block teardown
            logger.exception("flushing stream redaction state failed run=%s", run_id)

        adapter = execution.adapter
        if adapter is not None:
            # Recover work products before the face is torn down: the sandbox
            # cannot be read after it is destroyed.
            try:
                recovery = adapter.recover_artifacts(execution.workspace)
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                recovery = ArtifactRecovery(
                    failures=(f"产物回收失败: {type(exc).__name__}: {exc}",)
                )
            execution.artifact_failures.extend(recovery.failures)
            try:
                execution.cleanup = adapter.close()
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                execution.cleanup = CleanupReport(
                    released=False,
                    failures=(f"关闭执行面失败: {type(exc).__name__}: {exc}",),
                )
        else:
            execution.cleanup = CleanupReport(released=True)

        try:
            if self._sanitize_workspace(execution.workspace):
                execution.redacted = True
        except Exception:  # noqa: BLE001 - sweep is defence in depth
            logger.exception("workspace credential sweep failed run=%s", run_id)
        try:
            if self._sanitize_session(execution):
                execution.redacted = True
        except Exception:  # noqa: BLE001
            logger.exception("session credential sweep failed run=%s", run_id)
        try:
            self._recover_report(execution)
        except Exception:  # noqa: BLE001 - reported as a rejected artifact
            logger.exception("report recovery failed run=%s", run_id)
            execution.report_status = "rejected"
            execution.report_path = None

        if status == FINISHED and execution.report_path is None and stop_reason is None:
            stop_reason = (
                "report_rejected" if execution.report_status == "rejected" else "no_report"
            )
        try:
            self._finalize_record(execution, status, stop_reason, error)
        finally:
            execution.done.set()
            self._release_slot(run_id)

    # -- status resolution ----------------------------------------------------

    def _resolve_status(
        self, execution: _Execution, outcome: ExecutionOutcome
    ) -> tuple[str, str | None, str | None]:
        """Combine execution, model result and cancellation into one verdict.

        ``agent_settled`` is deliberately not the success signal: pi can settle
        after an error stop reason or a truncated (``length``) response. The
        artifact policy is applied later, in :meth:`_finish`.
        """
        if execution.event_log_error:
            return FAILED, "event_log_failed", execution.event_log_error
        if execution.cancel_requested or outcome.cancelled:
            return CANCELLED, "abort", None
        if outcome.timed_out:
            return FAILED, "timeout", outcome.error or "run timed out"
        if outcome.error:
            return FAILED, outcome.stop_reason or "execution_error", outcome.error
        stop = execution.model_stop_reason
        if stop == "error":
            return (
                FAILED,
                "model_error",
                execution.model_error or "模型返回了错误结果",
            )
        if stop == "aborted":
            return FAILED, "aborted", execution.model_error or "模型输出被中断"
        if stop == "length":
            # The execution succeeded, but the answer was cut off at the output
            # limit. That is a delivery fact the record must carry.
            return FINISHED, "length", None
        return FINISHED, None, None

    # -- pi configuration -----------------------------------------------------

    def _prepare_runtime(self, execution: _Execution) -> AgentRuntime:
        """Render the run's own pi configuration and session directory.

        One run, one runtime directory: nothing in ``data_root/pi-agent`` is
        shared or mutated by a run any more, so two runs cannot overwrite each
        other's credentials, extensions or pi session state. The session
        directory is the run's subdirectory of the configured session root
        (``PI_CODING_AGENT_SESSION_DIR``), which keeps that setting meaningful
        while keeping two runs apart.
        """
        run_id = execution.record.run_id
        run_dir = self.store.run_dir(run_id)
        runtime = prepare_agent_dir(
            self.config,
            runtime_dir=run_dir / RUNTIME_DIRNAME,
            session_dir=Path(self.config.session_dir) / run_id,
        )
        execution.runtime = runtime
        # The search credential only exists in the rendered file, so it is
        # learned here (never logged, never returned).
        self.redactor.add_from_file(runtime.directory / "web-search.json")
        execution.stream = self.redactor.stream()
        return runtime

    # -- adapters -------------------------------------------------------------

    def _build_adapter(
        self, execution: _Execution, runtime: AgentRuntime
    ) -> ExecutionAdapter:
        run_id = execution.record.run_id
        spec = ExecutionSpec(
            config=self.config,
            runtime=runtime,
            run_id=run_id,
            prompt=execution.kernel_prompt or execution.record.prompt,
            system_prompt=self._research_prompt_text(),
            workspace=execution.workspace,
            timeout_seconds=self.config.task_timeout_seconds,
            extra_argv=tuple(self._extra_argv(run_id)),
        )
        return build_adapter(
            spec,
            executor=self.config.executor,
            client_factory=self._client_factory,
            on_frame=lambda frame: self._on_frame(execution, frame),
            sandbox_factory=self._sandbox_factory,
            rpc_plan=self._rpc_plan,
            docker_plan=self._docker_plan,
            docker_stop=docker_stop_argv,
        )

    def _rpc_plan(
        self, spec: ExecutionSpec
    ) -> tuple[list[str], dict[str, str], Path | None]:
        return (
            build_pi_argv(spec.config, spec.runtime, extra=list(spec.extra_argv)),
            build_pi_env(spec.config, spec.runtime),
            spec.workspace,
        )

    def _docker_plan(
        self, spec: ExecutionSpec
    ) -> tuple[list[str], dict[str, str], str]:
        if not docker_available():
            raise ConfigError(
                "LOCAL_WEB_EXECUTOR=docker requires a reachable Docker daemon"
            )
        plan = build_docker_plan(
            spec.config,
            spec.runtime,
            run_id=spec.run_id,
            workspace=spec.workspace,
            session_dir=spec.runtime.session_dir,
            extra=list(spec.extra_argv),
        )
        return plan.argv, plan.env, plan.container_name

    def _store_external_identity(
        self, execution: _Execution, adapter: ExecutionAdapter
    ) -> None:
        """Persist the container/sandbox identity as soon as it exists.

        Without it, a host restart could not tell an operator which leftover
        resource belongs to which run; with it, reconciliation is bounded to the
        named resources instead of guessing.
        """
        identity = adapter.identity()
        changes: dict[str, Any] = {}
        if identity.get("container"):
            changes["container_name"] = identity["container"]
        if identity.get("sandbox"):
            changes["sandbox_id"] = identity["sandbox"]
        if changes:
            self.store.update(execution.record.run_id, **changes)

    def _extra_argv(self, run_id: str) -> list[str]:
        extra = ["--name", f"pi-glink-{run_id}"]
        text = self._research_prompt_text()
        if text:
            extra.extend(["--append-system-prompt", text])
        return extra

    def _research_prompt_text(self) -> str:
        path = self.config.agent_config_dir / RESEARCH_PROMPT_FILENAME
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8").strip()

    # -- event projection -----------------------------------------------------

    def _on_frame(self, execution: _Execution, frame: Mapping[str, Any]) -> None:
        run_id = execution.record.run_id
        if frame.get("type") in _STREAM_BOUNDARY_FRAMES:
            self._flush_streams(execution)
        for event_type, payload in project_event(frame, self.redactor):
            try:
                if event_type in _DELTA_EVENTS:
                    payload = self._stream_delta(execution, event_type, payload)
                    if payload is None:
                        continue
                elif event_type == events.USAGE_REPORTED:
                    payload = self._accumulate_usage(execution, frame, payload)
                    if payload is None:
                        continue
                self._emit(run_id, event_type, payload)
            except EventLogError as exc:
                # The durable log is the fact source: a frame that cannot be
                # committed is not shown either. End the run now instead of
                # letting the reader thread keep collecting unpersisted frames.
                execution.event_log_error = str(exc)
                logger.error("event log failed run=%s: %s", run_id, exc)
                return
        self._record_model_result(execution, frame)

    def _record_model_result(
        self, execution: _Execution, frame: Mapping[str, Any]
    ) -> None:
        frame_type = frame.get("type")
        if frame_type == "message_end":
            message = frame.get("message")
            if isinstance(message, Mapping) and message.get("role") == "assistant":
                stop = message.get("stopReason") or message.get("stop_reason")
                if isinstance(stop, str) and stop:
                    execution.model_stop_reason = stop
                error = message.get("errorMessage")
                execution.model_error = (
                    error if isinstance(error, str) and error.strip() else None
                )
        elif frame_type == "auto_retry_end" and not frame.get("success"):
            final = frame.get("finalError")
            if isinstance(final, str) and final.strip():
                execution.model_error = final

    def _stream_delta(
        self, execution: _Execution, event_type: str, payload: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        stream = execution.stream
        text = payload.get("text")
        if stream is None or not isinstance(text, str) or not text:
            return dict(payload)
        safe = stream.push(event_type, text)
        if not safe:
            # The whole delta was withheld: it may be the prefix of a
            # credential. It is emitted once the value is recognisable.
            return None
        return {**payload, "text": safe}

    def _flush_streams(self, execution: _Execution) -> None:
        stream = execution.stream
        if stream is None:
            return
        for kind in (events.THINKING_DELTA, events.ASSISTANT_DELTA):
            safe = stream.flush(kind)
            if not safe:
                continue
            try:
                self._emit(execution.record.run_id, kind, {"text": safe})
            except EventLogError as exc:
                execution.event_log_error = str(exc)
                return

    def _accumulate_usage(
        self, execution: _Execution, frame: Mapping[str, Any], payload: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Turn one response's usage snapshot into a run cumulative total.

        pi reports the usage of *one assistant response* (its own fields are
        already cumulative for that request). The run total is the sum across
        responses; a frame carrying the same message identity is not counted
        twice. No price is invented: ``cost`` is summed only when pi reported it.
        """
        snapshot = payload.get("usage")
        if not isinstance(snapshot, Mapping):
            return None
        key = self._usage_key(frame)
        if key is not None:
            if key in execution.usage_keys:
                return None
            execution.usage_keys.add(key)
        total = execution.usage_total
        for name in _USAGE_FIELDS:
            value = snapshot.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total[name] = total.get(name, 0) + value
        cost = snapshot.get("cost")
        if isinstance(cost, Mapping):
            cost_total = total.setdefault("cost", {})
            for name, value in cost.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    cost_total[name] = cost_total.get(name, 0) + value
        total["requests"] = total.get("requests", 0) + 1
        return {"usage": dict(total), "requestUsage": dict(snapshot)}

    @staticmethod
    def _usage_key(frame: Mapping[str, Any]) -> str | None:
        message = frame.get("message")
        if not isinstance(message, Mapping):
            return None
        for name in ("timestamp", "id"):
            value = message.get(name)
            if isinstance(value, str) and value:
                return f"{name}:{value}"
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return f"{name}:{value}"
        return None

    def _publish_session_ready(
        self, execution: _Execution, session: SessionInfo
    ) -> None:
        run_id = execution.record.run_id
        session_file = session.session_file
        if isinstance(session_file, str) and session_file and self.config.executor == "docker":
            assert execution.runtime is not None
            mapped = self._host_session_file(session_file, execution.runtime)
            if mapped is None:
                logger.warning(
                    "container session path does not map back to the host mount: %s",
                    session_file,
                )
            else:
                session_file = mapped
        if session.session_id or session_file:
            self.store.update(
                run_id,
                session_id=session.session_id,
                session_file=session_file,
            )
        payload: dict[str, Any] = dict(session.payload)
        payload["sessionFile"] = session_file
        self._emit(run_id, events.SESSION_READY, payload)

    def _host_session_file(self, reported: str, runtime: AgentRuntime) -> str | None:
        """Translate a pi-reported container path into its host location.

        The in-process face reports host paths directly. The Docker face
        reports container paths (``/sessions/…``) while the file itself lives in
        the run's mounted session directory; only the host path can be read back
        for sanitising. Only the session mount is translated: an unmappable path
        is refused instead of being trusted.
        """
        mapped = docker_host_session_path(reported, runtime.session_dir)
        return str(mapped) if mapped is not None else None

    # -- artifacts ------------------------------------------------------------

    def _recover_report(self, execution: _Execution) -> None:
        """Validate the report and copy it to a host-owned, redacted artifact.

        The kernel-produced file is never served directly and never written
        through: it is read through the safe artifact reader (no symlink, regular
        file only, bounded), redacted, and published under
        ``<run_dir>/artifacts/report.md``. A file that cannot be validated is a
        visible ``report_rejected`` with a warning event — not a fake report.
        """
        run_id = execution.record.run_id
        candidate = execution.workspace / REPORT_FILENAME
        if not (candidate.exists() or candidate.is_symlink()):
            execution.report_status = "missing"
            return
        try:
            data = read_regular_file(
                candidate, max_bytes=REPORT_MAX_BYTES, root=execution.workspace
            )
        except ArtifactError as exc:
            execution.report_status = "rejected"
            execution.report_path = None
            self._report_warning(run_id, f"report.md 未通过安全校验，已拒绝：{exc}")
            return
        redacted = self.redactor.redact(data.decode("utf-8", errors="replace"))
        if redacted != data.decode("utf-8", errors="replace"):
            execution.redacted = True
        destination = self.store.run_dir(run_id) / ARTIFACTS_DIRNAME / REPORT_FILENAME
        try:
            replace_with_text(destination, redacted, max_bytes=REPORT_MAX_BYTES)
        except (ArtifactError, OSError) as exc:
            execution.report_status = "rejected"
            execution.report_path = None
            self._report_warning(run_id, f"report.md 无法写入宿主产物目录，已拒绝：{exc}")
            return
        execution.report_path = destination
        execution.report_status = "ok"

    def _report_warning(self, run_id: str, detail: str) -> None:
        logger.warning("run=%s %s", run_id, detail)
        try:
            self._emit(
                run_id,
                events.KERNEL_STATUS,
                {"level": "warning", "message": "report_rejected", "detail": detail},
            )
        except EventLogError as exc:
            logger.error("report warning could not be persisted run=%s: %s", run_id, exc)

    def _sanitize_session(self, execution: _Execution) -> bool:
        """Mask credentials pi wrote into its own session log.

        The kernel owns that file, so redaction cannot happen on the way in; it
        happens here on the way out. The path is only trusted inside the run's
        own session directory — a kernel-reported path elsewhere on the host is
        refused rather than rewritten.
        """
        record = self.store.get(execution.record.run_id)
        session_file = record.session_file if record is not None else None
        if not session_file:
            return False
        runtime = execution.runtime
        if runtime is None:
            return False
        return self._sanitize_session_file(session_file, root=runtime.session_dir)

    def _sanitize_session_file(self, session_file: str, *, root: Path) -> bool:
        """Redact one session file; returns whether its bytes changed."""
        try:
            original = read_regular_file(
                session_file, max_bytes=REPORT_MAX_BYTES, root=root
            ).decode("utf-8", errors="replace")
        except ArtifactError as exc:
            logger.warning("会话文件未通过安全校验，拒绝改写: %s (%s)", session_file, exc)
            return False
        redacted = self.redactor.redact(original)
        if redacted == original:
            return False
        try:
            replace_with_text(session_file, redacted, max_bytes=REPORT_MAX_BYTES)
        except (ArtifactError, OSError) as exc:
            logger.error("failed to redact session file: %s (%s)", session_file, exc)
            return False
        logger.warning("session file contained credentials and was redacted: %s", session_file)
        return True

    def _sanitize_workspace(self, workspace: Path) -> bool:
        """Redact credentials in every text artifact the run left behind.

        The report is not the only file a tool echo can reach; a ``write`` call
        can leak the same value into any artifact the operator later opens. The
        sweep is bounded (file count, size cap, binary skip) and symlink-safe:
        linked files and directories are skipped entirely, and every rewrite is
        an atomic replace, so a link planted in the workspace can neither be
        followed for reading nor written through.
        """
        if not workspace.is_dir():
            return False
        changed = False
        for path in self._iter_workspace_files(workspace):
            try:
                raw = read_regular_file(
                    path, max_bytes=SWEEP_MAX_BYTES, root=workspace
                )
            except ArtifactError:
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
                replace_with_text(path, redacted, max_bytes=SWEEP_MAX_BYTES)
            except (ArtifactError, OSError) as exc:
                logger.error("failed to redact artifact: %s (%s)", path, exc)
                continue
            changed = True
            logger.warning("artifact contained credentials and was redacted: %s", path)
        return changed

    @staticmethod
    def _iter_workspace_files(workspace: Path) -> list[Path]:
        """Bounded, symlink-free walk of the run workspace (files only)."""
        found: list[Path] = []
        stack = [workspace]
        while stack and len(found) < SWEEP_MAX_FILES:
            directory = stack.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError:
                continue
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    found.append(Path(entry.path))
                    if len(found) >= SWEEP_MAX_FILES:
                        break
        return found

    # -- persistence ----------------------------------------------------------

    def _finalize_record(
        self,
        execution: _Execution,
        status: str,
        stop_reason: str | None,
        error: str | None,
    ) -> None:
        run_id = execution.record.run_id
        previous = self.store.get(run_id) or execution.record
        cleanup = execution.cleanup or CleanupReport(released=True)
        finished_at = time.time()
        terminal_type = (
            events.RUN_FAILED
            if status == FAILED
            else events.RUN_CANCELLED
            if status == CANCELLED
            else events.RUN_FINISHED
        )
        try:
            self._emit(
                run_id,
                terminal_type,
                {
                    "status": status,
                    "stopReason": stop_reason,
                    "error": self.redactor.redact(error) if error else None,
                    "reportPath": str(execution.report_path) if execution.report_path else None,
                    "reportStatus": execution.report_status,
                    "modelStopReason": execution.model_stop_reason,
                    "sessionId": previous.session_id,
                    "sessionFile": previous.session_file,
                    "usage": execution.usage_total or None,
                    "durationSeconds": round(finished_at - previous.created_at, 3),
                    "cleanup": {
                        "released": cleanup.released,
                        "failures": list(cleanup.failures),
                        "external": dict(cleanup.external),
                    },
                    "artifactFailures": list(execution.artifact_failures),
                },
            )
        except EventLogError as exc:
            # The log is the replay fact source, so the frame is not published.
            # The run record must still reach a terminal state: a failed append
            # may not leave a finished run looking like it is still running.
            logger.error("terminal event could not be persisted run=%s: %s", run_id, exc)
            execution.artifact_failures.append(f"终帧未能写入事件日志: {exc}")
            if error is None:
                error = str(exc)
        redacted_error = self.redactor.redact(error) if error else None
        # The count is read back from the durable log after the terminal frame,
        # so ``event_count`` always equals the lines a reader would see.
        event_count = self.store.count_events(run_id)
        changes: dict[str, Any] = {
            "status": status,
            "finished_at": finished_at,
            "report_path": str(execution.report_path) if execution.report_path else None,
            "error": redacted_error,
            "stop_reason": stop_reason,
            "usage": execution.usage_total or None,
            "redacted": execution.redacted,
            "model_stop_reason": execution.model_stop_reason,
            "report_status": execution.report_status,
            "cleanup": {
                "released": cleanup.released,
                "failures": list(cleanup.failures),
                "external": dict(cleanup.external),
            },
            "event_count": event_count,
        }
        try:
            self.store.update(run_id, **changes)
        except Exception:  # noqa: BLE001 - retry with the minimum state
            logger.exception("run record update failed run=%s; retrying", run_id)
            try:
                self.store.update(
                    run_id,
                    status=status,
                    finished_at=finished_at,
                    error=redacted_error,
                    stop_reason=stop_reason,
                    event_count=event_count,
                )
            except Exception:  # noqa: BLE001 - reported loudly, never hidden
                logger.critical(
                    "run record could not be finalised run=%s; status=%s",
                    run_id,
                    status,
                )
        finally:
            self.bus.close(run_id)
        logger.info(
            "run finished run=%s status=%s stop=%s report=%s released=%s",
            run_id,
            status,
            stop_reason,
            execution.report_path or "<none>",
            cleanup.released,
        )

    def _release_slot(self, run_id: str) -> None:
        with self._lock:
            self._executions.pop(run_id, None)
            if self._active_run_id == run_id:
                self._active_run_id = None

    def _emit(
        self, run_id: str, event_type: str, payload: Mapping[str, Any] | None = None
    ) -> events.RunEvent:
        # Belt-and-braces: projection already redacts, but host-owned events
        # (run_finished, kernel_status, …) are built here, so the same boundary
        # runs again on the way out. The frame is persisted before it is shown.
        safe_payload = (
            self.redactor.redact_payload(dict(payload)) if payload is not None else None
        )
        try:
            return self.bus.commit(
                run_id,
                event_type,
                safe_payload,
                persist=lambda event: self.store.append_event(
                    run_id, event.as_dict()
                ),
            )
        except OSError as exc:
            raise EventLogError(
                f"事件日志写入失败: {type(exc).__name__}: {exc}"
            ) from exc
