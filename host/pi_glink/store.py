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

**The in-process index is an accelerator, never a second source.** One run
record is one directory with its own ``meta.json``; listing runs, listing one
tenant's runs and deriving the resource ledger used to walk and re-parse every
one of them on every call — inside the admission lock. The data root has exactly
one owning process (:mod:`pi_glink.ownership`), so :class:`_RunIndex` builds
that projection once from disk and every write path (``create``, ``update``,
``settle``, terminal commit) refreshes its own entry. Reads keep their exact
semantics: the committed-prefix rule, the terminal projection, the newest-first
order and the multi-tenant owner filter all come from the same helpers the disk
scan uses, and :meth:`RunStore.scan_all` is the uncached reference the
regressions compare the index against.

The store is deliberately small. The workbench needs run history, reconnect
support and an auditable event log, none of which require a database yet; when
multi-user or multi-tenant lands, this module is the seam that changes.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Iterator, Mapping

from .events import TERMINAL_EVENT_TYPES

logger = logging.getLogger("pi_glink.store")

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
    #: Previous execution in the same research conversation; None for its first turn.
    parent_run_id: str | None = None
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
    #: Owner of the run, written atomically with admission. Records written by
    #: the single-user surface (or before owner tracking) carry ``None`` and are
    #: therefore invisible to every multi-tenant read: history is never
    #: imported into a tenant and never rewritten.
    tenant_id: str | None = None
    creator_user_id: str | None = None

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
class _IndexEntry:
    """One run as written to disk plus the projection every reader must see.

    ``raw`` is the ``meta.json`` record; ``projected`` additionally carries a
    committed terminal frame that the settled copy has not caught up with yet.
    """

    raw: RunRecord
    projected: RunRecord


class _RunIndex:
    """Process-wide projection of one runs root (built once, then incremental).

    The data root has a single owning process, so every :class:`RunStore` in
    this process must agree on the same records; a per-instance cache would let
    two stores disagree after one of them wrote. Readers therefore share one
    index per root, and a fresh process rebuilds it from disk on first use.

    ``lock`` guards the in-memory index state (a reader copies its snapshot
    there). ``record_lock`` is the shared per-root write lock: the metadata
    replacement and the index update happen inside it, so two writers of the
    same record cannot leave the index ordered opposite to disk — and no reader
    blocks behind a writer's disk write. ``append_lock`` serializes appends to
    the shared event logs for the same reason on the log side.

    ``unreadable`` holds run directories whose ``meta.json`` cannot be read:
    they are excluded from every record read (a corrupt record must never be
    served, least of all cross-tenant) but the ledger counts them as global
    holds until an operator adjudicates them in their own sidecar file.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.record_lock = threading.RLock()
        self.append_lock = threading.RLock()
        self.entries: dict[str, _IndexEntry] = {}
        self.unreadable: dict[str, Mapping[str, Any] | None] = {}
        self.loaded = False


#: Per-run sidecar recording an operator decision for a run whose ``meta.json``
#: cannot be parsed. The corrupt file itself is never rewritten.
UNREADABLE_ADJUDICATION_NAME = "resource-adjudication.json"


_INDEXES: dict[str, _RunIndex] = {}
_INDEXES_LOCK = threading.Lock()


def _index_for(root: Path) -> _RunIndex:
    key = os.path.abspath(root)
    with _INDEXES_LOCK:
        index = _INDEXES.get(key)
        if index is None:
            index = _RunIndex()
            _INDEXES[key] = index
        return index


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
        self._index = _index_for(root)
        # The per-root shared locks: the meta replacement and the index update
        # are one critical section, and a second RunStore over the same root
        # cannot interleave with the first on the same record.
        self._lock = self._index.record_lock
        self._events_lock = self._index.append_lock

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
        tenant_id: str | None = None,
        creator_user_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> RunRecord:
        """Admit a run and write its assignment facts in one atomic meta write.

        The owner labels (``tenant_id`` / ``creator_user_id``) are part of the
        same replacement as the run id and created-at stamp, so no reader can
        observe an ownerless run that an authenticated request must hide. The
        optional ``parent_run_id`` records the follow-up chain in the same write.
        """
        identifier = run_id or uuid.uuid4().hex[:12]
        directory = self.run_dir(identifier)
        directory.mkdir(parents=True, exist_ok=True)
        record = RunRecord(
            run_id=identifier,
            prompt=prompt,
            status=RUNNING,
            created_at=time.time(),
            workspace=str(workspace) if workspace is not None else str(directory / "workspace"),
            tenant_id=tenant_id,
            creator_user_id=creator_user_id,
            parent_run_id=parent_run_id,
        )
        return self._write(record)

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
            return self._write(replace(record, **changes))

    def get(self, run_id: str) -> RunRecord | None:
        """The run as every reader must see it (committed terminal projected)."""
        entry = self._entries().get(run_id)
        return entry.projected if entry is not None else None

    def get_for(self, run_id: str, tenant_id: str) -> RunRecord | None:
        """The run only when it belongs to ``tenant_id``.

        An unknown run, another tenant's run and a legacy record without an
        owner are all ``None``: the HTTP layer turns every one of them into the
        same 404, so existence is not disclosed across tenants.
        """
        record = self.get(run_id)
        if record is None or record.tenant_id != tenant_id:
            return None
        return record

    def list_for(self, tenant_id: str, limit: int | None = 50) -> list[RunRecord]:
        """Runs of exactly one tenant, newest first (legacy records excluded)."""
        records = [record for record in self._records() if record.tenant_id == tenant_id]
        return records if limit is None else records[:limit]

    def list(self, limit: int | None = 50) -> list[RunRecord]:
        records = self._records()
        return records if limit is None else records[:limit]

    def holding_records(self, tenant_id: str | None = None) -> list[RunRecord]:
        """Records whose execution resources are not confirmed released.

        ``tenant_id`` selects one tenant's holds; ``None`` returns every hold,
        including records without an owner (legacy and reconciled leftovers),
        because those still consume the global capacity. Newest first, like
        :meth:`list`.

        Run directories whose metadata cannot be read are not records at all;
        they are reported separately by :meth:`unreadable_holds` and count as
        global holds there.
        """
        return [
            record
            for record in self._records()
            if record.holds_resources and (tenant_id is None or record.tenant_id == tenant_id)
        ]

    def unsettled(self) -> list[str]:
        """Run ids whose ``meta.json`` still says ``running`` (no settled copy)."""
        entries = self._entries()
        return [run_id for run_id in sorted(entries) if entries[run_id].raw.status == RUNNING]

    def unreadable_holds(self) -> list[str]:
        """Run directories whose metadata cannot be read and are not adjudicated.

        Such a directory is hidden from every record read (a corrupt record must
        not be listed, downloaded or replayed, least of all across tenants), but
        it is **not** evidence that its container or sandbox is gone: the
        resource ledger counts it as a global hold until an operator records a
        decision via :meth:`adjudicate_unreadable`.
        """
        return [
            run_id
            for run_id, adjudication in sorted(self._unreadable().items())
            if adjudication is None
        ]

    def adjudicate_unreadable(self, run_id: str, adjudication: Mapping[str, Any]) -> None:
        """Record the operator decision for a run whose ``meta.json`` is unreadable.

        The corrupt ``meta.json`` is left byte-for-byte untouched; the decision
        lives in :data:`UNREADABLE_ADJUDICATION_NAME` next to it, which is what
        the index reads back on a restart. Raises ``KeyError`` when the directory
        is unknown and ``ValueError`` when its metadata is actually readable (the
        normal record path owns that decision).
        """
        with self._lock:
            entries, unreadable = self._scan()
            if run_id in {entry.raw.run_id for entry in entries}:
                raise ValueError(f"run {run_id} 的 meta.json 可读，应使用普通裁决")
            if run_id not in unreadable:
                raise KeyError(run_id)
            directory = self.run_dir(run_id)
            temporary = directory / f".{UNREADABLE_ADJUDICATION_NAME}.tmp"
            temporary.write_text(
                json.dumps(dict(adjudication), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(directory / UNREADABLE_ADJUDICATION_NAME)
            with self._index.lock:
                self._index.unreadable[run_id] = dict(adjudication)

    def scan_all(self) -> list[RunRecord]:
        """Uncached reference read: walk every run directory, project each record.

        This is what the index is built from and the comparison the regressions
        use; production reads must not call it per request.
        """
        entries, _unreadable = self._scan()
        records = [entry.projected for entry in entries]
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records

    def scan_unreadable(self) -> list[str]:
        """Uncached reference read of the unreadable, unadjudicated run ids."""
        _entries, unreadable = self._scan()
        return [run_id for run_id, value in sorted(unreadable.items()) if value is None]

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
            return self._write(_apply_terminal(record, terminal))

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
        with self._events_lock:
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
                if durable:
                    # The index must never run ahead of the committed prefix:
                    # refresh it only after the watermark passed the new line.
                    self._refresh_projection(run_id)
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

    def _entries(self) -> dict[str, _IndexEntry]:
        """Snapshot of the readable records (built once from disk on first use)."""
        index = self._index
        if not index.loaded:
            self._load_index()
        with index.lock:
            return dict(index.entries)

    def _unreadable(self) -> dict[str, Mapping[str, Any] | None]:
        """Snapshot of the unreadable run directories and their adjudications."""
        index = self._index
        if not index.loaded:
            self._load_index()
        with index.lock:
            return dict(index.unreadable)

    def _load_index(self) -> None:
        """Build the index from disk exactly once per process and root."""
        index = self._index
        with index.lock:
            if index.loaded:
                return
            entries, unreadable = self._scan()
            for entry in entries:
                index.entries[entry.raw.run_id] = entry
            index.unreadable.update(unreadable)
            index.loaded = True

    def _records(self) -> list[RunRecord]:
        """Projected records newest first (stable by directory name on ties)."""
        entries = self._entries()
        records = [entries[run_id].projected for run_id in sorted(entries)]
        records.sort(key=lambda item: item.created_at, reverse=True)
        return records

    def _scan(self) -> tuple[list[_IndexEntry], dict[str, Mapping[str, Any] | None]]:
        """Read every run directory (index build and the uncached reference).

        A directory whose ``meta.json`` cannot be read produces no record: it is
        returned as an unreadable entry together with the operator adjudication
        sidecar, if any. A directory without ``meta.json`` at all is not a run.
        """
        if not self._root.is_dir():
            return [], {}
        entries: list[_IndexEntry] = []
        unreadable: dict[str, Mapping[str, Any] | None] = {}
        for entry in sorted(self._root.iterdir(), key=lambda item: item.name):
            if not entry.is_dir():
                continue
            record, problem = self._load_meta(entry.name)
            if problem:
                unreadable[entry.name] = self._read_unreadable_adjudication(entry.name)
                logger.warning(
                    "run 记录不可读，已计入全局资源持有直至人工裁决 run=%s", entry.name
                )
            elif record is not None:
                entries.append(_IndexEntry(raw=record, projected=self._project(record)))
        return entries, unreadable

    def _refresh_projection(self, run_id: str) -> None:
        """Fold a just-committed terminal frame into the run's index entry."""
        index = self._index
        with index.lock:
            entry = index.entries.get(run_id)
            if entry is None:
                return
            terminal = self.committed_terminal(run_id)
            if terminal is None:
                return
            index.entries[run_id] = _IndexEntry(
                raw=entry.raw,
                projected=_apply_terminal(entry.raw, terminal),
            )

    def _load_meta(self, run_id: str) -> tuple[RunRecord | None, bool]:
        """``(record, unreadable)`` for one run directory.

        ``unreadable`` is true only when ``meta.json`` exists but cannot be used:
        a missing file is simply not a run record. Corruption is never silently
        equal to absence — the resource ledger must keep holding for it (B.7).
        """
        path = self.run_dir(run_id) / _META_NAME
        if not path.is_file():
            return None, False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, True
        if not isinstance(data, dict):
            return None, True
        known = {field.name for field in fields(RunRecord)}
        return RunRecord(**{key: value for key, value in data.items() if key in known}), False

    def _read_meta(self, run_id: str) -> RunRecord | None:
        return self._load_meta(run_id)[0]

    def _read_unreadable_adjudication(self, run_id: str) -> Mapping[str, Any] | None:
        path = self.run_dir(run_id) / UNREADABLE_ADJUDICATION_NAME
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _write(self, record: RunRecord) -> RunRecord:
        """Atomically replace ``meta.json`` and refresh the index entry.

        Both steps run in one per-root critical section, so two writers of the
        same run cannot leave the index describing an older meta than the file.
        """
        directory = self.run_dir(record.run_id)
        directory.mkdir(parents=True, exist_ok=True)
        payload = asdict(record)
        temporary = directory / f".{_META_NAME}.tmp"
        with self._lock:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            temporary.replace(directory / _META_NAME)
            projected = self._project(record)
            with self._index.lock:
                self._index.entries[record.run_id] = _IndexEntry(
                    raw=record, projected=projected
                )
            return projected


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
