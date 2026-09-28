"""Read and drive a :class:`RunStore` the way tests need, through its public API.

The run database is host-owned; tests read committed frames with
``iter_events`` and reach a terminal state only by committing a terminal frame
(``commit_terminal``), exactly like the runner — never by writing ``status``.
``stored_bytes`` returns the database files as they lie on disk, for the leak
checks that must see every byte the store persisted.
"""

from __future__ import annotations

import json
import time
from typing import Any, Mapping

from pi_glink.store import DATABASE_NAME, RunRecord, RunStore

_TERMINAL_TYPE = {"finished": "run_finished", "failed": "run_failed", "cancelled": "run_cancelled"}


def read_events(store: RunStore, run_id: str) -> list[dict[str, Any]]:
    """Every committed frame of one run, in ``seq`` order."""
    return list(store.iter_events(run_id))


def events_text(store: RunStore, run_id: str) -> str:
    """The run's committed frame bodies exactly as stored, one per line."""
    with store._lock:  # noqa: SLF001 - raw stored bodies, read-only
        rows = store._connection.execute(  # noqa: SLF001
            "SELECT body FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
    return "".join(row[0] + "\n" for row in rows)


def stored_bytes(store: RunStore) -> bytes:
    """The database file plus its WAL, as bytes on disk (leak checks)."""
    chunks = []
    for suffix in ("", "-wal"):
        path = store.root / f"{DATABASE_NAME}{suffix}"
        if path.is_file():
            chunks.append(path.read_bytes())
    return b"".join(chunks)


def finish(
    store: RunStore,
    run_id: str,
    status: str = "finished",
    *,
    payload: Mapping[str, Any] | None = None,
    **fields: Any,
) -> RunRecord:
    """Commit a terminal frame for ``run_id`` (next ``seq``) and return the record.

    ``fields`` maps record-style names onto the frame payload the store
    projects (``cleanup``, ``report_path``, ``stop_reason``, ``error``,
    ``finished_at``).
    """
    body: dict[str, Any] = {"status": status}
    mapping = {
        "cleanup": "cleanup",
        "report_path": "reportPath",
        "report_status": "reportStatus",
        "stop_reason": "stopReason",
        "error": "error",
        "usage": "usage",
        "redacted": "redacted",
        "model_stop_reason": "modelStopReason",
    }
    ts = fields.pop("finished_at", None)
    for name, value in fields.items():
        body[mapping[name]] = value
    if payload:
        body.update(payload)
    return store.commit_terminal(
        run_id,
        {
            "type": _TERMINAL_TYPE[status],
            "runId": run_id,
            "seq": store.last_seq(run_id) + 1,
            "ts": ts if ts is not None else time.time(),
            "payload": body,
        },
    )


def record_text(store: RunStore, run_id: str) -> str:
    """The run's record exactly as stored (the ``runs.record`` JSON text)."""
    with store._lock:  # noqa: SLF001 - raw stored row, read-only
        row = store._connection.execute(  # noqa: SLF001
            "SELECT record FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
    assert row is not None, run_id
    return row[0]


def stored_record(store: RunStore, run_id: str) -> dict[str, Any]:
    """The run's stored record, parsed (fields as persisted, not projected)."""
    return json.loads(record_text(store, run_id))
