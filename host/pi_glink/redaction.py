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
