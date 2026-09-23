"""Run metadata and event log persistence — files, no database.

A run owns one directory:

    <data_root>/runs/<run_id>/
    ├── meta.json      status, prompt, workspace, session file, report path
    ├── events.jsonl   every projected event, one JSON object per line
    └── … (nothing else: artifacts live in the run's workspace)

The store is deliberately small. The workbench needs run history, reconnect
support and an auditable event log, none of which require a database yet; when
multi-user or multi-tenant lands, this module is the seam that changes.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Iterator, Mapping

#: Run states. ``running`` is the only non-terminal one.
RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL_STATUSES = frozenset({FINISHED, FAILED, CANCELLED})

_META_NAME = "meta.json"
_EVENTS_NAME = "events.jsonl"


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
    #: ``{"released": bool, "failures": [...], "external": {...}}`` for the
    #: process/container/sandbox teardown; a failure here is reported, not hidden.
    cleanup: Mapping[str, Any] | None = None
    #: Durable external identity for restart reconciliation (never guessed).
    container_name: str | None = None
    sandbox_id: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


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
        with self._lock:
            record = self.get(run_id)
            if record is None:
                raise KeyError(run_id)
            allowed = {field.name for field in fields(RunRecord)}
            unknown = set(changes) - allowed
            if unknown:
                raise ValueError(f"unknown run fields: {sorted(unknown)}")
            updated = replace(record, **changes)
            self._write(updated)
            return updated

    def get(self, run_id: str) -> RunRecord | None:
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

    def iter_events(self, run_id: str, after: int = 0) -> Iterator[dict[str, Any]]:
        """Yield persisted events with ``seq > after``, in file order.

        The in-memory bus only knows runs started by the current host process;
        after a restart this file is the only record of a run's timeline, so
        history replay reads it. Malformed lines are skipped, not fatal: a torn
        last line from a crash must not hide the rest of the timeline.
        """
        path = self.events_path(run_id)
        if not path.is_file():
            return
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    continue
                seq = event.get("seq")
                if isinstance(seq, int) and seq <= after:
                    continue
                yield event

    def append_event(self, run_id: str, event: Mapping[str, Any]) -> None:
        path = self.events_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False)
        with self._lock:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def count_events(self, run_id: str) -> int:
        """Number of readable persisted events (what a reader would see)."""
        return sum(1 for _event in self.iter_events(run_id))

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
