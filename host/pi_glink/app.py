"""HTTP entry point for the pi-glink host.

One process serves the workbench front-end and the run event stream. There is
no web framework in the loop: the surface is small (health, public config, a
per-run SSE stream and static assets), and keeping it on the standard library
makes ``python -m pi_glink.app`` the only thing an operator has to run.

Run events are never stored here — the bus is the live fan-out plus a bounded
replay window; durable run metadata and artifacts belong to the store added
with the first real run (task-3).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import threading
from dataclasses import replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .config import ConfigError, HostConfig, load_config
from .events import EventBus
from .kernel.runner import ClientFactory, RunBusy, RunManager
from .kernel.rpc import PiRpcClient
from .static_files import Asset, WebAssets
from .store import RunRecord, RunStore

logger = logging.getLogger("pi_glink.host")

_RUN_EVENTS_RE = re.compile(r"^/api/runs/(?P<run_id>[A-Za-z0-9_-]{1,64})/events$")
_RUN_STOP_RE = re.compile(r"^/api/runs/(?P<run_id>[A-Za-z0-9_-]{1,64})/stop$")
_RUN_REPORT_RE = re.compile(r"^/api/runs/(?P<run_id>[A-Za-z0-9_-]{1,64})/report$")
_RUN_RE = re.compile(r"^/api/runs/(?P<run_id>[A-Za-z0-9_-]{1,64})$")
_MAX_REQUEST_BYTES = 64 * 1024
_HEARTBEAT_SECONDS = 15.0


class PiGlinkServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying the frozen config, bus, store and manager."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        config: HostConfig,
        bus: EventBus,
        store: RunStore,
        manager: RunManager,
        assets: WebAssets,
    ) -> None:
        self.config = config
        self.bus = bus
        self.store = store
        self.manager = manager
        self.assets = assets
        self.stopping = threading.Event()
        super().__init__((config.host, config.port), HostRequestHandler)


class HostRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "pi-glink"

    server: PiGlinkServer

    # -- routes ---------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server contract
        self._dispatch(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802 - http.server contract
        self._dispatch(head_only=True)

    def do_POST(self) -> None:  # noqa: N802 - http.server contract
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        try:
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
            logger.exception("unhandled error serving POST %s", self.path)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error"}, head_only=False
            )

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
            logger.exception("unhandled error serving %s", self.path)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "internal_error"},
                head_only=head_only,
            )

    def _handle_api(self, path: str, query: str, *, head_only: bool) -> None:
        if path == "/api/health":
            self._send_json(HTTPStatus.OK, self._health_payload(), head_only=head_only)
            return
        if path == "/api/config":
            self._send_json(
                HTTPStatus.OK, self.server.config.public_view(), head_only=head_only
            )
            return
        if path == "/api/runs":
            records = self.server.store.list(limit=100)
            self._send_json(
                HTTPStatus.OK,
                {"runs": [self._run_view_live(record) for record in records]},
                head_only=head_only,
            )
            return
        events_match = _RUN_EVENTS_RE.match(path)
        if events_match is not None:
            self._stream_run_events(events_match.group("run_id"), query)
            return
        report_match = _RUN_REPORT_RE.match(path)
        if report_match is not None:
            self._send_report(report_match.group("run_id"), head_only=head_only)
            return
        run_match = _RUN_RE.match(path)
        if run_match is not None:
            self._send_run(run_match.group("run_id"), head_only=head_only)
            return
        self._send_json(
            HTTPStatus.NOT_FOUND, {"error": "unknown_endpoint", "path": path},
            head_only=head_only,
        )

    def _handle_static(self, path: str, *, head_only: bool) -> None:
        asset = self.server.assets.resolve(path)
        if asset is None:
            self._send_json(
                HTTPStatus.NOT_FOUND, {"error": "not_found", "path": path},
                head_only=head_only,
            )
            return
        self._send_asset(asset, head_only=head_only)

    # -- payloads -------------------------------------------------------------

    def _health_payload(self) -> dict[str, Any]:
        config = self.server.config
        pi_path = shutil.which(config.pi_bin)
        return {
            "status": "ok",
            "executor": config.executor,
            "pi": {"bin": config.pi_bin, "available": pi_path is not None},
            "configured": config.public_view()["configured"],
            "activeRunId": self.server.manager.active_run_id,
        }

    # -- run endpoints --------------------------------------------------------

    def _start_run(self) -> None:
        body = self._read_json_body()
        if body is None:
            return
        prompt = body.get("prompt")
        if not isinstance(prompt, str):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_request", "message": "prompt must be a string"},
                head_only=False,
            )
            return
        try:
            record = self.server.manager.start(prompt)
        except RunBusy as busy:
            self._send_json(
                HTTPStatus.CONFLICT,
                {
                    "error": "run_busy",
                    "message": "another run is still active",
                    "activeRunId": busy.active_run_id,
                },
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
        requested = self.server.manager.stop(run_id)
        self._send_json(HTTPStatus.OK, {"requested": requested}, head_only=False)

    def _run_view_live(self, record: RunRecord) -> dict[str, Any]:
        """Run view with the live event count while the run is still open.

        ``meta.json`` is only rewritten at state transitions, so the persisted
        count is stale during a run; the bus knows the real number and the
        browser uses it to show progress.
        """
        live = self.server.bus.last_seq(record.run_id)
        return _run_view(record, live_event_count=max(live, record.event_count))

    def _send_run(self, run_id: str, *, head_only: bool) -> None:
        record = self.server.store.get(run_id)
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

    def _send_report(self, run_id: str, *, head_only: bool) -> None:
        record = self.server.store.get(run_id)
        if record is None or not record.report_path:
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "no_report", "runId": run_id},
                head_only=head_only,
            )
            return
        path = Path(record.report_path)
        if not path.is_file():
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {"error": "no_report", "runId": run_id},
                head_only=head_only,
            )
            return
        # The report is a product surface too: a credential the model echoed into
        # it must not reach the browser. The header records whether it happened.
        original = path.read_text(encoding="utf-8", errors="replace")
        redactor = self.server.manager.redactor
        redacted = redactor.redact(original)
        self._send_bytes(
            redacted.encode("utf-8"),
            "text/markdown; charset=utf-8",
            "no-cache",
            head_only=head_only,
            extra_headers={"X-Redacted": "true" if redacted != original else "false"},
        )

    def _read_json_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_REQUEST_BYTES:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_request", "message": "missing or oversized body"},
                head_only=False,
            )
            return None
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_request", "message": "body must be JSON"},
                head_only=False,
            )
            return None
        if not isinstance(payload, dict):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_request", "message": "body must be a JSON object"},
                head_only=False,
            )
            return None
        return payload

    # -- responses ------------------------------------------------------------

    def _send_json(
        self, status: HTTPStatus, payload: dict[str, Any], *, head_only: bool
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_asset(self, asset: Asset, *, head_only: bool) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", asset.content_type)
        self.send_header("Content-Length", str(len(asset.body)))
        self.send_header("Cache-Control", asset.cache_control)
        self.end_headers()
        if not head_only:
            self.wfile.write(asset.body)

    def _send_bytes(
        self,
        body: bytes,
        content_type: str,
        cache_control: str,
        *,
        head_only: bool,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    # -- server-sent events ---------------------------------------------------

    def _stream_run_events(self, run_id: str, query: str) -> None:
        bus = self.server.bus
        if not bus.is_open(run_id):
            self._send_json(
                HTTPStatus.NOT_FOUND, {"error": "unknown_run", "runId": run_id},
                head_only=False,
            )
            return
        after = _resume_offset(self.headers.get("Last-Event-ID"), query)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        logger.info("event stream attached run=%s after=%s", run_id, after)
        try:
            for event in bus.subscribe(run_id, after=after):
                self._write_event(event.to_sse())
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            logger.info("event stream detached run=%s", run_id)
            self._write_end_of_stream()

    def _write_event(self, frame: str) -> None:
        body = frame.encode("utf-8")
        self.wfile.write(f"{len(body):X}\r\n".encode("ascii") + body + b"\r\n")
        self.wfile.flush()

    def _write_end_of_stream(self) -> None:
        """Terminate the chunked response so a read sees a clean EOF."""
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("%s - %s", self.address_string(), format % args)

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.warning("%s - %s", self.address_string(), format % args)


def _run_view(record: RunRecord, *, live_event_count: int | None = None) -> dict[str, Any]:
    """Public projection of a run: no host paths, no credentials."""
    duration = None
    if record.finished_at is not None:
        duration = round(record.finished_at - record.created_at, 3)
    return {
        "runId": record.run_id,
        "status": record.status,
        "prompt": record.prompt,
        "createdAt": record.created_at,
        "finishedAt": record.finished_at,
        "durationSeconds": duration,
        "sessionId": record.session_id,
        "hasReport": bool(record.report_path),
        "stopReason": record.stop_reason,
        "error": record.error,
        "eventCount": record.event_count if live_event_count is None else live_event_count,
        "usage": record.usage,
        "redacted": record.redacted,
    }


def _resume_offset(last_event_id: str | None, query: str) -> int:
    """Resolve the resume cursor: header wins, then ``?after=``, else 0."""
    candidates = [last_event_id]
    candidates.extend(parse_qs(query).get("after", []))
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            return max(0, int(candidate))
        except (TypeError, ValueError):
            continue
    return 0


def create_server(
    config: HostConfig, *, client_factory: ClientFactory | None = None
) -> PiGlinkServer:
    """Build a ready-to-serve host (used by tests and by ``main``).

    ``client_factory`` exists for tests: production always talks to the real
    ``pi`` binary through :class:`~pi_glink.kernel.rpc.PiRpcClient`.
    """
    bus = EventBus()
    store = RunStore(config.data_root / "runs")
    manager = RunManager(
        config, bus, store, client_factory=client_factory or PiRpcClient
    )
    return PiGlinkServer(config, bus, store, manager, WebAssets(config.web_root))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pi-glink",
        description="本地工作台宿主 / local workbench host (pi kernel)",
    )
    parser.add_argument("--host", help="覆盖 LOCAL_WEB_HOST")
    parser.add_argument("--port", type=int, help="覆盖 LOCAL_WEB_PORT")
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="打印生效配置（不含密钥）后退出",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"配置错误 / configuration error: {exc}", file=sys.stderr)
        return 2
    if args.host or args.port:
        config = replace(
            config,
            host=args.host or config.host,
            port=args.port or config.port,
        )
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.print_config:
        print(json.dumps(config.public_view(), ensure_ascii=False, indent=2))
        return 0

    server = create_server(config)
    url = f"http://{config.host}:{server.server_address[1]}/"
    pi_path = shutil.which(config.pi_bin)
    print(f"pi-glink 宿主已启动 / host listening: {url}")
    print(f"执行面 executor: {config.executor}")
    print(f"内核 pi: {pi_path or f'未找到 ({config.pi_bin}) / not found'}")
    print(
        "模型 model: "
        + (config.openai_model or "未配置 / unset")
        + (" (已配置密钥 / key set)" if config.has_openai_key else " (缺少密钥 / no key)")
    )
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，正在退出 / interrupted, shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
