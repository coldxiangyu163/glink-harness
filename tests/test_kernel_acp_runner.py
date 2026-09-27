"""ACP run-lifecycle regressions, driven by the fake ACP agent (`fake_acp.py`).

These cover the M1 acceptance list on the real run path: capability negotiation
into ``session_ready``, the §4.1 event mapping, the usage conversion, the
``stopReason`` verdict table, the ``session/cancel`` chain, fail-closed
permission answers, the ``/`` gate, and the host-level harness switch. The
execution adapter is the production one; only the agent process is synthetic.
"""

from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import pytest

from pi_glink.config import ConfigError, HostConfig, build_config

from acp_pack import apply_acp_pack
from pi_glink.events import EventBus
from pi_glink.kernel.acp_env import AcpRuntime
from pi_glink.kernel.acp_execution import AcpExecutionAdapter
from pi_glink.kernel.execution import (
    ExecutionAdapter,
    ExecutionSpec,
    build_adapter,
)
from pi_glink.kernel.rpc import PiRpcClient
from pi_glink.runs.runner import (
    FollowupUnavailable,
    RunManager,
    UnsupportedCommand,
)
from pi_glink.store import CANCELLED, FAILED, FINISHED, RunStore

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_ACP = Path(__file__).resolve().parent / "fixtures" / "fake_acp.py"
SESSION_ID = "fake-acp-session"


def acp_command(tmp_path: Path, settings: Mapping[str, Any], *, name: str = "settings.json") -> str:
    """The frozen ``PI_GLINK_ACP_COMMAND`` for one fake-agent configuration."""
    path = tmp_path / name
    path.write_text(json.dumps(dict(settings), ensure_ascii=False), encoding="utf-8")
    return (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_ACP))}"
        f" --config {shlex.quote(str(path))}"
    )


def make_config(tmp_path: Path, settings: Mapping[str, Any] | None = None, **overrides: str) -> HostConfig:
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent-web-search.json"),
        "PI_GLINK_HARNESS": "acp",
        "PI_GLINK_ACP_COMMAND": acp_command(tmp_path, settings or {}),
    }
    values.update(overrides)
    apply_acp_pack(values, tmp_path)
    return build_config(values, REPO_ROOT)


def build_manager(
    tmp_path: Path, settings: Mapping[str, Any] | None = None, **overrides: str
) -> tuple[HostConfig, RunStore, RunManager]:
    config = make_config(tmp_path, settings, **overrides)
    store = RunStore(config.data_root / "runs")
    return config, store, RunManager(config, EventBus(), store)


def read_events(store: RunStore, run_id: str) -> list[Mapping[str, Any]]:
    path = store.events_path(run_id)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_log(path: Path) -> list[Mapping[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def wait_for(predicate, *, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def event_types(events: list[Mapping[str, Any]]) -> list[str]:
    return [str(event["type"]) for event in events]


def payloads(events: list[Mapping[str, Any]], event_type: str) -> list[Mapping[str, Any]]:
    return [event["payload"] for event in events if event["type"] == event_type]


# -- the complete lifecycle ---------------------------------------------------


def test_acp_run_projects_the_whole_lifecycle_and_the_report(tmp_path: Path) -> None:
    log = tmp_path / "agent.jsonl"
    settings = {"FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True}
    config, store, manager = build_manager(tmp_path, settings)
    record = manager.start("用一句话说明这个仓库是做什么的")
    final = manager.wait(record.run_id, timeout=30)

    assert final is not None
    assert final.status == FINISHED and final.stop_reason is None
    assert final.report_path is not None and Path(final.report_path).is_file()
    assert "fake acp" in Path(final.report_path).read_text(encoding="utf-8")
    # ACP has a session id but no pi session file; nothing pretends otherwise.
    assert final.session_id == SESSION_ID
    assert final.session_file is None
    assert final.model_stop_reason == "end_turn"
    assert manager.active_run_id is None

    events = read_events(store, record.run_id)
    types = event_types(events)
    assert types[0] == "run_started"
    assert types[-1] == "run_finished"
    # §4.1 mapping: every ACP update family has its product event.
    for expected in (
        "session_ready",
        "turn_started",
        "thinking_delta",
        "assistant_delta",
        "run_stage",
        "tool_started",
        "tool_updated",
        "tool_finished",
        "usage_reported",
        "kernel_status",
    ):
        assert expected in types, expected
    # The turn boundary frames make the product's stream model close and render
    # the streamed blocks; they must bracket the deltas and precede the verdict.
    assert types.index("turn_started") < types.index("thinking_delta")
    assert types.index("assistant_delta") < types.index("turn_finished")
    assert types.index("turn_finished") < types.index("run_finished")

    ready = payloads(events, "session_ready")[0]
    assert ready["harness"] == "acp"
    assert ready["protocolVersion"] == 1
    assert ready["sessionId"] == SESSION_ID
    assert ready["agentInfo"] == {"name": "fake-acp", "version": "0.1.0"}
    # The negotiated capability matrix is part of the run record.
    assert ready["agentCapabilities"]["promptCapabilities"]["image"] is False
    assert ready["agentCapabilities"]["sessionCapabilities"] == {"resume": {}}

    assert [p["text"] for p in payloads(events, "thinking_delta")] == ["先看目录结构"]
    deltas = [p["text"] for p in payloads(events, "assistant_delta")]
    assert deltas == ["我来看一下工作区。", "工作区里有一份报告草稿。"]

    plan = payloads(events, "run_stage")[0]
    assert plan["stage"] == "plan"
    assert [entry["content"] for entry in plan["entries"]] == ["检查工作区", "写出报告"]

    started = payloads(events, "tool_started")
    assert started[0] == {
        "toolCallId": "call_1",
        "toolName": "bash",
        "args": {"command": "ls -la"},
        "kind": "execute",
    }
    updated = payloads(events, "tool_updated")
    assert updated[0]["toolCallId"] == "call_1"
    assert updated[0]["output"] == "total 8\nreport.md"
    finished = payloads(events, "tool_finished")
    assert finished[0] == {
        "toolCallId": "call_1",
        "toolName": "bash",
        "isError": False,
        "result": "total 8\nreport.md",
    }
    assert finished[1]["toolCallId"] == "call_2" and finished[1]["isError"] is True

    usage = payloads(events, "usage_reported")[0]
    assert usage["usage"] == {
        "totalTokens": 1200,
        "contextSize": 400000,
        "basis": "acp_usage_update",
        "cost": {"amount": 0.012, "currency": "USD"},
    }
    # The terminal record carries the same converted view, not a fabricated sum.
    terminal = events[-1]["payload"]
    assert terminal["usage"]["basis"] == "acp_usage_update"
    assert terminal["usage"]["totalTokens"] == 1200
    assert terminal["modelStopReason"] == "end_turn"
    assert terminal["cleanup"]["released"] is True

    # The agent itself got: our protocol version, no client capabilities, the
    # workspace as session cwd, and this run's private HOME.
    entries = read_log(log)
    initialize = next(entry for entry in entries if entry["event"] == "initialize")
    assert initialize["protocolVersion"] == 1
    assert initialize["clientCapabilities"]["fs"] == {
        "readTextFile": False,
        "writeTextFile": False,
    }
    assert initialize["clientCapabilities"]["terminal"] is False
    session_new = next(entry for entry in entries if entry["event"] == "session_new")
    assert session_new["cwd"] == str(Path(final.workspace))
    assert session_new["mcpServers"] == []
    prompt = next(entry for entry in entries if entry["event"] == "prompt")
    # The working agreement (harnesses/pi/research-prompt.md) is delivered as the
    # leading block of the same text message; ACP has no system-role channel,
    # so the product cannot rely on one (see docs/acp-integration-notes.md M2).
    system_text = (REPO_ROOT / "harnesses" / "pi" / "research-prompt.md").read_text(encoding="utf-8").strip()
    assert prompt["text"].startswith("以下是宿主工作台注入的 system prompt")
    assert f"<system-prompt>\n{system_text}\n</system-prompt>" in prompt["text"]
    assert prompt["text"].endswith("--- 用户请求 ---\n\n用一句话说明这个仓库是做什么的")
    assert prompt["sessionId"] == SESSION_ID
    # The order proves the adapter drove the ACP methods, not a private shortcut.
    order = [entry["event"] for entry in entries if entry["event"] in {"initialize", "session_new", "prompt"}]
    assert order == ["initialize", "session_new", "prompt"]
    # Per-run state root: HOME is this run's own directory, and the agent wrote there.
    home = next(entry for entry in entries if entry["event"] == "home")["home"]
    assert Path(home) == store.run_dir(record.run_id) / "acp-agent" / "home"
    assert (Path(home) / "fake-acp-state.json").is_file()
    # The pi configuration branch is not involved for an ACP run.
    assert (store.run_dir(record.run_id) / "acp-agent").is_dir()
    assert not (store.run_dir(record.run_id) / "pi-agent").exists()


def test_acp_initialize_identifies_the_client_with_a_version(tmp_path: Path) -> None:
    """The client's ``clientInfo`` carries both required fields.

    Original obligation (M1): the client introduced itself by name; ``version``
    was informational and omitted. New obligation (M4): ACP's ``Implementation``
    declares both fields, and the M4 measurement of an ACP agent showed it
    validating the initialize params strictly -- a ``clientInfo`` without
    ``version`` is answered with ``-32602 Invalid params`` before any session
    exists, so the host sends its own version and this regression pins it.
    """
    from pi_glink import __version__ as host_version

    log = tmp_path / "agent.jsonl"
    _config, store, manager = build_manager(tmp_path, {"FAKE_ACP_LOG": str(log)})
    record = manager.start("检查 initialize 的 clientInfo")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None and final.status == FINISHED
    initialize = next(
        entry for entry in read_log(log) if entry["event"] == "initialize"
    )
    assert initialize["clientInfo"] == {"name": "pi-glink", "version": host_version}


def test_acp_usage_updates_replace_the_total_instead_of_summing(tmp_path: Path) -> None:
    settings = {"FAKE_ACP_WRITE_REPORT": True}
    _config, store, manager = build_manager(tmp_path, settings)
    record = manager.start("告诉我用量 USAGE_TWICE")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None and final.status == FINISHED
    events = read_events(store, record.run_id)
    usages = payloads(events, "usage_reported")
    assert [entry["usage"]["totalTokens"] for entry in usages] == [1200, 2400]
    terminal = events[-1]["payload"]
    assert terminal["usage"]["totalTokens"] == 2400
    assert terminal["usage"]["cost"]["amount"] == 0.03


# -- stopReason -> verdict ----------------------------------------------------


@pytest.mark.parametrize(
    "marker, status, stop_reason, model_stop_reason",
    [
        ("", FINISHED, "no_report", "end_turn"),
        ("MAXTOKENS", FINISHED, "length", "max_tokens"),
        ("MAXTURN", FINISHED, "max_turn_requests", "max_turn_requests"),
        ("REFUSAL", FAILED, "model_error", "refusal"),
        ("CANCEL", CANCELLED, "abort", "cancelled"),
        ("UNKNOWNSTOP", FAILED, "execution_error", "made_up_reason"),
    ],
)
def test_acp_stop_reason_decides_the_terminal_state(
    tmp_path: Path, marker: str, status: str, stop_reason: str, model_stop_reason: str
) -> None:
    _config, store, manager = build_manager(tmp_path)
    record = manager.start(f"跑一轮 {marker}".strip())
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None
    assert (final.status, final.stop_reason, final.model_stop_reason) == (
        status,
        stop_reason,
        model_stop_reason,
    )
    events = read_events(store, record.run_id)
    assert events[-1]["type"] == {
        FINISHED: "run_finished",
        FAILED: "run_failed",
        CANCELLED: "run_cancelled",
    }[status]
    assert manager.active_run_id is None


def test_acp_prompt_without_a_stop_reason_fails_closed(tmp_path: Path) -> None:
    settings = {"FAKE_ACP_NO_STOP_REASON": True}
    _config, store, manager = build_manager(tmp_path, settings)
    record = manager.start("协议缺字段")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None
    assert final.status == FAILED and final.stop_reason == "execution_error"
    assert "stopReason" in (final.error or "")


def test_acp_agent_crash_before_the_prompt_answer_is_kernel_exit(tmp_path: Path) -> None:
    _config, _store, manager = build_manager(tmp_path)
    record = manager.start("CRASH 掉这个 agent")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None
    assert final.status == FAILED and final.stop_reason == "kernel_exit"
    assert "exit code 3" in (final.error or "")


def test_acp_crash_after_the_stream_ends_still_reports_the_exit_code(tmp_path: Path) -> None:
    """The crash's event order must not lose the agent's exit status.

    A real crash closes stdout (the client reads EOF) microseconds before the
    process is reapable, so a report composed from a status sampled at the
    stream's end can miss the code -- the flake this regression pins down.
    ``FAKE_ACP_CRASH_EXIT_DELAY`` widens exactly that window (stdout closed,
    then the delay, then exit 3) so the ordering is exercised every run instead
    of once per suite under load.
    """
    settings = {"FAKE_ACP_CRASH_EXIT_DELAY": "0.3"}
    _config, _store, manager = build_manager(tmp_path, settings)
    record = manager.start("CRASH 掉这个 agent")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None
    assert final.status == FAILED and final.stop_reason == "kernel_exit"
    assert "exit code 3" in (final.error or "")


def test_acp_initialize_failure_fails_the_run(tmp_path: Path) -> None:
    settings = {"FAKE_ACP_INIT_ERROR": True, "FAKE_ACP_LOG": str(tmp_path / "agent.jsonl")}
    _config, store, manager = build_manager(tmp_path, settings)
    record = manager.start("没有凭据")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None
    assert final.status == FAILED and final.stop_reason == "execution_error"
    assert "authentication required" in (final.error or "")
    assert "session_ready" not in event_types(read_events(store, record.run_id))


def test_acp_protocol_version_mismatch_fails_closed(tmp_path: Path) -> None:
    settings = {"FAKE_ACP_PROTOCOL_VERSION": 2, "FAKE_ACP_LOG": str(tmp_path / "agent.jsonl")}
    _config, store, manager = build_manager(tmp_path, settings)
    record = manager.start("版本不一致")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None
    assert final.status == FAILED and final.stop_reason == "execution_error"
    assert "协议版本" in (final.error or "")
    # The mismatch is detected before a session is ever created.
    assert "session_ready" not in event_types(read_events(store, record.run_id))


def test_acp_prompt_rejection_fails_the_run(tmp_path: Path) -> None:
    _config, _store, manager = build_manager(tmp_path)
    record = manager.start("FAILPROMPT 被拒绝")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None
    assert final.status == FAILED and final.stop_reason == "execution_error"
    assert "session/prompt" in (final.error or "")


def test_acp_run_timeout_cancels_the_agent(tmp_path: Path) -> None:
    log = tmp_path / "agent.jsonl"
    settings = {"FAKE_ACP_LOG": str(log)}
    _config, _store, manager = build_manager(
        tmp_path, settings, LOCAL_WEB_TASK_TIMEOUT_SECONDS="1"
    )
    record = manager.start("NOSETTLE 一直不回答")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None
    assert final.status == FAILED and final.stop_reason == "timeout"
    assert wait_for(lambda: any(entry["event"] == "cancel_received" for entry in read_log(log)))


def test_acp_timeout_error_text_survives_a_late_kernel_exit_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deadline's error text is final; the driver's late write cannot rewrite it.

    A hung agent ignores ``session/cancel`` (``FAKE_ACP_CANCEL_HANG``), so the
    driver's ``session/prompt`` request only unblocks when the host's teardown
    closes the stream -- after ``wait()`` has already decided ``timeout``. The
    window is held open deterministically: the run-level ``_decide`` joins the
    driver thread before reading the outcome, so the late ``kernel_exit`` write
    lands exactly where it lands under load. Without a freeze the record keeps
    ``stop_reason=timeout`` but its error text becomes the kernel-exit report.
    """
    log = tmp_path / "agent.jsonl"
    settings = {"FAKE_ACP_LOG": str(log), "FAKE_ACP_CANCEL_HANG": True}
    _config, _store, manager = build_manager(
        tmp_path, settings, LOCAL_WEB_TASK_TIMEOUT_SECONDS="1"
    )
    original_decide = RunManager._decide
    driver_states: list[bool] = []

    def decide_after_the_driver_stops(self, execution):
        driver = getattr(execution.adapter, "_driver", None)
        if driver is not None:
            driver.join(timeout=10)
            driver_states.append(driver.is_alive())
        return original_decide(self, execution)

    monkeypatch.setattr(RunManager, "_decide", decide_after_the_driver_stops)
    record = manager.start("NOSETTLE 收到取消也不回答")
    assert wait_for(lambda: any(entry["event"] == "prompt" for entry in read_log(log)))
    final = manager.wait(record.run_id, timeout=30)

    # The race window was actually entered: the driver finished (its late write
    # landed) before the verdict read the outcome.
    assert driver_states == [False]
    assert final is not None
    assert (final.status, final.stop_reason) == (FAILED, "timeout")
    assert "run did not settle within" in (final.error or "")


# -- cancellation -------------------------------------------------------------


def test_acp_cancel_sends_session_cancel_and_settles_cancelled(tmp_path: Path) -> None:
    log = tmp_path / "agent.jsonl"
    settings = {"FAKE_ACP_LOG": str(log)}
    _config, store, manager = build_manager(tmp_path, settings)
    record = manager.start("NOSETTLE 等着我取消")
    # Cancel only once streamed work has actually been projected. The prompt
    # reaching the agent is not enough: the notifications are still in flight,
    # and a cancel can legitimately land before any of them is committed, which
    # would fail the assertion below on scheduling rather than on behaviour.
    assert wait_for(
        lambda: payloads(read_events(store, record.run_id), "assistant_delta")
    )
    decision = manager.stop(record.run_id)
    assert decision.accepted is True
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None
    assert (final.status, final.stop_reason) == (CANCELLED, "abort")
    assert wait_for(
        lambda: any(entry["event"] == "cancel_received" for entry in read_log(log))
    )
    cancel = next(entry for entry in read_log(log) if entry["event"] == "cancel_received")
    assert cancel["sessionId"] == SESSION_ID
    events = read_events(store, record.run_id)
    assert events[-1]["type"] == "run_cancelled"
    # The streamed work before the cancel is preserved.
    assert payloads(events, "assistant_delta")


# -- permission ---------------------------------------------------------------


def test_acp_permission_request_is_refused_and_recorded(tmp_path: Path) -> None:
    log = tmp_path / "agent.jsonl"
    settings = {"FAKE_ACP_PERMISSION": True, "FAKE_ACP_LOG": str(log)}
    _config, store, manager = build_manager(tmp_path, settings)
    record = manager.start("需要权限的一次运行")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None and final.status == FINISHED

    entries = read_log(log)
    outcome = next(entry for entry in entries if entry["event"] == "permission_outcome")
    assert outcome["outcome"] == {"outcome": "selected", "optionId": "reject-once"}
    assert outcome["error"] is None

    notices = [
        payload
        for payload in payloads(read_events(store, record.run_id), "kernel_status")
        if payload.get("message") == "acp_permission_denied"
    ]
    assert len(notices) == 1
    assert notices[0]["level"] == "warning"
    assert notices[0]["decision"] == "rejected"
    assert notices[0]["optionId"] == "reject-once"
    assert notices[0]["optionKind"] == "reject_once"
    assert notices[0]["toolCallId"] == "call_1"


def test_acp_permission_without_a_reject_option_is_cancelled(tmp_path: Path) -> None:
    log = tmp_path / "agent.jsonl"
    settings = {
        "FAKE_ACP_PERMISSION": True,
        "FAKE_ACP_LOG": str(log),
        "FAKE_ACP_PERMISSION_OPTIONS": [
            {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"}
        ],
    }
    _config, store, manager = build_manager(tmp_path, settings)
    record = manager.start("只给了允许选项")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None and final.status == FINISHED
    outcome = next(
        entry for entry in read_log(log) if entry["event"] == "permission_outcome"
    )
    assert outcome["outcome"] == {"outcome": "cancelled"}
    notices = [
        payload
        for payload in payloads(read_events(store, record.run_id), "kernel_status")
        if payload.get("message") == "acp_permission_denied"
    ]
    assert notices[0]["decision"] == "cancelled"


# -- the explicit permission allowlist (default stays fail-closed) -------------


def test_acp_permission_allowlist_selects_the_narrow_allow_option(tmp_path: Path) -> None:
    """``PI_GLINK_ACP_PERMISSION_ALLOW=allow_once`` is the smallest gate.

    dsh offers only ``allow-once``/``reject-once`` and its request carries no
    tool kind, so an option-kind allowlist is the narrowest policy that can
    make an unattended run progress; the decision is still recorded.
    """
    log = tmp_path / "agent.jsonl"
    settings = {"FAKE_ACP_PERMISSION": True, "FAKE_ACP_LOG": str(log)}
    _config, store, manager = build_manager(
        tmp_path, settings, PI_GLINK_ACP_PERMISSION_ALLOW="allow_once"
    )
    record = manager.start("允许一次权限请求")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None and final.status == FINISHED
    outcome = next(
        entry for entry in read_log(log) if entry["event"] == "permission_outcome"
    )
    assert outcome["outcome"] == {"outcome": "selected", "optionId": "allow-once"}
    notices = [
        payload
        for payload in payloads(read_events(store, record.run_id), "kernel_status")
        if payload.get("message", "").startswith("acp_permission_")
    ]
    assert notices[0]["message"] == "acp_permission_allowed"
    assert notices[0]["decision"] == "allowed"
    assert notices[0]["policy"] == "allowlist"
    assert notices[0]["optionKind"] == "allow_once"


def test_acp_permission_allowlist_gates_on_the_tool_kind(tmp_path: Path) -> None:
    log = tmp_path / "agent.jsonl"
    settings = {
        "FAKE_ACP_PERMISSION": True,
        "FAKE_ACP_LOG": str(log),
        "FAKE_ACP_PERMISSION_TOOL_KIND": "read",
    }
    _config, store, manager = build_manager(
        tmp_path,
        settings,
        PI_GLINK_ACP_PERMISSION_ALLOW="allow_once,execute",
    )
    record = manager.start("工具类型不在白名单")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None and final.status == FINISHED
    outcome = next(
        entry for entry in read_log(log) if entry["event"] == "permission_outcome"
    )
    assert outcome["outcome"] == {"outcome": "selected", "optionId": "reject-once"}
    notices = [
        payload
        for payload in payloads(read_events(store, record.run_id), "kernel_status")
        if payload.get("message") == "acp_permission_denied"
    ]
    assert notices[0]["decision"] == "rejected"
    assert notices[0]["policy"] == "fail_closed"
    assert notices[0]["toolKind"] == "read"


def test_acp_permission_allowlist_refuses_a_kindless_request_when_kinds_are_set(
    tmp_path: Path,
) -> None:
    """A tool-kind allowlist must not blanket-allow requests that carry no kind."""
    log = tmp_path / "agent.jsonl"
    settings = {"FAKE_ACP_PERMISSION": True, "FAKE_ACP_LOG": str(log)}
    _config, store, manager = build_manager(
        tmp_path,
        settings,
        PI_GLINK_ACP_PERMISSION_ALLOW="allow_once,execute",
    )
    record = manager.start("请求没有 tool kind")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None and final.status == FINISHED
    outcome = next(
        entry for entry in read_log(log) if entry["event"] == "permission_outcome"
    )
    assert outcome["outcome"] == {"outcome": "selected", "optionId": "reject-once"}


def test_acp_permission_allowlist_resolves_the_kind_published_by_the_agent(
    tmp_path: Path,
) -> None:
    """The kind from the agent's own ``tool_call`` notice classifies the request.

    Original obligation (M2): a tool-kind allowlist refused a permission request
    that carried no ``kind`` of its own. New obligation (M4): the measured ACP
    agent omitted ``kind`` from the request but published it for the same
    ``toolCallId`` in the preceding ``tool_call`` notification, so the adapter
    resolves the request against that published classification instead of
    guessing from the title. A request whose kind is unknown in both places is
    still refused — the regression above pins that half.
    """
    log = tmp_path / "agent.jsonl"
    settings = {
        "FAKE_ACP_PERMISSION": True,
        # the measured M4 order: tool_call notification first, then
        # the permission request without a kind of its own.
        "FAKE_ACP_PERMISSION_AFTER_TOOL_CALL": True,
        "FAKE_ACP_TOOL_CALL_KIND": "read",
        "FAKE_ACP_LOG": str(log),
    }
    _config, store, manager = build_manager(
        tmp_path,
        settings,
        PI_GLINK_ACP_PERMISSION_ALLOW="allow_once,read,search,fetch,edit",
    )
    record = manager.start("通知里已经公布过 kind")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None and final.status == FINISHED
    outcome = next(
        entry for entry in read_log(log) if entry["event"] == "permission_outcome"
    )
    assert outcome["error"] is None
    assert outcome["outcome"] == {"outcome": "selected", "optionId": "allow-once"}
    notices = [
        payload
        for payload in payloads(read_events(store, record.run_id), "kernel_status")
        if payload.get("message", "").startswith("acp_permission_")
    ]
    assert notices[0]["message"] == "acp_permission_allowed"
    assert notices[0]["decision"] == "allowed"
    assert notices[0]["toolKind"] == "read"
    assert notices[0]["toolKindSource"] == "tool_call"


def test_acp_permission_allowlist_refuses_a_published_execute_kind(
    tmp_path: Path,
) -> None:
    """The resolved classification is authoritative in the refusal direction too.

    Same mechanism as the test above, opposite kind: an ``execute`` tool whose
    request omits ``kind`` is refused by an allowlist that lists only the
    research kinds, so resolving the kind cannot become a blanket allow.
    """
    log = tmp_path / "agent.jsonl"
    settings = {
        "FAKE_ACP_PERMISSION": True,
        "FAKE_ACP_PERMISSION_AFTER_TOOL_CALL": True,
        "FAKE_ACP_TOOL_CALL_KIND": "execute",
        "FAKE_ACP_LOG": str(log),
    }
    _config, store, manager = build_manager(
        tmp_path,
        settings,
        PI_GLINK_ACP_PERMISSION_ALLOW="allow_once,read,search,fetch,edit",
    )
    record = manager.start("通知里公布的是 execute")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None and final.status == FINISHED
    outcome = next(
        entry for entry in read_log(log) if entry["event"] == "permission_outcome"
    )
    assert outcome["outcome"] == {"outcome": "selected", "optionId": "reject-once"}
    notices = [
        payload
        for payload in payloads(read_events(store, record.run_id), "kernel_status")
        if payload.get("message") == "acp_permission_denied"
    ]
    assert notices[0]["decision"] == "rejected"
    assert notices[0]["policy"] == "fail_closed"
    assert notices[0]["toolKind"] == "execute"
    assert notices[0]["toolKindSource"] == "tool_call"


# -- session configuration -----------------------------------------------------

CONFIG_OPTIONS = [
    {
        "id": "mode",
        "name": "Approval Preset",
        "type": "select",
        "currentValue": "read-only",
        "options": [
            {"value": "read-only", "name": "Read Only"},
            {"value": "auto", "name": "Default"},
        ],
    },
    {
        "id": "reasoning_effort",
        "name": "Reasoning effort",
        "type": "select",
        "currentValue": "high",
        "options": [{"value": "high", "name": "High"}],
    },
]

MODES = {
    "currentModeId": "read-only",
    "availableModes": [
        {"id": "read-only", "name": "Read Only"},
        {"id": "auto", "name": "Default"},
    ],
}


def test_acp_session_config_is_applied_and_recorded(tmp_path: Path) -> None:
    log = tmp_path / "agent.jsonl"
    settings = {
        "FAKE_ACP_LOG": str(log),
        "FAKE_ACP_CONFIG_OPTIONS": CONFIG_OPTIONS,
        "FAKE_ACP_MODES": MODES,
        "FAKE_ACP_WRITE_REPORT": True,
    }
    _config, store, manager = build_manager(
        tmp_path, settings, PI_GLINK_ACP_SESSION_CONFIG="mode=auto"
    )
    record = manager.start("选择可写工作区的会话模式")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None and final.status == FINISHED
    applied = [entry for entry in read_log(log) if entry["event"] == "config_option"]
    assert [entry["configId"] for entry in applied] == ["mode"]
    assert applied[0]["value"] == "auto"
    ready = payloads(read_events(store, record.run_id), "session_ready")[0]
    modes = {option["id"]: option["currentValue"] for option in ready["configOptions"]}
    assert modes["mode"] == "auto"
    assert modes["reasoning_effort"] == "high"
    assert ready["modes"]["currentModeId"] == "auto"
    assert [mode["id"] for mode in ready["modes"]["availableModes"]] == [
        "read-only",
        "auto",
    ]


def test_acp_session_config_already_current_is_not_re_sent(tmp_path: Path) -> None:
    log = tmp_path / "agent.jsonl"
    settings = {
        "FAKE_ACP_LOG": str(log),
        "FAKE_ACP_CONFIG_OPTIONS": CONFIG_OPTIONS,
        "FAKE_ACP_WRITE_REPORT": True,
    }
    _config, _store, manager = build_manager(
        tmp_path, settings, PI_GLINK_ACP_SESSION_CONFIG="reasoning_effort=high"
    )
    record = manager.start("模式已经是目标值")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None and final.status == FINISHED
    assert [entry for entry in read_log(log) if entry["event"] == "config_option"] == []


def test_acp_session_config_the_agent_does_not_advertise_fails_the_run(
    tmp_path: Path,
) -> None:
    """An explicitly configured option is never silently dropped (fail closed)."""
    log = tmp_path / "agent.jsonl"
    settings = {
        "FAKE_ACP_LOG": str(log),
        "FAKE_ACP_CONFIG_OPTIONS": CONFIG_OPTIONS,
    }
    _config, store, manager = build_manager(
        tmp_path, settings, PI_GLINK_ACP_SESSION_CONFIG="mode=full-access"
    )
    record = manager.start("白名单外的模式值")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None and final.status == FAILED
    assert [entry for entry in read_log(log) if entry["event"] == "prompt"] == []
    terminal = read_events(store, record.run_id)[-1]["payload"]
    assert "ACP 配置选项" in (terminal.get("error") or "")


# -- credential injection ------------------------------------------------------


def test_injected_credential_values_are_redacted_from_the_run(tmp_path: Path) -> None:
    """A value only the injected file carries must be masked when echoed.

    The secret is opaque (no provider shape) and lives under a credential-named
    key in a file the host copies into the run HOME; nothing else — not the
    host config, not the fake agent's settings — knows it. Redaction therefore
    proves the injected values joined this run's boundary.
    """
    secret = "opaque-shared-value-1234567890"
    source = tmp_path / "credentials.json"
    source.write_text(json.dumps({"seedToken": secret}), encoding="utf-8")
    log = tmp_path / "agent.jsonl"
    settings = {
        "FAKE_ACP_SECRET": secret,
        "FAKE_ACP_LOG": str(log),
        "FAKE_ACP_WRITE_REPORT": True,
    }
    config, store, manager = build_manager(
        tmp_path,
        settings,
        PI_GLINK_ACP_CREDENTIAL_FILES=f"{source}=state/credentials.json",
    )
    record = manager.start("SECRET 请回显凭据")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None and final.status == FINISHED
    run_home = store.run_dir(record.run_id) / "acp-agent" / "home"
    copied = run_home / "state" / "credentials.json"
    assert copied.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert (copied.stat().st_mode & 0o777) == 0o600
    assert (copied.parent.stat().st_mode & 0o777) == 0o700
    assert source.read_text(encoding="utf-8") == json.dumps({"seedToken": secret})
    raw = store.events_path(record.run_id).read_text(encoding="utf-8")
    assert secret not in raw
    assert "[REDACTED]" in raw
    # The host config did not carry the secret: only the copied file could
    # have taught this run's redactor the value.
    assert secret not in json.dumps(config.public_view())


def test_acp_env_entries_reach_the_agent_process(tmp_path: Path) -> None:
    log = tmp_path / "agent.jsonl"
    settings = {"FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True}
    _config, _store, manager = build_manager(
        tmp_path,
        settings,
        PI_GLINK_ACP_ENV="ACME_RESEARCH_MODE=workspace;FAKE_ACP_ENV_PROBE=ACME_RESEARCH_MODE",
    )
    record = manager.start("环境变量探测")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None and final.status == FINISHED
    probe = next(entry for entry in read_log(log) if entry["event"] == "env_probe")
    assert probe["name"] == "ACME_RESEARCH_MODE"
    assert probe["value"] == "workspace"


# -- admission: slash gate and follow-ups -------------------------------------


@pytest.mark.parametrize(
    "prompt", ["/deepresearch", "  /deepresearch", "/run --bg", "/subagents-detach", "/"]
)
def test_acp_refuses_every_slash_prompt_before_creating_anything(
    tmp_path: Path, prompt: str
) -> None:
    _config, store, manager = build_manager(tmp_path)
    with pytest.raises(UnsupportedCommand) as excinfo:
        manager.start(prompt)
    assert excinfo.value.allowed == ()
    assert store.list(limit=None) == []


def test_acp_accepts_ordinary_prompts_with_a_slash_inside(tmp_path: Path) -> None:
    settings = {"FAKE_ACP_WRITE_REPORT": True}
    _config, _store, manager = build_manager(tmp_path, settings)
    record = manager.start("请说明 /run 这种写法是否被接受")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None and final.status == FINISHED


def test_acp_followups_without_a_resume_capability_are_refused(tmp_path: Path) -> None:
    # Original obligation (M1): ACP had no ``session/resume`` at all, so every
    # ACP follow-up was refused as ``session_unavailable``.
    # New obligation (M7): a follow-up is admitted when the parent really can be
    # continued (same harness, terminal, latest turn, a session id and a
    # declared continuation capability); this agent declares none, so the same
    # refusal now covers exactly the capability-less case — with no child run.
    settings = {
        "FAKE_ACP_WRITE_REPORT": True,
        "FAKE_ACP_CAPABILITIES": {"loadSession": False, "sessionCapabilities": {}},
    }
    _config, store, manager = build_manager(tmp_path, settings)
    parent = manager.start("第一轮")
    assert manager.wait(parent.run_id, timeout=30) is not None
    before = len(store.list(limit=None))
    with pytest.raises(FollowupUnavailable) as excinfo:
        manager.start("继续追问", parent_run_id=parent.run_id)
    assert excinfo.value.code == "session_unavailable"
    assert len(store.list(limit=None)) == before


# -- host-level integration ---------------------------------------------------


def test_acp_run_does_not_require_the_host_model_key(tmp_path: Path) -> None:
    settings = {"FAKE_ACP_WRITE_REPORT": True}
    _config, _store, manager = build_manager(tmp_path, settings, OPENAI_API_KEY="")
    record = manager.start("宿主没有模型 key 也能跑 ACP")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None and final.status == FINISHED


def test_two_acp_runs_get_their_own_agent_state_root(tmp_path: Path) -> None:
    log = tmp_path / "agent.jsonl"
    settings = {"FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True}
    _config, store, manager = build_manager(tmp_path, settings)
    runs = []
    for index in range(2):
        record = manager.start(f"第 {index + 1} 轮")
        assert manager.wait(record.run_id, timeout=30) is not None
        runs.append(record)
    homes = [entry["home"] for entry in read_log(log) if entry["event"] == "home"]
    assert len(homes) == 2 and homes[0] != homes[1]
    for record, home in zip(runs, homes):
        expected = store.run_dir(record.run_id) / "acp-agent" / "home"
        assert Path(home) == expected
        assert (expected / "fake-acp-state.json").is_file()


def test_acp_secrets_streamed_by_the_agent_are_redacted(tmp_path: Path) -> None:
    secret = "sk-live-abcdefghijklmnopqrstuvwxyz"
    settings = {"FAKE_ACP_SECRET": secret, "FAKE_ACP_WRITE_REPORT": True}
    _config, store, manager = build_manager(tmp_path, settings, OPENAI_API_KEY=secret)
    record = manager.start("SECRET 请回显凭据")
    final = manager.wait(record.run_id, timeout=30)
    assert final is not None and final.status == FINISHED
    raw = store.events_path(record.run_id).read_text(encoding="utf-8")
    assert secret not in raw
    assert "[REDACTED]" in raw


def test_acp_adapter_is_composed_through_the_same_contract(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime = AcpRuntime(
        directory=tmp_path / "acp-agent",
        home=tmp_path / "acp-agent" / "home",
        command=config.acp_command,
    )
    runtime.directory.mkdir(parents=True, exist_ok=True)
    runtime.home.mkdir(parents=True, exist_ok=True)
    spec = ExecutionSpec(
        config=config,
        runtime=runtime,
        run_id="contract-run",
        prompt="契约",
        system_prompt="",
        workspace=tmp_path,
        timeout_seconds=5,
    )
    adapter = build_adapter(
        spec,
        harness="acp",
        executor="inproc",
        client_factory=PiRpcClient,
        on_frame=lambda _frame: None,
        rpc_plan=lambda _spec: ([], {}, None),
        docker_plan=lambda _spec: ([], {}, "container"),
        docker_stop=lambda _name: [],
        acp_plan=lambda _spec: ([sys.executable, "-c", "pass"], {}, _spec.workspace),
    )
    assert isinstance(adapter, ExecutionAdapter)
    assert isinstance(adapter, AcpExecutionAdapter)
    assert adapter.kind == "acp"

    # The pi path is untouched by the new dispatch.
    from pi_glink.kernel.execution import RpcExecutionAdapter

    pi_adapter = build_adapter(
        spec,
        executor="inproc",
        client_factory=PiRpcClient,
        on_frame=lambda _frame: None,
        rpc_plan=lambda _spec: (["pi", "--mode", "rpc"], {}, _spec.workspace),
        docker_plan=lambda _spec: ([], {}, "container"),
        docker_stop=lambda _name: [],
    )
    assert isinstance(pi_adapter, RpcExecutionAdapter)

    # M3a：docker 面由同一 ACP 适配器承载（容器 stdio 交给同一个 M1 客户端）。
    # 原义务（M1）：harness=acp + executor=docker 在装配层即拒绝；新义务：
    # 可装配，且容器身份/停止语义走 docker 面约定（详见
    # tests/test_kernel_acp_docker.py 的 plan 组合回归）。
    docker_adapter = build_adapter(
        spec,
        harness="acp",
        executor="docker",
        client_factory=PiRpcClient,
        on_frame=lambda _frame: None,
        rpc_plan=lambda _spec: ([], {}, None),
        docker_plan=lambda _spec: ([], {}, "container"),
        docker_stop=lambda _name: [],
        acp_plan=lambda _spec: ([], {}, None),
        acp_docker_plan=lambda _spec: (["docker", "run", "-i"], {}, "pi-glink-contract"),
    )
    assert isinstance(docker_adapter, AcpExecutionAdapter)
    assert docker_adapter.identity() == {"container": "pi-glink-contract"}

    # M3b：e2b 面已实现，装配层不再拒绝；沙箱客户端/桥接与终态走
    # tests/test_kernel_acp_e2b.py（fake SDK），这里只确认接缝不再失败。
    # 原义务（M1–M3a）：build_adapter(harness="acp", executor="e2b") 必须报
    # ConfigError（不静默降级）；新义务：接受，但缺少 acp_e2b_plan 时显式失败。
    from pi_glink.kernel.acp_execution import AcpSandboxExecutionAdapter

    class _FakeSandboxClient:
        sandbox_id = ""
        argv: list[str] = []
        template = "glink-acp-worker"

        def start(self, **_kwargs) -> None:
            pass

        def spawn(self):  # pragma: no cover - 本用例不启动 agent
            raise AssertionError("spawn 不应在本用例中被调用")

    e2b_adapter = build_adapter(
        spec,
        harness="acp",
        executor="e2b",
        client_factory=PiRpcClient,
        on_frame=lambda _frame: None,
        rpc_plan=lambda _spec: ([], {}, None),
        docker_plan=lambda _spec: ([], {}, "container"),
        docker_stop=lambda _name: [],
        acp_plan=lambda _spec: ([], {}, None),
        acp_e2b_plan=lambda _spec: _FakeSandboxClient(),
    )
    assert isinstance(e2b_adapter, AcpSandboxExecutionAdapter)
    with pytest.raises(ConfigError):
        build_adapter(
            spec,
            harness="acp",
            executor="e2b",
            client_factory=PiRpcClient,
            on_frame=lambda _frame: None,
            rpc_plan=lambda _spec: ([], {}, None),
            docker_plan=lambda _spec: ([], {}, "container"),
            docker_stop=lambda _name: [],
            acp_plan=lambda _spec: ([], {}, None),
        )
