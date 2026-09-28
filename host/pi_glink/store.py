"""Run records, event logs and the resource ledger — one SQLite database.

Structured run state lives in ``<data_root>/runs/runs.db`` (standard-library
:mod:`sqlite3`, WAL). Files a run produces stay files, all under the run's own
directory:

    <data_root>/runs/
    ├── runs.db              run records + events (+ -wal / -shm sidecars)
    └── <run_id>/
        ├── pi-agent/ | acp-agent/   the run's rendered runtime configuration
        ├── sessions/            PI_CODING_AGENT_SESSION_DIR of a pi run
        ├── subagents/           E2B-recovered subagent mirror
        ├── children/<n>/child.json  authoritative document of one subagent
        └── artifacts/report.md  the host-owned, redacted report copy

**Two tables, one transaction per fact.**

* ``runs`` holds one row per run: the full :class:`RunRecord` (JSON) plus the
  columns every query filters on (``created_at``, ``tenant_id``, ``status``
  and ``holds`` — whether the run's execution resources are unconfirmed).
* ``events`` holds every committed frame keyed by ``(run_id, seq)``. A reader
  sees a frame exactly when its insert committed; there is no partial line,
  no watermark and no repair.

**The terminal frame is the terminal commit.** :meth:`RunStore.commit_terminal`
inserts the ``run_finished`` / ``run_failed`` / ``run_cancelled`` frame and
rewrites the run row to its terminal record *in the same transaction*, and
that transaction is committed with ``synchronous=FULL``. Before it returns no
reader can see either half; after it returns every reader — run detail, run
list, report download, SSE replay, the resource ledger — sees both. A run row
that is already terminal refuses any further frame, inside the same
transaction, so nothing can follow the terminal frame.

**Admission reads the ledger it writes.** :meth:`RunStore.create` takes an
``admission_check``: the callback sees the holding rows read inside the
``BEGIN IMMEDIATE`` transaction that then inserts the new row, so the quota
count and the insert cannot interleave with another writer of the same
database.

Records written before this layout (``meta.json`` / ``events.jsonl`` files)
are not read: there is no migration.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from .artifacts import ArtifactError, ArtifactTree
from .events import TERMINAL_EVENT_TYPES

logger = logging.getLogger("pi_glink.store")

#: Run states. ``running`` is the only non-terminal one.
RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"
CANCELLED = "cancelled"
TERMINAL_STATUSES = frozenset({FINISHED, FAILED, CANCELLED})

#: The run database, inside the runs root.
DATABASE_NAME = "runs.db"
#: Artifact the research contract asks pi to produce.
REPORT_FILENAME = "report.md"
#: Host-owned artifact directory inside the run directory.
ARTIFACTS_DIRNAME = "artifacts"
#: A pi run's session directory (``PI_CODING_AGENT_SESSION_DIR``), inside the
#: run directory: one run owns exactly one, created with the run's runtime.
SESSIONS_DIRNAME = "sessions"
#: Host-owned mirror of a run's subagent sessions and artifacts (recovered
#: out of the sandbox before it is destroyed; other faces keep theirs in the
#: session directory). Recovered raw, then swept by the run's redactor.
SUBAGENTS_DIRNAME = "subagents"
#: Authoritative child-run (subagent) documents (docs/architecture.md §3.5):
#: one ``children/<n>/child.json`` per dispatched subagent, atomically
#: rewritten on every state migration. The typed events are only the push
#: side of these documents.
CHILDREN_DIRNAME = "children"
CHILD_DOC_NAME = "child.json"
#: Bound on how many child documents one run read returns. A runaway dispatch
#: must not turn a read into an unbounded walk; the terminal-frame summary
#: shares this bound.
CHILD_READ_LIMIT = 64

#: Schema version stored in ``PRAGMA user_version``. A database carrying any
#: other version is refused at open (no migration).
SCHEMA_VERSION = 1
#: Bound on one child document. The projection already clips ``task`` and
#: ``resultPreview``; this is the store's own backstop, not the editorial limit.
_CHILD_DOC_MAX_BYTES = 64 * 1024
#: Candidate files ``read_children`` visits before stopping. Corruption in the
#: host-owned tree must not turn a bounded read into an unbounded one.
_CHILD_VISIT_LIMIT = 4 * CHILD_READ_LIMIT
#: Bounded retries when a generated run id collides with an existing run.
_RUN_ID_ATTEMPTS = 5
#: Record fields a stored row must carry as non-empty strings.
_REQUIRED_TEXT_FIELDS = ("run_id", "prompt", "status", "workspace")
#: ``RunSpec`` fields a record's ``spec`` object must carry as non-empty strings.
_REQUIRED_SPEC_FIELDS = ("family", "pack", "executor", "credential_origin")
#: Events one replay query fetches before yielding them (no lock is held
#: while the caller consumes a page).
_EVENT_PAGE = 500
#: Record fields only :meth:`RunStore.commit_terminal` may change.
_TERMINAL_FIELDS = frozenset({"status", "finished_at", "cleanup", "event_count"})
_STATUS_BY_EVENT = {
    "run_finished": FINISHED,
    "run_failed": FAILED,
    "run_cancelled": CANCELLED,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id     TEXT PRIMARY KEY,
  created_at REAL NOT NULL,
  tenant_id  TEXT,
  status     TEXT NOT NULL,
  holds      INTEGER NOT NULL CHECK (holds IN (0, 1)),
  record     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_by_created ON runs(created_at DESC, run_id);
CREATE INDEX IF NOT EXISTS runs_by_tenant ON runs(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS runs_holding ON runs(holds) WHERE holds = 1;
CREATE TABLE IF NOT EXISTS events (
  run_id TEXT NOT NULL REFERENCES runs(run_id),
  seq    INTEGER NOT NULL CHECK (seq > 0),
  type   TEXT NOT NULL,
  body   TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
) WITHOUT ROWID;
"""


@dataclass(frozen=True, slots=True)
class RunSpec:
    """What one run executes with, resolved once at admission (docs/architecture.md §2).

    The runner resolves it from the frozen host configuration when it admits
    the run and writes it inside the same admission insert as the run id and
    owner; everything after admission — the harness family, the
    sandbox face, the follow-up gate, the ACP state root a continuation
    inherits — reads it from the record, never from the host's current
    configuration (docs/adr/0002).

    * ``family`` — harness family (``pi`` | ``acp``);
    * ``pack`` — harness pack (``harnesses/<pack>/``);
    * ``executor`` — sandbox face (``inproc`` | ``docker`` | ``e2b``);
    * ``blueprint`` — session blueprint stem, ``None`` without one;
    * ``credential_origin`` — where the run's credentials came from
      (``host`` | ``tenant`` | ``shared``, :class:`~pi_glink.kernel.agent_env.RunCredentials`).
      The values themselves are never part of the spec.
    """

    family: str
    pack: str
    executor: str
    blueprint: str | None = None
    credential_origin: str = "host"

    @classmethod
    def from_mapping(cls, data: Any) -> "RunSpec | None":
        """Parse a stored record's ``spec`` object; ``None`` when it is unusable."""
        if not isinstance(data, Mapping):
            return None
        if any(
            not isinstance(data.get(name), str) or not data[name]
            for name in _REQUIRED_SPEC_FIELDS
        ):
            return None
        blueprint = data.get("blueprint")
        if blueprint is not None and (not isinstance(blueprint, str) or not blueprint):
            return None
        return cls(
            family=data["family"],
            pack=data["pack"],
            executor=data["executor"],
            blueprint=blueprint,
            credential_origin=data["credential_origin"],
        )


@dataclass(frozen=True, slots=True)
class RunRecord:
    """Everything the workbench shows about a run, minus its event stream."""

    run_id: str
    prompt: str
    status: str
    created_at: float
    workspace: str
    #: The admission-time :class:`RunSpec` (family, pack, sandbox face,
    #: blueprint, credential origin). Required: every later decision reads it
    #: instead of the host's current configuration.
    spec: RunSpec
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
    #: unknown). ``None`` while the run has not committed a terminal frame.
    cleanup: Mapping[str, Any] | None = None
    #: Durable external identity for restart reconciliation (never guessed).
    container_name: str | None = None
    sandbox_id: str | None = None
    #: Operator decision that an unconfirmed resource is released
    #: (``{"decision": "released", "note": …, "at": …}``); never set automatically.
    resource_adjudication: Mapping[str, Any] | None = None
    #: Owner of the run, written atomically with admission. Records written by
    #: the single-user surface carry ``None`` and are therefore invisible to
    #: every multi-tenant read: they are never imported into a tenant and never
    #: rewritten.
    tenant_id: str | None = None
    creator_user_id: str | None = None
    #: The stages of the blueprint named in ``spec.blueprint``, as parsed at
    #: admission (``id``/``title``/``goal``/``deliverable``/``checkpoint``).
    #: Empty for a run without a blueprint.
    blueprint_stages: tuple[Mapping[str, Any], ...] = ()
    #: Agent identity the kernel itself reported (ACP ``agentInfo.name`` from the
    #: negotiated initialize result), written back at the session_ready commit
    #: point like ``session_id``. ``None`` for pi-family runs (the pi protocol
    #: reports no agent identity; the model id stays a session_ready payload
    #: fact, never a stand-in for agent identity), for ACP runs that never
    #: reached session_ready.
    agent: str | None = None

    def __post_init__(self) -> None:
        """Canonicalize the stage list whatever representation reached us.

        The stored record carries the stages as a JSON array, so a record read
        back from the database holds a list; the in-memory record must still compare equal to
        the one ``create`` returned. Coercing here keeps that true for every
        construction path (admission, update, load).
        """
        object.__setattr__(
            self,
            "blueprint_stages",
            tuple(dict(stage) for stage in (self.blueprint_stages or ())),
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def holds_resources(self) -> bool:
        """True while this run's resources are not confirmed released.

        A run that is still ``running`` holds its resources by definition; a
        terminal run holds them until its cleanup reported ``released: true`` or
        an operator adjudicated them. A terminal record without a ``cleanup``
        fact makes no claim either way (every terminal frame the runner and
        the resource ledger commit carries one).
        """
        if not self.is_terminal:
            return True
        if self.cleanup is None or self.resource_adjudication is not None:
            return False
        return self.cleanup.get("released") is not True


class StoreError(RuntimeError):
    """The run database cannot be opened or holds a row that is not a record."""


class StoreWriteError(OSError):
    """A write to the run database failed; nothing of it is visible.

    An :class:`OSError` on purpose: the event channel turns every failed
    persist into ``event_log_failed`` through the same ``except OSError`` path
    it always used for the disk.
    """


class RunAlreadyTerminal(RuntimeError):
    """The run's terminal frame is committed: no frame can follow it."""


@dataclass(frozen=True, slots=True)
class ReportLocation:
    """Where a record's report may be read: a run-owned root and a fixed name."""

    root: Path
    relative: str


@dataclass(frozen=True, slots=True)
class ChildDocuments:
    """The readable child-run documents of one run, plus the honest skip count.

    ``documents`` holds the parsed ``child.json`` payloads (ordered by ``n``);
    ``skipped`` counts the documents that existed but could not be read or
    parsed. A missing ``children/`` tree is not corruption: it is simply a run
    without subagents (``skipped`` stays 0).
    """

    documents: tuple[dict[str, Any], ...] = ()
    skipped: int = 0


#: Called inside the admission transaction with the holding records (newest
#: first); raising refuses the admission and rolls the transaction back.
AdmissionCheck = Callable[[Sequence["RunRecord"]], None]


class RunStore:
    """Thread-safe run store: one SQLite database plus per-run directories."""

    def __init__(self, root: Path) -> None:
        self._root = root
        # The runs root is host-owned data: create it private (0700).
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db_path = self._root / DATABASE_NAME
        self._lock = threading.RLock()
        self._connection = _open_database(self._db_path)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def database_path(self) -> Path:
        return self._db_path

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def run_dir(self, run_id: str) -> Path:
        return self._root / run_id

    def session_dir(self, run_id: str) -> Path:
        """The run's own pi session directory (``runs/<id>/sessions``)."""
        return self.run_dir(run_id) / SESSIONS_DIRNAME

    def report_artifact_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / ARTIFACTS_DIRNAME / REPORT_FILENAME

    # -- transactions ---------------------------------------------------------

    @contextmanager
    def _transaction(self, *, durable: bool = False) -> Iterator[sqlite3.Connection]:
        """One ``BEGIN IMMEDIATE`` write transaction (all or nothing).

        ``durable`` commits with ``synchronous=FULL`` (the WAL is synced before
        the commit returns); ordinary frames use ``NORMAL``, which WAL keeps
        consistent but may lose on power failure. Any SQLite failure is raised
        as :class:`StoreWriteError` after the rollback.
        """
        with self._lock:
            connection = self._connection
            try:
                if durable:
                    connection.execute("PRAGMA synchronous=FULL")
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                self._restore_sync(durable)
                raise StoreWriteError(f"run 数据库不可写: {exc}") from exc
            try:
                yield connection
                connection.execute("COMMIT")
            except BaseException as exc:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                if isinstance(exc, sqlite3.Error):
                    raise StoreWriteError(f"run 数据库写入失败: {exc}") from exc
                raise
            finally:
                self._restore_sync(durable)

    def _restore_sync(self, durable: bool) -> None:
        if durable:
            try:
                self._connection.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error:
                logger.warning("恢复 synchronous=NORMAL 失败", exc_info=True)

    def _query(self, sql: str, parameters: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with self._lock:
            try:
                return self._connection.execute(sql, parameters).fetchall()
            except sqlite3.Error as exc:
                raise StoreError(f"run 数据库读取失败: {exc}") from exc

    # -- records --------------------------------------------------------------

    def create(
        self,
        prompt: str,
        *,
        run_id: str | None = None,
        workspace: Path | None = None,
        workspace_root: Path | None = None,
        spec: RunSpec,
        tenant_id: str | None = None,
        creator_user_id: str | None = None,
        parent_run_id: str | None = None,
        blueprint_stages: Sequence[Mapping[str, Any]] = (),
        admission_check: AdmissionCheck | None = None,
    ) -> RunRecord:
        """Admit a run: one transaction checks the ledger and inserts the row.

        ``admission_check`` (when given) sees the holding records read inside
        the same ``BEGIN IMMEDIATE`` transaction that inserts the new row; it
        refuses by raising, which rolls the transaction back and propagates.

        The run id is the row's primary key and the run directory is created
        **exclusively** inside the transaction: a new run never adopts an
        existing row or directory. A generated id that collides is regenerated
        a bounded number of times; an explicitly given ``run_id`` that already
        exists raises :class:`FileExistsError`. ``workspace_root`` derives the
        run's workspace from the generated id, so the store stays the one place
        that allocates ids.

        The owner labels, ``parent_run_id``, ``spec`` and ``blueprint_stages``
        are facts decided before admission and land in this same insert, so no
        reader can observe an ownerless or spec-less run. ``agent`` is written
        back at the session_ready commit (it is only known after the start).
        """
        with self._transaction() as connection:
            if admission_check is not None:
                admission_check(self._holding(connection, None))
            identifier, directory = self._allocate(connection, run_id)
            try:
                if workspace is not None:
                    workspace_path = str(workspace)
                elif workspace_root is not None:
                    workspace_path = str(workspace_root / identifier)
                else:
                    workspace_path = str(directory / "workspace")
                record = RunRecord(
                    run_id=identifier,
                    prompt=prompt,
                    status=RUNNING,
                    created_at=time.time(),
                    workspace=workspace_path,
                    tenant_id=tenant_id,
                    creator_user_id=creator_user_id,
                    parent_run_id=parent_run_id,
                    blueprint_stages=tuple(blueprint_stages),
                    spec=spec,
                )
                connection.execute(
                    "INSERT INTO runs(run_id, created_at, tenant_id, status, holds, record)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        record.run_id,
                        record.created_at,
                        record.tenant_id,
                        record.status,
                        int(record.holds_resources),
                        _encode(record),
                    ),
                )
            except BaseException:
                _remove_empty(directory)
                raise
        return record

    def _allocate(self, connection: sqlite3.Connection, run_id: str | None) -> tuple[str, Path]:
        """Pick an unused id and create its directory exclusively (0700)."""
        if run_id is not None:
            if _row_exists(connection, run_id):
                raise FileExistsError(f"run {run_id} 已存在")
            directory = self.run_dir(run_id)
            directory.mkdir(parents=True, exist_ok=False, mode=0o700)
            return run_id, directory
        for _attempt in range(_RUN_ID_ATTEMPTS):
            candidate = uuid.uuid4().hex[:12]
            if _row_exists(connection, candidate):
                continue
            directory = self.run_dir(candidate)
            try:
                directory.mkdir(parents=True, exist_ok=False, mode=0o700)
            except FileExistsError:
                continue
            return candidate, directory
        raise RuntimeError("无法生成唯一的 run id（连续冲突）")

    def update(self, run_id: str, **changes: Any) -> RunRecord:
        """Change in-flight facts or the adjudication of one run.

        The terminal fields (``status``, ``finished_at``, ``cleanup``,
        ``event_count``) are refused: a run becomes terminal only through
        :meth:`commit_terminal`, together with its terminal frame.
        """
        allowed = {field.name for field in fields(RunRecord)}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown run fields: {sorted(unknown)}")
        forbidden = set(changes) & (_TERMINAL_FIELDS | {"run_id"})
        if forbidden:
            raise ValueError(f"终态字段只能随终帧提交: {sorted(forbidden)}")
        with self._transaction() as connection:
            record = self._read(connection, run_id)
            if record is None:
                raise KeyError(run_id)
            updated = replace(record, **changes)
            _write_row(connection, updated)
        return updated

    def get(self, run_id: str) -> RunRecord | None:
        """The committed run record (terminal once its terminal frame committed)."""
        with self._lock:
            return self._read(self._connection, run_id)

    def get_for(self, run_id: str, tenant_id: str) -> RunRecord | None:
        """The run only when it belongs to ``tenant_id``.

        An unknown run, another tenant's run and an ownerless (single-user)
        record are all ``None``: the HTTP layer turns every one of them into the
        same 404, so existence is not disclosed across tenants.
        """
        record = self.get(run_id)
        if record is None or record.tenant_id != tenant_id:
            return None
        return record

    def list_for(self, tenant_id: str, limit: int | None = 50) -> list[RunRecord]:
        """Runs of exactly one tenant, newest first (ownerless records excluded)."""
        return self._select("WHERE tenant_id = ?", (tenant_id,), limit)

    def list(self, limit: int | None = 50) -> list[RunRecord]:
        """Every run, newest first (ties broken by run id)."""
        return self._select("", (), limit)

    def holding_records(self, tenant_id: str | None = None) -> list[RunRecord]:
        """Records whose execution resources are not confirmed released.

        ``tenant_id`` selects one tenant's holds; ``None`` returns every hold,
        including records without an owner (single-user runs), because those
        still consume the global capacity. Newest first, like :meth:`list`.
        """
        with self._lock:
            return self._holding(self._connection, tenant_id)

    def running_ids(self) -> list[str]:
        """Run ids whose row is still ``running`` (no terminal frame committed)."""
        rows = self._query("SELECT run_id FROM runs WHERE status = ? ORDER BY run_id", (RUNNING,))
        return [row[0] for row in rows]

    def report_location(self, record: RunRecord) -> ReportLocation | None:
        """The only place a record's ``report_path`` may point to.

        ``runs/<id>/artifacts/report.md`` — the host-owned redacted copy.
        Anything else is not a report, whatever the record says.
        """
        if not record.report_path:
            return None
        if Path(record.report_path) != self.report_artifact_path(record.run_id):
            return None
        return ReportLocation(
            self.run_dir(record.run_id), f"{ARTIFACTS_DIRNAME}/{REPORT_FILENAME}"
        )

    def _select(
        self, where: str, parameters: Sequence[Any], limit: int | None
    ) -> list[RunRecord]:
        sql = f"SELECT record FROM runs {where} ORDER BY created_at DESC, run_id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (*parameters, max(0, int(limit)))
        return [_decode(row[0]) for row in self._query(sql, parameters)]

    def _holding(self, connection: sqlite3.Connection, tenant_id: str | None) -> list[RunRecord]:
        sql = "SELECT record FROM runs WHERE holds = 1"
        parameters: tuple[Any, ...] = ()
        if tenant_id is not None:
            sql += " AND tenant_id = ?"
            parameters = (tenant_id,)
        sql += " ORDER BY created_at DESC, run_id ASC"
        try:
            rows = connection.execute(sql, parameters).fetchall()
        except sqlite3.Error as exc:
            raise StoreError(f"run 数据库读取失败: {exc}") from exc
        return [_decode(row[0]) for row in rows]

    @staticmethod
    def _read(connection: sqlite3.Connection, run_id: str) -> RunRecord | None:
        try:
            row = connection.execute(
                "SELECT record FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise StoreError(f"run 数据库读取失败: {exc}") from exc
        return _decode(row[0]) if row is not None else None

    # -- events ---------------------------------------------------------------

    def iter_events(self, run_id: str, after: int = 0) -> Iterator[dict[str, Any]]:
        """Yield committed events with ``seq > after``, in ``seq`` order.

        The in-memory bus only knows runs started by the current host process;
        after a restart the database is the only record of a run's timeline,
        so history replay reads it. Pages are fetched under the lock and
        yielded without it, so a slow consumer never blocks a writer.
        """
        cursor = max(0, int(after))
        while True:
            rows = self._query(
                "SELECT seq, body FROM events WHERE run_id = ? AND seq > ?"
                " ORDER BY seq LIMIT ?",
                (run_id, cursor, _EVENT_PAGE),
            )
            for seq, body in rows:
                cursor = seq
                yield json.loads(body)
            if len(rows) < _EVENT_PAGE:
                return

    def last_seq(self, run_id: str) -> int:
        """The highest committed ``seq`` of one run (0 without events)."""
        rows = self._query("SELECT MAX(seq) FROM events WHERE run_id = ?", (run_id,))
        return int(rows[0][0] or 0)

    def append_event(self, run_id: str, event: Mapping[str, Any]) -> None:
        """Commit one ordinary frame: visible exactly when its insert commits."""
        if event.get("type") in TERMINAL_EVENT_TYPES:
            raise ValueError("终帧只能经 commit_terminal 提交")
        with self._transaction() as connection:
            self._require_running(connection, run_id)
            _insert_event(connection, run_id, event)

    def commit_terminal(self, run_id: str, event: Mapping[str, Any]) -> RunRecord:
        """Commit the run's terminal frame and terminal record in one transaction.

        The frame insert and the row rewrite are one ``synchronous=FULL``
        transaction: either both are durable and visible, or neither is and the
        run stays ``running`` for every reader. The terminal record is the
        frame's payload projected onto the admission record
        (:func:`_apply_terminal`). Returns the terminal record.
        """
        if event.get("type") not in TERMINAL_EVENT_TYPES:
            raise ValueError("commit_terminal 只接受终帧")
        with self._transaction(durable=True) as connection:
            record = self._require_running(connection, run_id)
            _insert_event(connection, run_id, event)
            terminal = _apply_terminal(record, event)
            _write_row(connection, terminal)
        return terminal

    def committed_terminal(self, run_id: str) -> dict[str, Any] | None:
        """The run's committed terminal frame, if it has one."""
        placeholders = ", ".join("?" for _ in TERMINAL_EVENT_TYPES)
        rows = self._query(
            f"SELECT body FROM events WHERE run_id = ? AND type IN ({placeholders})"
            " ORDER BY seq DESC LIMIT 1",
            (run_id, *sorted(TERMINAL_EVENT_TYPES)),
        )
        return json.loads(rows[0][0]) if rows else None

    def _require_running(self, connection: sqlite3.Connection, run_id: str) -> RunRecord:
        record = self._read(connection, run_id)
        if record is None:
            raise KeyError(run_id)
        if record.is_terminal:
            raise RunAlreadyTerminal(f"run {run_id} 已提交终帧，不再接受事件")
        return record

    # -- children (authoritative subagent documents, §3.5) ---------------------

    def child_doc_path(self, run_id: str, n: int) -> Path:
        return self.run_dir(run_id) / CHILDREN_DIRNAME / str(n) / CHILD_DOC_NAME

    def write_child(self, run_id: str, document: Mapping[str, Any]) -> None:
        """Atomically rewrite one child-run document (the authoritative record).

        The document is small and always written whole: every state migration
        republishes ``children/<n>/child.json`` through the run directory's
        handle (temporary file + rename inside the verified parent, the same
        boundary as the report copy). ``n`` is taken from the document itself;
        anything but a non-negative integer is refused.
        """
        n = document.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            raise ValueError(f"child.json 需要非负整数序号 n: {n!r}")
        payload = json.dumps(dict(document), ensure_ascii=False, indent=2) + "\n"
        with ArtifactTree(self.run_dir(run_id)) as tree:
            tree.replace(
                f"{CHILDREN_DIRNAME}/{n}/{CHILD_DOC_NAME}",
                payload.encode("utf-8"),
                max_bytes=_CHILD_DOC_MAX_BYTES,
                create_parents=True,
            )

    def read_child(self, run_id: str, n: int) -> dict[str, Any] | None:
        """One child-run document, or ``None`` when it is missing or unusable."""
        try:
            with ArtifactTree(self.run_dir(run_id)) as tree:
                raw = tree.read(
                    f"{CHILDREN_DIRNAME}/{n}/{CHILD_DOC_NAME}",
                    max_bytes=_CHILD_DOC_MAX_BYTES,
                )
            data = json.loads(raw.decode("utf-8"))
        except (ArtifactError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or data.get("n") != n:
            return None
        return data

    def read_children(self, run_id: str) -> ChildDocuments:
        """All readable child-run documents of one run, bounded and honest.

        At most :data:`CHILD_READ_LIMIT` documents are returned, ordered by
        ``n``. A document that exists but cannot be read, parsed or matched to
        its directory is skipped and counted in ``skipped`` — corruption is
        reported, never silently equated with absence; the same holds for a
        ``children/`` tree that itself cannot be opened (counted as one) and
        for documents beyond the read cap. A run without a ``children/`` tree
        simply has no children.
        """
        try:
            tree = ArtifactTree(self.run_dir(run_id) / CHILDREN_DIRNAME)
        except ArtifactError as exc:
            children_root = self.run_dir(run_id) / CHILDREN_DIRNAME
            if children_root.exists():
                # The tree exists but cannot be opened: one unreadable unit,
                # reported instead of silently equated with absence.
                return ChildDocuments((), 1)
            logger.debug("children tree unavailable run=%s: %s", run_id, exc)
            return ChildDocuments()
        with tree:
            found = tree.files(max_files=_CHILD_VISIT_LIMIT)
            candidates: list[tuple[int, str]] = []
            for relative in found:
                parts = relative.split("/")
                if len(parts) != 2 or parts[1] != CHILD_DOC_NAME:
                    continue
                try:
                    number = int(parts[0])
                except ValueError:
                    continue
                candidates.append((number, relative))
            candidates.sort()
            documents: list[dict[str, Any]] = []
            skipped = 0
            for number, relative in candidates:
                if len(documents) >= CHILD_READ_LIMIT:
                    # Every document past the cap is absent from the page; the
                    # absence is counted, not silent.
                    skipped += 1
                    continue
                try:
                    raw = tree.read(relative, max_bytes=_CHILD_DOC_MAX_BYTES)
                    data = json.loads(raw.decode("utf-8"))
                except (ArtifactError, OSError, UnicodeDecodeError, json.JSONDecodeError):
                    skipped += 1
                    continue
                if not isinstance(data, dict) or data.get("n") != number:
                    skipped += 1
                    continue
                documents.append(data)
        return ChildDocuments(tuple(documents), skipped)

    def settle_children(self, run_id: str, status: str) -> int:
        """Close every still-``running`` child document to ``status``.

        Shared by the run's own verdict sweep and the restart ledger's
        interrupted close-out (both apply the same "children follow the run"
        rule). Returns the number of documents that could not be rewritten.
        """
        try:
            page = self.read_children(run_id)
        except Exception:  # noqa: BLE001 - the caller reports, never blocks
            return -1
        failures = 0
        for document in page.documents:
            if document.get("status") != "running":
                continue
            try:
                self.write_child(run_id, {**document, "status": status})
            except (ArtifactError, OSError, ValueError):
                failures += 1
        return failures

    def children_summary(self, run_id: str) -> list[dict[str, Any]]:
        """The ``children`` summary of a terminal frame, built from documents."""
        try:
            page = self.read_children(run_id)
        except Exception:  # noqa: BLE001 - a summary must not break its caller
            logger.warning("children summary unavailable run=%s", run_id)
            return []
        summary: list[dict[str, Any]] = []
        for document in page.documents:
            entry: dict[str, Any] = {
                "n": document.get("n"),
                "agent": document.get("agent") or "",
                "task": document.get("task") or "",
                "status": document.get("status") or "running",
            }
            # R2 增强（协议可见事实之外的可选附着）：只有文档里真实存在才带；
            # enrichment 为 none 是默认态，不占摘要空间。
            for key in ("durationMs", "toolCount", "model", "usage", "artifacts"):
                if key in document:
                    entry[key] = document[key]
            if document.get("enrichment") not in (None, "none"):
                entry["enrichment"] = document["enrichment"]
            summary.append(entry)
        return summary


# -- database helpers ---------------------------------------------------------


def _open_database(path: Path) -> sqlite3.Connection:
    """Open (creating 0600 when absent) the run database and check its schema.

    The file must be a regular file owned by nothing but this path: a symlink
    is refused (``O_NOFOLLOW``), as is any non-regular file. A database whose
    ``user_version`` is neither 0 (fresh) nor :data:`SCHEMA_VERSION` is refused
    — there is no migration.
    """
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise StoreError(f"无法打开 run 数据库 {path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise StoreError(f"run 数据库不是普通文件: {path}")
    finally:
        os.close(descriptor)
    try:
        connection = sqlite3.connect(
            str(path), timeout=30.0, isolation_level=None, check_same_thread=False
        )
    except sqlite3.Error as exc:
        raise StoreError(f"无法打开 run 数据库 {path}: {exc}") from exc
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise StoreError(f"run 数据库无法启用 WAL（journal_mode={mode}）")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys=ON")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise StoreError(
                f"run 数据库版本 {version} 与当前版本 {SCHEMA_VERSION} 不符（不做迁移）"
            )
        if version == 0:
            connection.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.execute("COMMIT")
    except sqlite3.Error as exc:
        connection.close()
        raise StoreError(f"run 数据库初始化失败 {path}: {exc}") from exc
    except BaseException:
        connection.close()
        raise
    return connection


def _encode(record: RunRecord) -> str:
    return json.dumps(asdict(record), ensure_ascii=False, separators=(",", ":"))


def _decode(text: str) -> RunRecord:
    """Parse one ``runs.record`` value; anything unusable is a :class:`StoreError`.

    The database is host-owned: a row that is not a complete record is not
    guessed around (fail closed — every reader, including admission, stops).
    """
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise StoreError(f"run 记录不是 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise StoreError("run 记录不是对象")
    for name in _REQUIRED_TEXT_FIELDS:
        if not isinstance(data.get(name), str) or not data[name]:
            raise StoreError(f"run 记录缺少字段 {name}")
    spec = RunSpec.from_mapping(data.get("spec"))
    if spec is None:
        raise StoreError(f"run 记录 {data['run_id']} 的 spec 不可用")
    known = {field.name for field in fields(RunRecord)}
    values = {key: value for key, value in data.items() if key in known}
    values["spec"] = spec
    try:
        return RunRecord(**values)
    except TypeError as exc:
        raise StoreError(f"run 记录 {data['run_id']} 字段不完整: {exc}") from exc


def _row_exists(connection: sqlite3.Connection, run_id: str) -> bool:
    row = connection.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return row is not None


def _write_row(connection: sqlite3.Connection, record: RunRecord) -> None:
    connection.execute(
        "UPDATE runs SET status = ?, holds = ?, tenant_id = ?, record = ? WHERE run_id = ?",
        (
            record.status,
            int(record.holds_resources),
            record.tenant_id,
            _encode(record),
            record.run_id,
        ),
    )


def _insert_event(connection: sqlite3.Connection, run_id: str, event: Mapping[str, Any]) -> None:
    """Insert one frame; its ``seq`` must be a positive int, unique per run."""
    seq = event.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq <= 0:
        raise ValueError(f"事件 seq 必须是正整数: {seq!r}")
    kind = event.get("type")
    if not isinstance(kind, str) or not kind:
        raise ValueError("事件缺少 type")
    body = json.dumps(dict(event), ensure_ascii=False)
    connection.execute(
        "INSERT INTO events(run_id, seq, type, body) VALUES (?, ?, ?, ?)",
        (run_id, seq, kind, body),
    )


def _remove_empty(directory: Path) -> None:
    try:
        directory.rmdir()
    except OSError:
        logger.warning("回滚 run 目录失败 %s", directory, exc_info=True)


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
