"""Low-level HTTP plumbing shared by the host's request handler.

Request bodies, JSON/asset responses, static file serving and sanitized access
logging live here so routing, identity and the SSE loop can build on one
implementation. Nothing in this module decides *what* a request means: no
routing, no authorization.
"""

from __future__ import annotations

import json
import logging
from http import HTTPStatus
from typing import Any, Mapping
from urllib.parse import urlsplit

from .static_files import Asset

logger = logging.getLogger("pi_glink.host")

#: Ceiling for one request body. Control payloads (prompt, credentials-free
#: JSON) are small; anything larger is refused instead of buffered.
MAX_REQUEST_BYTES = 64 * 1024


class HttpPlumbingMixin:
    """Bodies, responses, static assets and log sanitization."""

    def _drain_body(self) -> None:
        """Read and discard an unconsumed request body.

        Keeps an HTTP/1.1 keep-alive connection in sync when a route ignores the
        body (``stop``, ``logout``). The read is bounded by the declared length
        and by :data:`MAX_REQUEST_BYTES`; an oversized body closes the
        connection instead of being drained.
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            length = 0
        if length > MAX_REQUEST_BYTES:
            self.close_connection = True
            return
        remaining = max(0, length)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                break
            remaining -= len(chunk)

    def _expects_json(self) -> bool:
        raw = self.headers.get("Content-Type", "")
        return raw.split(";", 1)[0].strip().lower() == "application/json"

    def _read_json_body(self) -> dict[str, Any] | None:
        # Mark the body as handled: this method either consumes it or closes the
        # connection, and ``do_POST`` must not drain it a second time.
        self._body_consumed = True
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_REQUEST_BYTES:
            if length > MAX_REQUEST_BYTES:
                # Never reuse a connection whose unread body we are not willing
                # to drain; the client must reconnect cleanly.
                self.close_connection = True
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

    def _send_json(
        self,
        status: HTTPStatus,
        payload: dict[str, Any],
        *,
        head_only: bool,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
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

    def _handle_static(self, path: str, *, head_only: bool) -> None:
        asset = self.server.assets.resolve(path)
        if asset is None:
            self._send_json(
                HTTPStatus.NOT_FOUND, {"error": "not_found", "path": path},
                head_only=head_only,
            )
            return
        self._send_asset(asset, head_only=head_only)

    def _client_ip(self) -> str:
        """The socket peer address.

        Deliberately not ``X-Forwarded-For``: a TLS-terminating proxy makes
        every request look like loopback, and a client-supplied header must
        never label a request (it would hand an attacker a free rate-limit
        bypass). Limits behind a proxy therefore act as a global throttle, and
        the per-account login limit is the primary protection.
        """
        return str(self.client_address[0]) if self.client_address else "unknown"

    # -- access logging -------------------------------------------------------

    def _sanitized_log_argument(self, value: Any) -> str:
        """A log argument with any query string/fragment removed.

        Invitation codes and session tokens must not be written to logs (which
        are commonly shipped to a collector). Our own URLs never carry them, but
        a hostile or outdated client can still try; everything from the first
        ``?`` or ``#`` on is dropped before the message is formatted.
        """
        text = str(value)
        for marker in ("?", "#"):
            head, found, _tail = text.partition(marker)
            if found:
                text = head
        return text

    def _log_target(self) -> str:
        """The request path without query/fragment, safe to log."""
        target = urlsplit(self.path).path
        return self._sanitized_log_argument(target or self.path)

    def _log_request(self, level: int, format: str, args: tuple[Any, ...]) -> None:
        safe = tuple(self._sanitized_log_argument(argument) for argument in args)
        try:
            message = format % safe
        except (TypeError, ValueError):  # pragma: no cover - unusual custom format
            message = self._log_target()
        logger.log(level, "%s - %s", self.address_string(), message)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        self._log_request(logging.DEBUG, format, args)

    def log_error(self, format: str, *args: Any) -> None:  # noqa: A002
        self._log_request(logging.WARNING, format, args)
