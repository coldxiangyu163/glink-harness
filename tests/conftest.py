"""Suite-wide guard: every product frame a test produces must match the schema.

Strict mode (:data:`pi_glink.event_schema.STRICT_ENV`) makes the bus and the
projectors raise on a violating frame. A violation raised on a background
thread (a run's worker, a stream pump) can be swallowed by the code under test,
so each test also fails when the schema recorded any violation during it.
Tests that deliberately feed a violating frame clear the record themselves
(``event_schema.reset_violations()``) after asserting the rejection.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("PI_GLINK_EVENT_SCHEMA", "strict")

from pi_glink import event_schema  # noqa: E402


@pytest.fixture(autouse=True)
def _no_event_schema_violations():
    event_schema.reset_violations()
    yield
    recorded = event_schema.violations()
    event_schema.reset_violations()
    assert not recorded, f"测试期间出现违反事件 schema 的帧: {recorded}"
