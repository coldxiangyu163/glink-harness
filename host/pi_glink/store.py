"""Run metadata and event log persistence — files, no database.

A run owns one directory:

    <data_root>/runs/<run_id>/
    ├── meta.json          admission facts, in-flight facts, settled terminal copy
    ├── events.jsonl       every committed event, one JSON object per line
    ├── pi-agent/          the run's runtime configuration (PI_CODING_AGENT_DIR)
    └── artifacts/report.md  the host-owned, redacted report copy

**Readers see only the committed prefix of a log.** The owning process keeps a
commit watermark per event log (:class:`_CommitWatermarks`): bytes past it are
being written and are invisible to every reader in the process. An ordinary
frame moves the watermark once its line is completely written; the terminal
frame moves it only after ``fsync`` returned. A failed write or sync is
truncated away below the watermark, so no reader ever saw it. After a restart
there is no watermark and the file itself is the committed record.

**The terminal frame is the terminal commit.** A run becomes terminal at exactly
one point: the watermark passing its durably synced ``run_finished`` /
``run_failed`` / ``run_cancelled`` line (:meth:`RunStore.commit_terminal`), after
which the log is sealed — nothing can follow the terminal frame. That frame's
payload is the terminal record (verdict, report, cleanup). ``meta.json`` carries
the admission and in-flight facts; its terminal fields are only a *settled copy*
written after the commit. :meth:`RunStore.get` therefore projects a committed
terminal frame onto a record whose ``meta.json`` still says ``running``: every
reader — run detail, run list, report download, SSE replay — sees the same
committed fact, never before it is durable, and never taken back.

The store is deliberately small. The workbench needs run history, reconnect
support and an auditable event log, none of which require a database yet; when
multi-user or multi-tenant lands, this module is the seam that changes.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Iterator, Mapping

from .events import TERMINAL_EVENT_TYPES

#: Run states. ``running`` is the only non-terminal one.
RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL_STATUSES = frozenset({FINISHED, FAILED, CANCELLED})

#: Artifact the research contract asks pi to produce.
REPORT_FILENAME = "report.md"
#: Host-owned artifact directory inside the run directory.
ARTIFACTS_DIRNAME = "artifacts"

_META_NAME = "meta.json"
_EVENTS_NAME = "events.jsonl"
_TAIL_CHUNK = 64 * 1024
_STATUS_BY_EVENT = {
    "run_finished": FINISHED,
    "run_failed": FAILED,
    "run_cancelled": CANCELLED,
}


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Everything the workbench shows about a run, minus its event stream."""

    run_id: str
    prompt: str
    status: str
    created_at: float
    workspace: str
    finished_at: float | None = None
    session_id: str | None = None
    session_file: str | None = None
    report_path: str | None = None
    error: str | None = None
    stop_reason: str | None = None
    event_count: int = 0
    usage: Mapping[str, Any] | None = None
    #: True when a credential was masked in the event stream or the report.
    redacted: bool = False
    #: pi's final assistant ``stopReason`` (``error``/``length``/``aborted``/…).
    #: Kept separately from ``status``: a truncated answer is a finished run with
    #: an incomplete delivery, not a successful one.
    model_stop_reason: str | None = None
    #: ``none`` | ``missing`` | ``ok`` | ``rejected`` — what happened to report.md.
    report_status: str = "none"
    #: ``{"released": true|false|null, "failures": [...], "external": {...}}`` —
    #: whether the process/container/sandbox is confirmed released (``null`` means
    #: unknown). ``None`` for records written before resources were tracked.
    cleanup: Mapping[str, Any] | None = None
    #: Durable external identity for restart reconciliation (never guessed).
    container_name: str | None = None
    sandbox_id: str | None = None
    #: Operator decision that an unconfirmed resource is released
    #: (``{"decision": "released", "note": …, "at": …}``); never set automatically.
    resource_adjudication: Mapping[str, Any] | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def holds_resources(self) -> bool:
        """True while this run's resources are not confirmed released.

        A run that is still ``running`` holds its resources by definition; a
        terminal run holds them until its cleanup reported ``released: true`` or
        an operator adjudicated them. Records that predate resource tracking
        (no ``cleanup``) carry no claim either way.
        """
        if not self.is_terminal:
            return True
        if self.cleanup is None or self.resource_adjudication is not None:
            return False
        return self.cleanup.get("released") is not True


class LogSealed(RuntimeError):
    """The run's log already holds a committed terminal frame."""


class _CommitWatermarks:
    """Committed length of every event log this process appends to.

    Process-wide on purpose: the data root has one owning process
    (:mod:`pi_glink.ownership`), and every reader in it must agree on what is
    committed. A log without an entry was not touched by this process; its
    committed length is the file size observed together with the lookup, which
    is safe because a writer registers the pre-append length (under the same
    lock) before writing a single byte.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lengths: dict[str, int] = {}
        self._sealed: set[str] = set()

    def committed_end(self, path: Path) -> int | None:
        key = os.path.abspath(path)
        with self._lock:
            length = self._lengths.get(key)
            if length is not None:
                return length
            try:
                return os.stat(key).st_size
            except FileNotFoundError:
                return None

    def begin(self, path: Path, length: int) -> None:
        with self._lock:
            self._lengths[os.path.abspath(path)] = length

    def advance(self, path: Path, length: int, *, seal: bool) -> None:
        key = os.path.abspath(path)
        with self._lock:
            self._lengths[key] = length
            if seal:
                self._sealed.add(key)

    def seal(self, path: Path) -> None:
        with self._lock:
            self._sealed.add(os.path.abspath(path))

    def is_sealed(self, path: Path) -> bool:
        with self._lock:
            return os.path.abspath(path) in self._sealed

    def is_tracked(self, path: Path) -> bool:
        with self._lock:
            return os.path.abspath(path) in self._lengths


_WATERMARKS = _CommitWatermarks()


@dataclass(frozen=True, slots=True)
class ReportLocation:
    """Where a record's report may be read: a run-owned root and a fixed name."""

    root: Path
    relative: str
    layout: str


class RunStore:
    """Thread-safe run metadata store rooted at one directory."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._lock = threading.RLock()

    @property
    def root(self) -> Path:
        return self._root

    def run_dir(self, run_id: str) -> Path:
        return self._root / run_id

    def events_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / _EVENTS_NAME

    def report_artifact_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / ARTIFACTS_DIRNAME / REPORT_FILENAME

    # -- records --------------------------------------------------------------

    def create(
        self,
        prompt: str,
        *,
        run_id: str | None = None,
        workspace: Path | None = None,
    ) -> RunRecord:
        identifier = run_id or uuid.uuid4().hex[:12]
        directory = self.run_dir(identifier)
        directory.mkdir(parents=True, exist_ok=True)
        record = RunRecord(
            run_id=identifier,
            prompt=prompt,
            status=RUNNING,
            created_at=time.time(),
            workspace=str(workspace) if workspace is not None else str(directory / "workspace"),
        )
        self._write(record)
        return record

    def update(self, run_id: str, **changes: Any) -> RunRecord:
        """Change facts in ``meta.json`` (in-flight facts, adjudication)."""
        with self._lock:
            record = self._read_meta(run_id)
            if record is None:
                raise KeyError(run_id)
            allowed = {field.name for field in fields(RunRecord)}
            unknown = set(changes) - allowed
            if unknown:
                raise ValueError(f"unknown run fields: {sorted(unknown)}")
            updated = replace(record, **changes)
            self._write(updated)
            return self._project(updated)

    def get(self, run_id: str) -> RunRecord | None:
        """The run as every reader must see it (committed terminal projected)."""
        record = self._read_meta(run_id)
        return self._project(record) if record is not None else None

    def list(self, limit: int | None = 50) -> list[RunRecord]:
        if not self._root.is_dir():
            return []
        records: list[RunRecord] = []
        for entry in sorted(self._root.iterdir(), key=lambda item: item.name):
            if not entry.is_dir():
                continue
            record = self.get(entry.name)
            if record is not None:
                records.append(record)
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records if limit is None else records[:limit]

    def unsettled(self) -> list[str]:
        """Run ids whose ``meta.json`` still says ``running`` (no settled copy)."""
        if not self._root.is_dir():
            return []
        return [
            entry.name
            for entry in sorted(self._root.iterdir(), key=lambda item: item.name)
            if entry.is_dir()
            and (record := self._read_meta(entry.name)) is not None
            and record.status == RUNNING
        ]

    def settle(self, run_id: str) -> RunRecord | None:
        """Copy the committed terminal record into ``meta.json``.

        Returns the settled record, or ``None`` when nothing is committed yet.
        The copy is an optimisation for readers, never a second source: it is
        derived from the committed frame and may be rewritten at any time.
        """
        with self._lock:
            record = self._read_meta(run_id)
            if record is None:
                return None
            if record.status != RUNNING:
                return record
            terminal = self.committed_terminal(run_id)
            if terminal is None:
                return None
            settled = _apply_terminal(record, terminal)
            self._write(settled)
            return settled

    def report_location(self, record: RunRecord) -> ReportLocation | None:
        """The only places a record's ``report_path`` may point to.

        * ``runs/<id>/artifacts/report.md`` — the host-owned redacted copy;
        * ``<workspace>/report.md`` of the run's own workspace
          (``workspaces/<id>``) — the layout written before host copies
          existed; served read-only, never rewritten.

        Anything else is not a report, whatever the record says.
        """
        if not record.report_path:
            return None
        reported = Path(record.report_path)
        if reported == self.report_artifact_path(record.run_id):
            return ReportLocation(
                self.run_dir(record.run_id),
                f"{ARTIFACTS_DIRNAME}/{REPORT_FILENAME}",
                "artifact",
            )
        workspace = Path(record.workspace)
        if workspace.name == record.run_id and reported == workspace / REPORT_FILENAME:
            return ReportLocation(workspace, REPORT_FILENAME, "legacy-workspace")
        return None

    # -- events ---------------------------------------------------------------

    def iter_events(self, run_id: str, after: int = 0) -> Iterator[dict[str, Any]]:
        """Yield committed events with ``seq > after``, in file order.

        The in-memory bus only knows runs started by the current host process;
        after a restart this file is the only record of a run's timeline, so
        history replay reads it. Only the committed prefix is read. Malformed
        lines are skipped, not fatal: a torn last line from a crash must not
        hide the rest of the timeline.
        """
        path = self.events_path(run_id)
        end = _WATERMARKS.committed_end(path)
        if end is None:
            return
        try:
            handle = path.open("rb")
        except FileNotFoundError:
            return
        with handle:
            consumed = 0
            for raw in handle:
                consumed += len(raw)
                if consumed > end:
                    break
                try:
                    event = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    continue
                seq = event.get("seq")
                if isinstance(seq, int) and seq <= after:
                    continue
                yield event

    def append_event(self, run_id: str, event: Mapping[str, Any]) -> None:
        """Commit one ordinary frame: visible once its whole line is written."""
        if event.get("type") in TERMINAL_EVENT_TYPES:
            raise ValueError("终帧只能经 commit_terminal 提交")
        self._append(run_id, event, durable=False)

    def commit_terminal(self, run_id: str, event: Mapping[str, Any]) -> None:
        """Commit the run's terminal frame; the run is terminal once this returns.

        The line is written and synced before the watermark passes it, so no
        reader can observe a terminal state that a failed sync would take back.
        A failure truncates the line (and any torn-tail repair) and raises,
        leaving every reader where it was. Success seals the log.
        """
        if event.get("type") not in TERMINAL_EVENT_TYPES:
            raise ValueError("commit_terminal 只接受终帧")
        self._append(run_id, event, durable=True)

    def _append(self, run_id: str, event: Mapping[str, Any], *, durable: bool) -> None:
        path = self.events_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            descriptor = os.open(
                path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o666
            )
            try:
                size = os.fstat(descriptor).st_size
                if not _WATERMARKS.is_tracked(path) and _terminal_line(path, size) is not None:
                    _WATERMARKS.seal(path)  # committed by an earlier process
                if _WATERMARKS.is_sealed(path):
                    raise LogSealed(f"run {run_id} 已提交终帧，事件日志不再追加")
                # Readers stay at the pre-append length until the line is committed.
                _WATERMARKS.begin(path, size)
                if size and os.pread(descriptor, 1, size - 1) != b"\n":
                    line = b"\n" + line  # close a torn tail left by a crash
                try:
                    view = memoryview(line)
                    while view:
                        written = os.write(descriptor, view)
                        view = view[written:]
                    if durable:
                        os.fsync(descriptor)
                except BaseException:
                    try:
                        os.ftruncate(descriptor, size)
                    except OSError:
                        pass
                    raise
                _WATERMARKS.advance(path, size + len(line), seal=durable)
            finally:
                os.close(descriptor)

    def committed_terminal(self, run_id: str) -> dict[str, Any] | None:
        """The run's committed terminal frame, if its log holds one."""
        path = self.events_path(run_id)
        end = _WATERMARKS.committed_end(path)
        return _terminal_line(path, end) if end else None

    # -- internals ------------------------------------------------------------

    def _project(self, record: RunRecord) -> RunRecord:
        if record.status != RUNNING:
            return record
        terminal = self.committed_terminal(record.run_id)
        return _apply_terminal(record, terminal) if terminal is not None else record

    def _read_meta(self, run_id: str) -> RunRecord | None:
        path = self.run_dir(run_id) / _META_NAME
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        known = {field.name for field in fields(RunRecord)}
        return RunRecord(**{key: value for key, value in data.items() if key in known})

    def _write(self, record: RunRecord) -> None:
        directory = self.run_dir(record.run_id)
        directory.mkdir(parents=True, exist_ok=True)
        payload = asdict(record)
        temporary = directory / f".{_META_NAME}.tmp"
        with self._lock:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(directory / _META_NAME)


def _terminal_line(path: Path, end: int) -> dict[str, Any] | None:
    """The last complete line in ``path[:end]``, if it is a terminal frame.

    A sealed log ends with its terminal frame, so the last committed line is
    the only place one can be. A trailing fragment without a newline is not a
    committed line.
    """
    try:
        handle = path.open("rb")
    except OSError:
        return None
    with handle:
        data = b""
        position = end
        line: bytes | None = None
        while position > 0:
            step = min(_TAIL_CHUNK, position)
            position -= step
            handle.seek(position)
            data = handle.read(step) + data
            last = data.rfind(b"\n")
            if last == -1:
                continue  # no committed line end yet in this window
            start = data.rfind(b"\n", 0, last)
            if start != -1 or position == 0:
                line = data[start + 1 : last]
                break
        if line is None:
            return None
    try:
        event = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(event, dict) or event.get("type") not in TERMINAL_EVENT_TYPES:
        return None
    return event


def _apply_terminal(record: RunRecord, event: Mapping[str, Any]) -> RunRecord:
    """Project a committed terminal frame onto the run's admission record."""
    payload = event.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    status = payload.get("status")
    if status not in TERMINAL_STATUSES:
        status = _STATUS_BY_EVENT[event["type"]]
    ts = event.get("ts")
    seq = event.get("seq")
    report_path = payload.get("reportPath")
    cleanup = payload.get("cleanup")
    return replace(
        record,
        status=status,
        finished_at=float(ts) if isinstance(ts, (int, float)) else record.finished_at,
        stop_reason=payload.get("stopReason"),
        error=payload.get("error"),
        report_path=report_path if isinstance(report_path, str) and report_path else None,
        report_status=str(payload.get("reportStatus") or record.report_status),
        model_stop_reason=payload.get("modelStopReason"),
        usage=payload.get("usage") if isinstance(payload.get("usage"), Mapping) else None,
        redacted=bool(payload.get("redacted", record.redacted)),
        cleanup=dict(cleanup) if isinstance(cleanup, Mapping) else record.cleanup,
        event_count=seq if isinstance(seq, int) else record.event_count,
    )
