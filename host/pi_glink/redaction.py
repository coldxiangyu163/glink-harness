"""Redaction boundary for everything the host emits or serves.

The host necessarily holds credentials (model key, sandbox key, search key), and
pi's tools can surface them: a `bash` call can run `env`, a `read` call can Open
the runtime config file, and the model may quote either back into its answer.
The event stream, `events.jsonl`, the report download and the browser are all
*product* surfaces, so a secret appearing there has already crossed the boundary
the objective defines: **密钥不进前端**.

This module is that boundary. Two complementary detections run together:

* **value detection** — the literal secrets the host knows about (from `.env`
  and from the rendered `web-search.json`), so an exact echo is always caught;
* **shape detection** — provider key shapes and ``KEY=value`` / ``Bearer …``
  assignments, so a *copy* of a credential that the host never stored (for
  example one pasted into a prompt, or a third-party token in tool output) is
  still masked.

Redaction is applied on the single egress path the runner owns (`_emit`), again
when a report is served, and is verified by regression plus a repo scanner
(`scripts/check-no-secrets.py`). Nothing in this module ever logs a secret.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

REPLACEMENT = "[REDACTED]"
#: Values shorter than this are not treated as secrets (avoids masking e.g. "yes").
MIN_SECRET_LENGTH = 8

#: Environment-variable names whose assignments must be masked (task contract
#: listed them explicitly, and audit reproduced the leak with OPENAI_API_KEY).
_SENSITIVE_NAMES = (
    "OPENAI_API_KEY",
    "E2B_API_KEY",
    "EXA_API_KEY",
    "SERPER_API_KEY",
    "JINA_API_KEY",
    "JUDGE_API_KEY",
    "SUMMARY_LLM_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "PINECONE_API_KEY",
    "AWS_SECRET_ACCESS_KEY",
)

#: Assignment form: NAME=value / NAME: value / "NAME": "value" (config files).
_ASSIGNMENT_RE = re.compile(
    r"(?P<name>" + "|".join(_SENSITIVE_NAMES) + r")"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<quote>[\"']?)"
    r"(?P<value>[^\s\"',}]{8,})"
    r"(?P=quote)",
    re.IGNORECASE,
)

#: Generic JSON-ish credential fields (web-search.json uses e.g. exaApiKey).
_JSON_FIELD_RE = re.compile(
    r"(?P<name>\"[A-Za-z0-9_]*(?:ApiKey|api_key|token|secret|password)[A-Za-z0-9_]*\")"
    r"(?P<sep>\s*:\s*)"
    r"(?P<quote>[\"'])"
    r"(?P<value>[^\"']{8,})"
    r"(?P=quote)",
    re.IGNORECASE,
)

#: Provider key shapes, so an unknown-but-obviously-secret token is still masked.
_SHAPE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}"),
    re.compile(r"\be2b_[A-Za-z0-9]{16,}"),
    re.compile(r"\bexa-[A-Za-z0-9]{16,}"),
    re.compile(r"\bBSA[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bjina_[A-Za-z0-9]{16,}"),
    re.compile(r"\bpplx-[A-Za-z0-9]{16,}"),
    re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(r"(?i)\bauthorization\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{20,}"),
)

#: Strings that look like opaque credentials in tool output (long base64/hex runs).
_OPAQUE_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{40,}\b")

#: Credential-shaped tokens whose *minimum length is not yet reached* must be
#: held at the tail of a delta stream: the shape patterns above need 16+ (or 20+)
#: trailing characters, so a token split before that boundary would be emitted
#: piecewise and never match again. Each entry mirrors one :data:`_SHAPE_PATTERNS`
#: rule with the tail length reduced to the below-threshold prefix.
_OPEN_TOKEN_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{0,15}$"),
    re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{0,15}$"),
    re.compile(r"\be2b_[A-Za-z0-9]{0,15}$"),
    re.compile(r"\bexa-[A-Za-z0-9]{0,15}$"),
    re.compile(r"\bBSA[A-Za-z0-9_\-]{0,15}$"),
    re.compile(r"\bjina_[A-Za-z0-9]{0,15}$"),
    re.compile(r"\bpplx-[A-Za-z0-9]{0,15}$"),
    re.compile(r"\bAIza[A-Za-z0-9_\-]{0,19}$"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{0,19}$"),
    re.compile(r"(?i)\bauthorization\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{0,19}$"),
)

#: An assignment / JSON credential field whose value is still incomplete: an
#: unquoted value that has not hit a terminator yet, or a quoted value whose
#: closing quote has not arrived. The value is held whole until the redactor
#: can mask it in one piece (a greedy match only works on the complete value).
_JSON_OR_ASSIGNMENT_NAME = (
    r"(?:\"[A-Za-z0-9_]*(?:ApiKey|api_key|token|secret|password)[A-Za-z0-9_]*\""
    r"|[A-Za-z0-9_]*(?:ApiKey|api_key|token|secret|password)[A-Za-z0-9_]*"
    r"|" + "|".join(_SENSITIVE_NAMES) + r")"
)
_OPEN_VALUE_PATTERN = re.compile(
    _JSON_OR_ASSIGNMENT_NAME
    + r"\s*[:=]\s*(?:[\"'][^\n\"']*|[^\s\"',}]*)$",
    re.IGNORECASE,
)

#: Literals a delta boundary can fall inside. A split that leaves only a prefix
#: of one of these at the buffer end is held until the next delta arrives.
_HOLD_LITERAL_PREFIXES = (
    "sk-", "sk_live_", "sk_test_", "e2b_", "exa-", "BSA", "jina_", "pplx-",
    "AIza", "Bearer ", "Authorization",
)
_HOLD_NAME_PREFIXES = tuple(_SENSITIVE_NAMES) + (
    "apiKey", "apikey", "api_key", "accessToken", "access_token",
    "token", "secret", "password",
)

#: Upper bound on how much a single delta stream may hold back. A value long
#: enough to reach this is redacted whole instead of buffered.
HOLD_CAP = 8192


class SecretRedactor:
    """Mask known secret values and credential-shaped text."""

    def __init__(self, values: Iterable[str] = ()) -> None:
        self._values: set[str] = set()
        for value in values:
            self.add(value)

    # -- construction ---------------------------------------------------------

    def add(self, value: str | None) -> None:
        if not isinstance(value, str):
            return
        text = value.strip()
        if len(text) >= MIN_SECRET_LENGTH:
            self._values.add(text)

    @property
    def known_values(self) -> frozenset[str]:
        return frozenset(self._values)

    def __bool__(self) -> bool:
        return bool(self._values)

    def add_from_file(self, path: Path) -> None:
        """Register every credential-shaped value inside a JSON config file."""
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        _add_json_secrets(self, document)

    # -- redaction ------------------------------------------------------------

    def redact(self, text: str) -> str:
        """Return ``text`` with every known or credential-shaped secret masked."""
        if not isinstance(text, str) or not text:
            return text
        result = text
        # 1) Exact values first: cheapest and catches arbitrary-shaped secrets.
        for value in sorted(self._values, key=len, reverse=True):
            if value in result:
                result = result.replace(value, REPLACEMENT)
        # 2) Assignment forms (env dumps, config files, headers).
        result = _ASSIGNMENT_RE.sub(
            lambda m: f"{m.group('name')}{m.group('sep')}{m.group('quote')}{REPLACEMENT}{m.group('quote')}",
            result,
        )
        result = _JSON_FIELD_RE.sub(
            lambda m: f"{m.group('name')}{m.group('sep')}{m.group('quote')}{REPLACEMENT}{m.group('quote')}",
            result,
        )
        # 3) Provider key shapes.
        for pattern in _SHAPE_PATTERNS:
            result = pattern.sub(REPLACEMENT, result)
        return result

    def redact_payload(self, payload: Any) -> Any:
        """Recursively redact every string in a JSON-like structure."""
        if isinstance(payload, str):
            return self.redact(payload)
        if isinstance(payload, Mapping):
            return {key: self.redact_payload(value) for key, value in payload.items()}
        if isinstance(payload, (list, tuple)):
            return [self.redact_payload(item) for item in payload]
        return payload

    def contains_secret(self, text: str) -> bool:
        """True when redaction would change ``text`` (used by the scanner)."""
        return isinstance(text, str) and self.redact(text) != text

    # -- streaming ------------------------------------------------------------

    def hold_length(self, buffer: str) -> int:
        """Length of the buffer tail that a future character could still make
        redactable, so a streaming caller must not emit it yet.

        Redaction is a *whole-value* operation: a credential split across two
        deltas matches neither half. This function names the boundaries — a
        proper prefix of a known value, a literal prefix such as ``sk-``, a
        shape token below its minimum length, an assignment whose value is
        incomplete, or a quoted value whose closing quote has not arrived — and
        returns the longest such suffix.
        """
        if not buffer:
            return 0
        hold = 0
        for value in self._values:
            limit = min(len(value) - 1, len(buffer))
            for length in range(limit, 0, -1):
                if buffer.endswith(value[:length]):
                    hold = max(hold, length)
                    break
        for literal in _HOLD_LITERAL_PREFIXES:
            limit = min(len(literal) - 1, len(buffer))
            for length in range(limit, 0, -1):
                if buffer.endswith(literal[:length]):
                    hold = max(hold, length)
                    break
        lowered = buffer.lower()
        for name in _HOLD_NAME_PREFIXES:
            needle = name.lower()
            # A *complete* name is held too: the separator/value may arrive in the
            # next delta, and ``OPENAI_API_KEY`` alone must not be emitted before
            # ``=value`` can be recognised as one assignment.
            limit = min(len(needle), len(lowered))
            for length in range(limit, 0, -1):
                if lowered.endswith(needle[:length]):
                    hold = max(hold, length)
                    break
        for pattern in _OPEN_TOKEN_PATTERNS:
            match = pattern.search(buffer)
            if match is not None:
                hold = max(hold, len(buffer) - match.start())
        match = _OPEN_VALUE_PATTERN.search(buffer)
        if match is not None:
            hold = max(hold, len(buffer) - match.start())
        return hold

    def stream(self) -> "StreamRedactor":
        """Create a stateful delta-stream view of this redactor."""
        return StreamRedactor(self)


#: Substrings that mark a value as documentation/example text, not a credential.
PLACEHOLDER_MARKERS = (
    "...", "your-", "your_", "<your", "xxx", "example", "changeme", "placeholder",
    "redacted", "dummy", "sample", "fake", "todo",
)


def is_placeholder_value(value: str) -> bool:
    """True when ``value`` looks like documentation rather than a live secret.

    Tool output constantly contains doc fragments such as ``sk-ant-...`` or
    ``your-key-here`` (the agent reads READMEs), so a scanner must not report
    those as leaks. Shape evidence is still surfaced via
    :func:`find_credential_shapes`.
    """
    lowered = value.lower()
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)


def find_credential_shapes(text: str) -> list[str]:
    """Return credential-shaped substrings for scanners (never logs values)."""
    if not isinstance(text, str) or not text:
        return []
    found: list[str] = []
    for pattern in (_ASSIGNMENT_RE, _JSON_FIELD_RE):
        found.extend(match.group("value") for match in pattern.finditer(text))
    for pattern in _SHAPE_PATTERNS:
        found.extend(match.group(0) for match in pattern.finditer(text))
    return found


def redactor_from_config(config: Any, runtime: Any = None) -> SecretRedactor:
    """Collect every credential this host can know about.

    Sources: the frozen configuration (model / sandbox / search keys from `.env`)
    and the rendered runtime `web-search.json` (the file pi-web-access reads, whose
    values the host never keeps in its own config).
    """
    redactor = SecretRedactor()
    for attribute in ("openai_api_key", "e2b_api_key", "web_search_key"):
        redactor.add(getattr(config, attribute, None))
    if runtime is not None:
        directory = getattr(runtime, "directory", None)
        if isinstance(directory, Path):
            redactor.add_from_file(directory / "web-search.json")
    return redactor


class StreamRedactor:
    """Redact a delta stream without letting a split credential through.

    Each stream kind (assistant text, thinking) keeps a bounded tail: text up to
    :meth:`SecretRedactor.hold_length` is withheld until the next delta, so the
    value is only emitted once the redactor can recognise it in one piece. The
    tail is flushed at stream boundaries (turn end, message end, run end) — a
    partial prefix is not a credential, and legitimate text that merely shares a
    prefix with one is delayed, never altered.

    This is echo protection for the product surfaces, not a sandbox: a kernel
    that exfiltrates a credential in an encoding this module does not recognise
    (base64, a split across separate tool outputs, an external network call) is
    outside what redaction promises. See ``docs/architecture.md`` §6.1.
    """

    def __init__(self, redactor: SecretRedactor) -> None:
        self._redactor = redactor
        self._pending: dict[str, str] = {}

    def push(self, kind: str, text: str) -> str:
        """Feed one delta; return the text that is now safe to emit."""
        if not isinstance(text, str) or not text:
            return ""
        buffer = self._pending.pop(kind, "") + text
        hold = self._redactor.hold_length(buffer)
        if hold > HOLD_CAP:
            # A single "value" this long is redacted whole: buffering it would
            # grow without bound and holding prefixes leaks nothing useful.
            return self._redactor.redact(buffer)
        if hold:
            self._pending[kind] = buffer[-hold:]
            buffer = buffer[:-hold]
        return self._redactor.redact(buffer) if buffer else ""

    def flush(self, kind: str) -> str:
        """Emit the withheld tail (at a stream boundary)."""
        tail = self._pending.pop(kind, "")
        return self._redactor.redact(tail) if tail else ""

    def pending_kinds(self) -> tuple[str, ...]:
        return tuple(kind for kind, tail in self._pending.items() if tail)


def _add_json_secrets(redactor: SecretRedactor, document: Any, key_hint: str = "") -> None:
    if isinstance(document, Mapping):
        for key, value in document.items():
            _add_json_secrets(redactor, value, f"{key_hint}{key}")
    elif isinstance(document, list):
        for item in document:
            _add_json_secrets(redactor, item, key_hint)
    elif isinstance(document, str):
        lowered = key_hint.lower()
        if any(hint in lowered for hint in ("key", "token", "secret", "password")):
            redactor.add(document)
