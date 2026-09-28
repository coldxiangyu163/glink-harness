"""Admission refusals and lifecycle errors of the run orchestration layer.

These are the exceptions :mod:`pi_glink.runs` raises at its public surface
(``RunManager.start`` refusals, the event-log failure the commit path turns
into a hidden frame). They carry only reviewed vocabulary — allowlists, scope
names, run ids of this host — never the refused client input.
"""

from __future__ import annotations

from typing import Sequence


class ResourcesUnreleased(RuntimeError):
    """Raised when earlier runs still hold resources that are not confirmed released."""

    def __init__(self, run_ids: list[str]) -> None:
        super().__init__("执行资源尚未确认释放: " + ", ".join(run_ids))
        self.run_ids = list(run_ids)


class RunQuotaExceeded(RuntimeError):
    """Raised when admitting a run would exceed a configured concurrency ceiling.

    ``scope`` names which ceiling was hit (``global`` or ``tenant``). The HTTP
    layer answers a 429 with that scope and limit, never with another tenant's
    run id: an over-quota refusal must not disclose who else is running.
    """

    def __init__(self, scope: str, limit: int) -> None:
        super().__init__(f"{scope} 并发上限 {limit} 已达")
        self.scope = scope
        self.limit = limit


class UnsupportedCommand(RuntimeError):
    """The prompt opens with a ``/`` command the host does not accept.

    Since GLINK-ARCH-6 the host itself interprets ``/<blueprint> <topic>`` as
    ``blueprint=<name>`` + ``prompt=<topic>``; every other ``/``-prefixed input is
    refused. The gate exists because pi dispatches extension commands and expands
    prompt templates for a message that starts with ``/`` (docs/rpc-commands.md):
    once pi-subagents is loaded, commands such as ``/run --bg`` or
    ``/subagents-detach`` would start a detached run that no longer follows this
    run's cancel/timeout/reclaim path, and the entry is client-controlled input.
    The rewritten prompt must never carry the slash into the kernel, so a topic
    that starts with ``/`` is refused too.

    ``allowed`` carries only the accepted blueprint names (reviewed repository
    content); the refused command itself is never echoed back to the caller (the
    HTTP layer reports the allowlist at most), so the refusal does not turn the
    endpoint into a command oracle.
    """

    def __init__(self, allowed: Sequence[str]) -> None:
        self.allowed = tuple(sorted(allowed))
        super().__init__("prompt names an unsupported / command")


class BlueprintUnavailable(RuntimeError):
    """The requested session blueprint cannot be used for this run.

    ``code`` is ``unknown_blueprint`` (no such template in the blueprint
    directory) or ``invalid_blueprint`` (present but not a usable document).
    ``available`` is the derived name list, which the HTTP layer may report back
    — it is reviewed repository content, not client input. The refused value
    itself is never echoed, so the endpoint is not an oracle for arbitrary
    strings.
    """

    def __init__(self, code: str, available: Sequence[str]) -> None:
        self.code = code
        self.available = tuple(sorted(available))
        super().__init__("session blueprint is unavailable")


class EventLogError(RuntimeError):
    """The durable event log could not be written; the frame must stay hidden."""
