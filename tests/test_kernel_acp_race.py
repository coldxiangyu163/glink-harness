"""The ACP crash race: the stream's end is not the agent's exit status.

A crash closes the protocol channel (stdout EOF) before the process exits and
is reapable, so anything that samples the exit status at the stream's end can
miss it -- the flake GLINK-ACP-07 fixes in the adapter's ``kernel_exit``
report. These cases pin the client-side contract the fix rests on: an exit
status exists only once the child has been reaped, and a status the teardown
itself caused is not the agent's own. The run-level regression that turns this
into a verdict is
``test_kernel_acp_runner.py::test_acp_crash_after_the_stream_ends_still_reports_the_exit_code``
(the fake agent's ``FAKE_ACP_CRASH_EXIT_DELAY`` widens the same window).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from glink_harness.kernel.acp_rpc import AcpClosed, AcpRpcClient


def _script(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def test_the_stream_ending_is_not_the_agents_exit_status(tmp_path: Path) -> None:
    """The channel can end while the agent is alive: no status exists yet.

    The child closes stdout (the client's reader sees EOF) and keeps running
    until the host closes its stdin, then exits 3 on its own -- the crash's
    event order with the window held open instead of raced.
    """
    ghost = _script(
        tmp_path,
        "ghost_agent.py",
        "\n".join(
            [
                "import os, sys",
                "for line in sys.stdin:",
                "    os.close(1)  # the protocol channel ends here",
                "    break",
                "for line in sys.stdin:  # still alive: no exit status exists",
                "    pass",
                "sys.exit(3)",
            ]
        ),
    )
    client = AcpRpcClient([sys.executable, str(ghost)])
    client.start()
    try:
        with pytest.raises(AcpClosed):
            client.request(
                "session/prompt",
                {"sessionId": "race", "prompt": []},
                timeout=None,
            )
        # The stream ended; the process has not. A status sampled here is the
        # race: the real crash closes the same channel microseconds before the
        # child is reapable.
        assert client.exit_code is None
        # The status comes from reaping the child, and it is the agent's own.
        assert client.close() == 3
        assert client.stopped_process is False
    finally:
        client.close()


def test_a_status_the_teardown_caused_is_not_the_agents_own(tmp_path: Path) -> None:
    """A child the teardown had to kill reports no status of its own.

    ``stopped_process`` is what separates "the agent exited" from "the host
    stopped it": without it a terminate/kill status would be attributed to the
    agent in the ``kernel_exit`` report.
    """
    stubborn = _script(
        tmp_path,
        "stubborn_agent.py",
        "import time\nwhile True:\n    time.sleep(0.2)\n",
    )
    client = AcpRpcClient([sys.executable, str(stubborn)])
    client.start()
    try:
        client.close(timeout=0.2)
        assert client.stopped_process is True
        assert client.exit_code is not None
    finally:
        client.close()
