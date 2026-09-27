"""Run orchestration: admission, execution, finalize and the resource ledger.

This is the host's orchestration layer, not a kernel adapter. It owns the run
lifecycle (admission gates, the execution skeleton, the finalize ordering, the
verdict commit and the external-resource ledger) and therefore may depend on
host state — storage, tenancy, ownership, blueprints. The kernel adapters in
:mod:`pi_glink.kernel` stay below it and must not import this package or any
other host orchestration module (enforced by
``tests/test_repository_boundaries.py``).

``ResourceLedger`` stays importable from its module
(:mod:`pi_glink.runs.resource_ledger`); consumers use the manager's forwards.
"""

from __future__ import annotations

from .runner import (
    REPORT_FILENAME,
    REPORT_MAX_BYTES,
    BlueprintUnavailable,
    ClientFactory,
    EventLogError,
    FollowupUnavailable,
    ResourcesUnreleased,
    RunBusy,
    RunManager,
    RunQuotaExceeded,
    StopDecision,
    UnsupportedCommand,
    parse_blueprint_command,
)

__all__ = [
    "BlueprintUnavailable",
    "ClientFactory",
    "EventLogError",
    "FollowupUnavailable",
    "REPORT_FILENAME",
    "REPORT_MAX_BYTES",
    "ResourcesUnreleased",
    "RunBusy",
    "RunManager",
    "RunQuotaExceeded",
    "StopDecision",
    "UnsupportedCommand",
    "parse_blueprint_command",
]
