"""Size and count bounds of the run lifecycle.

Stage code reads these as module attributes at call time (``limits.X``), so a
test that narrows one bound (``monkeypatch.setattr(limits, ...)``) narrows it on
every path that honours it.
"""

from __future__ import annotations

#: Ceiling for artifacts the host reads back and serves (report, session files).
REPORT_MAX_BYTES = 32 * 1024 * 1024

#: Ceiling for one file touched by the workspace credential sweep.
SWEEP_MAX_BYTES = 2 * 1024 * 1024
#: Bound on how many files one sweep may visit (a runaway workspace must not
#: turn run teardown into an unbounded walk).
SWEEP_MAX_FILES = 2000

#: Bound on the failure detail one log line may carry. The terminal frame keeps
#: the full (redacted) text; the log line is truncated to the same order as the
#: ACP stderr tail it may embed.
LOG_ERROR_MAX_CHARS = 4000
