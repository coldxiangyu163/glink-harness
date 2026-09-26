"""Regressions for the E2B execution face (no cloud calls).

pi E2B 面自 GLINK-ARCH-3 收敛起走 RPC 通道：pi 以 ``pi --mode rpc`` 作为沙
箱 background 命令（``stdin=True``）启动，沙箱 handle 经
``SandboxCommandProcess`` 包装后注入与其余两面共享的 ``PiRpcClient``——
只有载体变化，协议驱动、投影与终态判定复用同一套代码。这里的假沙箱
（tests/e2b_fake.py）以真实子进程跑 tests/fixtures/fake_pi.py，正是
tests/test_kernel_acp_e2b.py 的 FakeAgentHandle 先例。

They pin the behaviours the completion audit flagged:

* the credential read from ``.env`` reaches ``Sandbox.create`` (``api_key``);
* a run streams its RPC frames and pulls the report back into the host workspace;
* a non-zero sandbox exit is a failure even when a report file exists.

They also pin the delivery contract added with the architecture hardening:
a verifiable configuration manifest, the guarded/quoted spawn command chain
(the prompt travels over stdin, never the command line), and bounded/safe
artifact recovery. New semantics from the RPC convergence: 断流即失败（不补
发、不伪造完整性）、取消是协议级 abort（沙箱存活到收尾，被取消的 run 也
能回收 report.md）、续接身份在回合前的 ``session_ready`` 即验证。
"""

from __future__ import annotations

import hashlib
import json
import shlex
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping

import pytest

from e2b_fake import BrokenHandle, FakeSandbox, IdleHandle
from pi_glink.config import build_config
from pi_glink.events import EventBus
from pi_glink.kernel import agent_env, e2b_env
from pi_glink.kernel.acp_e2b_env import SandboxCommandProcess
from pi_glink.kernel.e2b_env import (
    ARTIFACT_MAX_BYTES,
    SANDBOX_PACKAGE_GUARD,
    SANDBOX_SESSIONS,
    SANDBOX_WORKSPACE,
    SUBAGENT_RECOVER_MAX_FILE_BYTES,
    E2BSandboxClient,
    _read_file_bounded,
)
from pi_glink.kernel.execution import E2BSandboxRpcExecutionAdapter, ExecutionSpec
from pi_glink.kernel.rpc import PiRpcClient
from pi_glink.kernel.runner import RunManager
from pi_glink.store import RunStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_config(tmp_path: Path, **overrides: str):
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_EXECUTOR": "e2b",
        "E2B_API_KEY": "e2b-secret-key",
        "LOCAL_WEB_E2B_TEMPLATE": "pi-glink-worker",
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent-web-search.json"),
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


def _prepare(config, tmp_path: Path, name: str = "run-1"):
    run_dir = tmp_path / "runs" / name
    return agent_env.prepare_agent_dir(
        config,
        runtime_dir=run_dir / "pi-agent",
        session_dir=run_dir / "sessions",
    )


def wait_for(predicate, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# -- credential plumbing ------------------------------------------------------


def test_e2b_key_reaches_create_and_model_key_stays_out_of_env(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    captured: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeSandbox:
        captured.update(kwargs)
        return FakeSandbox()

    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=factory
    )
    client.start()
    assert captured["template"] == "pi-glink-worker"
    # The *E2B* key authenticates the sandbox API…
    assert captured["api_key"] == "e2b-secret-key"
    # …while the *model* key travels in auth.json, never as an env var.
    assert "OPENAI_API_KEY" not in captured["envs"]
    client.close()


def test_missing_credential_is_refused_before_any_cloud_call(tmp_path: Path) -> None:
    config = make_config(tmp_path, E2B_API_KEY="")
    runtime = _prepare(config, tmp_path)
    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=lambda **_: FakeSandbox()
    )
    with pytest.raises(Exception) as excinfo:
        client.start()
    assert "E2B_API_KEY" in str(excinfo.value)
    assert config.has_e2b_key is False


def test_config_repr_hides_the_cloud_credential(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    assert "e2b-secret-key" not in repr(config)
    assert config.has_e2b_key is True


# -- configuration delivery ---------------------------------------------------


def test_sandbox_followup_uploads_and_recovers_a_private_session(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    sandbox.session_files = ("continued.jsonl",)
    # The main session is identified by its header (a ``type: "session"`` line
    # without ``parentSession``), matching what pi actually writes.
    session_bytes = (
        b'{"type":"session","version":3,"id":"same-session","timestamp":"t","cwd":"/home/user/workspace"}\n'
    )
    sandbox.readable["/home/user/sessions/continued.jsonl"] = session_bytes
    runtime.session_dir.mkdir(parents=True, exist_ok=True)
    (runtime.session_dir / "continued.jsonl").write_bytes(session_bytes)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "report.md").write_text("# previous report", encoding="utf-8")
    client = E2BSandboxClient(config, runtime, run_id="child", workspace=workspace,
                              sandbox_factory=lambda **_: sandbox)
    client.start()
    client.upload_config()
    client.upload_continuation("continued.jsonl", workspace)
    assert sandbox.written["/home/user/sessions/continued.jsonl"] == session_bytes.decode(
        "utf-8"
    )
    assert sandbox.written["/home/user/workspace/report.md"] == "# previous report"
    # The continuation argv names the uploaded session at its sandbox path, in
    # the same ``--session <path>`` shape the in-process and Docker faces use.
    process = client.spawn_rpc(
        ["pi", "--mode", "rpc", "--session", f"{SANDBOX_SESSIONS}/continued.jsonl"]
    )
    try:
        assert "--session /home/user/sessions/continued.jsonl" in sandbox.command
    finally:
        process.kill()
    saved, error = client.recover_session()
    assert error is None
    assert saved == runtime.session_dir / "continued.jsonl"
    assert saved.read_bytes() == session_bytes
    client.close()


def test_sandbox_session_recovery_rejects_a_path_escape(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    sandbox.session_files = ("../outside.jsonl",)
    client = E2BSandboxClient(config, runtime, run_id="root", workspace=tmp_path / "ws",
                              sandbox_factory=lambda **_: sandbox)
    client.start()
    path, error = client.recover_session()
    assert path is None and error
    assert not (tmp_path / "outside.jsonl").exists()


def _session_bytes(session_id: str, *, parent: str | None = None) -> bytes:
    """A pi session file's first line, as pi actually writes it."""
    header: dict[str, Any] = {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": "2026-09-24T10:00:00.000Z",
        "cwd": "/home/user/workspace",
    }
    if parent is not None:
        header["parentSession"] = parent
    return (json.dumps(header) + "\n").encode("utf-8")


def _recovery_client(config, runtime, sandbox, tmp_path):
    client = E2BSandboxClient(
        config, runtime, run_id="root", workspace=tmp_path / "ws",
        sandbox_factory=lambda **_: sandbox,
    )
    client.start()
    return client


def test_recover_session_selects_the_main_session_among_subagent_branches(
    tmp_path: Path,
) -> None:
    """A subagent fork shares the session dir; only its header tells it apart."""
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    main = _session_bytes("main-session")
    sandbox.session_files = ("research.jsonl", "2026-09-24T10-05-00-000Z_branch.jsonl")
    sandbox.readable["/home/user/sessions/research.jsonl"] = main
    sandbox.readable["/home/user/sessions/2026-09-24T10-05-00-000Z_branch.jsonl"] = (
        _session_bytes("branch-session", parent="/home/user/sessions/research.jsonl")
    )
    client = _recovery_client(config, runtime, sandbox, tmp_path)
    saved, error = client.recover_session()
    assert error is None
    assert saved == runtime.session_dir / "research.jsonl"
    assert saved.read_bytes() == main


def test_recover_session_ignores_the_subagent_artifacts_subdirectory(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    main = _session_bytes("main-session")
    sandbox.session_files = ("continued.jsonl", "subagent-artifacts")
    sandbox.entry_types["/home/user/sessions/subagent-artifacts"] = "dir"
    sandbox.readable["/home/user/sessions/continued.jsonl"] = main
    client = _recovery_client(config, runtime, sandbox, tmp_path)
    saved, error = client.recover_session()
    assert error is None
    assert saved == runtime.session_dir / "continued.jsonl"
    assert saved.read_bytes() == main


def test_recover_session_refuses_two_main_candidates(tmp_path: Path) -> None:
    """Two header-mains cannot be told apart; never guess by name or recency."""
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    sandbox.session_files = ("a.jsonl", "b.jsonl")
    sandbox.readable["/home/user/sessions/a.jsonl"] = _session_bytes("one")
    sandbox.readable["/home/user/sessions/b.jsonl"] = _session_bytes("two")
    client = _recovery_client(config, runtime, sandbox, tmp_path)
    saved, error = client.recover_session()
    assert saved is None and error


def test_recover_session_refuses_when_only_branches_remain(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    sandbox.session_files = ("branch.jsonl",)
    sandbox.readable["/home/user/sessions/branch.jsonl"] = _session_bytes(
        "branch-session", parent="/home/user/sessions/gone.jsonl"
    )
    client = _recovery_client(config, runtime, sandbox, tmp_path)
    saved, error = client.recover_session()
    assert saved is None and error


def test_recover_session_refuses_an_unreadable_candidate(tmp_path: Path) -> None:
    """A listed file that cannot even be sniffed refuses the follow-up."""
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    sandbox.session_files = ("research.jsonl", "branch.jsonl")
    sandbox.readable["/home/user/sessions/research.jsonl"] = _session_bytes("main")
    # branch.jsonl is listed but not readable: no guessing past it.
    client = _recovery_client(config, runtime, sandbox, tmp_path)
    saved, error = client.recover_session()
    assert saved is None and error
    assert "branch.jsonl" in error


def test_read_file_bounded_refuses_a_full_load_without_size_or_stream() -> None:
    """SDK 既不提供文件大小也不支持流式读取时，拒绝整份载入（不猜）。

    ``_read_file_bounded`` 的这个分支是 recover_session/recover_artifacts/
    recover_subagent_files 四处回收的共同前置限幅：只有整读 ``read()`` 的
    SDK（或替身）让限幅无从执行，必须如实失败，而不是载入后才发现超限。
    """

    class _WholeReadOnlyFiles:
        def get_info(self, path: str) -> object:
            return SimpleNamespace(size=None)

        def read(self, path: str) -> bytes:
            return b"x" * 1024

    class _WholeReadOnlySandbox:
        files = _WholeReadOnlyFiles()

    data, reason = _read_file_bounded(_WholeReadOnlySandbox(), "/home/user/x", 16)
    assert data is None
    assert reason is not None and "拒绝整份载入" in reason


# -- subagent mirror -----------------------------------------------------------


def test_recover_subagent_files_mirrors_branches_and_artifacts(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    sandbox.session_files = ("research.jsonl", "branch.jsonl")
    sandbox.readable["/home/user/sessions/research.jsonl"] = _session_bytes("main")
    branch = _session_bytes("branch", parent="/home/user/sessions/research.jsonl")
    sandbox.readable["/home/user/sessions/branch.jsonl"] = branch
    sandbox.artifact_files = ("r1_researcher_0_output.md", "nested/r1_meta.json")
    sandbox.readable["/home/user/sessions/subagent-artifacts/r1_researcher_0_output.md"] = (
        b"# notes\n"
    )
    sandbox.readable["/home/user/sessions/subagent-artifacts/nested/r1_meta.json"] = b"{}\n"
    client = _recovery_client(config, runtime, sandbox, tmp_path)
    destination = tmp_path / "run" / "subagents"

    files, failures = client.recover_subagent_files(destination)

    assert failures == []
    assert (destination / "sessions" / "branch.jsonl").read_bytes() == branch
    assert (destination / "artifacts" / "r1_researcher_0_output.md").read_bytes() == b"# notes\n"
    assert (destination / "artifacts" / "nested" / "r1_meta.json").read_bytes() == b"{}\n"
    # The main session is left to recover_session, never duplicated here.
    assert not (destination / "sessions" / "research.jsonl").exists()
    assert len(files) == 3


def test_recover_subagent_files_skips_links_and_records_failures(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    sandbox.session_files = ("research.jsonl",)
    sandbox.readable["/home/user/sessions/research.jsonl"] = _session_bytes("main")
    sandbox.artifact_files = ("linked.md", "unreadable.md")
    sandbox.entry_types["/home/user/sessions/subagent-artifacts/linked.md"] = "symlink"
    # unreadable.md is listed but has no content behind it.
    client = _recovery_client(config, runtime, sandbox, tmp_path)
    destination = tmp_path / "run" / "subagents"

    files, failures = client.recover_subagent_files(destination)

    assert files == []
    # A link is skipped (never followed, not a failure); an unreadable regular
    # file is reported.
    assert not (destination / "artifacts" / "linked.md").exists()
    assert len(failures) == 1 and "unreadable.md" in failures[0]


def test_recover_subagent_files_enforces_the_single_file_cap(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    sandbox.session_files = ("research.jsonl",)
    sandbox.readable["/home/user/sessions/research.jsonl"] = _session_bytes("main")
    sandbox.artifact_files = ("huge.md",)
    sandbox.readable["/home/user/sessions/subagent-artifacts/huge.md"] = b"x"
    sandbox.sizes["/home/user/sessions/subagent-artifacts/huge.md"] = (
        SUBAGENT_RECOVER_MAX_FILE_BYTES + 1
    )
    client = _recovery_client(config, runtime, sandbox, tmp_path)
    destination = tmp_path / "run" / "subagents"

    files, failures = client.recover_subagent_files(destination)

    assert files == []
    assert len(failures) == 1 and "huge.md" in failures[0]
    assert not (destination / "artifacts" / "huge.md").exists()


def test_e2b_run_mirrors_and_redacts_subagent_files(tmp_path: Path) -> None:
    """Through the runner: the mirror lands in the run dir, swept by the redactor."""
    secret = "sk-subagent-echo-000111222333"
    config = make_config(tmp_path, OPENAI_API_KEY=secret)
    branch = _session_bytes("branch", parent="/home/user/sessions/research.jsonl")
    branch += f'{{"type":"message","text":"the key {secret} was here"}}\n'.encode("utf-8")
    sandbox = FakeSandbox(
        tmp_path / "sandbox", report=None, fake_pi_env={"FAKE_PI_WRITE_REPORT": "1"}
    )
    sandbox.session_files = ("research.jsonl", "branch.jsonl")
    sandbox.readable["/home/user/sessions/research.jsonl"] = _session_bytes("main")
    sandbox.readable["/home/user/sessions/branch.jsonl"] = branch
    sandbox.artifact_files = ("r1_researcher_0_output.md",)
    sandbox.readable["/home/user/sessions/subagent-artifacts/r1_researcher_0_output.md"] = (
        f"notes mentioning {secret}\n".encode("utf-8")
    )
    manager, storage = _manager(config, sandbox, tmp_path)

    record = manager.start("研究")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == "finished"
    subagents_dir = storage.run_dir(record.run_id) / "subagents"
    mirrored_branch = (subagents_dir / "sessions" / "branch.jsonl").read_text("utf-8")
    mirrored_notes = (subagents_dir / "artifacts" / "r1_researcher_0_output.md").read_text(
        "utf-8"
    )
    assert secret not in mirrored_branch and "[REDACTED]" in mirrored_branch
    assert secret not in mirrored_notes
    terminal = _event_kinds(storage, record.run_id)[-1]
    assert terminal == "run_finished"
    last_frame = json.loads(
        (storage.run_dir(record.run_id) / "events.jsonl").read_text("utf-8").strip().splitlines()[-1]
    )
    assert last_frame["payload"].get("artifactFailures", []) == []


def test_upload_config_delivers_the_runtime_directory_with_a_manifest(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=lambda **_: sandbox
    )
    client.start()
    client.upload_config()

    assert "/home/user/config/models.json" in sandbox.written
    assert "/home/user/config/settings.json" in sandbox.written
    assert "/home/user/config/auth.json" in sandbox.written
    expected_dirs = {
        "/home/user/config", "/home/user/workspace", "/home/user/sessions",
        *(f"/home/user/config/{entry.relative_to(runtime.directory).as_posix()}"
          for entry in runtime.directory.rglob("*") if entry.is_dir()),
    }
    assert set(sandbox.dirs) == expected_dirs
    assert len(sandbox.dirs) == len(expected_dirs)

    # The manifest names every delivered path with its content digest, so the
    # delivery is verifiable instead of "the directory happened to be empty".
    manifest = dict(client.upload_manifest)
    for relative in ("models.json", "settings.json", "auth.json"):
        assert relative in manifest
        digest = hashlib.sha256((runtime.directory / relative).read_bytes()).hexdigest()
        assert manifest[relative] == digest
    assert client.uploaded, "the uploaded path list must not be empty"
    client.close()


def test_upload_manifest_covers_reviewable_template_entries(tmp_path: Path) -> None:
    """Extensions/skills/prompts are delivered with the same path semantics."""
    template = tmp_path / "template"
    (template / "extensions").mkdir(parents=True)
    (template / "settings.json").write_text('{"defaultThinkingLevel": "high"}\n', encoding="utf-8")
    (template / "AGENTS.md").write_text("# agent\n", encoding="utf-8")
    (template / "extensions" / "hello.js").write_text("export default () => {};\n", encoding="utf-8")
    (template / "prompts").mkdir()
    (template / "prompts" / "research.md").write_text("prompt\n", encoding="utf-8")
    config = make_config(tmp_path, PI_CODING_AGENT_DIR=str(template))
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=lambda **_: sandbox
    )
    client.start()
    client.upload_config()

    delivered = {relative for relative, _digest in client.upload_manifest}
    assert {"AGENTS.md", "settings.json", "extensions/hello.js", "prompts/research.md"} <= delivered
    assert "/home/user/config/extensions/hello.js" in sandbox.written
    assert "/home/user/config/prompts/research.md" in sandbox.written
    client.close()


# -- sandbox run (RPC channel) --------------------------------------------------


def _manager(config, sandbox, tmp_path: Path):
    storage = RunStore(config.data_root / "runs")
    manager = RunManager(
        config, EventBus(), store=storage, sandbox_factory=lambda **_: sandbox
    )
    return manager, storage


def _event_kinds(storage: RunStore, run_id: str) -> list[str]:
    path = storage.events_path(run_id)
    if not path.is_file():
        return []
    return [
        json.loads(line)["type"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_run_replays_frames_and_pulls_the_report(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    sandbox = FakeSandbox(
        tmp_path / "sandbox", report=None, fake_pi_env={"FAKE_PI_WRITE_REPORT": "1"}
    )
    manager, storage = _manager(config, sandbox, tmp_path)
    record = manager.start("把结果写成 report.md")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == "finished"
    assert final.report_path is not None
    report_text = Path(final.report_path).read_text(encoding="utf-8")
    assert "研究报告（fake pi）" in report_text and "把结果写成 report.md" in report_text
    # The served report is the host-owned artifact copy inside the run directory.
    assert Path(final.report_path).is_relative_to(storage.run_dir(record.run_id))
    assert final.report_status == "ok"

    kinds = _event_kinds(storage, record.run_id)
    assert "assistant_delta" in kinds
    assert "tool_started" in kinds and "tool_finished" in kinds
    assert kinds[-1] == "run_finished"
    assert sandbox.killed is True, "the sandbox must be destroyed after the run"
    # Neither the prompt nor the model credential travels through the sandbox env.
    assert "OPENAI_API_KEY" not in sandbox.command_envs
    assert "把结果写成 report.md" not in sandbox.command
    # The prompt travels as a stdin ``prompt`` frame, never on the command line.
    prompts = [frame for frame in sandbox.stdin_frames if frame.get("type") == "prompt"]
    assert prompts and prompts[-1].get("message") == "把结果写成 report.md"
    # The sandbox identity is durable for restart reconciliation.
    assert final.sandbox_id == "fake-sandbox"


def test_sandbox_followup_through_runner_keeps_session_and_run_boundaries(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    parent_session = (
        b'{"type":"session","version":3,"id":"same-session","timestamp":"t1","cwd":"/home/user/workspace"}\n'
    )
    child_session = (
        b'{"type":"session","version":3,"id":"same-session","timestamp":"t2","cwd":"/home/user/workspace"}\n'
    )
    first = FakeSandbox(
        tmp_path / "sb1",
        report=None,
        fake_pi_env={"FAKE_PI_WRITE_REPORT": "1", "FAKE_PI_SESSION_ID": "same-session"},
    )
    first.session_files = ("research.jsonl",)
    first.readable["/home/user/sessions/research.jsonl"] = parent_session
    second = FakeSandbox(
        tmp_path / "sb2", report=None, fake_pi_env={"FAKE_PI_SESSION_ID": "same-session"}
    )
    second.session_files = ("continued.jsonl",)
    second.readable["/home/user/sessions/continued.jsonl"] = child_session
    sandboxes = iter([first, second])
    storage = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store=storage, sandbox_factory=lambda **_: next(sandboxes))
    parent = manager.start("初始问题")
    original = manager.wait(parent.run_id, timeout=30)
    assert original.status == "finished" and original.session_id == "same-session"
    assert Path(original.session_file).read_bytes() == first.readable["/home/user/sessions/research.jsonl"]
    deadline = time.monotonic() + 5
    while manager.active_run_id is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.active_run_id is None
    child = manager.start("追问细节", parent_run_id=parent.run_id)
    final = manager.wait(child.run_id, timeout=30)
    assert final.status == "finished" and final.parent_run_id == parent.run_id
    assert final.report_path is None, "an unchanged seeded report is not a new deliverable"
    assert final.session_id == original.session_id
    assert "--session /home/user/sessions/continued.jsonl" in second.command
    assert second.written["/home/user/sessions/continued.jsonl"] == parent_session.decode(
        "utf-8"
    )
    parent_report = (Path(original.workspace) / "report.md").read_text(encoding="utf-8")
    assert second.written["/home/user/workspace/report.md"] == parent_report
    assert Path(original.session_file).read_bytes() == first.readable["/home/user/sessions/research.jsonl"]
    assert Path(final.session_file).read_bytes() == second.readable["/home/user/sessions/continued.jsonl"]
    assert _event_kinds(storage, parent.run_id)[-1] == "run_finished"
    child_kinds = _event_kinds(storage, child.run_id)
    assert child_kinds[-1] == "run_finished"
    # 续接身份在回合前验证（三面同规则）：session_ready 先于回合帧发布。
    assert child_kinds.index("session_ready") < child_kinds.index("turn_started")
    prompts = [frame for frame in second.stdin_frames if frame.get("type") == "prompt"]
    assert prompts and prompts[-1].get("message") == "追问细节"


def test_nonzero_exit_fails_the_run_even_with_a_report(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    # fake pi 接受 prompt、写下报告后以非零码退出（不报告 agent_settled）：
    # returncode 经 SandboxCommandProcess 到达客户端，run 以 kernel_exit 收场。
    sandbox = FakeSandbox(
        tmp_path / "sandbox", report=None, fake_pi_env={"FAKE_PI_WRITE_REPORT": "1"}
    )
    manager, storage = _manager(config, sandbox, tmp_path)
    record = manager.start("强制失败 CRASH")
    final = manager.wait(record.run_id, timeout=30)
    assert final.status == "failed"
    assert final.stop_reason == "kernel_exit", "真实非零退出是 kernel_exit，不是 execution_error"
    assert final.error is not None and "exit" in final.error.lower()
    kinds = _event_kinds(storage, record.run_id)
    assert kinds[-1] == "run_failed"


# -- cancellation (protocol-level abort) -----------------------------------------
#
# 新义务（GLINK-ARCH-3 收敛）：取消是协议级 abort——宿主向运行中的 pi 发 RPC
# ``abort`` 命令，沙箱存活到收尾，因此被取消的 run 也能回收 report.md 与主
# 会话。原义务（旧 ``--mode json`` 路径运行中不能 abort，cancel 只能直接销
# 毁沙箱，report 与主会话随沙箱即毁）已随旧适配器删除。


def test_stop_during_a_sandbox_run_aborts_the_kernel_then_cancels(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    sandbox = FakeSandbox(
        tmp_path / "sandbox", report=None, fake_pi_env={"FAKE_PI_WRITE_REPORT": "1"}
    )
    manager, storage = _manager(config, sandbox, tmp_path)
    record = manager.start("永不结束 NOSETTLE 的研究")
    assert wait_for(lambda: "assistant_delta" in _event_kinds(storage, record.run_id)), (
        "the kernel never streamed"
    )
    assert manager.stop(record.run_id).accepted is True

    final = manager.wait(record.run_id, timeout=30)
    assert final is not None, "the run never reached a terminal state"
    assert final.status == "cancelled", final.error
    assert final.stop_reason == "abort"
    kinds = _event_kinds(storage, record.run_id)
    assert kinds[-1] == "run_cancelled"
    assert "run_failed" not in kinds and "run_finished" not in kinds
    # 取消经 RPC abort 到达运行中的 pi，而不是只能销毁沙箱（对旧路径红：
    # 旧面没有 abort 通道，cancel 即毁沙箱）。
    assert any(frame.get("type") == "abort" for frame in sandbox.stdin_frames)
    assert sandbox.killed is True, "a cancelled run must still destroy its sandbox"
    # 沙箱存活到收尾：被取消的 run 也能回收 report.md（行为改善）。
    assert final.report_path is not None


def test_stop_that_lands_before_the_sandbox_starts_is_still_cancelled(
    tmp_path: Path,
) -> None:
    """A stop during ``Sandbox.create`` returns the sandbox and returns normally.

    ``stop()`` only closes the client once it has a sandbox; if the stop lands
    while the factory is still running, the runner must notice the cancellation
    after ``start()`` and end as cancelled without prompting.
    """
    config = make_config(tmp_path)
    sandbox = FakeSandbox(tmp_path / "sandbox", report=None)
    created = threading.Event()
    release = threading.Event()

    def factory(**_: Any) -> FakeSandbox:
        created.set()
        assert release.wait(timeout=10), "the factory was never released"
        return sandbox

    manager, storage = _manager(config, sandbox, tmp_path)
    manager._sandbox_factory = factory  # noqa: SLF001 - the injected face
    record = manager.start("沙箱启动前就取消")
    assert created.wait(timeout=10), "the sandbox factory was never called"
    assert manager.stop(record.run_id).accepted is True
    release.set()

    final = manager.wait(record.run_id, timeout=30)
    assert final is not None, "the run never reached a terminal state"
    assert final.status == "cancelled", final.error
    assert final.stop_reason == "abort"
    kinds = _event_kinds(storage, record.run_id)
    assert kinds[-1] == "run_cancelled"
    assert not any(frame.get("type") == "prompt" for frame in sandbox.stdin_frames), (
        "a cancelled run must not prompt the kernel"
    )
    assert sandbox.killed is True, "a cancelled run must still destroy its sandbox"


# -- stream loss: 断流即失败 -----------------------------------------------------
#
# RPC 通道没有第二份事件副本（旧 tee 路径的 events.jsonl 权威已删）：沙箱流
# 断开时 run 判 execution_error——不补发、不做 get_state 对账、不伪造完整
# 性。已交付的帧保留在事件日志（它们真实到达过）。


def test_a_dropped_stream_fails_the_run_without_replaying(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    # 交付到第一个 toolcall 帧后流断开：此前帧真实到达，此后尾巴不存在于
    # 任何权威副本（对旧路径红：旧面会从 events.jsonl 补齐并 finished）。
    sandbox = FakeSandbox(
        tmp_path / "sandbox",
        report=None,
        fake_pi_env={"FAKE_PI_WRITE_REPORT": "1"},
        stream_break_marker=b"toolcall_start",
    )
    manager, storage = _manager(config, sandbox, tmp_path)
    record = manager.start("断流的研究")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == "failed"
    assert final.stop_reason == "execution_error", final.error
    assert final.error is not None and "沙箱流断开" in final.error
    assert "不补发" in final.error and "不伪造完整性" in final.error
    kinds = _event_kinds(storage, record.run_id)
    assert kinds[-1] == "run_failed"
    # 已交付的帧保留；断点之后的回合收尾不存在于事件日志。
    assert "assistant_delta" in kinds
    assert "turn_finished" not in kinds
    # 断流已判定失败：close 不再把「无退出码」谎报为资源未释放（沙箱
    # kill 才是释放权威），cleanup 如实 released。
    assert final.cleanup is not None and final.cleanup["released"] is True
    assert sandbox.killed is True


# -- continuation identity: 回合前验证 -------------------------------------------


def test_a_followup_with_a_mismatched_session_is_refused_before_the_prompt(
    tmp_path: Path,
) -> None:
    """RPC get_state 在 session_ready 即知会话身份：不符在回合前拒收。

    对旧路径红：旧 e2b 面流中才发现 session id，只能回合后由驱动线程比对
    ``expected_session_id``——第一次模型调用已经发生。
    """
    config = make_config(tmp_path)
    parent_session = (
        b'{"type":"session","version":3,"id":"same-session","timestamp":"t1","cwd":"/home/user/workspace"}\n'
    )
    first = FakeSandbox(
        tmp_path / "sb1",
        report=None,
        fake_pi_env={"FAKE_PI_WRITE_REPORT": "1", "FAKE_PI_SESSION_ID": "same-session"},
    )
    first.session_files = ("research.jsonl",)
    first.readable["/home/user/sessions/research.jsonl"] = parent_session
    # The follow-up's kernel reports a *different* session id: the refusal
    # must land before any prompt frame reaches the sandbox.
    second = FakeSandbox(
        tmp_path / "sb2", report=None, fake_pi_env={"FAKE_PI_SESSION_ID": "other-session"}
    )
    second.session_files = ("continued.jsonl",)
    second.readable["/home/user/sessions/continued.jsonl"] = (
        b'{"type":"session","version":3,"id":"same-session","timestamp":"t2","cwd":"/home/user/workspace"}\n'
    )
    sandboxes = iter([first, second])
    storage = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store=storage, sandbox_factory=lambda **_: next(sandboxes))
    parent = manager.start("初始问题")
    original = manager.wait(parent.run_id, timeout=30)
    assert original.status == "finished" and original.session_id == "same-session"
    deadline = time.monotonic() + 5
    while manager.active_run_id is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager.active_run_id is None

    child = manager.start("追问", parent_run_id=parent.run_id)
    final = manager.wait(child.run_id, timeout=30)

    assert final.status == "failed"
    assert "session_unavailable" in (final.error or "")
    child_kinds = _event_kinds(storage, child.run_id)
    assert "turn_started" not in child_kinds, "回合前拒收：回合从未开始"
    assert not any(frame.get("type") == "prompt" for frame in second.stdin_frames), (
        "追问身份不符时不得发出 prompt 帧"
    )
    assert second.killed is True


# -- spawn command chain ---------------------------------------------------------


def test_spawn_rpc_composes_the_guarded_command_chain(tmp_path: Path) -> None:
    """沙箱面同样从镜像内路径加载包；模板早于宿主/缺包时必须明确失败。

    E2B 没有 docker 的 ``--entrypoint``，所以守卫是命令链的一环：先验证
    守卫脚本存在（旧模板立刻 exit 78），再由守卫验证包存在，然后才 exec pi。
    """
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=lambda **_: sandbox
    )
    client.start()
    argv = ["pi", "--mode", "rpc", "--provider", "fake", "--model", "fake/model"]
    process = client.spawn_rpc(argv)
    try:
        command = sandbox.command
        guard = SANDBOX_PACKAGE_GUARD
        assert f"test -x {guard}" in command
        assert "exit 78" in command
        assert command.index(f"test -x {guard}") < command.index(f"exec {guard}")
        assert f"cd {SANDBOX_WORKSPACE} && exec {shlex.join([guard, *argv])}" in command
        kwargs = sandbox.command_kwargs
        assert kwargs["background"] is True and kwargs["stdin"] is True
        assert kwargs["cwd"] == SANDBOX_WORKSPACE
        assert kwargs["timeout"] == client.sandbox_timeout
        assert "OPENAI_API_KEY" not in sandbox.command_envs
        settings = json.loads((runtime.directory / "settings.json").read_text(encoding="utf-8"))
        assert settings["packages"] == [
            f"{agent_env.IMAGE_PACKAGE_NODE_MODULES}/pi-web-access",
            f"{agent_env.IMAGE_PACKAGE_NODE_MODULES}/pi-subagents",
        ]
    finally:
        process.kill()
        client.close()


def test_spawn_rpc_quotes_hostile_argv_and_keeps_the_prompt_off_the_command_line(
    tmp_path: Path,
) -> None:
    """``--append-system-prompt`` 文本作为一个 argv 词存活；prompt 走 stdin。

    原义务：prompt/system 经 base64 编码进命令行。新义务：prompt 本体走
    stdin 的 ``prompt`` 帧，从不出现在命令行；argv 里的文本由
    ``shlex.join`` 引用，逐词回解析等于原 argv。
    """
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=lambda **_: sandbox
    )
    client.start()
    hostile = "系统提示 with 'quotes' and $(rm -rf /) `whoami` \\ backslash\n第二行  line"
    argv = ["pi", "--mode", "rpc", "--append-system-prompt", hostile]
    process = client.spawn_rpc(argv)
    try:
        command = sandbox.command
        assert shlex.quote(hostile) in command, "the text must survive as one quoted word"
        # The fake parsed the chain back into an argv: quoting cannot have
        # silently split or re-joined a word.
        assert sandbox.agent_argv == argv
        assert "把结果写成 report.md" not in command
        assert "base64" not in command, "旧的 base64 编码机制已整体废除"
    finally:
        process.kill()
        client.close()


# -- artifact recovery --------------------------------------------------------


def test_sandbox_lifetime_is_bounded_by_configuration(tmp_path: Path) -> None:
    """The configured ceiling wins over the adapter's larger bound.

    E2B's SDK path exposes no CPU/memory controls, so lifetime is the one
    resource limit the host can hand the cloud provider; it never claims more
    than the configuration allows.
    """
    config = make_config(tmp_path, LOCAL_WEB_E2B_TIMEOUT_SECONDS="120")
    runtime = _prepare(config, tmp_path)
    captured: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeSandbox:
        captured.update(kwargs)
        return FakeSandbox()

    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=factory
    )
    client.start(timeout_seconds=99_999)
    assert captured["timeout"] == 120
    client.close()


def test_sandbox_lifetime_defaults_to_the_task_timeout_plus_headroom(tmp_path: Path) -> None:
    config = make_config(tmp_path, LOCAL_WEB_TASK_TIMEOUT_SECONDS="900")
    runtime = _prepare(config, tmp_path)
    captured: dict[str, Any] = {}

    def factory(**kwargs: Any) -> FakeSandbox:
        captured.update(kwargs)
        return FakeSandbox()

    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=factory
    )
    client.start()
    assert captured["timeout"] == 1200
    client.close()


def test_the_kernel_command_is_allowed_to_run_for_the_whole_sandbox_lifetime(
    tmp_path: Path,
) -> None:
    """The SDK default of 60 s must never be what bounds a research run.

    A real run (retrieval + synthesis + report) takes minutes, while the sandbox
    is created with the run's full budget. Leaving ``timeout`` unset let E2B kill
    the kernel after 60 seconds and the run failed with ``TimeoutException`` even
    though the sandbox was healthy; the host therefore passes the sandbox's own
    lifetime explicitly.
    """
    config = make_config(tmp_path, LOCAL_WEB_TASK_TIMEOUT_SECONDS="900")
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=lambda **_: sandbox
    )
    client.start()
    process = client.spawn_rpc(["pi", "--mode", "rpc"])
    try:
        assert sandbox.command_timeout is not None, "the SDK default (60 s) must not be left in place"
        # The command may run as long as the sandbox it runs in; never longer.
        assert sandbox.command_timeout == client.sandbox_timeout == 1200
    finally:
        process.kill()
        client.close()


def test_the_command_timeout_follows_a_tighter_configured_sandbox_ceiling(
    tmp_path: Path,
) -> None:
    config = make_config(
        tmp_path, LOCAL_WEB_TASK_TIMEOUT_SECONDS="900", LOCAL_WEB_E2B_TIMEOUT_SECONDS="300"
    )
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=lambda **_: sandbox
    )
    client.start()
    process = client.spawn_rpc(["pi", "--mode", "rpc"])
    try:
        assert sandbox.command_timeout == 300
    finally:
        process.kill()
        client.close()


def test_recovered_artifacts_are_bounded_and_path_safe(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox(workspace_files=("report.md", "notes.md", "../escape.md", "huge.bin"))
    sandbox.readable["/home/user/workspace/report.md"] = "# 报告\n"
    sandbox.readable["/home/user/workspace/notes.md"] = "记录\n"
    sandbox.readable["/home/user/workspace/../escape.md"] = "越界\n"
    sandbox.readable["/home/user/workspace/huge.bin"] = b"x" * (ARTIFACT_MAX_BYTES + 1)
    client = E2BSandboxClient(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws", sandbox_factory=lambda **_: sandbox
    )
    client.start()
    files, failures = client.recover_artifacts(tmp_path / "ws")

    names = {path.name for path in files}
    assert {"report.md", "notes.md"} <= names
    assert not (tmp_path / "escape.md").exists(), "a traversal name must not escape"
    assert any("非法产物名" in failure for failure in failures)
    assert any("上限" in failure for failure in failures)
    client.close()


# -- bounded reads ---------------------------------------------------------------


def test_artifacts_are_bounded_by_listing_metadata_before_reading(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    sandbox = FakeSandbox(workspace_files=("report.md", "huge.bin"))
    sandbox.readable["/home/user/workspace/report.md"] = "# 报告\n"
    sandbox.readable["/home/user/workspace/huge.bin"] = b"small"
    sandbox.sizes["/home/user/workspace/huge.bin"] = ARTIFACT_MAX_BYTES + 1  # metadata lies, read must not
    sandbox.reads.clear()
    client = E2BSandboxClient(
        config,
        _prepare(config, tmp_path),
        run_id="abc123",
        workspace=tmp_path / "ws",
        sandbox_factory=lambda **_: sandbox,
    )
    client.start()
    files, failures = client.recover_artifacts(tmp_path / "ws")
    client.close()

    assert {path.name for path in files} == {"report.md"}
    assert any("上限" in failure for failure in failures)
    assert "/home/user/workspace/huge.bin" not in sandbox.reads


def _recover_from(sandbox: FakeSandbox, tmp_path: Path):
    config = make_config(tmp_path)
    client = E2BSandboxClient(
        config,
        _prepare(config, tmp_path),
        run_id="abc123",
        workspace=tmp_path / "ws",
        sandbox_factory=lambda **_: sandbox,
    )
    client.start()
    try:
        return client.recover_artifacts(tmp_path / "ws")
    finally:
        client.close()


def test_directories_and_symlinks_are_skipped_without_a_false_failure(tmp_path: Path) -> None:
    """The audit blocker: a directory entry must not become an artifact failure.

    The real SDK reports ``EntryInfo.type`` as the ``FileType`` enum, so an
    agent that wrote a ``notes/`` directory (very common) must not turn every
    E2B run's terminal record into a false alarm.
    """
    sandbox = FakeSandbox(workspace_files=("report.md", "notes", "linked.md"))
    sandbox.readable["/home/user/workspace/report.md"] = "# 报告\n"
    sandbox.entry_types["/home/user/workspace/notes"] = "dir"
    sandbox.entry_types["/home/user/workspace/linked.md"] = "symlink"
    sandbox.reads.clear()

    files, failures = _recover_from(sandbox, tmp_path)

    assert {path.name for path in files} == {"report.md"}
    assert failures == [], "目录/符号链接不是产物的失败，不得写进终态 artifactFailures"
    assert "/home/user/workspace/notes" not in sandbox.reads, "目录不得被读取"
    assert "/home/user/workspace/linked.md" not in sandbox.reads, "链接不得被跟随"


def test_the_real_sdk_file_type_enum_is_understood(tmp_path: Path) -> None:
    e2b = pytest.importorskip("e2b")
    from e2b import FileType  # noqa: PLC0415 - optional dependency

    assert FileType.DIR.value == "dir" and str(FileType.DIR) == "FileType.DIR"
    sandbox = FakeSandbox(workspace_files=("report.md", "notes", "linked.md"))
    sandbox.readable["/home/user/workspace/report.md"] = "# 报告\n"
    sandbox.entry_types["/home/user/workspace/notes"] = FileType.DIR
    sandbox.entry_types["/home/user/workspace/linked.md"] = FileType.SYMLINK

    files, failures = _recover_from(sandbox, tmp_path)

    assert {path.name for path in files} == {"report.md"}
    assert failures == []
    assert sandbox.reads.count("/home/user/workspace/report.md") >= 1
    assert "/home/user/workspace/notes" not in sandbox.reads
    assert e2b is not None


# -- adapter unit tests (E2BSandboxRpcExecutionAdapter) ---------------------------


def _adapter_spec(config, runtime, tmp_path: Path, **overrides: Any) -> ExecutionSpec:
    values: dict[str, Any] = {
        "config": config,
        "runtime": runtime,
        "run_id": "abc123",
        "prompt": "合成问题",
        "system_prompt": "",
        "workspace": tmp_path / "ws",
        "timeout_seconds": 30,
    }
    values.update(overrides)
    return ExecutionSpec(**values)


def test_start_records_the_sandbox_identity_before_uploading(tmp_path: Path) -> None:
    """身份在上传之前落盘：部分失败也留下可点名的资源。"""
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    identities: list[dict[str, str]] = []
    written_at_identity: list[int] = []

    def on_identity(identity: Mapping[str, str]) -> None:
        identities.append(dict(identity))
        written_at_identity.append(len(sandbox.written))

    adapter = E2BSandboxRpcExecutionAdapter(
        _adapter_spec(config, runtime, tmp_path),
        argv=["pi", "--mode", "rpc"],
        client_factory=PiRpcClient,
        on_frame=lambda frame: None,
        sandbox_factory=lambda **_: sandbox,
        on_identity=on_identity,
    )
    adapter.start()
    try:
        assert identities == [{"sandbox": "fake-sandbox"}]
        assert written_at_identity == [0], "身份落盘之前不得有任何上传"
        assert sandbox.written, "upload_config 在身份落盘之后运行"
    finally:
        adapter.close()


def test_popen_injection_reaches_the_shared_rpc_client(tmp_path: Path) -> None:
    """spawn_rpc 的进程门面经 ``popen=`` 注入共享的 PiRpcClient。"""
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    captured: dict[str, Any] = {}

    def spy_factory(argv, **kwargs):
        captured.update(kwargs)
        return PiRpcClient(argv, **kwargs)

    adapter = E2BSandboxRpcExecutionAdapter(
        _adapter_spec(config, runtime, tmp_path),
        argv=["pi", "--mode", "rpc"],
        client_factory=spy_factory,
        on_frame=lambda frame: None,
        sandbox_factory=lambda **_: sandbox,
    )
    adapter.start()
    try:
        popen = captured.get("popen")
        assert popen is not None, "E2B 面必须把进程门面经 popen= 注入"
        process = popen("ignored-argv")
        assert isinstance(process, SandboxCommandProcess)
        assert process.sandbox_id == "fake-sandbox"
        # session_info 包装：get_state 载荷上追加 sandboxId/template。
        session = adapter.session_info()
        assert session is not None
        assert session.payload["sandboxId"] == "fake-sandbox"
        assert session.payload["template"] == "pi-glink-worker"
    finally:
        adapter.close()


def test_abort_settle_grace_does_not_change_the_cancelled_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """宽限只等收尾落盘，不改终态判定；pi 不理会 abort 时宽限到期照常返回。"""
    import pi_glink.kernel.execution as execution_module

    monkeypatch.setattr(execution_module, "ABORT_SETTLE_GRACE_SECONDS", 0.2)
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    sandbox = FakeSandbox()
    requests: list[str] = []

    class _Client:
        """A client whose kernel never settles after the abort command."""

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            pass

        @property
        def running(self) -> bool:
            return True

        def request(self, command: Mapping[str, Any], *, timeout: float = 30.0):
            requests.append(str(command.get("type")))
            return SimpleNamespace(success=True, data={}, error=None)

        def close(self, *, timeout: float = 5.0) -> int:
            return 0

    adapter = E2BSandboxRpcExecutionAdapter(
        _adapter_spec(config, runtime, tmp_path),
        argv=["pi", "--mode", "rpc"],
        client_factory=_Client,
        on_frame=lambda frame: None,
        sandbox_factory=lambda **_: sandbox,
    )
    started_at = time.monotonic()
    adapter.start()
    outcome = adapter.wait(cancel=lambda: True, deadline=time.monotonic() + 60)
    elapsed = time.monotonic() - started_at

    assert outcome.cancelled is True and outcome.stop_reason == "abort"
    assert "abort" in requests
    assert elapsed < 10, "宽限到期照常返回，不拖住终态"
    report = adapter.close()
    assert report.released is True, report.failures


def test_stream_loss_oserror_in_close_is_a_log_not_a_cleanup_failure(tmp_path: Path) -> None:
    """断流后 ``SandboxCommandProcess.wait()`` 的 OSError 在 close 内降级。

    沙箱 ``kill()`` 才是释放权威：一次已判定失败的断流不再被谎报为资源未
    释放——OSError 降级为日志，不进 ``CleanupReport.failures``。
    """
    config = make_config(tmp_path)
    runtime = _prepare(config, tmp_path)
    # get_state 的响应帧还没收完整流就断了：pump 以「无退出码」收场。
    sandbox = FakeSandbox(tmp_path / "sandbox", stream_break_marker=b"get_state")
    adapter = E2BSandboxRpcExecutionAdapter(
        _adapter_spec(config, runtime, tmp_path),
        argv=["pi", "--mode", "rpc"],
        client_factory=PiRpcClient,
        on_frame=lambda frame: None,
        sandbox_factory=lambda **_: sandbox,
    )
    adapter.start()
    process = adapter._process  # noqa: SLF001 - 测试钉死断流事实
    # fake pi 沉默到收到命令：get_state 触发那半条响应帧，流随即断开。
    assert adapter.session_info() is None
    assert wait_for(lambda: process.stream_error is not None), "the stream never broke"

    report = adapter.close()

    assert report.released is True, report.failures
    assert report.failures == ()
    assert sandbox.killed is True


def test_returncode_mirrors_the_popen_shape(tmp_path: Path) -> None:
    """``subprocess.Popen`` 形状：运行中 None，退出后是状态，断流无码为 -1。"""
    idle = SandboxCommandProcess(IdleHandle())
    try:
        assert idle.returncode is None, "运行中不得伪装已退出"
        assert idle.poll() is None
    finally:
        idle.kill()
    assert idle.wait(timeout=10) == 0
    assert idle.returncode == 0

    exited = SandboxCommandProcess(IdleHandle(exit_code=3, blocked=False))
    assert exited.wait(timeout=10) == 3
    assert exited.returncode == 3

    broken = SandboxCommandProcess(BrokenHandle())
    with pytest.raises(OSError):
        broken.wait(timeout=10)
    assert broken.poll() == -1, "断流不得伪装成活进程或真实退出码"
    assert broken.returncode == -1
    assert broken.stream_error is not None
