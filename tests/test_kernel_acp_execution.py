"""Direct unit tests for the ACP execution adapters (``kernel/acp_execution.py``).

The runner-level ACP regressions (``test_kernel_acp_runner.py`` /
``test_kernel_acp_resume.py`` / ``test_kernel_acp_e2b.py``) exercise these
adapters through ``RunManager`` with a real child process or a fake SDK. The
tests here construct :class:`AcpExecutionAdapter` and
:class:`AcpSandboxExecutionAdapter` directly against a scripted in-process
``AcpRpcClient`` stand-in — no subprocess, no store, no HTTP — and pin the
adapter contract itself:

* the lifecycle call order (start → session_info → deliver_input → wait →
  recover_artifacts → close) and the turn-boundary frames;
* the outcome seal: once ``wait`` has decided, late driver writes (settle,
  cancel, failure, kernel_exit) are refused;
* cancel/timeout/deadline semantics, including the difference between a
  host-initiated cancel and a timeout's teardown ``session/cancel``;
* ``kernel_exit`` attribution: the reaped exit status completes the report in
  ``close()`` only while the frozen decision is that report;
* the ``CleanupReport`` released/failures shape for process, container and
  sandbox teardown.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Mapping

import pytest

from pi_glink.config import build_config
from pi_glink.kernel.acp_e2b_env import AcpStateRecovery
from pi_glink.kernel.acp_env import AcpRuntime
from pi_glink.kernel.acp_execution import (
    AcpExecutionAdapter,
    AcpSandboxExecutionAdapter,
    compose_prompt,
)
from pi_glink.kernel.acp_rpc import (
    AcpClosed,
    AcpError,
    AcpMethodNotFound,
    AcpRequestError,
)
from pi_glink.kernel.e2b_env import SANDBOX_WORKSPACE
from pi_glink.kernel.execution import ExecutionSpec

REPO_ROOT = Path(__file__).resolve().parents[1]


# -- scripted client ------------------------------------------------------------


class ScriptedAcpClient:
    """An in-process ``AcpRpcClient`` stand-in: recorded calls, scripted answers.

    ``script`` keys:

    ``capabilities``        initialize's ``agentCapabilities`` (default {})
    ``session_id``          ``session/new``'s answer (default ``sess-1``)
    ``initialize_error``    raise ``AcpRequestError`` from ``initialize``
    ``prompt``              ``session/prompt`` behaviour: a stop reason string,
                            ``"closed"`` (raise ``AcpClosed``), ``"rejected"``
                            (raise ``AcpRequestError``) or ``"answer_on_cancel"``
                            (block until ``session/cancel`` then answer
                            ``cancelled``)
    ``prompt_gate``         an Event the prompt answer blocks on first
    ``cancel_seen``         an Event set when ``session/cancel`` arrives
    ``exit_code``           what ``close()`` returns (default 0)
    ``stopped_process``     the client's ``stopped_process`` flag
    ``close_raises``        ``close()`` raises instead of returning a status
    """

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        on_notification=None,
        on_request=None,
        spawn=None,
        script: Mapping[str, Any] | None = None,
        log: list[str] | None = None,
    ) -> None:
        self.argv = list(argv)
        self.cwd = cwd
        self.env = dict(env or {})
        self.on_notification = on_notification
        self.on_request = on_request
        self.spawn = spawn
        self.script = dict(script or {})
        self.log = log if log is not None else []
        self.started = False
        self.closed_count = 0
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.notifications: list[tuple[str, dict[str, Any]]] = []
        self.stopped_process = bool(self.script.get("stopped_process", False))

    def start(self) -> None:
        self.log.append("client.start")
        self.started = True

    def request(self, method: str, params: Mapping[str, Any] | None = None, timeout=None) -> dict:
        self.log.append(f"request:{method}")
        self.requests.append((method, dict(params or {})))
        if method == "initialize":
            error = self.script.get("initialize_error")
            if error:
                raise AcpRequestError("initialize", {"code": -32000, "message": str(error)})
            return {
                "protocolVersion": 1,
                "agentInfo": {"name": "scripted-acp", "version": "0.0.1"},
                "agentCapabilities": dict(self.script.get("capabilities", {})),
                "authMethods": [],
            }
        if method == "session/new":
            return {"sessionId": str(self.script.get("session_id", "sess-1"))}
        if method in ("session/resume", "session/load"):
            return {}
        if method == "session/prompt":
            return self._answer_prompt()
        raise AssertionError(f"unexpected request {method}")

    def _answer_prompt(self) -> dict:
        gate = self.script.get("prompt_gate")
        if gate is not None:
            assert gate.wait(timeout=10), "the test never released the prompt gate"
        behaviour = self.script.get("prompt", "end_turn")
        if behaviour == "closed":
            raise AcpClosed("stream ended before the prompt answer")
        if behaviour == "rejected":
            raise AcpRequestError("session/prompt", {"code": -32000, "message": "refused"})
        if behaviour == "answer_on_cancel":
            assert self.script["cancel_seen"].wait(timeout=10), "session/cancel never arrived"
            return {"stopReason": "cancelled"}
        return {"stopReason": str(behaviour)}

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        self.log.append(f"notify:{method}")
        self.notifications.append((method, dict(params or {})))
        seen = self.script.get("cancel_seen")
        if method == "session/cancel" and seen is not None:
            seen.set()

    def close(self, *, timeout: float = 5.0) -> int | None:
        self.log.append("client.close")
        self.closed_count += 1
        if self.script.get("close_raises"):
            raise RuntimeError("close boom")
        return self.script.get("exit_code", 0)


class FakeSandboxClient:
    """An ``E2BAcpSandboxClient`` stand-in for the sandbox face's own work."""

    def __init__(self, log: list[str], *, sandbox_id: str = "sbx-1") -> None:
        self.argv = ["agent", "--acp"]
        self.template = "glink-acp-worker"
        self.sandbox_id = sandbox_id
        self.log = log
        self.start_timeouts: list[int | None] = []
        self.workspace_files: list[Path] = []
        self.workspace_failures: list[str] = []
        self.state = AcpStateRecovery(
            ok=True, files=("home/.agent/state.json",), secrets=("state-secret",)
        )
        self.close_failures: list[str] = []

    def start(self, *, timeout_seconds: int | None = None) -> None:
        self.log.append("sandbox.start")
        self.start_timeouts.append(timeout_seconds)

    def upload_home(self) -> None:
        self.log.append("sandbox.upload_home")

    def upload_continuation(self) -> None:
        self.log.append("sandbox.upload_continuation")

    def spawn(self) -> object:
        self.log.append("sandbox.spawn")
        return object()

    def recover_artifacts(self, destination: Path) -> tuple[list[Path], list[str]]:
        self.log.append("sandbox.recover_artifacts")
        return (list(self.workspace_files), list(self.workspace_failures))

    def recover_state(self) -> AcpStateRecovery:
        self.log.append("sandbox.recover_state")
        return self.state

    def close(self) -> list[str]:
        self.log.append("sandbox.close")
        return list(self.close_failures)


# -- helpers --------------------------------------------------------------------


def make_config(tmp_path: Path, **overrides: str):
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent-web-search.json"),
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


def make_spec(config, tmp_path: Path, **overrides: Any) -> ExecutionSpec:
    runtime = AcpRuntime(
        directory=tmp_path / "acp-agent",
        home=tmp_path / "acp-agent" / "home",
        command="agent --acp",
    )
    values: dict[str, Any] = {
        "config": config,
        "runtime": runtime,
        "run_id": "unit-run",
        "prompt": "用户问题",
        "system_prompt": "工作约定",
        "workspace": tmp_path / "ws",
        "timeout_seconds": 30,
    }
    values.update(overrides)
    return ExecutionSpec(**values)


def make_adapter(
    tmp_path: Path,
    *,
    script: Mapping[str, Any] | None = None,
    config_overrides: Mapping[str, str] | None = None,
    spec_overrides: Mapping[str, Any] | None = None,
    log: list[str] | None = None,
    **adapter_kwargs: Any,
):
    """An adapter over a scripted client; returns (adapter, created, log, frames)."""
    config = make_config(tmp_path, **dict(config_overrides or {}))
    spec = make_spec(config, tmp_path, **dict(spec_overrides or {}))
    shared_log = log if log is not None else []
    frames: list[Mapping[str, Any]] = []
    created: list[ScriptedAcpClient] = []
    script_dict = dict(script or {})

    def factory(argv, **kwargs):
        client = ScriptedAcpClient(argv, script=script_dict, log=shared_log, **kwargs)
        created.append(client)
        return client

    adapter = AcpExecutionAdapter(
        spec,
        argv=["agent", "--acp"],
        env={"A": "1"},
        cwd=tmp_path,
        client_factory=factory,
        on_frame=frames.append,
        **adapter_kwargs,
    )
    return adapter, created, shared_log, frames


def make_sandbox_adapter(
    tmp_path: Path,
    *,
    script: Mapping[str, Any] | None = None,
    spec_overrides: Mapping[str, Any] | None = None,
):
    config = make_config(tmp_path)
    spec = make_spec(config, tmp_path, **dict(spec_overrides or {}))
    log: list[str] = []
    frames: list[Mapping[str, Any]] = []
    created: list[ScriptedAcpClient] = []
    identities: list[Mapping[str, str]] = []
    sandbox = FakeSandboxClient(log)
    script_dict = dict(script or {})

    def factory(argv, **kwargs):
        client = ScriptedAcpClient(argv, script=script_dict, log=log, **kwargs)
        created.append(client)
        return client

    def on_identity(identity: Mapping[str, str]) -> None:
        log.append("identity")
        identities.append(dict(identity))

    adapter = AcpSandboxExecutionAdapter(
        spec,
        sandbox=sandbox,
        client_factory=factory,
        on_frame=frames.append,
        on_identity=on_identity,
    )
    return adapter, sandbox, created, log, frames, identities


def wait_for(predicate, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def join_driver(adapter: AcpExecutionAdapter) -> None:
    driver = adapter._driver
    if driver is not None:
        driver.join(timeout=10)
        assert not driver.is_alive()


# -- lifecycle order and turn frames ---------------------------------------------


def test_the_adapter_contract_order_and_turn_frames(tmp_path: Path) -> None:
    adapter, created, log, frames = make_adapter(tmp_path)

    adapter.start()
    info = adapter.session_info()
    adapter.deliver_input()
    outcome = adapter.wait(cancel=lambda: False, deadline=time.monotonic() + 30)
    join_driver(adapter)
    recovery = adapter.recover_artifacts(tmp_path / "ws")
    report = adapter.close()

    client = created[0]
    # The factory received the frozen argv/env/cwd and the frame callbacks.
    assert client.argv == ["agent", "--acp"]
    assert client.env == {"A": "1"}
    assert client.cwd == tmp_path
    assert client.spawn is None
    # Protocol order: initialize → session/new → (driver) session/prompt.
    assert [entry for entry in log if entry.startswith("request:")] == [
        "request:initialize",
        "request:session/new",
        "request:session/prompt",
    ]
    assert log[0] == "client.start" and log[-1] == "client.close"
    # session/new sees the agent-visible workspace; the prompt carries the
    # composed working agreement plus the user text in one text block.
    requests = dict()
    for method, params in client.requests:
        requests.setdefault(method, params)
    assert requests["session/new"]["cwd"] == str(tmp_path / "ws")
    assert requests["session/new"]["mcpServers"] == []
    prompt = requests["session/prompt"]
    assert prompt["sessionId"] == "sess-1"
    assert prompt["prompt"] == [
        {"type": "text", "text": compose_prompt("工作约定", "用户问题")}
    ]
    # The turn boundary frames bracket the result and precede the verdict.
    assert [frame["type"] for frame in frames] == [
        "acp_turn_started",
        "acp_prompt_result",
        "acp_turn_finished",
    ]
    assert frames[1]["stopReason"] == "end_turn"
    assert outcome.settled is True
    assert (outcome.cancelled, outcome.timed_out, outcome.error) == (False, False, None)
    # close() records the reaped status even on a settled run.
    assert outcome.exit_code == 0
    # In-process face: recovery is a no-op, there is no durable identity.
    assert recovery.files == () and recovery.failures == ()
    assert recovery.state_recovered is None and recovery.secrets == ()
    assert adapter.identity() == {}
    assert report.released is True and report.failures == () and report.external == {}
    assert info is not None and info.session_id == "sess-1"


def test_session_info_records_the_negotiated_session(tmp_path: Path) -> None:
    capabilities = {"loadSession": False, "sessionCapabilities": {"resume": {}}}
    adapter, _created, _log, _frames = make_adapter(
        tmp_path,
        script={"capabilities": capabilities, "session_id": "sess-9"},
        session_cwd="/workspace",
    )
    adapter.start()
    info = adapter.session_info()
    assert info is not None
    assert info.session_id == "sess-9"
    assert info.session_file is None
    payload = info.payload
    assert payload["harness"] == "acp"
    assert payload["protocolVersion"] == 1
    assert payload["sessionId"] == "sess-9"
    assert payload["agentInfo"] == {"name": "scripted-acp", "version": "0.0.1"}
    assert payload["agentCapabilities"] == capabilities
    assert payload["authMethods"] == []
    # The ACP cwd is the agent-visible workspace (the container mount point on
    # the Docker face), never silently the host path.
    assert payload["cwd"] == "/workspace"
    # A first turn does not masquerade as a continuation.
    assert "resumed" not in payload and "resumeMethod" not in payload
    adapter.close()


def test_compose_prompt_fences_the_working_agreement() -> None:
    assert compose_prompt("", "问题") == "问题"
    composed = compose_prompt(" 约定 ", "问题")
    assert "<system-prompt>\n约定\n</system-prompt>" in composed
    assert composed.endswith("--- 用户请求 ---\n\n问题")


# -- the outcome seal ------------------------------------------------------------


def test_the_sealed_outcome_refuses_late_driver_writes(tmp_path: Path) -> None:
    adapter, _created, _log, _frames = make_adapter(tmp_path)
    adapter.start()
    adapter.deliver_input()
    outcome = adapter.wait(cancel=lambda: False, deadline=time.monotonic() + 30)
    join_driver(adapter)
    assert outcome.settled is True

    adapter._record_cancel()
    adapter._fail("execution_error", "late failure")
    adapter._record_kernel_exit("late stream loss")
    adapter._record_settled()

    assert outcome.settled is True
    assert (outcome.cancelled, outcome.timed_out) == (False, False)
    assert outcome.stop_reason is None and outcome.error is None
    adapter.close()


def test_a_timeout_decision_survives_a_late_settle(tmp_path: Path) -> None:
    gate = threading.Event()
    adapter, created, _log, _frames = make_adapter(
        tmp_path, script={"prompt_gate": gate, "prompt": "end_turn"}
    )
    adapter.start()
    adapter.deliver_input()

    outcome = adapter.wait(cancel=lambda: False, deadline=time.monotonic() - 1)

    assert outcome.timed_out is True and outcome.stop_reason == "timeout"
    assert "run did not settle within 30s" in (outcome.error or "")
    # The deadline's teardown is a session/cancel request, not a host cancel:
    # the host-cancellation flag must stay unset.
    assert adapter._cancelled.is_set() is False
    client = created[0]
    assert client.notifications == [("session/cancel", {"sessionId": "sess-1"})]

    # The driver unblocks after the verdict and its late settle is refused.
    gate.set()
    join_driver(adapter)
    assert outcome.timed_out is True and outcome.settled is False
    assert outcome.stop_reason == "timeout"
    adapter.close()


def test_a_cancelled_answer_to_timeout_teardown_stays_a_timeout(tmp_path: Path) -> None:
    adapter, _created, _log, _frames = make_adapter(
        tmp_path,
        script={"prompt": "answer_on_cancel", "cancel_seen": threading.Event()},
    )
    adapter.start()
    adapter.deliver_input()

    outcome = adapter.wait(cancel=lambda: False, deadline=time.monotonic() - 1)
    join_driver(adapter)

    # The agent answered the teardown cancel with stopReason=cancelled; the
    # deadline verdict was already decided and must not flip to a user cancel.
    assert outcome.timed_out is True and outcome.stop_reason == "timeout"
    assert outcome.cancelled is False and outcome.settled is False
    adapter.close()


# -- cancellation ------------------------------------------------------------------


def test_host_cancellation_decides_abort_and_notifies_once(tmp_path: Path) -> None:
    gate = threading.Event()
    adapter, created, _log, _frames = make_adapter(
        tmp_path, script={"prompt_gate": gate}
    )
    adapter.start()
    adapter.deliver_input()

    outcome = adapter.wait(cancel=lambda: True, deadline=time.monotonic() + 30)

    assert outcome.cancelled is True and outcome.stop_reason == "abort"
    assert outcome.timed_out is False and outcome.settled is False
    # cancel() is idempotent: the wait loop's own cancel plus an explicit one
    # still produce exactly one session/cancel notification.
    adapter.cancel()
    client = created[0]
    assert client.notifications == [("session/cancel", {"sessionId": "sess-1"})]

    gate.set()
    join_driver(adapter)
    assert outcome.cancelled is True and outcome.stop_reason == "abort"
    adapter.close()


def test_cancel_before_delivery_never_starts_the_turn(tmp_path: Path) -> None:
    adapter, _created, log, _frames = make_adapter(tmp_path)
    adapter.start()
    adapter.cancel()
    adapter.deliver_input()

    assert adapter._driver is None, "a cancelled run must not start the driver"
    assert "request:session/prompt" not in log

    outcome = adapter.wait(cancel=lambda: True, deadline=time.monotonic() + 30)
    assert outcome.cancelled is True and outcome.stop_reason == "abort"
    adapter.close()


def test_an_agent_initiated_cancel_settles_as_abort(tmp_path: Path) -> None:
    adapter, created, _log, frames = make_adapter(tmp_path, script={"prompt": "cancelled"})
    adapter.start()
    adapter.deliver_input()
    outcome = adapter.wait(cancel=lambda: False, deadline=time.monotonic() + 30)
    join_driver(adapter)

    assert outcome.cancelled is True and outcome.stop_reason == "abort"
    assert outcome.timed_out is False
    # The agent stopped the turn itself; no session/cancel left the host.
    assert created[0].notifications == []
    assert frames[1] == {"type": "acp_prompt_result", "stopReason": "cancelled"}
    adapter.close()


# -- kernel_exit attribution -------------------------------------------------------


def test_kernel_exit_is_completed_from_the_reaped_status(tmp_path: Path) -> None:
    adapter, _created, _log, _frames = make_adapter(
        tmp_path, script={"prompt": "closed", "exit_code": 3}
    )
    adapter.start()
    adapter.deliver_input()
    outcome = adapter.wait(cancel=lambda: False, deadline=time.monotonic() + 30)
    join_driver(adapter)

    # The stream ended before the answer; the exit status does not exist yet.
    assert outcome.stop_reason == "kernel_exit"
    assert outcome.settled is False and outcome.cancelled is False
    assert "退出/失联" in (outcome.error or "")
    assert "exit code" not in (outcome.error or "")
    assert outcome.exit_code is None

    report = adapter.close()
    # close() reaped the child: the pending report gets the agent's own status.
    assert outcome.exit_code == 3
    assert "exit code 3" in (outcome.error or "")
    assert report.released is True


def test_a_host_stopped_process_status_is_not_attributed_to_the_agent(
    tmp_path: Path,
) -> None:
    adapter, _created, _log, _frames = make_adapter(
        tmp_path,
        script={"prompt": "closed", "exit_code": 9, "stopped_process": True},
    )
    adapter.start()
    adapter.deliver_input()
    outcome = adapter.wait(cancel=lambda: False, deadline=time.monotonic() + 30)
    join_driver(adapter)
    adapter.close()

    # The status was produced by the host's own terminate/kill: it is recorded
    # as a fact but never attributed to the agent in the report text.
    assert outcome.exit_code == 9
    assert outcome.stop_reason == "kernel_exit"
    assert "exit code" not in (outcome.error or "")


def test_kernel_exit_completion_never_rewrites_a_decided_cancellation(
    tmp_path: Path,
) -> None:
    adapter, _created, _log, _frames = make_adapter(
        tmp_path, script={"prompt": "closed", "exit_code": 7}
    )
    adapter.start()
    adapter.deliver_input()
    # The driver's stream-loss report lands first; the host's cancel then
    # overrides it (an accepted cancellation stays the verdict).
    assert wait_for(adapter._done.is_set)
    outcome = adapter.wait(cancel=lambda: True, deadline=time.monotonic() + 30)
    join_driver(adapter)

    assert outcome.cancelled is True and outcome.stop_reason == "abort"
    adapter.close()
    # The reaped status is recorded as a fact, but the cancelled verdict is not
    # rewritten into a kernel_exit and gains no agent exit-code attribution.
    assert outcome.exit_code == 7
    assert outcome.cancelled is True and outcome.stop_reason == "abort"
    assert "exit code" not in (outcome.error or "")


# -- start failure ------------------------------------------------------------------


def test_a_failed_start_closes_the_client_and_stays_closed(tmp_path: Path) -> None:
    adapter, created, _log, _frames = make_adapter(
        tmp_path, script={"initialize_error": "authentication required"}
    )
    with pytest.raises(AcpRequestError):
        adapter.start()

    client = created[0]
    assert client.closed_count == 1, "nothing may outlive a failed start"
    report = adapter.close()
    assert client.closed_count == 1, "close() must not double-close the client"
    assert report.released is True and report.failures == ()
    with pytest.raises(AcpError):
        adapter.deliver_input()


def test_deliver_input_before_start_fails_closed(tmp_path: Path) -> None:
    adapter, _created, _log, _frames = make_adapter(tmp_path)
    with pytest.raises(AcpError):
        adapter.deliver_input()


# -- cleanup report -------------------------------------------------------------------


def test_close_reports_client_and_container_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[str] = []
    adapter, _created, _log, _frames = make_adapter(
        tmp_path,
        script={"close_raises": True},
        container_name="ctr-9",
        container_stop=lambda name: stopped.append(name) or ["docker", "rm", "-f", name],
    )
    monkeypatch.setattr(
        "pi_glink.kernel.acp_execution.subprocess.run",
        lambda *args, **kwargs: type(
            "R", (), {"returncode": 1, "stderr": "permission denied"}
        )(),
    )
    adapter.start()
    report = adapter.close()

    assert stopped == ["ctr-9"], "teardown addresses the persisted name only"
    assert report.released is False
    assert len(report.failures) == 2
    assert "关闭 ACP agent 进程失败" in report.failures[0]
    assert "删除容器失败" in report.failures[1]
    assert "permission denied" in report.failures[1]
    # The external identity survives a failed teardown for reconciliation.
    assert report.external == {"container": "ctr-9"}
    assert adapter.identity() == {"container": "ctr-9"}


def test_a_container_already_gone_is_not_a_teardown_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, _created, _log, _frames = make_adapter(
        tmp_path,
        container_name="ctr-1",
        container_stop=lambda name: ["docker", "rm", "-f", name],
    )
    monkeypatch.setattr(
        "pi_glink.kernel.acp_execution.subprocess.run",
        lambda *args, **kwargs: type(
            "R", (), {"returncode": 1, "stderr": "Error: No such container: ctr-1"}
        )(),
    )
    adapter.start()
    report = adapter.close()
    assert report.released is True and report.failures == ()
    assert report.external == {"container": "ctr-1"}


# -- agent -> client requests ---------------------------------------------------------


def _permission_params(options: list[Mapping[str, Any]], **tool_call: Any) -> dict:
    call = {"toolCallId": "call_1", "title": "运行 ls -la"}
    call.update(tool_call)
    return {"sessionId": "sess-1", "toolCall": call, "options": options}


def test_permission_requests_fail_closed_without_an_allowlist(tmp_path: Path) -> None:
    adapter, _created, _log, frames = make_adapter(tmp_path)
    options = [
        {"optionId": "allow-once", "name": "Allow", "kind": "allow_once"},
        {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
    ]
    answer = adapter._on_request(
        "session/request_permission", _permission_params(options, kind="execute")
    )
    assert answer == {"outcome": {"outcome": "selected", "optionId": "reject-once"}}
    notice = frames[-1]
    assert notice["type"] == "acp_permission"
    assert notice["decision"] == "rejected" and notice["policy"] == "fail_closed"
    assert notice["optionKind"] == "reject_once"
    assert notice["toolKind"] == "execute" and notice["toolKindSource"] == "request"


def test_a_permission_request_without_a_reject_option_is_cancelled(
    tmp_path: Path,
) -> None:
    adapter, _created, _log, frames = make_adapter(tmp_path)
    options = [{"optionId": "allow-once", "name": "Allow", "kind": "allow_once"}]
    answer = adapter._on_request("session/request_permission", _permission_params(options))
    # An unattended host must not allow the tool when no rejection was offered.
    assert answer == {"outcome": {"outcome": "cancelled"}}
    assert frames[-1]["decision"] == "cancelled"
    assert frames[-1]["policy"] == "fail_closed"


def test_a_permission_request_on_a_cancelled_run_is_cancelled(tmp_path: Path) -> None:
    adapter, _created, _log, frames = make_adapter(tmp_path)
    adapter.cancel()
    options = [{"optionId": "reject-once", "name": "Reject", "kind": "reject_once"}]
    answer = adapter._on_request("session/request_permission", _permission_params(options))
    assert answer == {"outcome": {"outcome": "cancelled"}}
    assert frames[-1]["decision"] == "cancelled"


def test_unknown_agent_requests_are_refused_fail_closed(tmp_path: Path) -> None:
    adapter, _created, _log, _frames = make_adapter(tmp_path)
    with pytest.raises(AcpMethodNotFound):
        adapter._on_request("fs/read_text_file", {"path": "/etc/passwd"})


def test_the_allowlist_prefers_the_narrowest_allow_option(tmp_path: Path) -> None:
    adapter, _created, _log, frames = make_adapter(
        tmp_path,
        config_overrides={
            "PI_GLINK_HARNESS": "acp",
            "PI_GLINK_ACP_COMMAND": "agent --acp",
            "PI_GLINK_ACP_PERMISSION_ALLOW": "allow_once,allow_always",
        },
    )
    # The agent lists the durable allow first; the selection must still be
    # allow_once (ACP_PERMISSION_OPTION_KINDS order), not the agent's order.
    options = [
        {"optionId": "allow-all", "name": "Always", "kind": "allow_always"},
        {"optionId": "allow-1", "name": "Once", "kind": "allow_once"},
    ]
    answer = adapter._on_request(
        "session/request_permission", _permission_params(options, kind="execute")
    )
    assert answer == {"outcome": {"outcome": "selected", "optionId": "allow-1"}}
    notice = frames[-1]
    assert notice["decision"] == "allowed" and notice["policy"] == "allowlist"
    assert notice["optionKind"] == "allow_once"


def test_tool_kinds_are_remembered_and_never_relabelled(tmp_path: Path) -> None:
    adapter, _created, _log, frames = make_adapter(tmp_path)
    published = {
        "method": "session/update",
        "params": {
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "t1",
                "kind": "read",
                "status": "pending",
            }
        },
    }
    adapter._on_notification(published)
    # The initial tool_call is the classification; a later update must not
    # re-label the same toolCallId.
    adapter._on_notification(
        {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "t1",
                    "kind": "execute",
                    "status": "completed",
                }
            },
        }
    )
    assert adapter._tool_kinds == {"t1": "read"}
    # Notifications are forwarded to the run's projection untouched.
    assert [frame["params"]["update"]["sessionUpdate"] for frame in frames] == [
        "tool_call",
        "tool_call_update",
    ]


# -- the sandbox face --------------------------------------------------------------------


def test_sandbox_start_orders_creation_identity_upload_and_protocol(
    tmp_path: Path,
) -> None:
    adapter, sandbox, created, log, _frames, identities = make_sandbox_adapter(tmp_path)
    adapter.start()

    # The sandbox exists (and is named to the host) before any step that can
    # fail; the run HOME is uploaded before the agent process starts.
    assert log[:4] == [
        "sandbox.start",
        "identity",
        "sandbox.upload_home",
        "client.start",
    ]
    assert identities == [{"sandbox": "sbx-1"}]
    # The sandbox outlives the run's deadline: task timeout plus the margin.
    assert sandbox.start_timeouts == [330]
    assert log[4:6] == ["request:initialize", "request:session/new"]
    # The client drives the sandbox background command, never a host process.
    client = created[0]
    assert client.spawn is not None
    assert client.env == {} and client.cwd is None
    client.spawn()
    assert log[-1] == "sandbox.spawn"
    # The agent-visible workspace is the sandbox path, not a host path.
    requests = dict()
    for method, params in client.requests:
        requests.setdefault(method, params)
    assert requests["session/new"]["cwd"] == SANDBOX_WORKSPACE
    assert adapter.identity() == {"sandbox": "sbx-1"}
    adapter.close()


def test_sandbox_continuation_uploads_the_parent_workspace_first(
    tmp_path: Path,
) -> None:
    adapter, sandbox, _created, log, frames, _identities = make_sandbox_adapter(
        tmp_path,
        script={"capabilities": {"sessionCapabilities": {"resume": {}}}},
        spec_overrides={"resume_session": "parent-sess"},
    )
    adapter.start()

    assert log.index("sandbox.upload_continuation") < log.index("client.start")
    assert "request:session/resume" in log
    resumed = [frame for frame in frames if frame["type"] == "acp_session_resumed"]
    assert resumed == [
        {
            "type": "acp_session_resumed",
            "method": "session/resume",
            "sessionId": "parent-sess",
            "replayedUpdates": 0,
        }
    ]
    adapter.close()


def test_sandbox_recovery_merges_the_workspace_and_the_state_root(
    tmp_path: Path,
) -> None:
    adapter, sandbox, _created, _log, _frames, _identities = make_sandbox_adapter(
        tmp_path
    )
    adapter.start()

    info = adapter.session_info()
    assert info is not None
    assert info.payload["sandboxId"] == "sbx-1"
    assert info.payload["template"] == "glink-acp-worker"
    # The recovery verdict does not exist before recover_artifacts ran.
    assert "stateRootRecovered" not in info.payload
    assert adapter.recovered_session() is None

    recovered = tmp_path / "ws" / "report.md"
    sandbox.workspace_files.append(recovered)
    recovery = adapter.recover_artifacts(tmp_path / "ws")
    assert recovery.files == (recovered,)
    assert recovery.failures == ()
    assert recovery.state_recovered is True
    assert recovery.secrets == ("state-secret",)

    info = adapter.session_info()
    assert info is not None and info.payload["stateRootRecovered"] is True
    # From here on the face has something new to publish.
    assert adapter.recovered_session() is not None
    adapter.close()


def test_sandbox_recovery_failures_fail_closed_and_stay_visible(
    tmp_path: Path,
) -> None:
    adapter, sandbox, _created, _log, _frames, _identities = make_sandbox_adapter(
        tmp_path
    )
    sandbox.workspace_failures.append("workspace read failed")
    sandbox.state = AcpStateRecovery(ok=False, failures=("state read failed",))
    adapter.start()

    recovery = adapter.recover_artifacts(tmp_path / "ws")
    assert recovery.failures == ("workspace read failed", "state read failed")
    assert recovery.state_recovered is False
    assert recovery.secrets == ()
    # A failed recovery publishes no recovered root; the verdict is on record.
    info = adapter.session_info()
    assert info is not None and info.payload["stateRootRecovered"] is False
    adapter.close()


def test_sandbox_close_reports_client_then_sandbox_failures(tmp_path: Path) -> None:
    adapter, sandbox, _created, log, _frames, _identities = make_sandbox_adapter(
        tmp_path
    )
    sandbox.close_failures.append("sandbox kill failed: boom")
    adapter.start()
    report = adapter.close()

    # Client first (stop the agent), then the sandbox; both failures surface.
    assert log.index("client.close") < log.index("sandbox.close")
    assert report.released is False
    assert report.failures == ("sandbox kill failed: boom",)
    assert report.external == {"sandbox": "sbx-1"}
