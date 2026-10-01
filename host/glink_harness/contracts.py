"""The run contract vocabulary shared by the kernel adapters and orchestration.

``Verdict`` is one run's terminal verdict and ``FollowupUnavailable`` is the
refusal an HTTP follow-up caller sees (its ``code``). Both are *product
contract* vocabulary, not kernel-adapter vocabulary: the families raise and
return them, the orchestration layer consumes them, and ``web_routes`` maps the
refusal code to its HTTP answer. Defining them here keeps that ownership
visible — the kernel adapter layer is not their definition point, so the
orchestration layer no longer forwards them through ``glink_harness.runs`` to reach
the public error surface.

This module is a leaf: it may not import the kernel or the orchestration layer
(the direction is enforced by ``tests/test_repository_boundaries.py``).
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


# One process owns the data root. Admission, state/cache mutation and resource
# destruction must share this same conversation-scoped exclusion. Never hold
# the manager's global admission lock while waiting for it. Entries disappear
# with their last user/waiter, so historical conversations do not leak locks.
_CONVERSATION_LOCKS: dict[str, tuple[threading.RLock, int]] = {}
_CONVERSATION_REGISTRY_LOCK = threading.Lock()


@contextmanager
def conversation_guard(directory: Path) -> Iterator[None]:
    key = os.path.abspath(directory)
    with _CONVERSATION_REGISTRY_LOCK:
        lock, users = _CONVERSATION_LOCKS.get(key, (threading.RLock(), 0))
        _CONVERSATION_LOCKS[key] = lock, users + 1
    try:
        with lock:
            yield
    finally:
        with _CONVERSATION_REGISTRY_LOCK:
            _, users = _CONVERSATION_LOCKS[key]
            if users == 1:
                del _CONVERSATION_LOCKS[key]
            else:
                _CONVERSATION_LOCKS[key] = lock, users - 1


class FollowupUnavailable(RuntimeError):
    """A parent run cannot safely supply a resumable session.

    ``code`` is the refusal an HTTP caller sees (``unknown_run``, ``stale_run``,
    ``run_busy``, ``session_unavailable``, ``report_unavailable``).
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Verdict:
    """The run's terminal verdict.

    产品终态词汇的定义点在编排层 ``DecideStage.run``：status 取 store 的
    FINISHED/FAILED/CANCELLED，stop_reason 取 abort/event_log_failed/
    execution_error/timeout/model_error/aborted/length/max_turn_requests/
    report_rejected/no_report。族方法只把 harness 自己的 stop 词汇译成这
    张表，不新增词汇。
    """

    status: str
    stop_reason: str | None
    error: str | None
