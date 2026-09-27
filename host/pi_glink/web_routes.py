"""Routing and the run/报告/health API of the workbench host.

The handler methods here decide *what* a request means: HTML assets, the health
and configuration probes, the run lifecycle endpoints (start, stop, detail,
report) and the dispatch into the SSE stream owned by :mod:`pi_glink.web_sse`.
Identity comes from :mod:`pi_glink.web_security`; bytes are written with
:mod:`pi_glink.web_http`.
"""

from __future__ import annotations

import logging
import re
import shutil
from http import HTTPStatus
from typing import Any
from urllib.parse import unquote, urlsplit

from .artifacts import ArtifactError, ArtifactTree
from .blueprints import Blueprint, list_blueprints
from .config import ConfigError
from .runs import (
    REPORT_MAX_BYTES,
    BlueprintUnavailable,
    FollowupUnavailable,
    ResourcesUnreleased,
    RunBusy,
    RunQuotaExceeded,
    UnsupportedCommand,
)
from .store import CHILDREN_DIRNAME, RunRecord
from .tenancy import TenantContext, TenantNotConfigured
from .web_security import AUTH_POST_PATHS

logger = logging.getLogger("pi_glink.host")

_RUN_EVENTS_RE = re.compile(r"^/api/runs/(?P<run_id>[A-Za-z0-9_-]{1,64})/events$")
_RUN_STOP_RE = re.compile(r"^/api/runs/(?P<run_id>[A-Za-z0-9_-]{1,64})/stop$")
_RUN_REPORT_RE = re.compile(r"^/api/runs/(?P<run_id>[A-Za-z0-9_-]{1,64})/report$")
_RUN_CHILDREN_RE = re.compile(r"^/api/runs/(?P<run_id>[A-Za-z0-9_-]{1,64})/children$")
_RUN_CHILD_FILE_RE = re.compile(
    r"^/api/runs/(?P<run_id>[A-Za-z0-9_-]{1,64})/children/(?P<n>\d{1,4})"
    r"/(?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,63})$"
)
_RUN_RE = re.compile(r"^/api/runs/(?P<run_id>[A-Za-z0-9_-]{1,64})$")

#: Run-body fields a client must never use to pick a model or supply a
#: credential. The provider is a server-side decision in every mode (the local
#: ``OPENAI_*`` configuration or the explicit multi-tenant policy), so these
#: names are refused outright instead of being silently ignored. Identity-ish
#: fields stay ignored (and are pinned by a regression) because they never
#: feed ``TenantContext``.
FORBIDDEN_RUN_BODY_FIELDS = frozenset(
    {
        "model",
        "modelid",
        "provider",
        "apikey",
        "modelapikey",
        "baseurl",
        "openaiapikey",
        "openaibaseurl",
        "openaimodel",
        "websearchkey",
        "credentials",
    }
)


def run_view(
    record: RunRecord, *, live_event_count: int | None = None, has_report: bool = False
) -> dict[str, Any]:
    """Public projection of a run: no host paths, no credentials."""
    duration = None
    if record.finished_at is not None:
        duration = round(record.finished_at - record.created_at, 3)
    return {
        "runId": record.run_id,
        "status": record.status,
        "prompt": record.prompt,
        "parentRunId": record.parent_run_id,
        "createdAt": record.created_at,
        "finishedAt": record.finished_at,
        "durationSeconds": duration,
        "sessionId": record.session_id,
        "hasReport": has_report,
        "stopReason": record.stop_reason,
        "error": record.error,
        "eventCount": record.event_count if live_event_count is None else live_event_count,
        "usage": record.usage,
        "redacted": record.redacted,
        # Session-template facts, recorded at admission. The host never advances
        # a stage, so these are labels of what the run was admitted with, not a
        # progression state.
        "blueprint": record.blueprint,
        "blueprintStages": [dict(stage) for stage in record.blueprint_stages],
    }


def blueprint_view(blueprint: Blueprint) -> dict[str, Any]:
    """Public projection of one usable blueprint: identity and stage metadata.

    The stage records use the same shape a run carries in ``blueprintStages``
    (:meth:`pi_glink.blueprints.Stage.as_record`), so a client can compare the
    catalogue with the contract a run was admitted with. The rendered body is
    deliberately absent: the listing names the contract, it does not ship the
    system-prompt text to the browser.
    """
    return {
        "name": blueprint.name,
        "description": blueprint.description,
        "stages": [stage.as_record() for stage in blueprint.stages],
    }


class RoutesMixin:
    """GET/HEAD/POST dispatch and the run API."""

    # -- dispatch -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server contract
        self._dispatch(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802 - http.server contract
        self._dispatch(head_only=True)

    def do_POST(self) -> None:  # noqa: N802 - http.server contract
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        # A handler that never reads the request body (stop, logout) would leave
        # those bytes in the socket; with HTTP/1.1 keep-alive the next request on
        # the same connection then starts with the leftover body — the server
        # parses it as a malformed request line and answers 501. The finally
        # block below drains whatever a handler did not consume.
        self._body_consumed = False
        try:
            # Multi-tenant POSTs carry a server-derived identity (an injected
            # assertion or a session cookie), and a browser can be made to send
            # one cross-site; refuse requests whose own origin metadata says
            # they came from another site. This runs before any credential is
            # checked — including login and registration.
            if self.server.tenancy is not None and not self._request_origin_allowed():
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {"error": "forbidden", "message": "cross-site request refused"},
                    head_only=False,
                )
                return
            if path in AUTH_POST_PATHS:
                self._handle_auth_post(path)
                return
            if path == "/api/runs":
                self._start_run()
                return
            stop_match = _RUN_STOP_RE.match(path)
            if stop_match is not None:
                self._stop_run(stop_match.group("run_id"))
                return
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "unknown_endpoint", "path": path},
                head_only=False,
            )
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - client left
            self.close_connection = True
        except Exception:  # pragma: no cover - defensive: never kill the server
            logger.exception("unhandled error serving POST %s", self._log_target())
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"}, head_only=False
            )
        finally:
            if not getattr(self, "_body_consumed", True):
                self._drain_body()

    def _dispatch(self, *, head_only: bool) -> None:
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        try:
            if path.startswith("/api/"):
                self._handle_api(path, parts.query, head_only=head_only)
                return
            self._handle_static(path, head_only=head_only)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - client left
            self.close_connection = True
        except Exception:  # pragma: no cover - defensive: never kill the server
            logger.exception("unhandled error serving %s", self._log_target())
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal_error"},
                head_only=head_only,
            )

    def _handle_api(self, path: str, query: str, *, head_only: bool) -> None:
        # The health probe stays reachable without an identity (a gateway or
        # process supervisor may poll it), but in multi-tenant mode it never
        # reports another tenant's active run. Every other /api route requires a
        # verified identity before it reads or reveals anything.
        if path == "/api/health":
            self._send_json(HTTPStatus.OK, self._health_payload(), head_only=head_only)
            return
        # ``/api/me`` answers for every mode: local mode reports the single-user
        # surface, gateway and accounts modes report the authenticated identity
        # (and answer 401 with the mode when there is none). The browser needs
        # this before it can decide whether to show a login form at all.
        if path == "/api/me":
            self._handle_me(head_only=head_only)
            return
        if path == "/api/invitations/check":
            # The check is a POST with a JSON body. A GET whose query carries a
            # token is refused before the query is read (and before anything can
            # log it), so the old URL form is dead rather than merely deprecated.
            if self.server.auth is None:
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "unknown_endpoint", "path": "/api/invitations/check"},
                    head_only=head_only,
                )
            else:
                self._send_json(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"error": "method_not_allowed", "message": "邀请校验只接受 POST JSON 请求"},
                    head_only=head_only,
                    extra_headers={"Allow": "POST"},
                )
            return
        context = self._authenticate()
        if self.server.tenancy is not None and context is None:
            self._send_json(
                HTTPStatus.UNAUTHORIZED,
                self._unauthenticated_payload(),
                head_only=head_only,
            )
            return
        if path == "/api/config":
            self._send_json(
                HTTPStatus.OK,
                self._public_config(context),
                head_only=head_only,
            )
            return
        if path == "/api/blueprints":
            # A read-only catalogue of the host's own repository content, so it
            # is a host-level resource: every authenticated identity (any tenant
            # in multi-tenant mode) sees the same derived list, exactly like
            # ``/api/config``. Identity is still required — the gate above is the
            # same one every other /api route uses. An empty or missing
            # directory answers 200 with an empty list: no blueprints is a
            # legitimate deployment (admission treats it the same way and never
            # fails startup), so it must not look like a broken endpoint; the
            # two states are deliberately indistinguishable and the host path is
            # never echoed.
            self._send_json(
                HTTPStatus.OK,
                {
                    "blueprints": [
                        blueprint_view(blueprint)
                        for blueprint in list_blueprints(self.server.config.blueprints_dir)
                    ]
                },
                head_only=head_only,
            )
            return
        if path == "/api/runs":
            records = (
                self.server.store.list_for(context.tenant_id, limit=100)
                if context is not None
                else self.server.store.list(limit=100)
            )
            self._send_json(
                HTTPStatus.OK,
                {"runs": [self._run_view_live(record) for record in records]},
                head_only=head_only,
            )
            return
        events_match = _RUN_EVENTS_RE.match(path)
        if events_match is not None:
            self._stream_run_events(events_match.group("run_id"), query, context)
            return
        report_match = _RUN_REPORT_RE.match(path)
        if report_match is not None:
            self._send_report(report_match.group("run_id"), context, head_only=head_only)
            return
        children_match = _RUN_CHILDREN_RE.match(path)
        if children_match is not None:
            self._send_children(children_match.group("run_id"), context, head_only=head_only)
            return
        child_file_match = _RUN_CHILD_FILE_RE.match(path)
        if child_file_match is not None:
            self._send_child_file(
                child_file_match.group("run_id"),
                child_file_match.group("n"),
                child_file_match.group("name"),
                context,
                head_only=head_only,
            )
            return
        run_match = _RUN_RE.match(path)
        if run_match is not None:
            self._send_run(run_match.group("run_id"), context, head_only=head_only)
            return
        self._send_json(
            HTTPStatus.NOT_FOUND, {"error": "unknown_endpoint", "path": path},
            head_only=head_only,
        )

    # -- payloads -------------------------------------------------------------

    def _health_payload(self) -> dict[str, Any]:
        config = self.server.config
        pi_path = shutil.which(config.pi_bin)
        payload: dict[str, Any] = {
            "status": "ok",
            "executor": config.executor,
            "pi": {"bin": config.pi_bin, "available": pi_path is not None},
            "configured": config.public_view()["configured"],
        }
        # An unauthenticated probe must not disclose whether (or which) runs are
        # active in the multi-tenant surface.
        if self.server.tenancy is None:
            payload["activeRunId"] = self.server.manager.active_run_id
        return payload

    # -- run endpoints --------------------------------------------------------

    def _start_run(self) -> None:
        context = self._authenticate()
        if self.server.tenancy is not None and context is None:
            self._send_json(
                HTTPStatus.UNAUTHORIZED, self._unauthenticated_payload(), head_only=False
            )
            return
        if not self._require_csrf(context):
            return
        body = self._read_json_body()
        if body is None:
            return
        # The model and its credential are server-side decisions: a client that
        # tries to select or supply either one is refused before any execution
        # face exists, instead of quietly running with an ignored field.
        supplied = sorted(
            name for name in body if name.strip().lower() in FORBIDDEN_RUN_BODY_FIELDS
        )
        if supplied:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "invalid_request",
                    "message": "模型与凭据只能由服务端配置，客户端字段不被接受",
                    "fields": supplied,
                },
                head_only=False,
            )
            return
        prompt = body.get("prompt")
        if not isinstance(prompt, str):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_request", "message": "prompt must be a string"},
                head_only=False,
            )
            return
        parent_run_id = body.get("parentRunId")
        if parent_run_id is not None and (not isinstance(parent_run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", parent_run_id)):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"}, head_only=False)
            return
        blueprint = body.get("blueprint")
        if blueprint is not None and not isinstance(blueprint, str):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_request", "message": "blueprint must be a string"},
                head_only=False,
            )
            return
        try:
            record = self.server.manager.start(
                prompt, context, parent_run_id=parent_run_id, blueprint=blueprint
            )
        except UnsupportedCommand as exc:
            # ``/``-prefix input is how pi reaches extension commands, including
            # pi-subagents' detaching ones. Only the prompt templates rendered
            # from ``harnesses/pi/prompts/`` are accepted; the refusal names the
            # allowlist and never echoes the rejected command or the prompt.
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "unsupported_command",
                    "allowed": list(exc.allowed),
                    "message": "prompt 以 / 开头但不是本工作台接受的模板命令",
                },
                head_only=False,
            )
            return
        except BlueprintUnavailable as exc:
            # The same fail-closed shape as the ``/`` gate: the answer names the
            # derived blueprint stems (reviewed repository content) and never
            # echoes the submitted value, so the endpoint reveals nothing about
            # arbitrary input. Unknown and malformed are distinct codes; both
            # are refused before any run directory or execution face exists, and
            # the same rule serves every harness.
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": exc.code,
                    "available": list(exc.available),
                    "message": (
                        "blueprint 不存在于 blueprints/ 目录"
                        if exc.code == "unknown_blueprint"
                        else "blueprint 文件不可用（格式校验失败）"
                    ),
                },
                head_only=False,
            )
            return
        except FollowupUnavailable as exc:
            # A follow-up may only continue a run its own tenant can read: an
            # unknown, other-tenant or ownerless parent is the same 404 as an
            # unknown run, and nothing of the parent is read before this gate.
            self._send_json(
                HTTPStatus.CONFLICT if exc.code != "unknown_run" else HTTPStatus.NOT_FOUND,
                {"error": exc.code},
                head_only=False,
            )
            return
        except RunQuotaExceeded as quota:
            # Explain the ceiling, never who else is using it.
            self._send_json(
                HTTPStatus.TOO_MANY_REQUESTS,
                {
                    "error": "quota_exceeded",
                    "scope": quota.scope,
                    "limit": quota.limit,
                    "message": "concurrency limit reached; retry later",
                },
                head_only=False,
            )
            return
        except RunBusy as busy:
            payload: dict[str, Any] = {
                "error": "run_busy",
                "message": "another run is still active",
            }
            if not self.server.config.multi_tenant:
                payload["activeRunId"] = busy.active_run_id
            self._send_json(HTTPStatus.CONFLICT, payload, head_only=False)
            return
        except ResourcesUnreleased as held:
            payload = {
                "error": "resources_unreleased",
                "message": "earlier runs hold execution resources that are not confirmed released",
            }
            # The held run ids belong to their owners: a tenant must not learn
            # another tenant's identifiers from a refusal.
            if not self.server.config.multi_tenant:
                payload["runIds"] = held.run_ids
            self._send_json(HTTPStatus.CONFLICT, payload, head_only=False)
            return
        except TenantNotConfigured as exc:
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "forbidden", "message": str(exc)},
                head_only=False,
            )
            return
        except ConfigError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "not_configured", "message": str(exc)},
                head_only=False,
            )
            return
        except ValueError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_request", "message": str(exc)},
                head_only=False,
            )
            return
        self._send_json(
            HTTPStatus.CREATED, {"run": self._run_view_live(record)}, head_only=False
        )

    def _stop_run(self, run_id: str) -> None:
        context = self._authenticate()
        if self.server.tenancy is not None and context is None:
            self._send_json(
                HTTPStatus.UNAUTHORIZED, self._unauthenticated_payload(), head_only=False
            )
            return
        if not self._require_csrf(context):
            return
        decision = self.server.manager.stop(run_id, context)
        if decision.accepted:
            self._send_json(
                HTTPStatus.OK,
                {"requested": True, "status": decision.status},
                head_only=False,
            )
        elif decision.reason == "unknown":
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "unknown_run", "requested": False, "runId": run_id},
                head_only=False,
            )
        elif decision.reason == "forbidden":
            self._send_json(
                HTTPStatus.FORBIDDEN,
                {"error": "forbidden", "requested": False},
                head_only=False,
            )
        else:
            # Past the run's commit point: the answer is the committed status,
            # not a promise that the run will be stopped.
            self._send_json(
                HTTPStatus.CONFLICT,
                {
                    "error": "run_not_cancellable",
                    "requested": False,
                    "status": decision.status,
                    "reason": decision.reason,
                },
                head_only=False,
            )

    def _run_view_live(self, record: RunRecord) -> dict[str, Any]:
        """Run view with the live event count while the run is still open.

        ``meta.json`` is only rewritten at state transitions, so the persisted
        count is stale during a run; an open run's count comes from the bus.
        Once the run is closed the persisted count is authoritative (the bus
        drops its per-run window at close).
        """
        live = (
            self.server.bus.last_seq(record.run_id)
            if self.server.bus.is_open(record.run_id)
            else None
        )
        return run_view(
            record, live_event_count=live, has_report=self._report_available(record)
        )

    def _report_available(self, record: RunRecord) -> bool:
        """Whether the report endpoint would serve this record's report.

        The flag and the download use the same location rule and the same
        refusals, so ``hasReport`` never promises a file the endpoint refuses.
        """
        location = self.server.store.report_location(record)
        if location is None:
            return False
        try:
            with ArtifactTree(location.root) as tree:
                tree.size(location.relative, max_bytes=REPORT_MAX_BYTES)
        except (ArtifactError, OSError):
            return False
        return True

    def _send_run(
        self, run_id: str, context: TenantContext | None, *, head_only: bool
    ) -> None:
        record = (
            self.server.store.get_for(run_id, context.tenant_id)
            if context is not None
            else self.server.store.get(run_id)
        )
        if record is None:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "unknown_run", "runId": run_id},
                head_only=head_only,
            )
            return
        self._send_json(
            HTTPStatus.OK, {"run": self._run_view_live(record)}, head_only=head_only
        )

    def _send_children(
        self, run_id: str, context: TenantContext | None, *, head_only: bool
    ) -> None:
        """The run's authoritative child-run documents (§3.5), read-only.

        Owner rules are exactly the run detail's: an unknown run, another
        tenant's run and a legacy ownerless record all answer the same 404, and
        local mode reads directly. The documents hold only redacted,
        protocol-visible facts (they are built from committed event payloads),
        so they are served as stored. ``skipped`` reports corruption honestly
        instead of equating an unreadable document with absence; it appears
        only when it is non-zero.
        """
        record = (
            self.server.store.get_for(run_id, context.tenant_id)
            if context is not None
            else self.server.store.get(run_id)
        )
        if record is None:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "unknown_run", "runId": run_id},
                head_only=head_only,
            )
            return
        page = self.server.store.read_children(run_id)
        payload: dict[str, Any] = {
            "children": [dict(document) for document in page.documents]
        }
        if page.skipped:
            payload["skipped"] = page.skipped
        self._send_json(HTTPStatus.OK, payload, head_only=head_only)

    #: Content types for the children artifact download, by file extension.
    _CHILD_FILE_TYPES = {
        ".md": "text/markdown; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".jsonl": "application/x-ndjson; charset=utf-8",
    }

    def _send_child_file(
        self,
        run_id: str,
        n_text: str,
        name: str,
        context: TenantContext | None,
        *,
        head_only: bool,
    ) -> None:
        """One artifact file of one child document (§3.5, R2 enrichment).

        Same rules as the report download: the path is not a capability — only
        a regular file directly inside ``children/<n>/`` of a run the caller
        may see is served, read through a directory handle that never follows
        a link, and redacted again on the way out with the record's own
        boundary (``X-Redacted`` records whether that happened).
        """
        record = (
            self.server.store.get_for(run_id, context.tenant_id)
            if context is not None
            else self.server.store.get(run_id)
        )
        not_found = {"error": "no_artifact", "runId": run_id}
        if record is None:
            self._send_json(HTTPStatus.NOT_FOUND, not_found, head_only=head_only)
            return
        try:
            with ArtifactTree(self.server.store.run_dir(run_id)) as tree:
                data = tree.read(
                    f"{CHILDREN_DIRNAME}/{int(n_text)}/{name}", max_bytes=REPORT_MAX_BYTES
                )
        except (ArtifactError, OSError, ValueError) as exc:
            logger.warning("child artifact refused run=%s n=%s name=%r: %s", run_id, n_text, name, exc)
            self._send_json(HTTPStatus.NOT_FOUND, not_found, head_only=head_only)
            return
        original = data.decode("utf-8", errors="replace")
        redactor = self.server.manager.redactor_for_record(record)
        redacted = redactor.redact(original)
        content_type = self._CHILD_FILE_TYPES.get(
            "." + name.rsplit(".", 1)[-1], "application/octet-stream"
        )
        self._send_bytes(
            redacted.encode("utf-8"),
            content_type,
            "no-cache",
            head_only=head_only,
            extra_headers={"X-Redacted": "true" if redacted != original else "false"},
        )

    def _send_report(
        self, run_id: str, context: TenantContext | None, *, head_only: bool
    ) -> None:
        record = (
            self.server.store.get_for(run_id, context.tenant_id)
            if context is not None
            else self.server.store.get(run_id)
        )
        # ``report_path`` is not a download capability: only the run's own
        # host artifact, or the report inside the run's own workspace written
        # by earlier versions, may be served — read-only, through a directory
        # handle that never follows a link. An unknown run, another tenant's run
        # and a legacy ownerless record all answer the same 404.
        location = self.server.store.report_location(record) if record else None
        if location is None:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "no_report", "runId": run_id},
                head_only=head_only,
            )
            return
        try:
            with ArtifactTree(location.root) as tree:
                data = tree.read(location.relative, max_bytes=REPORT_MAX_BYTES)
        except (ArtifactError, OSError) as exc:
            logger.warning(
                "report refused run=%s layout=%s: %s", run_id, location.layout, exc
            )
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "no_report", "runId": run_id},
                head_only=head_only,
            )
            return
        # The report is a product surface too: a credential the model echoed into
        # it must not reach the browser. The header records whether it happened.
        # The redaction boundary is the record's own tenant credentials, so one
        # tenant's report is never compared against another tenant's secrets.
        original = data.decode("utf-8", errors="replace")
        redactor = self.server.manager.redactor_for_record(record)
        redacted = redactor.redact(original)
        self._send_bytes(
            redacted.encode("utf-8"),
            "text/markdown; charset=utf-8",
            "no-cache",
            head_only=head_only,
            extra_headers={"X-Redacted": "true" if redacted != original else "false"},
        )
