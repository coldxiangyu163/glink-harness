"""JSONL framing shared by every execution face.

pi documents strict LF framing: records are separated by ``\\n`` only, with an
optional preceding ``\\r``; Unicode line/paragraph separators (U+2028/U+2029)
are valid **inside** a JSON string and are not record boundaries
(``docs/json.md``, "Framing and process I/O"). A reader that also splits on
U+2028 — ``str.splitlines``, ``readline``-style decoders — tears a valid frame in
two and loses both halves.

Both faces therefore frame through this module: the RPC client on the raw byte
stream, the E2B face on the decoded sandbox file. ``tests/fixtures/lf-framing.json``
is the shared corpus both faces are exercised with.
"""

from __future__ import annotations

from typing import Iterator


def split_lf_records(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Split complete LF-terminated records out of a byte buffer.

    Returns ``(records, remainder)``; the remainder is the still-incomplete tail
    that must be kept until more bytes arrive.
    """
    parts = buffer.split(b"\n")
    remainder = parts.pop()
    return parts, remainder


def iter_lf_lines(text: str) -> Iterator[str]:
    """Yield records of a decoded JSONL stream, splitting on LF only."""
    yield from text.split("\n")


def split_lf_text(text: str) -> list[str]:
    """Records of a decoded JSONL stream (LF only; U+2028/U+2029 preserved)."""
    return list(iter_lf_lines(text))


def strip_trailing_cr(record: str) -> str:
    """Drop the optional CR a CRLF writer leaves before the LF."""
    return record.removesuffix("\r")


def strip_trailing_cr_bytes(record: bytes) -> bytes:
    return record[:-1] if record.endswith(b"\r") else record
