"""Redaction boundary for everything the host emits or serves.

The host necessarily holds credentials (model key, sandbox key, search key), and
pi's tools can surface them: a `bash` call can run `env`, a `read` call can open
the runtime config file, and the model may quote either back into its answer.
The event stream, the persisted event log, the report download and the browser are all
*product* surfaces, so a secret appearing there has already crossed the boundary
the objective defines: **密钥不进前端**.

Detection is one function, :meth:`SecretRedactor.spans`, that returns the
character ranges to mask. Three detectors feed it:

* **value detection** — every occurrence (overlapping ones included) of the
  literal secrets the host knows about (from `.env` and the rendered
  `web-search.json`);
* **assignment detection** — ``NAME=value`` / ``"fieldApiKey": "value"`` forms,
  masking the value;
* **shape detection** — provider key shapes and ``Bearer …`` tokens, so a copy of
  a credential the host never stored is still masked.

Overlapping ranges are merged and each merged range is replaced once, so a
whole-text redaction never leaves part of a matched secret behind.

Streaming text (assistant/thinking deltas) uses the same detection through
:class:`StreamRedactor`. Every detector also declares its *open* form — where a
match could still begin or be extended by later text — and the stream only
releases text before the earliest such position, never cutting through a
complete match. The released stream therefore equals the whole-text redaction
of everything released so far; see ``docs/architecture.md`` §3.3.

Nothing in this module ever logs a secret.
"""

from __future__ import annotations

import json
import re
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
_NAME_ALTERNATION = "|".join(_SENSITIVE_NAMES)
_ASSIGNMENT_VALUE = r"[^\s\"',}]"
_JSON_NAME = r"[A-Za-z0-9_]*(?:ApiKey|api_key|token|secret|password)[A-Za-z0-9_]*"


def _proper_prefixes(literal: str) -> str:
    """Regex alternation of every non-empty proper prefix of ``literal``."""
    return "|".join(re.escape(literal[:length]) for length in range(len(literal) - 1, 0, -1))


#: Assignment form: NAME=value / NAME: value / "NAME": "value" (config files).
_ASSIGNMENT_RE = re.compile(
    r"(?P<name>" + _NAME_ALTERNATION + r")"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<quote>[\"']?)"
    r"(?P<value>" + _ASSIGNMENT_VALUE + r"{8,})"
    r"(?P=quote)",
    re.IGNORECASE,
)

#: Generic JSON-ish credential fields (web-search.json uses e.g. exaApiKey).
_JSON_FIELD_RE = re.compile(
    r"(?P<name>\"" + _JSON_NAME + r"\")"
    r"(?P<sep>\s*:\s*)"
    r"(?P<quote>[\"'])"
    r"(?P<value>[^\"']{8,})"
    r"(?P=quote)",
    re.IGNORECASE,
)

#: Provider key shapes: (literal prefixes, tail character class, minimum tail).
_SHAPE_TOKENS = (
    (("sk-",), r"[A-Za-z0-9_\-]", 16),
    (("sk_live_", "sk_test_"), r"[A-Za-z0-9]", 16),
    (("e2b_",), r"[A-Za-z0-9]", 16),
    (("exa-",), r"[A-Za-z0-9]", 16),
    (("BSA",), r"[A-Za-z0-9_\-]", 16),
    (("jina_",), r"[A-Za-z0-9]", 16),
    (("pplx-",), r"[A-Za-z0-9]", 16),
    (("AIza",), r"[A-Za-z0-9_\-]", 20),
)
_HEADER_TOKEN = r"[A-Za-z0-9._\-]"

_SHAPE_PATTERNS = tuple(
    re.compile(
        r"\b(?:" + "|".join(re.escape(prefix) for prefix in prefixes) + r")"
        + tail + "{" + str(minimum) + ",}"
    )
    for prefixes, tail, minimum in _SHAPE_TOKENS
) + (
    re.compile(r"(?i)\bbearer\s+" + _HEADER_TOKEN + r"{20,}"),
    re.compile(r"(?i)\bauthorization\"?\s*[:=]\s*\"?" + _HEADER_TOKEN + r"{20,}"),
)

#: The *open* form of every detector: anchored at the end of the text, each
#: matches from the earliest position where more text could still complete or
#: extend a match. Keeping the two lists next to each other is what makes the
#: streaming view equal the whole-text view.
_OPEN_PATTERNS = tuple(
    re.compile(
        r"\b(?:"
        + "|".join(
            f"{_proper_prefixes(prefix)}|{re.escape(prefix)}{tail}*"
            for prefix in prefixes
        )
        + r")$"
    )
    for prefixes, tail, _minimum in _SHAPE_TOKENS
) + (
    re.compile(
        r"(?i)\b(?:" + _proper_prefixes("bearer")
        + r"|bearer(?:\s+" + _HEADER_TOKEN + r"*)?)$"
    ),
    re.compile(
        r"(?i)\b(?:" + _proper_prefixes("authorization")
        + r"|authorization\"?\s*(?:[:=]\s*\"?" + _HEADER_TOKEN + r"*)?)$"
    ),
    re.compile(
        r"(?i)(?:"
        + "|".join(_proper_prefixes(name) for name in _SENSITIVE_NAMES)
        + r"|(?:" + _NAME_ALTERNATION + r")\s*(?:[:=]\s*[\"']?" + _ASSIGNMENT_VALUE + r"*)?)$"
    ),
    re.compile(
        r"(?i)(?:\"[A-Za-z0-9_]*|\"" + _JSON_NAME + r"\"\s*(?::\s*(?:[\"'][^\"']*)?)?)$"
    ),
)

#: Upper bound on how much undecided text one delta stream may hold. Text that
#: is still undecided at this bound is never released: the stream fails closed
#: for the rest of its block (see :class:`StreamRedactor`).
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

    # -- detection ------------------------------------------------------------

    def spans(self, text: str) -> list[tuple[int, int]]:
        """Merged, ordered ``[start, end)`` ranges that redaction must mask."""
        found: list[tuple[int, int]] = []
        for value in self._values:
            start = text.find(value)
            while start != -1:
                found.append((start, start + len(value)))
                start = text.find(value, start + 1)
        for pattern in (_ASSIGNMENT_RE, _JSON_FIELD_RE):
            found.extend(match.span("value") for match in pattern.finditer(text))
        for pattern in _SHAPE_PATTERNS:
            found.extend(match.span() for match in pattern.finditer(text))
        found.sort()
        merged: list[tuple[int, int]] = []
        for start, end in found:
            if merged and start < merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    def open_start(self, text: str, start: int = 0) -> int:
        """Earliest index ``>= start`` where later text could still form a match.

        ``text[:start]`` is left context only (word boundaries); the result is
        ``len(text)`` when nothing at the end of ``text`` is still open.
        """
        earliest = len(text)
        for pattern in _OPEN_PATTERNS:
            match = pattern.search(text, start)
            if match is not None:
                earliest = min(earliest, match.start())
        tail = text[start:]
        for value in self._values:
            for length in range(min(len(value) - 1, len(tail)), 0, -1):
                if tail.endswith(value[:length]):
                    earliest = min(earliest, len(text) - length)
                    break
        return earliest

    # -- redaction ------------------------------------------------------------

    def redact(self, text: str) -> str:
        """Return ``text`` with every known or credential-shaped secret masked."""
        if not isinstance(text, str) or not text:
            return text
        return _render(text, self.spans(text), 0, len(text))

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

    def stream(self) -> "StreamRedactor":
        """Create a stateful delta-stream view of this redactor."""
        return StreamRedactor(self)


def _render(text: str, spans: list[tuple[int, int]], start: int, end: int) -> str:
    """``text[start:end]`` with the spans inside that window replaced."""
    pieces: list[str] = []
    position = start
    for span_start, span_end in spans:
        if span_end <= start or span_start >= end:
            continue
        span_start = max(span_start, start)
        pieces.append(text[position:span_start])
        pieces.append(REPLACEMENT)
        position = min(span_end, end)
    pieces.append(text[position:end])
    return "".join(pieces)


class StreamRedactor:
    """Redact a delta stream without letting a split credential through.

    Each stream kind (assistant text, thinking) keeps a pending tail. On every
    delta the detector runs over ``pending + delta`` and only the text before
    :meth:`SecretRedactor.open_start` is released — moved further back to the
    start of any complete match that the open position falls inside, so a
    match is never cut in two (a known secret whose suffix equals its own
    prefix is the R1 reproduction). The released text is rendered from the
    spans of the whole buffer; one character of released text is kept as left
    context so word-boundary rules see what the whole text would show.

    Pending text is flushed at stream boundaries (turn end, message end, run
    end): legitimate text that merely resembles a credential prefix is delayed,
    never altered.

    **The hold is bounded and fails closed.** At most :data:`HOLD_CAP`
    characters are ever pending. When the undecided tail would exceed it — a
    known value longer than the bound, an overlapping repeat chain, an
    unterminated assignment or token — the decided text before it is released,
    the undecided text is replaced by one ``[REDACTED]``, and the stream emits
    nothing more for that kind until the block's boundary. Undecided text is
    never released raw; the block's authoritative ``assistant_message`` still
    carries the whole-text-redacted answer.

    This is echo protection for the product surfaces, not a sandbox: a kernel
    that exfiltrates a credential in an encoding this module does not recognise
    (base64, a split across separate tool outputs, an external network call) is
    outside what redaction promises. See ``docs/architecture.md`` §6.1.
    """

    def __init__(self, redactor: SecretRedactor) -> None:
        self._redactor = redactor
        self._pending: dict[str, str] = {}
        self._context: dict[str, str] = {}
        #: Kinds whose current block failed closed at the hold bound.
        self._closed: set[str] = set()

    def push(self, kind: str, text: str) -> str:
        """Feed one delta; return the text that is now safe to emit."""
        if not isinstance(text, str) or not text or kind in self._closed:
            return ""
        left = self._context.get(kind, "")
        window = left + self._pending.pop(kind, "") + text
        offset = len(left)
        spans = self._redactor.spans(window)
        cut = self._redactor.open_start(window, offset)
        for start, end in reversed(spans):
            if start < cut < end:
                cut = start
                break
        cut = max(cut, offset)
        if len(window) - cut > HOLD_CAP:
            # Undecidable within the bound: never release it. Fail closed for
            # the rest of this block instead of growing without limit.
            self._pending.pop(kind, None)
            self._context.pop(kind, None)
            self._closed.add(kind)
            return _render(window, spans, offset, cut) + REPLACEMENT
        released = _render(window, spans, offset, cut)
        if cut > offset:
            self._context[kind] = window[cut - 1]
        if cut < len(window):
            self._pending[kind] = window[cut:]
        return released

    def flush(self, kind: str) -> str:
        """Emit the withheld tail (at a stream boundary) and reset the stream."""
        if kind in self._closed:
            self._closed.discard(kind)
            return ""
        left = self._context.pop(kind, "")
        tail = self._pending.pop(kind, "")
        if not tail:
            return ""
        window = left + tail
        return _render(window, self._redactor.spans(window), len(left), len(window))


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


def redactor_from_config(config: Any) -> SecretRedactor:
    """Collect every credential the host's frozen configuration carries.

    Sources: the model / sandbox / search keys from `.env`. No rendered file is
    read: since the egress proxy (docs/adr/0008) a pi run's files carry only
    its token, which the runner adds to that run's own boundary.
    """
    redactor = SecretRedactor()
    for attribute in ("openai_api_key", "e2b_api_key", "web_search_key"):
        redactor.add(getattr(config, attribute, None))
    return redactor


# -- credential files: values a copied credential document carries ----------------

#: Key-name hints for values that must join the redaction boundary even when a
#: scanner cannot recognise their shape (an opaque token, a short shared secret).
CREDENTIAL_NAME_RE = re.compile(r"(?i)(key|token|secret|password|credential|auth)")
#: OAuth credential stores (opencode's ``auth.json``) name their fields exactly
#: ``access``/``refresh``, without a ``token`` suffix the generic rule could see.
#: Matching the whole field name (never a substring) keeps ordinary keys such as
#: ``accessMode`` out of the redaction boundary.
_OAUTH_SECRET_FIELDS = frozenset({"access", "refresh"})
#: ``key: value`` / ``key = value`` scalar lines inside the credential file
#: (dsh's ``.credentials.yaml`` is YAML; the JSON branch handles the rest).
_SCALAR_LINE_RE = re.compile(r"^\s*(?P<name>[A-Za-z0-9_.\-]+)\s*:\s*(?P<value>.+?)\s*$")


def credential_secrets_from_text(text: str) -> tuple[str, ...]:
    """Credential values inside one credential file's text, in stable order.

    Three detectors run over the copied material:

    * **JSON** (codex/opencode ``auth.json``): values under a key whose name looks
      like a credential (``*_token``, ``*ApiKey``, ``*secret``, …) or is exactly
      ``access``/``refresh`` (opencode's OAuth store);
    * **scalar lines** (dsh ``.credentials.yaml``): ``key: value`` pairs with the
      same key-name rule;
    * **shape detection**: :func:`~glink_harness.redaction.find_credential_shapes`
      over the raw text, which finds provider-shaped values a key name alone
      would miss.

    Documentation placeholders (``your-key-here``) are skipped — they are not
    secrets, and masking them would only corrupt output. The result keeps the
    redactor's own minimum length rule.
    """
    found: list[str] = []
    _collect_json_values(_parsed_json(text), found)
    for line in text.splitlines():
        match = _SCALAR_LINE_RE.match(line)
        if match and _is_secret_field(match.group("name")):
            found.append(_unquote(match.group("value")))
    found.extend(find_credential_shapes(text))
    stable: list[str] = []
    for value in found:
        if not isinstance(value, str):
            continue
        text_value = value.strip()
        if len(text_value) < MIN_SECRET_LENGTH or is_placeholder_value(text_value):
            continue
        if text_value not in stable:
            stable.append(text_value)
    return tuple(stable)


def _is_secret_field(key_hint: str) -> bool:
    """Does a (dotted) key path name a credential field?

    The generic rule matches credential-ish substrings anywhere in the path
    (``openai.access_token``). The OAuth rule matches only the last path element
    exactly, so ``openai.access`` is a credential while ``openai.accessMode``
    is not.
    """
    if CREDENTIAL_NAME_RE.search(key_hint):
        return True
    return key_hint.rsplit(".", 1)[-1].strip().lower() in _OAUTH_SECRET_FIELDS


def _collect_json_values(document: object, found: list[str], key_hint: str = "") -> None:
    if isinstance(document, Mapping):
        for key, value in document.items():
            _collect_json_values(value, found, f"{key_hint}.{key}" if key_hint else str(key))
    elif isinstance(document, list):
        for item in document:
            _collect_json_values(item, found, key_hint)
    elif isinstance(document, str) and _is_secret_field(key_hint):
        found.append(document)


def _parsed_json(text: str) -> object:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _unquote(value: str) -> str:
    text_value = value.strip()
    if len(text_value) >= 2 and text_value[0] == text_value[-1] and text_value[0] in "\"'":
        return text_value[1:-1]
    return text_value
