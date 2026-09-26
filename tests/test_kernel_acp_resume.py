"""ACP continuation regressions (GLINK-ACP-09 / M7).

The feature under test is a real continuation: a follow-up run reattaches to the
parent's ACP session (``session/resume``, or ``session/load`` when only the older
capability is declared) over the parent's inherited agent state root. The tests
are synthetic — the real ACP agent is ``tests/fixtures/fake_acp.py`` and the run
lifecycle is the production one — and cover the four contracts of the milestone:

* admission: a follow-up is admitted only for a same-harness, terminal, latest
  parent with a session id and a declared continuation capability; everything
  else stays refused;
* state inheritance: the child's own HOME receives a bounded, link-safe copy of
  the parent's state root (credential destinations excluded and re-rendered
  fresh), and a failure refuses the follow-up instead of resuming from nothing;
* the protocol path: the adapter prefers ``session/resume``, falls back to
  ``session/load`` only when the matrix says so, drops the load's history replay
  instead of duplicating the parent's transcript, and fails closed — never
  answering a follow-up with a brand-new session — when the agent refuses;
* the redaction boundary: values discovered in the inherited state join the
  child's redactor, so an inherited credential echoed by the agent is masked.

``test_kernel_acp_runner.py`` keeps the run-lifecycle matrix; this file owns the
continuation-specific behaviour.
"""

from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import pytest

from pi_glink.config import HostConfig, build_config
from pi_glink.events import EventBus
from pi_glink.kernel.acp_env import INHERIT_MAX_FILE_BYTES
from pi_glink.kernel.acp_execution import (
    LOAD_METHOD,
    RESUME_METHOD,
    AcpExecutionAdapter,
    acp_resume_method,
)
from pi_glink.kernel.acp_rpc import AcpError
from pi_glink.kernel.execution import ExecutionAdapter, ExecutionSpec, SessionInfo
from pi_glink.kernel.runner import FollowupUnavailable, RunManager
from pi_glink.store import FAILED, FINISHED, RunStore
from pi_glink.tenancy import TenantContext, TenantCredentialStore

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_ACP = Path(__file__).resolve().parent / "fixtures" / "fake_acp.py"
SESSION_ID = "fake-acp-session"
AUDIENCE = "pi-glink-test"
SECRET = b"0123456789abcdef0123456789abcdef"


# -- fixtures -----------------------------------------------------------------


def acp_command(
    tmp_path: Path, settings: Mapping[str, Any], *, name: str = "settings.json"
) -> str:
    """The frozen ``PI_GLINK_ACP_COMMAND`` for one fake-agent configuration."""
    path = tmp_path / name
    path.write_text(json.dumps(dict(settings), ensure_ascii=False), encoding="utf-8")
    return (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(FAKE_ACP))}"
        f" --config {shlex.quote(str(path))}"
    )


def make_config(
    tmp_path: Path, settings: Mapping[str, Any] | None = None, **overrides: str
) -> HostConfig:
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent-web-search.json"),
        "PI_GLINK_HARNESS": "acp",
        "PI_GLINK_ACP_COMMAND": acp_command(tmp_path, settings or {}),
    }
    values.update(overrides)
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
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def read_log(path: Path) -> list[Mapping[str, Any]]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def payloads(events: list[Mapping[str, Any]], event_type: str) -> list[Mapping[str, Any]]:
    return [event["payload"] for event in events if event["type"] == event_type]


def assistant_text(events: list[Mapping[str, Any]]) -> str:
    return "".join(
        str(payload.get("text") or "")
        for payload in payloads(events, "assistant_delta")
    )


def state_home(store: RunStore, run_id: str) -> Path:
    return store.run_dir(run_id) / "acp-agent" / "home"


def run_first_turn(
    manager: RunManager, prompt: str, *, timeout: float = 30.0
) -> Any:
    record = manager.start(prompt)
    final = manager.wait(record.run_id, timeout=timeout)
    assert final is not None and final.status == FINISHED
    return final


# -- the capability matrix ----------------------------------------------------


def test_resume_method_prefers_resume_and_requires_a_declared_channel() -> None:
    assert acp_resume_method({"sessionCapabilities": {"resume": {}}}) == RESUME_METHOD
    assert (
        acp_resume_method({"loadSession": True, "sessionCapabilities": {"resume": {}}})
        == RESUME_METHOD
    )
    assert acp_resume_method({"loadSession": True, "sessionCapabilities": {}}) == LOAD_METHOD
    assert acp_resume_method({"loadSession": True}) == LOAD_METHOD
    # No continuation bit, a false bit, or an absent matrix: no continuation.
    assert acp_resume_method({"sessionCapabilities": {}}) is None
    assert acp_resume_method({"sessionCapabilities": {"resume": False}}) is None
    assert acp_resume_method({"sessionCapabilities": {"resume": None}, "loadSession": False}) is None
    assert acp_resume_method({}) is None


def test_adapter_refuses_a_followup_the_agent_cannot_resume(tmp_path: Path) -> None:
    """Bypassing admission still cannot produce a fresh session for a follow-up.

    The adapter negotiates the capability matrix itself; an agent without a
    continuation channel gets an explicit error before any ``session/new``. This
    pins the fail-closed rule at the protocol boundary, independent of the
    admission gate.
    """
    log = tmp_path / "agent.jsonl"
    settings = {
        "FAKE_ACP_LOG": str(log),
        "FAKE_ACP_CAPABILITIES": {"loadSession": False, "sessionCapabilities": {}},
    }
    config = make_config(tmp_path, settings)
    runtime_dir = tmp_path / "runtime" / "acp-agent"
    from pi_glink.kernel.acp_env import prepare_acp_runtime

    runtime = prepare_acp_runtime(config, runtime_dir=runtime_dir)
    spec = ExecutionSpec(
        config=config,
        runtime=runtime,
        run_id="resume-contract",
        prompt="追问",
        system_prompt="",
        workspace=tmp_path,
        timeout_seconds=5,
        resume_session=SESSION_ID,
        expected_session_id=SESSION_ID,
    )
    adapter = AcpExecutionAdapter(
        spec,
        argv=shlex.split(config.acp_command),
        env={},
        cwd=tmp_path,
        on_frame=lambda _frame: None,
    )
    with pytest.raises(AcpError) as excinfo:
        adapter.start()
    assert "拒绝以新会话冒充追问" in str(excinfo.value)
    adapter.close()
    # No session was ever created for this run.
    assert [entry for entry in read_log(log) if entry["event"] == "session_new"] == []


# -- the full resume lifecycle ------------------------------------------------


def test_acp_followup_resumes_parent_session_and_inherits_state(tmp_path: Path) -> None:
    log = tmp_path / "agent.jsonl"
    settings = {"FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True}
    _config, store, manager = build_manager(tmp_path, settings)

    parent = run_first_turn(manager, "第一轮：请记住代号 REMEMBER:ZETA-7319，然后写报告")
    parent_home = state_home(store, parent.run_id)
    parent_state = json.loads((parent_home / "fake-acp-state.json").read_text(encoding="utf-8"))
    assert parent_state["remembered"] == "ZETA-7319"
    assert parent.session_id == SESSION_ID

    child = manager.start("RECALL 不要读文件，只用一行复述上一个会话里要求记住的代号", parent_run_id=parent.run_id)
    child_final = manager.wait(child.run_id, timeout=30)
    assert child_final is not None and child_final.status == FINISHED
    # The continuation keeps the parent's session id — no new session was made.
    assert child_final.session_id == parent.session_id
    assert child_final.parent_run_id == parent.run_id
    # Original obligation (pi family, and the ACP family's first cut): every run
    # got its own freshly allocated workspace. New obligation (M7, ACP): an
    # agent session records its ``cwd`` and keeps working there however the next
    # request is phrased (the opencode live probe wrote to the remembered parent
    # path), so the continuation runs in the conversation's workspace — the
    # parent run's — and the child's own artifacts are still published under
    # ``runs/<child>/artifacts``. The parent's record, events and artifacts stay
    # immutable.
    assert Path(child.workspace) == Path(parent.workspace)

    child_home = state_home(store, child.run_id)
    assert child_home != parent_home
    assert child_home.is_dir()
    # The child's HOME holds the parent's own state (not a re-created empty one).
    assert (
        json.loads((child_home / "fake-acp-state.json").read_text(encoding="utf-8"))
        == parent_state
    )

    events = read_events(store, child.run_id)
    ready = payloads(events, "session_ready")[0]
    assert ready["harness"] == "acp"
    assert ready["sessionId"] == parent.session_id
    assert ready["resumed"] is True
    assert ready["resumeMethod"] == RESUME_METHOD
    # The resumed session keeps its own cwd: the same directory the parent's
    # session recorded, so the agent's remembered paths stay inside the run.
    assert ready["cwd"] == str(Path(parent.workspace))
    resumed = [
        payload
        for payload in payloads(events, "kernel_status")
        if payload.get("message") == "acp_session_resumed"
    ]
    assert len(resumed) == 1 and resumed[0]["resumeMethod"] == RESUME_METHOD

    entries = read_log(log)
    resume_entries = [entry for entry in entries if entry["event"] == "session_resume"]
    assert len(resume_entries) == 1
    assert resume_entries[0]["sessionId"] == parent.session_id
    # The agent really found the inherited state: it logged the state file's id.
    assert resume_entries[0]["stateSessionId"] == parent.session_id
    # The resume request carries the parent session's own cwd.
    assert resume_entries[0]["cwd"] == str(Path(parent.workspace))
    assert resume_entries[0]["mcpServers"] == []
    # Exactly one session was ever created: the parent's first turn.
    assert sum(1 for entry in entries if entry["event"] == "session_new") == 1

    # Continuity: the child's answer carries the first turn's codename, and the
    # child's own prompt never mentions it (so a fresh session could not answer).
    text = assistant_text(events)
    assert "REMEMBERED:ZETA-7319" in text
    child_prompts = [entry for entry in entries if entry["event"] == "prompt"]
    assert "ZETA-7319" not in str(child_prompts[-1]["text"])
    assert "ZETA-7319" not in child.prompt


def test_acp_followup_uses_session_load_and_drops_the_replayed_history(tmp_path: Path) -> None:
    log = tmp_path / "agent.jsonl"
    settings = {
        "FAKE_ACP_LOG": str(log),
        "FAKE_ACP_WRITE_REPORT": True,
        # Only the older top-level capability: the host must use session/load.
        "FAKE_ACP_CAPABILITIES": {"loadSession": True, "sessionCapabilities": {}},
        "FAKE_ACP_LOAD_REPLAY_UPDATES": 3,
    }
    _config, store, manager = build_manager(tmp_path, settings)
    parent = run_first_turn(manager, "第一轮：请记住代号 REMEMBER:OPAL-2846")

    child = manager.start("RECALL 复述代号", parent_run_id=parent.run_id)
    child_final = manager.wait(child.run_id, timeout=30)
    assert child_final is not None and child_final.status == FINISHED

    entries = read_log(log)
    load_entries = [entry for entry in entries if entry["event"] == "session_load"]
    assert len(load_entries) == 1
    assert load_entries[0]["stateSessionId"] == parent.session_id
    assert sum(1 for entry in entries if entry["event"] == "session_new") == 1

    events = read_events(store, child.run_id)
    ready = payloads(events, "session_ready")[0]
    assert ready["resumeMethod"] == LOAD_METHOD
    resumed = [
        payload
        for payload in payloads(events, "kernel_status")
        if payload.get("message") == "acp_session_resumed"
    ]
    assert resumed[0]["resumeMethod"] == LOAD_METHOD
    # The three replayed frames were dropped, and the drop is visible.
    assert resumed[0]["replayedUpdates"] == 3
    assert "REPLAYED-HISTORY" not in assistant_text(events)
    assert "REMEMBERED:OPAL-2846" in assistant_text(events)


def test_acp_followup_resume_error_fails_closed_without_falling_back(tmp_path: Path) -> None:
    log = tmp_path / "agent.jsonl"
    settings = {
        "FAKE_ACP_LOG": str(log),
        "FAKE_ACP_WRITE_REPORT": True,
        "FAKE_ACP_RESUME_ERROR": True,
    }
    _config, store, manager = build_manager(tmp_path, settings)
    parent = run_first_turn(manager, "第一轮")

    child = manager.start("追问", parent_run_id=parent.run_id)
    child_final = manager.wait(child.run_id, timeout=30)
    assert child_final is not None
    assert child_final.status == FAILED
    assert child_final.error and "resume refused by fake agent" in child_final.error
    # No session was ever recorded for the child: session_ready never happened.
    assert child_final.session_id is None
    events = read_events(store, child.run_id)
    assert payloads(events, "session_ready") == []

    entries = read_log(log)
    assert len([entry for entry in entries if entry["event"] == "session_resume"]) == 1
    # The single session/new belongs to the parent; the refused follow-up did
    # not get a fresh session instead.
    assert sum(1 for entry in entries if entry["event"] == "session_new") == 1


# -- state inheritance failures ----------------------------------------------


def test_acp_followup_refused_when_parent_state_root_is_gone(tmp_path: Path) -> None:
    settings = {"FAKE_ACP_WRITE_REPORT": True}
    _config, store, manager = build_manager(tmp_path, settings)
    parent = run_first_turn(manager, "第一轮")
    home = state_home(store, parent.run_id)
    for child in sorted(home.rglob("*"), reverse=True):
        if child.is_file():
            child.unlink()
    home.rmdir()

    before = len(store.list(limit=None))
    with pytest.raises(FollowupUnavailable) as excinfo:
        manager.start("追问", parent_run_id=parent.run_id)
    assert excinfo.value.code == "session_unavailable"
    # Refused at admission: nothing was created for the follow-up.
    assert len(store.list(limit=None)) == before


def test_acp_inheritance_bound_failure_refuses_the_followup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state too large to inherit refuses the follow-up, never truncates it."""
    log = tmp_path / "agent.jsonl"
    settings = {"FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True}
    _config, store, manager = build_manager(tmp_path, settings)
    parent = run_first_turn(manager, "第一轮")
    # Shrink the per-file bound so the test does not write 8 MiB: the constant
    # is read at call time, and the production value is asserted separately.
    monkeypatch.setattr("pi_glink.kernel.acp_env.INHERIT_MAX_FILE_BYTES", 1024)
    assert INHERIT_MAX_FILE_BYTES > 1024
    (state_home(store, parent.run_id) / "oversized.bin").write_bytes(b"x" * 4096)

    child = manager.start("追问", parent_run_id=parent.run_id)
    child_final = manager.wait(child.run_id, timeout=30)
    assert child_final is not None and child_final.status == FAILED
    assert child_final.error and "继承父 run 的 ACP 状态失败" in child_final.error
    events = read_events(store, child.run_id)
    assert payloads(events, "session_ready") == []
    # The agent was never asked to resume: the failure happened before start().
    assert [entry for entry in read_log(log) if entry["event"] == "session_resume"] == []


# -- the parent gate ----------------------------------------------------------


def test_acp_followup_refuses_a_parent_that_is_not_an_acp_run(tmp_path: Path) -> None:
    """A pi-shaped parent is invisible to the ACP continuation gate."""
    settings = {"FAKE_ACP_WRITE_REPORT": True}
    config, store, manager = build_manager(tmp_path, settings)
    record = store.create("pi 父 run", workspace_root=config.workspace_root)
    session_file = Path(config.session_dir) / record.run_id / "pi-session.jsonl"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text('{"type":"message"}\n', encoding="utf-8")
    store.update(record.run_id, session_id="pi-session", session_file=str(session_file))
    now = time.time()
    store.append_event(
        record.run_id,
        {
            "type": "session_ready",
            "runId": record.run_id,
            "seq": 1,
            "ts": now,
            "payload": {
                "sessionId": "pi-session",
                "sessionFile": str(session_file),
                "model": "test/model",
            },
        },
    )
    store.commit_terminal(
        record.run_id,
        {
            "type": "run_finished",
            "runId": record.run_id,
            "seq": 2,
            "ts": now,
            "payload": {"status": "finished", "reportStatus": "none"},
        },
    )

    before = len(store.list(limit=None))
    with pytest.raises(FollowupUnavailable) as excinfo:
        manager.start("追问", parent_run_id=record.run_id)
    assert excinfo.value.code == "session_unavailable"
    assert len(store.list(limit=None)) == before


def test_acp_followup_refuses_an_agent_without_a_continuation_capability(
    tmp_path: Path,
) -> None:
    settings = {
        "FAKE_ACP_WRITE_REPORT": True,
        "FAKE_ACP_CAPABILITIES": {"loadSession": False, "sessionCapabilities": {}},
    }
    _config, store, manager = build_manager(tmp_path, settings)
    parent = run_first_turn(manager, "第一轮")
    before = len(store.list(limit=None))
    with pytest.raises(FollowupUnavailable) as excinfo:
        manager.start("追问", parent_run_id=parent.run_id)
    assert excinfo.value.code == "session_unavailable"
    assert len(store.list(limit=None)) == before


def _fake_acp_adapter_factory(created: list[Any]):
    """A synthetic execution face that publishes an ACP-shaped session_ready.

    Used only by the tenant test: the gate reads the parent's recorded frames,
    so the parent must complete with the same shape a real ACP run publishes.
    """

    class FakeAcpAdapter(ExecutionAdapter):
        def __init__(self, spec, *, on_frame):
            super().__init__(spec, on_frame=on_frame)
            created.append(self)

        def start(self) -> None:
            workspace = Path(self.spec.workspace)
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "report.md").write_text("# 合成报告\n", encoding="utf-8")

        def deliver_input(self) -> None:
            pass

        def session_info(self):
            return SessionInfo(
                payload={
                    "harness": "acp",
                    "sessionId": "acp-parent-session",
                    "agentCapabilities": {"sessionCapabilities": {"resume": {}}},
                    "resumed": False,
                },
                session_id="acp-parent-session",
                session_file=None,
            )

        def wait(self, *, cancel, deadline):
            from pi_glink.kernel.execution import ExecutionOutcome

            return ExecutionOutcome(settled=True)

        def cancel(self) -> None:
            pass

        def recover_artifacts(self, destination):
            from pi_glink.kernel.execution import ArtifactRecovery

            return ArtifactRecovery()

        def close(self):
            from pi_glink.kernel.execution import CleanupReport

            return CleanupReport(released=True)

        def identity(self):
            return {}

    def build(spec, **kwargs):
        return FakeAcpAdapter(spec, on_frame=kwargs["on_frame"])

    return build


def test_acp_followup_refuses_a_parent_owned_by_another_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_file = tmp_path / "gateway.key"
    secret_file.write_bytes(SECRET)
    secret_file.chmod(0o600)
    tenants_file = tmp_path / "tenants.json"
    tenants_file.write_text(
        json.dumps(
            {
                "tenants": {
                    "tenant-alpha": {"modelApiKey": "sk-alpha-0000000000000000"},
                    "tenant-beta": {"modelApiKey": "sk-beta-0000000000000000"},
                }
            }
        ),
        encoding="utf-8",
    )
    tenants_file.chmod(0o600)
    values = {
        "OPENAI_API_KEY": "sk-host-global-00000000000000",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_PORT": "0",
        "LOCAL_WEB_EXECUTOR": "docker",
        "PI_GLINK_TENANCY": "multitenant",
        "PI_GLINK_GATEWAY_SECRET_FILE": str(secret_file),
        "PI_GLINK_TENANT_CONFIG_FILE": str(tenants_file),
        "PI_GLINK_TENANCY_AUDIENCE": AUDIENCE,
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent-web-search.json"),
        "LOCAL_WEB_TASK_TIMEOUT_SECONDS": "60",
        "PI_GLINK_HARNESS": "acp",
        "PI_GLINK_ACP_COMMAND": acp_command(tmp_path, {}),
    }
    config = build_config(values, REPO_ROOT)
    store = RunStore(config.data_root / "runs")
    credentials = TenantCredentialStore.from_file(tenants_file, repo_root=REPO_ROOT)
    created: list[Any] = []
    monkeypatch.setattr(
        "pi_glink.kernel.harness_acp.compose_adapter", _fake_acp_adapter_factory(created)
    )
    manager = RunManager(config, EventBus(), store, credential_store=credentials)
    alpha = TenantContext(
        tenant_id="tenant-alpha",
        user_id="user-a",
        role="member",
        audience=AUDIENCE,
        expires_at=time.time() + 60,
    )
    beta = TenantContext(
        tenant_id="tenant-beta",
        user_id="user-b",
        role="member",
        audience=AUDIENCE,
        expires_at=time.time() + 60,
    )
    parent = manager.start("第一轮", alpha)
    assert manager.wait(parent.run_id, timeout=30) is not None
    assert parent.tenant_id == "tenant-alpha"

    before = len(store.list(limit=None))
    with pytest.raises(FollowupUnavailable) as excinfo:
        manager.start("追问", beta, parent_run_id=parent.run_id)
    # The owner gate runs first: a foreign parent is invisible (unknown_run),
    # never a source of session, state or status.
    assert excinfo.value.code == "unknown_run"
    assert len(store.list(limit=None)) == before
    assert len(created) == 1  # no child adapter was ever composed


# -- the redaction boundary ---------------------------------------------------


def test_inherited_state_secrets_join_the_child_redaction_boundary(tmp_path: Path) -> None:
    """A credential-shaped value inherited with the state is masked in the child.

    The value is not one of the operator's configured credentials; it exists
    only in the parent's own state root, so the child's redactor can only know
    it through the inheritance scan. The fake agent echoes it back on the
    ``RECALL`` turn, which is where the mask must appear.
    """
    inherited_secret = "sk-live-inherited-credential-0123456789"
    log = tmp_path / "agent.jsonl"
    settings = {"FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True}
    _config, store, manager = build_manager(tmp_path, settings)
    parent = run_first_turn(
        manager, f"第一轮：请记住代号 REMEMBER:{inherited_secret}，不要写进报告"
    )
    assert inherited_secret in (state_home(store, parent.run_id) / "fake-acp-state.json").read_text(
        encoding="utf-8"
    )

    child = manager.start("RECALL 复述代号", parent_run_id=parent.run_id)
    child_final = manager.wait(child.run_id, timeout=30)
    assert child_final is not None and child_final.status == FINISHED

    raw_events = store.events_path(child.run_id).read_text(encoding="utf-8")
    assert inherited_secret not in raw_events
    assert "[REDACTED]" in raw_events
    # The value never reaches the child's stream in readable form.
    assert inherited_secret not in assistant_text(read_events(store, child.run_id))


# -- run 记录落 harness/executor/agent 事实 + 追问门禁比字段 ----------------------


def craft_parent(
    store: RunStore,
    run_id: str,
    *,
    harness: str | None = None,
    executor: str | None = None,
    frame_marker: bool = True,
    state_root: str = "home",
    session_id: str | None = SESSION_ID,
    capabilities: Mapping[str, Any] | None = None,
) -> Any:
    """手写一份 ACP 追问门禁能消费的父记录（不真的执行、不经过 create）。

    手写 meta.json 而不用 ``store.create`` 是为了让红→绿机制落在行为上：
    旧代码读回时把 harness/executor 当未知键过滤、帧扫描仍兼作族证明，
    于是「字段证明族」「按父记录 executor 推状态根」两类测试在旧代码上
    因拒绝路径不同而变红，而不是死在 create 的签名差异（TypeError）上。

    ``frame_marker=False`` 模拟能力帧缺 ``harness: "acp"`` 标记；
    ``harness=None`` / ``executor=None`` 模拟本次改动之前写入的 legacy 记录。
    """
    directory = store.run_dir(run_id)
    meta: dict[str, Any] = {
        "run_id": run_id,
        "prompt": "第一轮",
        "status": FINISHED,
        "created_at": time.time(),
        "workspace": str(directory / "workspace"),
        "session_id": session_id,
        "finished_at": time.time(),
    }
    if harness is not None:
        meta["harness"] = harness
    if executor is not None:
        meta["executor"] = executor
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    (directory / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    payload: dict[str, Any] = {
        "sessionId": session_id,
        "agentCapabilities": (
            capabilities
            if capabilities is not None
            else {"sessionCapabilities": {"resume": {}}}
        ),
    }
    if frame_marker:
        payload["harness"] = "acp"
    store.append_event(run_id, {"type": "session_ready", "seq": 1, "payload": payload})
    root = directory / "acp-agent" / state_root
    root.mkdir(parents=True, exist_ok=True)
    return store.get(run_id)


def test_agent_identity_is_written_back_at_session_ready(tmp_path: Path) -> None:
    """ACP 协商到的 ``agentInfo.name`` 在 session_ready 提交点写回记录。

    与 ``session_id`` 同一先例：启动后才知道的事实不进 create。红→绿机制：
    旧代码 ``RunRecord`` 无 agent 字段（AttributeError），``store.update``
    也会以未知字段拒绝（ValueError）。
    """
    _config, store, manager = build_manager(tmp_path, {"FAKE_ACP_WRITE_REPORT": True})
    record = run_first_turn(manager, "第一轮：记住代号 REMEMBER:AGENT-1，然后写报告")
    final = store.get(record.run_id)

    assert final.harness == "acp"
    assert final.executor == "inproc"
    assert final.agent == "fake-acp"  # fake_acp 协商结果里的 agentInfo.name


def test_agent_identity_is_redacted_and_capped_on_writeback(tmp_path: Path) -> None:
    """内核自报的 agent 身份过 run 脱敏边界并限幅后才落 meta.json。

    与事件载荷同一边界：``agentInfo.name`` 里拼进模型凭据时写回值必须被
    掩码，超长名字截断到 128 字符。红→绿机制：旧代码原样写回——凭据
    子串仍在（掩码断言变红）且无长度上限（长度断言变红）。
    """
    secret = "sk-agent-writeback-secret-0001"
    advertised = f"pre-{secret}-" + "x" * 200
    _config, store, manager = build_manager(
        tmp_path,
        {"FAKE_ACP_WRITE_REPORT": True, "FAKE_ACP_AGENT_NAME": advertised},
        OPENAI_API_KEY=secret,
    )
    record = run_first_turn(manager, "第一轮：记住代号 REMEMBER:AGENT-2，然后写报告")
    final = store.get(record.run_id)

    assert final.agent is not None
    assert secret not in final.agent, "写回的 agent 身份必须过 run 的脱敏边界"
    assert final.agent.startswith("pre-")
    assert len(final.agent) <= 128


def test_family_proof_prefers_the_record_field_over_the_frame_marker(
    tmp_path: Path,
) -> None:
    """父记录 harness="acp" 但事件流无标记帧（能力帧保留）→ 仍受理。

    能力矩阵仍只能来自 ``session_ready`` 帧；帧扫描不再兼作族证明。
    红→绿机制：旧代码的扫描要求帧自带 ``harness: "acp"`` 标记，扫不到标
    记返回 None → ``session_unavailable``，本测试因意外拒绝而变红。
    """
    _config, store, manager = build_manager(tmp_path, {})
    parent = craft_parent(
        store, "fieldproof01", harness="acp", executor="inproc", frame_marker=False
    )
    admitted = manager._resumable_parent(None, parent.run_id)  # noqa: SLF001
    manager._family.check_continuation(admitted)  # noqa: SLF001
    assert admitted.run_id == parent.run_id


def test_a_field_proven_parent_without_a_capability_frame_is_still_refused(
    tmp_path: Path,
) -> None:
    """已知字段的 ACP 父 run 扫不到续接通道 → 仍按无通道拒绝（语义不变）。

    帧扫描此时只是能力来源：族已由记录字段证明，但能力矩阵只能来自帧，
    空矩阵 = 无续接通道。新旧代码同拒绝，本测试钉住这一不变式。
    """
    _config, store, manager = build_manager(tmp_path, {})
    parent = craft_parent(
        store,
        "nocap0000001",
        harness="acp",
        executor="inproc",
        frame_marker=False,
        capabilities={},  # 无续接通道的能力矩阵
    )
    with pytest.raises(FollowupUnavailable) as excinfo:
        manager._family.check_continuation(  # noqa: SLF001
            manager._resumable_parent(None, parent.run_id)  # noqa: SLF001
        )
    assert excinfo.value.code == "session_unavailable"


def test_state_home_follows_the_parent_recorded_executor(tmp_path: Path) -> None:
    """宿主从 e2b 切到 inproc 后，e2b 父 run 的合法追问仍受理。

    状态根按父 run 自己记录的 executor 推导到 ``acp-agent/recovered``。
    红→绿机制：旧代码用当前配置推导出 ``acp-agent/home``（不存在）→
    ``session_unavailable``，本测试因误拒而变红。
    """
    config, store, manager = build_manager(tmp_path, {})
    assert config.executor == "inproc"
    parent = craft_parent(
        store,
        "e2bparent001",
        harness="acp",
        executor="e2b",
        state_root="recovered",
    )
    state_home = manager._family.state_home(parent)  # noqa: SLF001
    assert state_home == store.run_dir(parent.run_id) / "acp-agent" / "recovered"
    admitted = manager._resumable_parent(None, parent.run_id)  # noqa: SLF001
    manager._family.check_continuation(admitted)  # noqa: SLF001
    assert admitted.run_id == parent.run_id


def test_legacy_parent_keeps_the_frame_marker_family_proof(tmp_path: Path) -> None:
    """legacy 规则 1a：无 harness 字段 + 帧带 ``harness:"acp"`` 标记 → 受理。

    帧扫描为 legacy 记录兼作族证明的路径逐断言保留。行为断言（受理）在旧
    代码上同绿；``parent.harness is None`` 的准备断言访问新字段，在旧代码
    上因 AttributeError 变红——红在字段不存在，不是行为差异。
    """
    _config, store, manager = build_manager(tmp_path, {})
    parent = craft_parent(store, "legacyparent", frame_marker=True)
    assert parent.harness is None
    admitted = manager._resumable_parent(None, parent.run_id)  # noqa: SLF001
    manager._family.check_continuation(admitted)  # noqa: SLF001
    assert admitted.run_id == parent.run_id


def test_legacy_parent_without_the_marker_is_still_refused(tmp_path: Path) -> None:
    """legacy 规则 1b：无 harness 字段 + 帧无标记 → session_unavailable（原行为）。"""
    _config, store, manager = build_manager(tmp_path, {})
    parent = craft_parent(store, "legacyparent", frame_marker=False)
    assert parent.harness is None
    with pytest.raises(FollowupUnavailable) as excinfo:
        manager._family.check_continuation(  # noqa: SLF001
            manager._resumable_parent(None, parent.run_id)  # noqa: SLF001
        )
    assert excinfo.value.code == "session_unavailable"


def test_legacy_parent_state_home_falls_back_to_the_current_executor(
    tmp_path: Path,
) -> None:
    """legacy 规则 3：无 executor 字段 → 按当前 config.executor 推导（原行为）。"""
    _config, store, manager = build_manager(tmp_path, {}, LOCAL_WEB_EXECUTOR="e2b")
    parent = craft_parent(store, "legacyparent")
    assert parent.executor is None
    state_home = manager._family.state_home(parent)  # noqa: SLF001
    assert state_home == store.run_dir(parent.run_id) / "acp-agent" / "recovered"
