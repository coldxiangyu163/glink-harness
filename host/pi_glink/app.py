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
from .static_files import Asset, WebAssets

logger = logging.getLogger("pi_glink.host")

_RUN_EVENTS_RE = re.compile(r"^/api/runs/(?P<run_id>[A-Za-z0-9_-]{1,64})/events$")
_HEARTBEAT_SECONDS = 15.0


class PiGlinkServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying the frozen config and the event bus."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, config: HostConfig, bus: EventBus, assets: WebAssets) -> None:
        self.config = config
        self.bus = bus
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
        match = _RUN_EVENTS_RE.match(path)
        if match is not None:
            self._stream_run_events(match.group("run_id"), query)
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
        }

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


def create_server(config: HostConfig) -> PiGlinkServer:
    """Build a ready-to-serve host (used by tests and by ``main``)."""
    return PiGlinkServer(config, EventBus(), WebAssets(config.web_root))


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
