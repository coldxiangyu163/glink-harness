"""The read model every run-scoped HTTP read goes through.

Five read faces of one run — the run detail, the child documents, a child
artifact file, the report download and the event stream — each have to take the
same four steps before a client learns anything:

1. **owner gate**: an unknown run, another tenant's run and an ownerless
   (single-user) record are *indistinguishable*, so the caller answers the same
   404 and existence is never disclosed across tenants;
2. **path resolution**: the host artifact copy is not a download capability, so
   the bytes come from :class:`~pi_glink.artifacts.ArtifactTree`, never from a
   resolved path string;
3. **read bound**: the same byte ceiling (:data:`~pi_glink.runs.limits.
   REPORT_MAX_BYTES`) as the rest of the host read surface;
4. **redaction boundary**: the record's *own* credentials, so one tenant's
   report is never compared against another tenant's secrets.

Doing this inline in every endpoint is how a new read face silently skips a
step; keeping it here means the route owns only the HTTP shape (status, payload
keys, headers). The kernel stays below this package and never imports it
(``tests/test_repository_boundaries.py`` pins that direction).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from ..artifacts import ArtifactError, ArtifactTree
from ..redaction import SecretRedactor
from ..store import CHILDREN_DIRNAME, ChildDocuments, RunRecord, RunStore
from ..tenancy import TenantContext
from .limits import REPORT_MAX_BYTES

logger = logging.getLogger("pi_glink.runs.read_model")

#: Content types for the child artifact download, by file extension.
CHILD_FILE_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".jsonl": "application/x-ndjson; charset=utf-8",
}
_DEFAULT_CONTENT_TYPE = "application/octet-stream"

#: A record's redactor is built from the record's own credentials.
RedactorFor = Callable[[RunRecord], SecretRedactor]


def visible_record(
    store: RunStore, run_id: str, context: TenantContext | None
) -> RunRecord | None:
    """The record of a run the caller may read, or ``None``.

    In multi-tenant mode the gate is the store's owner filter: an unknown run,
    another tenant's run and an ownerless (single-user) record all come back as
    ``None`` so every read endpoint answers the same 404. Local mode has a
    single-user surface and reads directly.
    """
    if context is not None:
        return store.get_for(run_id, context.tenant_id)
    return store.get(run_id)


@dataclass(frozen=True)
class ReadArtifact:
    """One file the read model is willing to serve, already redacted."""

    content: bytes
    content_type: str
    redacted: bool


def report_available(store: RunStore, record: RunRecord) -> bool:
    """Whether the report download would serve this record's report.

    ``hasReport`` and the download share this location rule and the same
    refusals, so the flag never promises a file the endpoint refuses.
    """
    location = store.report_location(record)
    if location is None:
        return False
    try:
        with ArtifactTree(location.root) as tree:
            tree.size(location.relative, max_bytes=REPORT_MAX_BYTES)
    except (ArtifactError, OSError):
        return False
    return True


def read_report(
    store: RunStore, record: RunRecord, redactor_for: RedactorFor
) -> ReadArtifact | None:
    """The redacted report of a record the caller already passed the gate on.

    ``store.report_location`` is the only place a report may come from; the
    read goes through a directory handle that never follows a link, and the
    bytes leave through the record's own redaction boundary. ``None`` means the
    endpoint answers its same 404.
    """
    location = store.report_location(record)
    if location is None:
        return None
    try:
        with ArtifactTree(location.root) as tree:
            data = tree.read(location.relative, max_bytes=REPORT_MAX_BYTES)
    except (ArtifactError, OSError) as exc:
        logger.warning("report refused run=%s: %s", record.run_id, exc)
        return None
    # The report is a product surface: a credential the model echoed into it
    # must not reach the browser. The redaction boundary is the record's own
    # tenant credentials, so one tenant's report is never compared against
    # another tenant's secrets.
    return _redacted(data, redactor_for(record), "text/markdown; charset=utf-8")


def available_deliverables(store: RunStore, record: RunRecord) -> list[dict[str, Any]]:
    """Detail read: keep failures visible, but mark vanished/linked copies unavailable."""
    result = []
    for index, item in enumerate(record.deliverables[:8]):
        public: dict[str, Any] = dict(item)
        location = store.deliverable_location(record, index)
        public["available"] = False
        if location is not None:
            try:
                with ArtifactTree(location.root) as tree:
                    tree.size(location.relative, max_bytes=REPORT_MAX_BYTES)
                public["available"] = True
            except (ArtifactError, OSError):
                pass
        result.append(public)
    return result


def read_deliverable(
    store: RunStore, record: RunRecord, index: int, redactor_for: RedactorFor
) -> ReadArtifact | None:
    location = store.deliverable_location(record, index)
    if location is None:
        return None
    try:
        with ArtifactTree(location.root) as tree:
            data = tree.read(location.relative, max_bytes=REPORT_MAX_BYTES)
    except (ArtifactError, OSError) as exc:
        logger.warning("deliverable refused run=%s index=%s: %s", record.run_id, index, exc)
        return None
    path = record.deliverables[index]["path"]
    suffix = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
    content_type = CHILD_FILE_TYPES.get(suffix, _DEFAULT_CONTENT_TYPE)
    original = data.decode("utf-8", errors="surrogateescape")
    redacted = redactor_for(record).redact(original).encode("utf-8", errors="surrogateescape")
    return ReadArtifact(redacted, content_type, redacted != data)


def read_child_documents(store: RunStore, record: RunRecord) -> ChildDocuments:
    """The record's authoritative child documents (§3.5), read-only.

    The documents hold only redacted, protocol-visible facts (they are built
    from committed event payloads), so they are served as stored.
    """
    return store.read_children(record.run_id)


def read_child_file(
    store: RunStore,
    record: RunRecord,
    n_text: str,
    name: str,
    redactor_for: RedactorFor,
) -> ReadArtifact | None:
    """One artifact file of one child document (§3.5, R2 enrichment).

    Same rules as the report download: only a regular file directly inside
    ``children/<n>/`` of a run the caller may see is served, read through a
    directory handle that never follows a link, and redacted again on the way
    out with the record's own boundary.
    """
    try:
        with ArtifactTree(store.run_dir(record.run_id)) as tree:
            data = tree.read(
                f"{CHILDREN_DIRNAME}/{int(n_text)}/{name}", max_bytes=REPORT_MAX_BYTES
            )
    except (ArtifactError, OSError, ValueError) as exc:
        logger.warning(
            "child artifact refused run=%s n=%s name=%r: %s", record.run_id, n_text, name, exc
        )
        return None
    suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    content_type = CHILD_FILE_TYPES.get(suffix, _DEFAULT_CONTENT_TYPE)
    return _redacted(data, redactor_for(record), content_type)


def _redacted(data: bytes, redactor: SecretRedactor, content_type: str) -> ReadArtifact:
    original = data.decode("utf-8", errors="replace")
    redacted = redactor.redact(original)
    return ReadArtifact(
        content=redacted.encode("utf-8"),
        content_type=content_type,
        redacted=redacted != original,
    )
