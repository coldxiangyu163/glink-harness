"""Static asset serving for the workbench front-end.

The front-end is plain ESM served from disk; there is no bundler in the loop.
Two contracts matter:

* ``index.html`` keeps a stable entry URL. ``app.js`` and ``styles.css`` are
  referenced as ``?v=auto`` and rewritten at serve time from file content, so a
  browser can never pair a stale entry script with a new module graph.
* Everything under ``modules/`` is served ``no-cache``: a changed submodule
  takes effect without the entry file changing content.

Unknown extension-less paths fall back to ``index.html`` so client-side routes
survive a reload. A missing module returns 404 instead: a module request must
never be answered with HTML.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

#: Assets whose ``?v=`` query is rewritten from content.
_VERSIONED_ASSETS = ("app.js", "styles.css")
_VERSION_QUERY_RE = re.compile(
    r"(?P<asset>app\.js|styles\.css)\?v=[^\"'\s<>&]*",
)
_MIME_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
}
_LONG_CACHE = "public, max-age=31536000, immutable"
_NO_CACHE = "no-cache"


@dataclass(frozen=True, slots=True)
class Asset:
    """A resolved asset ready to be written to the socket."""

    path: Path
    content_type: str
    cache_control: str
    body: bytes


class WebAssets:
    """Resolve request paths to files under one root, with no traversal."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._versions: dict[str, tuple[tuple[int, int], str]] = {}

    @property
    def root(self) -> Path:
        return self._root

    def index(self) -> bytes:
        """Return ``index.html`` with content-hashed asset queries."""
        content = (self._root / "index.html").read_text(encoding="utf-8")
        return _VERSION_QUERY_RE.sub(
            lambda match: f"{match.group('asset')}?v={self._version(match.group('asset'))}",
            content,
        ).encode("utf-8")

    def resolve(self, request_path: str) -> Asset | None:
        """Return the asset for ``request_path`` or ``None`` when absent."""
        relative = request_path.split("?", 1)[0].lstrip("/")
        if not relative:
            return self._index_asset()
        candidate = (self._root / relative).resolve()
        if not candidate.is_relative_to(self._root) or not candidate.is_file():
            # A module or asset request must not be answered with the SPA shell.
            if Path(relative).suffix:
                return None
            return self._index_asset()
        return self._file_asset(candidate)

    def _index_asset(self) -> Asset:
        return Asset(
            path=self._root / "index.html",
            content_type=_MIME_TYPES[".html"],
            cache_control=_NO_CACHE,
            body=self.index(),
        )

    def _file_asset(self, path: Path) -> Asset:
        suffix = path.suffix.lower()
        content_type = _MIME_TYPES.get(suffix, "application/octet-stream")
        cache_control = _NO_CACHE if _is_uncacheable(path, self._root) else _LONG_CACHE
        return Asset(
            path=path,
            content_type=content_type,
            cache_control=cache_control,
            body=path.read_bytes(),
        )

    def _version(self, asset_name: str) -> str:
        path = self._root / asset_name
        metadata = path.stat()
        cache_key = (metadata.st_mtime_ns, metadata.st_size)
        cached = self._versions.get(asset_name)
        if cached is not None and cached[0] == cache_key:
            return cached[1]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        self._versions[asset_name] = (cache_key, digest)
        return digest


def _is_uncacheable(path: Path, root: Path) -> bool:
    if path.name in _VERSIONED_ASSETS or path.suffix.lower() == ".html":
        return True
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    return bool(relative.parts) and relative.parts[0] == "modules"
