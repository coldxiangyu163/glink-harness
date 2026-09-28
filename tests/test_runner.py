"""Regressions for run orchestration, driven by the fake pi protocol stand-in."""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping

import pytest

from pi_glink.config import HostConfig, build_config

from acp_pack import apply_acp_pack
from pi_glink.events import EventBus
from pi_glink.kernel.rpc import PiRpcClient

from e2b_fake import FakeSandbox
from pi_glink.kernel.execution import (
    ArtifactRecovery,
    CleanupReport,
    ExecutionAdapter,
    ExecutionOutcome,
    SessionInfo,
)
from pi_glink.kernel.harness import Verdict
from pi_glink.runs.runner import (
    FollowupUnavailable,
    RunBusy,
    RunManager,
    UnsupportedCommand,
    parse_blueprint_command,
)
from pi_glink.store import CANCELLED, FAILED, FINISHED, RUNNING, RunStore, RunSpec
from pi_glink.tenancy import TenantContext
from store_helpers import events_text, finish, stored_record

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_PI = Path(__file__).resolve().parent / "fixtures" / "fake_pi.py"


def make_config(tmp_path: Path, **overrides: str) -> HostConfig:
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
    }
    values.update(overrides)
    apply_acp_pack(values, tmp_path)
    return build_config(values, REPO_ROOT)


def fake_factory(*, write_report: bool):
    """Replace the pi binary with the protocol stand-in, keep the argv shape."""

    def factory(argv, *, cwd=None, env=None, on_event=None):
        merged = dict(env or {})
        merged["FAKE_PI_WRITE_REPORT"] = "1" if write_report else "0"
        return PiRpcClient(
            [sys.executable, str(FAKE_PI), *argv[1:]],
            cwd=cwd,
            env=merged,
            on_event=on_event,
        )

    return factory


def build_manager(tmp_path: Path, *, write_report: bool = False, **overrides: str):
    config = make_config(tmp_path, **overrides)
    bus = EventBus()
    store = RunStore(config.data_root / "runs")
    manager = RunManager(
        config, bus, store, client_factory=fake_factory(write_report=write_report)
    )
    return manager, store


def read_events(store: RunStore, run_id: str) -> list[Mapping[str, Any]]:
    text = events_text(store, run_id)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def wait_for(predicate, *, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# -- happy path ---------------------------------------------------------------


def test_run_produces_report_and_a_complete_event_log(tmp_path: Path) -> None:
    manager, store = build_manager(tmp_path, write_report=True)
    record = manager.start("用一句话说明这个仓库是做什么的")
    final = manager.wait(record.run_id, timeout=30)

    assert final is not None
    assert final.status == FINISHED
    assert final.report_path is not None and Path(final.report_path).is_file()
    assert "fake pi" in Path(final.report_path).read_text(encoding="utf-8")
    # Identity comes from pi's own state, not from guessing files.
    assert final.session_id == "fake-session"
    assert manager.active_run_id is None

    events = read_events(store, record.run_id)
    types = [event["type"] for event in events]
    assert types[0] == "run_started"
    assert events[0]["payload"]["prompt"].startswith("用一句话")
    assert types[-1] == "run_finished"
    assert "session_ready" in types
    assert "assistant_delta" in types
    assert "tool_started" in types and "tool_finished" in types
    assert "usage_reported" in types
    assert final.event_count == len(events)


def test_report_path_is_recorded_in_run_metadata(tmp_path: Path) -> None:
    manager, store = build_manager(tmp_path, write_report=True)
    record = manager.start("生成报告")
    final = manager.wait(record.run_id, timeout=30)
    meta = stored_record(store, record.run_id)
    assert meta["report_path"] == final.report_path
    assert meta["status"] == FINISHED
    assert meta["session_file"] == "/tmp/fake-session.jsonl"


def test_missing_report_is_reported_not_hidden(tmp_path: Path) -> None:
    manager, _store = build_manager(tmp_path, write_report=False)
    record = manager.start("这次不会写报告")
    final = manager.wait(record.run_id, timeout=30)
    assert final.status == FINISHED
    assert final.report_path is None
    assert final.stop_reason == "no_report"


# -- lifecycle ----------------------------------------------------------------


def test_second_run_is_refused_while_one_is_active(tmp_path: Path) -> None:
    manager, store = build_manager(tmp_path, write_report=True)
    record = manager.start("第一个 run NOSETTLE 让它挂住")
    assert wait_for(
        lambda: any(event["type"] == "session_ready" for event in read_events(store, record.run_id))
    )

    with pytest.raises(RunBusy) as excinfo:
        manager.start("第二个 run")
    assert excinfo.value.active_run_id == record.run_id

    assert manager.stop(record.run_id).accepted is True
    final = manager.wait(record.run_id, timeout=30)
    assert final.status == CANCELLED
    assert final.stop_reason == "abort"
    assert manager.active_run_id is None


def test_timeout_marks_the_run_failed(tmp_path: Path) -> None:
    manager, _store = build_manager(
        tmp_path, write_report=False, LOCAL_WEB_TASK_TIMEOUT_SECONDS="1"
    )
    record = manager.start("这一轮永远不结束 NOSETTLE")
    final = manager.wait(record.run_id, timeout=30)
    assert final.status == FAILED
    assert final.stop_reason == "timeout"
    assert final.error is not None and "settle" in final.error


def test_stop_on_unknown_run_is_a_no_op(tmp_path: Path) -> None:
    manager, _store = build_manager(tmp_path)
    decision = manager.stop("does-not-exist")
    assert decision.accepted is False
    assert decision.status is None and decision.reason == "unknown"


def test_docker_session_file_is_mapped_back_to_the_host_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit regression: a container ``/sessions/…`` path must be read on the host.

    pi reports container paths on the Docker face while the file itself lives
    under the run's mounted session directory. Reading the raw container path
    found nothing and the closing sanitise pass returned silently, leaving a
    tool-echoed credential in the host session log.
    """
    monkeypatch.setattr("pi_glink.kernel.sandbox.docker_available", lambda: True)

    secret = "sk-test-0123456789abcdef"
    config = make_config(tmp_path, LOCAL_WEB_EXECUTOR="docker", OPENAI_API_KEY=secret)
    session_files: list[Path] = []

    def factory(argv, *, cwd=None, env=None, on_event=None):
        merged = dict(env or {})
        merged["FAKE_PI_WRITE_REPORT"] = "0"
        merged["FAKE_PI_SESSION_FILE"] = "/sessions/fake-session.jsonl"
        # pi writes its own session log into the mounted directory; the stand-in
        # does the same. The run's directory is identified the way the runtime
        # does it: by the run id carried in the container/name arguments.
        match = re.search(r"pi-glink-([0-9a-f]{12})", " ".join(argv))
        assert match is not None, argv
        session_dir = config.data_root / "runs" / match.group(1) / "sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / "fake-session.jsonl"
        session_file.write_text(
            f'{{"role":"tool","text":"OPENAI_API_KEY={secret}"}}\n', encoding="utf-8"
        )
        session_files.append(session_file)
        return PiRpcClient(
            [sys.executable, str(FAKE_PI)], cwd=cwd, env=merged, on_event=on_event
        )

    bus = EventBus()
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, bus, store, client_factory=factory)
    record = manager.start("docker 会话文件映射回归")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == FINISHED
    # The stored path points at the run's own host session file, and it was sanitized.
    assert len(session_files) == 1
    assert final.session_file == str(session_files[0])
    text = session_files[0].read_text(encoding="utf-8")
    assert secret not in text
    assert "[REDACTED]" in text
    assert final.redacted is True


def test_subagent_files_in_the_session_dir_are_sanitized_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Branched sessions and subagent artifacts share the session directory on
    the host-side faces; the closing sweep covers them, not just the main file."""
    monkeypatch.setattr("pi_glink.kernel.sandbox.docker_available", lambda: True)

    secret = "sk-subagent-0123456789abcdef"
    config = make_config(tmp_path, LOCAL_WEB_EXECUTOR="docker", OPENAI_API_KEY=secret)
    written: dict[str, Path] = {}

    def factory(argv, *, cwd=None, env=None, on_event=None):
        merged = dict(env or {})
        merged["FAKE_PI_WRITE_REPORT"] = "0"
        merged["FAKE_PI_SESSION_FILE"] = "/sessions/fake-session.jsonl"
        match = re.search(r"pi-glink-([0-9a-f]{12})", " ".join(argv))
        assert match is not None, argv
        session_dir = config.data_root / "runs" / match.group(1) / "sessions"
        (session_dir / "subagent-artifacts").mkdir(parents=True, exist_ok=True)
        (session_dir / "fake-session.jsonl").write_text(
            f'{{"role":"tool","text":"OPENAI_API_KEY={secret}"}}\n', encoding="utf-8"
        )
        # A subagent fork's branched session and its notes echo the same value.
        (session_dir / "branch.jsonl").write_text(
            f'{{"role":"assistant","text":"using {secret}"}}\n', encoding="utf-8"
        )
        (session_dir / "subagent-artifacts" / "r1_output.md").write_text(
            f"notes with {secret}\n", encoding="utf-8"
        )
        written["main"] = session_dir / "fake-session.jsonl"
        written["branch"] = session_dir / "branch.jsonl"
        written["notes"] = session_dir / "subagent-artifacts" / "r1_output.md"
        return PiRpcClient(
            [sys.executable, str(FAKE_PI)], cwd=cwd, env=merged, on_event=on_event
        )

    bus = EventBus()
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, bus, store, client_factory=factory)
    record = manager.start("子代理会话目录清扫回归")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == FINISHED
    for key in ("main", "branch", "notes"):
        text = written[key].read_text(encoding="utf-8")
        assert secret not in text, f"{key} was not swept"
        assert "[REDACTED]" in text


# -- model result vs execution settle ------------------------------------------

@pytest.mark.parametrize(
    ("marker", "stop_reason"),
    [("MODELERROR", "model_error"), ("MODELABORT", "aborted")],
)
def test_a_settled_but_errored_model_message_fails_the_run(
    tmp_path: Path, marker: str, stop_reason: str
) -> None:
    """Audit regression: settled → finished even though the model errored.

    ``agent_settled`` only means pi will not continue automatically; the final
    assistant message's ``stopReason`` is the model result and decides the run.
    """
    manager, store = build_manager(tmp_path, write_report=False)
    record = manager.start(f"{marker} 让模型以错误结束")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == FAILED, "a settled error must not be reported as success"
    assert final.stop_reason == stop_reason
    assert final.model_stop_reason in {"error", "aborted"}
    assert final.error
    events = read_events(store, record.run_id)
    assert events[-1]["type"] == "run_failed"
    assert not any(event["type"] == "run_finished" for event in events)


def test_length_truncation_is_recorded_as_an_incomplete_delivery(tmp_path: Path) -> None:
    """A truncated answer is finished execution but not a verified full delivery."""
    manager, store = build_manager(tmp_path, write_report=True)
    record = manager.start("MODELLENGTH 输出被截断")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == FINISHED
    assert final.model_stop_reason == "length"
    assert final.stop_reason == "length"
    terminal = read_events(store, record.run_id)[-1]
    assert terminal["type"] == "run_finished"
    assert terminal["payload"]["modelStopReason"] == "length"


def test_a_recoverable_error_then_a_successful_retry_finishes(tmp_path: Path) -> None:
    """An intermediate failure must not permanently fail the run."""
    manager, store = build_manager(tmp_path, write_report=True)
    record = manager.start("RETRYTHENOK 先失败后重试成功")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == FINISHED, final.error
    assert final.model_stop_reason == "stop"
    events = read_events(store, record.run_id)
    assert events[-1]["type"] == "run_finished"
    kinds = [event["type"] for event in events]
    assert "usage_reported" in kinds


def test_exhausted_retries_fail_with_the_final_error(tmp_path: Path) -> None:
    """A retry that never succeeds must still end as a failure."""
    manager, store = build_manager(tmp_path, write_report=True)
    record = manager.start("RETRYTHENFAIL 重试耗尽")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == FAILED
    assert final.stop_reason == "model_error"
    assert final.error and "after 3 attempts" in final.error
    assert read_events(store, record.run_id)[-1]["type"] == "run_failed"


def test_prompt_reaches_the_kernel_verbatim_and_never_through_argv(tmp_path: Path) -> None:
    """Unicode, quotes and newlines cross the RPC frame, not the command line."""
    prompt = '检查 "引号"、$(命令)、`反引号`、换行\n第二段 \u2028 更长的中文'
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    captured: dict[str, object] = {"argv": None, "messages": []}

    def factory(argv, *, cwd=None, env=None, on_event=None):
        captured["argv"] = list(argv)
        merged = dict(env or {})
        merged["FAKE_PI_WRITE_REPORT"] = "1"
        client = PiRpcClient(
            [sys.executable, str(FAKE_PI), *argv[1:]],
            cwd=cwd,
            env=merged,
            on_event=on_event,
        )
        original_request = client.request

        def request(command, **kwargs):
            if command.get("type") == "prompt":
                captured["messages"].append(str(command.get("message", "")))
            return original_request(command, **kwargs)

        client.request = request  # type: ignore[method-assign]
        return client

    manager = RunManager(config, EventBus(), store, client_factory=factory)
    record = manager.start(prompt)
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == FINISHED
    assert captured["messages"] == [prompt], "the prompt must arrive byte for byte"
    argv = captured["argv"] or []
    assert prompt not in " ".join(argv), (
        "an instruction with shell metacharacters must never be argv"
    )
    # The research contract and the pack's capability prompt travel as one
    # appended system prompt, read from repository files (offline smoke for the
    # `--append-system-prompt` path).
    assert "--append-system-prompt" in argv
    appended = argv[argv.index("--append-system-prompt") + 1]
    contract = (REPO_ROOT / "methodology" / "work-agreement.md").read_text(encoding="utf-8").strip()
    capabilities = (REPO_ROOT / "harnesses" / "pi" / "capabilities.md").read_text(encoding="utf-8").strip()
    assert contract in appended, "the rendered research prompt must reach the kernel verbatim"
    assert capabilities in appended, "the pack capability prompt must reach the kernel verbatim"


# -- 斜杠命令门禁（GLINK-ARCH-6：宿主解释蓝图命令） ---------------------------


def _run_dirs(config: HostConfig) -> list[str]:
    runs = config.data_root / "runs"
    return sorted(path.name for path in runs.glob("*") if path.is_dir()) if runs.is_dir() else []


def _write_blueprint(directory: Path, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        '+++\nformat = "pi-glink-blueprint/v1"\n'
        f'name = "{name}"\ndescription = "合成蓝图"\n\n'
        '[[stages]]\nid = "one"\ntitle = "阶段"\ngoal = "目标"\n'
        'deliverable = "产出"\ncheckpoint = "检查点"\n+++\n\n正文。\n',
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "text",
    [
        "/run researcher --bg 查跨境资金监管",
        "/subagents-detach",
        "   /chain scout -> planner",
        "\n\t/parallel researcher --bg",
        "/",
        # 名字精确且大小写敏感：这些既不是蓝图，也不是普通文本。
        "/DeepResearch 题目",
        "/deepresearch2 题目",
        "/skill:pi-subagents",
        # 题目本身仍以 / 开头：重写后的 prompt 绝不能带着 / 进内核。
        "/deepresearch /run --bg",
    ],
)
def test_the_command_gate_refuses_before_any_run_exists(tmp_path: Path, text: str) -> None:
    """pi 会把以 / 开头的消息交给扩展命令；宿主只解释 /<蓝图名> <题目>。

    原义务（GLINK-SUB-02）：只放行从模板目录推导出的模板命令。
    新义务（GLINK-ARCH-6）：宿主自己解释 `/<蓝图名> <题目>`（蓝图名从
    `methodology/blueprints/` 推导），其余 `/` 开头输入一律拒绝；拒绝必须发生在
    凭据解析、父会话读取、run 目录与执行面创建之前。
    """
    manager, _store = build_manager(tmp_path)
    with pytest.raises(UnsupportedCommand) as excinfo:
        manager.start(text)
    assert excinfo.value.allowed == ("deepresearch",)
    assert _run_dirs(manager.config) == [], "拒绝必须早于 run 目录创建"


def test_a_bare_blueprint_command_has_no_topic(tmp_path: Path) -> None:
    manager, _store = build_manager(tmp_path)
    with pytest.raises(ValueError, match="must not be empty"):
        manager.start("/deepresearch")
    assert _run_dirs(manager.config) == []


def test_a_blueprint_cannot_be_given_twice(tmp_path: Path) -> None:
    manager, _store = build_manager(tmp_path)
    with pytest.raises(ValueError, match="不能同时"):
        manager.start("/deepresearch 题目", blueprint="deepresearch")
    assert _run_dirs(manager.config) == []


def test_a_blueprint_command_equals_the_blueprint_field(tmp_path: Path) -> None:
    """`/deepresearch <题目>` ≡ `blueprint=deepresearch` + `prompt=<题目>`。"""
    field_manager, field_store = build_manager(tmp_path / "field")
    field_run = field_manager.start("监管新规", blueprint="deepresearch")
    assert field_manager.wait(field_run.run_id, timeout=30).status == FINISHED

    slash_manager, slash_store = build_manager(tmp_path / "slash")
    slash_run = slash_manager.start("/deepresearch 监管新规")
    assert slash_manager.wait(slash_run.run_id, timeout=30).status == FINISHED

    field = field_store.get(field_run.run_id)
    slash = slash_store.get(slash_run.run_id)
    assert field is not None and slash is not None
    assert slash.prompt == field.prompt == "监管新规", "斜杠命令必须被重写，不能原样进内核"
    assert slash.spec.blueprint == field.spec.blueprint == "deepresearch"
    assert slash.blueprint_stages == field.blueprint_stages


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("普通问题", (None, "普通问题")),
        ("正文里出现 /run --bg 也不算命令", (None, "正文里出现 /run --bg 也不算命令")),
        ("  普通问题前导空白", (None, "  普通问题前导空白")),
        ("/deepresearch 题目", ("deepresearch", "题目")),
        ("\t/deepresearch 题目", ("deepresearch", "题目")),
        ("/deepresearch   两个空格 的题目 ", ("deepresearch", "两个空格 的题目")),
    ],
)
def test_command_detection_splits_the_blueprint_form(
    text: str, expected: tuple[str | None, str]
) -> None:
    assert parse_blueprint_command(text, ("deepresearch",)) == expected


@pytest.mark.parametrize(
    "text",
    [
        "/run --bg x",
        "  /subagents-detach",
        "/",
        "/DeepResearch x",
        "/deepresearch2 x",
        "/skill:pi-subagents",
        "/deepresearch /run --bg",
    ],
)
def test_command_detection_refuses_everything_else(text: str) -> None:
    with pytest.raises(UnsupportedCommand) as excinfo:
        parse_blueprint_command(text, ("deepresearch",))
    assert excinfo.value.allowed == ("deepresearch",)


def test_a_bare_blueprint_command_is_not_a_command_form() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        parse_blueprint_command("/deepresearch", ("deepresearch",))


def test_the_blueprint_command_names_come_from_the_blueprint_directory(tmp_path: Path) -> None:
    """命令名单不硬编码：蓝图目录里有什么，就放行什么。"""
    blueprints = tmp_path / "blueprints"
    _write_blueprint(blueprints, "synthetic")
    manager, _store = build_manager(tmp_path, PI_GLINK_BLUEPRINTS_DIR=str(blueprints))

    record = manager.start("/synthetic 题目")
    assert manager.wait(record.run_id, timeout=30).status == FINISHED
    assert record.spec.blueprint == "synthetic"

    with pytest.raises(UnsupportedCommand):
        manager.start("/notes 还没有这个蓝图")


def test_two_runs_do_not_share_runtime_configuration_or_credentials(tmp_path: Path) -> None:
    """B2: each run renders its own config, session and egress token files.

    ``auth.json`` / ``web-search.json`` carry only the run's egress token
    (docs/adr/0008-egress-proxy.md), never the real key; the token is revoked
    at the verdict commit, so a file left in the runtime directory unlocks
    nothing. Isolation is checked on what each kernel saw at launch.
    """
    import json as json_module

    store = RunStore(tmp_path / "data" / "runs")
    seen: dict[str, dict[str, str]] = {}

    def capturing_factory(argv, *, cwd=None, env=None, on_event=None):
        directory = Path((env or {})["PI_CODING_AGENT_DIR"])
        seen[directory.parent.name] = {
            path.name: path.read_text(encoding="utf-8")
            for path in directory.iterdir()
            if path.name in ("auth.json", "web-search.json")
        }
        return fake_factory(write_report=True)(argv, cwd=cwd, env=env, on_event=on_event)

    with_key = make_config(tmp_path, PI_GLINK_WEB_SEARCH_KEY="exa-secret-key-1234567890")
    manager_a = RunManager(with_key, EventBus(), store, client_factory=capturing_factory)
    run_a = manager_a.start("第一个 run 带搜索凭据")
    final_a = manager_a.wait(run_a.run_id, timeout=30)
    assert final_a.status == FINISHED

    without_key = make_config(tmp_path)
    manager_b = RunManager(without_key, EventBus(), store, client_factory=capturing_factory)
    run_b = manager_b.start("第二个 run 不带搜索凭据")
    final_b = manager_b.wait(run_b.run_id, timeout=30)
    assert final_b.status == FINISHED

    runtime_a = store.run_dir(run_a.run_id) / "pi-agent"
    runtime_b = store.run_dir(run_b.run_id) / "pi-agent"
    assert runtime_a != runtime_b
    assert "web-search.json" in seen[run_a.run_id]
    assert "web-search.json" not in seen[run_b.run_id], (
        "a credential removed from the configuration must not survive in another run"
    )
    # Session directories are per run too (subdirectory of the configured root).
    assert (without_key.data_root / "runs" / run_a.run_id / "sessions") != (without_key.data_root / "runs" / run_b.run_id / "sessions")
    assert ((without_key.data_root / "runs" / run_a.run_id / "sessions")).is_dir()
    assert ((without_key.data_root / "runs" / run_b.run_id / "sessions")).is_dir()
    # The rendered model credential is a per-run egress token, never the
    # real key, and two runs never share one.
    tokens = []
    for run_id in (run_a.run_id, run_b.run_id):
        auth = json_module.loads(seen[run_id]["auth.json"])
        token = auth["frontier"]["key"]
        assert token.startswith("pge_") and "sk-test" not in seen[run_id]["auth.json"]
        tokens.append(token)
    assert tokens[0] != tokens[1]
    search = json_module.loads(seen[run_a.run_id]["web-search.json"])
    assert search["exaApiKey"] == tokens[0]
    assert "exa-secret-key" not in seen[run_a.run_id]["web-search.json"]
    # Both tokens were revoked with the verdict; nothing on disk holds a real key.
    for manager, token in ((manager_a, tokens[0]), (manager_b, tokens[1])):
        assert manager.egress.grant_for(token) is None
    for runtime in (runtime_a, runtime_b):
        assert "models.json" in {path.name for path in runtime.rglob("*")}
        for path in runtime.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                assert "sk-test" not in text and "exa-secret-key" not in text


def test_process_death_after_the_prompt_converges_quickly(tmp_path: Path) -> None:
    """A dead kernel must not wait for the full task timeout to be noticed."""
    manager, store = build_manager(
        tmp_path, write_report=False, LOCAL_WEB_TASK_TIMEOUT_SECONDS="120"
    )
    started = time.monotonic()
    record = manager.start("CRASH 接受指令后进程退出")
    final = manager.wait(record.run_id, timeout=30)
    elapsed = time.monotonic() - started

    assert final.status == FAILED
    assert final.stop_reason == "kernel_exit"
    assert final.error and "agent_settled" in final.error
    assert elapsed < 20, f"the run waited {elapsed:.1f}s instead of converging on exit"
    assert read_events(store, record.run_id)[-1]["type"] == "run_failed"


# -- usage semantics -----------------------------------------------------------

def test_usage_is_summed_across_requests_and_not_double_counted(tmp_path: Path) -> None:
    """100 + 200 tokens must add up to 300; a repeated identity counts once."""
    from pi_glink.kernel.projection import Projector  # noqa: PLC0415
    from pi_glink.runs.state import RunExecution as _Execution  # noqa: PLC0415 - private unit under test

    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    record = store.create("用量", run_id="usage", workspace=tmp_path / "ws", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    execution = _Execution(record=record, workspace=tmp_path / "ws")
    execution.projector = Projector(manager.redactor)
    manager.bus.open_run(record.run_id)

    def turn(timestamp: int, tokens: int) -> dict:
        return {
            "type": "turn_end",
            "message": {
                "role": "assistant",
                "timestamp": timestamp,
                "usage": {"input": tokens, "output": 0, "totalTokens": tokens},
            },
            "toolResults": [],
        }

    manager._pipeline.channel.on_frame(execution, turn(1, 100))  # noqa: SLF001
    manager._pipeline.channel.on_frame(execution, turn(2, 200))  # noqa: SLF001
    manager._pipeline.channel.on_frame(execution, turn(2, 200))  # duplicate identity: ignore

    events = read_events(store, record.run_id)
    usage_events = [event for event in events if event["type"] == "usage_reported"]
    assert len(usage_events) == 2
    assert usage_events[-1]["payload"]["usage"]["totalTokens"] == 300
    assert usage_events[-1]["payload"]["usage"]["requests"] == 2
    # The per-request cumulative snapshot stays available next to the run total.
    assert usage_events[-1]["payload"]["requestUsage"]["totalTokens"] == 200
    assert "cost" not in usage_events[-1]["payload"]["usage"], (
        "no price table is known, so no billing may be fabricated"
    )


# -- teardown and shutdown -----------------------------------------------------

def test_shutdown_cancels_and_releases_an_active_run(tmp_path: Path) -> None:
    manager, _store = build_manager(tmp_path)
    record = manager.start("NOSETTLE 挂住的 run")
    assert wait_for(lambda: manager.active_run_id == record.run_id)

    unreleased = manager.shutdown(timeout=10)

    assert unreleased == [], "an in-process run must be cancellable and releasable"
    assert manager.active_run_id is None
    final = manager.wait(record.run_id, timeout=5)
    assert final is not None and final.status == CANCELLED


def test_cleanup_failure_is_recorded_not_hidden(tmp_path: Path) -> None:
    """A sandbox that cannot be destroyed must not be reported as released."""
    config = make_config(
        tmp_path,
        LOCAL_WEB_EXECUTOR="e2b",
        E2B_API_KEY="e2b-secret",
        PI_GLINK_EGRESS_PUBLIC_URL="https://egress.example.test",
    )
    sandbox = _PoorlyClosingSandbox(tmp_path / "sandbox")
    store = RunStore(config.data_root / "runs")
    manager = RunManager(
        config, EventBus(), store, sandbox_factory=lambda **_: sandbox
    )
    record = manager.start("沙箱无法销毁")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == FINISHED
    assert final.cleanup is not None
    assert final.cleanup["released"] is False
    assert any("kill" in failure for failure in final.cleanup["failures"])
    assert final.sandbox_id == "poorly-closing"


class _PoorlyClosingSandbox(FakeSandbox):
    """stdin 假沙箱：内核真实跑完 fake pi，但沙箱 ``kill()`` 失败。"""

    def __init__(self, root: Path) -> None:
        super().__init__(
            root,
            sandbox_id="poorly-closing",
            report=None,
            fake_pi_env={"FAKE_PI_WRITE_REPORT": "1"},
        )

    def kill(self) -> None:
        raise RuntimeError("kill failed")


def test_missing_credential_refuses_to_start(tmp_path: Path) -> None:
    manager, _store = build_manager(tmp_path, OPENAI_API_KEY="")
    with pytest.raises(Exception) as excinfo:
        manager.start("没有密钥")
    assert "OPENAI_API_KEY" in str(excinfo.value)


# -- R4: resource release is its own fact ---------------------------------------


class ScriptedAdapter(ExecutionAdapter):
    """External execution stand-in: settles at once, blocks where told to.

    It writes a report during recovery like a sandbox pull-back would, and its
    ``close()`` result is scripted. ``gates`` maps a phase name to an event the
    call waits on after signalling ``entered[phase]``. Inheriting the ABC gives
    it the default empty ``recovered_session``/``recover_subagents``/
    ``child_progress_source``; the spec stub is never read because its
    ``start()`` is a no-op.
    """

    def __init__(self, *, close=None, gates=None) -> None:
        super().__init__(None, on_frame=lambda frame: None)  # type: ignore[arg-type]
        self._close = close or (lambda: CleanupReport(released=True))
        self.gates = gates or {}
        self.entered = {name: threading.Event() for name in ("recover", "close")}
        self.cancelled = threading.Event()

    def start(self) -> None:
        pass

    def identity(self):
        return {"sandbox": "synthetic-owned-resource"}

    def session_info(self):
        return None

    def deliver_input(self) -> None:
        pass

    def wait(self, *, cancel, deadline):
        return ExecutionOutcome(settled=True)

    def _gate(self, name: str) -> None:
        self.entered[name].set()
        gate = self.gates.get(name)
        if gate is not None:
            assert gate.wait(10), f"test never released the {name} gate"

    def recover_artifacts(self, destination):
        self._gate("recover")
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "report.md").write_text("# 替身报告\n", encoding="utf-8")
        return ArtifactRecovery(files=(destination / "report.md",))

    def close(self):
        self._gate("close")
        return self._close()

    def cancel(self) -> None:
        self.cancelled.set()


def scripted_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: ScriptedAdapter):
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    monkeypatch.setattr("pi_glink.kernel.harness_pi.PiExecutionAdapter", lambda spec, **kwargs: adapter)
    return config, store, manager


def _raise_on_close():
    raise RuntimeError("synthetic kill failed")


@pytest.mark.parametrize(
    "close",
    [
        lambda: CleanupReport(
            released=False,
            failures=("synthetic kill failed",),
            external={"sandbox": "synthetic-owned-resource"},
        ),
        _raise_on_close,
    ],
    ids=["released-false", "close-raises"],
)
def test_an_unconfirmed_release_keeps_capacity_until_an_operator_adjudicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, close
) -> None:
    """R4: a finished thread is not a released resource.

    The WIP recorded ``released=false`` and then freed the slot: the next run
    was admitted and shutdown returned an empty list.
    """
    config, store, manager = scripted_manager(tmp_path, monkeypatch, ScriptedAdapter(close=close))
    first = manager.start("资源释放失败")
    final = manager.wait(first.run_id, timeout=30)
    assert final.status == FINISHED, "the verdict does not depend on cleanup"
    assert final.cleanup["released"] is False and final.cleanup["failures"]
    assert final.sandbox_id == "synthetic-owned-resource"

    with pytest.raises(Exception) as refused:
        manager.start("第二个 run 不能被放行")
    assert type(refused.value).__name__ == "ResourcesUnreleased"
    assert refused.value.run_ids == [first.run_id]
    assert manager.shutdown(timeout=0.1) == [first.run_id], "shutdown must report it"
    listed = manager.unreleased_resources()
    assert [item["runId"] for item in listed] == [first.run_id]
    assert listed[0]["sandbox"] == "synthetic-owned-resource"

    # The hold is durable: a restarted manager (no memory of the run) refuses too.
    restarted = RunManager(config, EventBus(), store)
    with pytest.raises(Exception) as again:
        restarted.start("重启后仍不放行")
    assert type(again.value).__name__ == "ResourcesUnreleased"

    # Only an explicit operator decision, made while owning the data root, frees it.
    from pi_glink.ownership import DataRootOwnership, OwnershipRequired  # noqa: PLC0415

    with pytest.raises(OwnershipRequired):
        restarted.adjudicate_release(first.run_id, "未持有所有权")
    ownership = DataRootOwnership.acquire(config.data_root)
    try:
        operator = RunManager(config, EventBus(), store, ownership=ownership)
        with pytest.raises(ValueError):
            operator.adjudicate_release(first.run_id, "   ")
        record = operator.adjudicate_release(first.run_id, "已在控制台确认沙箱不存在")
        assert record.cleanup["released"] is False, "the original fact is kept"
        assert record.resource_adjudication["decision"] == "released"
        assert operator.unreleased_resources() == []
    finally:
        ownership.release()
    admitted = restarted.start("裁决后放行")
    assert restarted.wait(admitted.run_id, timeout=30).is_terminal


def test_shutdown_reports_a_release_that_does_not_finish_in_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Event()
    adapter = ScriptedAdapter(gates={"close": gate})
    _config, store, manager = scripted_manager(tmp_path, monkeypatch, adapter)
    record = manager.start("关闭迟迟不返回")
    assert adapter.entered["close"].wait(10)
    try:
        assert manager.shutdown(timeout=0.3) == [record.run_id]
        with pytest.raises(RunBusy):
            manager.start("释放未确认时不放行")
    finally:
        gate.set()
    final = manager.wait(record.run_id, timeout=30)
    assert final.cleanup["released"] is True
    # The stop from shutdown was accepted while the run was open.
    assert final.status == CANCELLED
    assert wait_for(lambda: manager.active_run_id is None)
    assert manager.shutdown(timeout=1) == []


# -- R5: cancellation and the verdict share one linearization point -------------


@pytest.mark.parametrize("phase", ["recover", "close", "report"])
def test_a_stop_accepted_during_finalization_becomes_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """R5: ``stop()`` returned true during recovery, yet the run ended finished."""
    gate = threading.Event()
    adapter = ScriptedAdapter(gates={phase: gate} if phase != "report" else {})
    _config, store, manager = scripted_manager(tmp_path, monkeypatch, adapter)
    entered = threading.Event()
    if phase == "report":
        seal = manager._pipeline.seal  # noqa: SLF001 - timing hook only
        original = seal.report

        def slow_report(execution):
            entered.set()
            assert gate.wait(10)
            return original(execution)

        monkeypatch.setattr(seal, "report", slow_report)
    else:
        entered = adapter.entered[phase]

    record = manager.start(f"收尾阶段取消：{phase}")
    assert entered.wait(10)
    try:
        decision = manager.stop(record.run_id)
    finally:
        gate.set()
    assert getattr(decision, "accepted", decision) is True
    final = manager.wait(record.run_id, timeout=30)
    events = read_events(store, record.run_id)
    assert final.status == CANCELLED and final.stop_reason == "abort"
    assert events[-1]["type"] == "run_cancelled"
    assert not any(event["type"] == "run_finished" for event in events)
    # Stop keeps what was produced: the report recovered before the verdict stays.
    assert final.report_status == "ok" and final.report_path


def test_a_stop_racing_the_commit_is_answered_by_the_committed_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop that arrives while the terminal frame is being committed waits
    for the commit and reports the committed status instead of accepting."""
    adapter = ScriptedAdapter()
    _config, store, manager = scripted_manager(tmp_path, monkeypatch, adapter)
    committing = threading.Event()
    release = threading.Event()
    original_commit = store.commit_terminal

    def slow_terminal(run_id, event):
        committing.set()
        assert release.wait(10)
        return original_commit(run_id, event)

    monkeypatch.setattr(store, "commit_terminal", slow_terminal)
    record = manager.start("与提交竞争的取消")
    assert committing.wait(10)
    answers: list[object] = []
    stopper = threading.Thread(target=lambda: answers.append(manager.stop(record.run_id)))
    stopper.start()
    stopper.join(0.3)
    assert stopper.is_alive(), "a stop may not be decided while the verdict is being committed"
    release.set()
    stopper.join(10)

    decision = answers[0]
    assert getattr(decision, "accepted", decision) is False
    assert decision.status == FINISHED and decision.reason == "terminal"
    final = manager.wait(record.run_id, timeout=30)
    assert final.status == FINISHED
    assert read_events(store, record.run_id)[-1]["type"] == "run_finished"
    assert not adapter.cancelled.is_set(), "a refused stop does not touch the face"

    after = manager.stop(record.run_id)
    assert (after.accepted, after.status, after.reason) == (False, FINISHED, "terminal")


def test_a_tail_withheld_during_finalization_is_released_before_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Frames can still arrive while artifacts are recovered; nothing the stream
    withheld may be lost, and nothing may follow the terminal frame."""
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    adapter = ScriptedAdapter()
    sink: dict[str, object] = {}

    def build(spec, **kwargs):
        sink["on_frame"] = kwargs["on_frame"]
        return adapter

    original_recover = adapter.recover_artifacts

    def recover_with_late_frame(destination):
        # "O" could start OPENAI_API_KEY=…, so the stream withholds it.
        sink["on_frame"](
            {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "迟到的尾部 O"}}
        )
        return original_recover(destination)

    adapter.recover_artifacts = recover_with_late_frame  # type: ignore[method-assign]
    monkeypatch.setattr("pi_glink.kernel.harness_pi.PiExecutionAdapter", build)
    record = manager.start("收尾期间到达的帧")
    manager.wait(record.run_id, timeout=30)
    events = read_events(store, record.run_id)
    text = "".join(e["payload"]["text"] for e in events if e["type"] == "assistant_delta")
    assert text == "迟到的尾部 O"
    assert events[-1]["type"] == "run_finished"


# -- follow-up admission: the snapshot is IO, the fork decision is not ----------


class _ResumingAdapter(ScriptedAdapter):
    """A stand-in kernel that reports the session id the run had to resume."""

    def __init__(self, expected_session_id: str | None) -> None:
        super().__init__()
        self._expected_session_id = expected_session_id

    def session_info(self):
        return SessionInfo({}, session_id=self._expected_session_id)


def test_two_concurrent_followups_of_one_parent_admit_exactly_one_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两个并发追问：一个建立子 run，一个得到 ``stale_run``。

    父会话快照（读取 + 脱敏，上限 32 MiB）在准入锁外完成，所以两个调用者能同时
    处在快照阶段：下面的 barrier 只有在两人都到达时才放行，锁内读取的第二个调用者
    永远到不了这里。而“父 run 是否已有后续轮次”的复核与子 run 的建立仍在同一临界区，
    所以两人都通过锁外检查（此时父 run 确实还没有后续轮次）后，只有一个能建立子 run，
    另一个必须被权威复核拦下——而不是从同一父 run 分叉出第二轮。

    观测点在多租户面上：本地 ``inproc`` 同一时刻只有一个 run 槽位，第二个追问会在
    准入处先得到 ``run_busy``，看不到父 run 的权威复核。
    """
    secret = "sk-shared-0123456789abcdef"
    config = make_config(
        tmp_path,
        OPENAI_API_KEY=secret,
        LOCAL_WEB_EXECUTOR="docker",
        PI_GLINK_TENANCY="multitenant",
        PI_GLINK_MODEL_CREDENTIALS="shared",
        PI_GLINK_TENANCY_AUDIENCE="pi-glink-test",
        PI_GLINK_GATEWAY_SECRET_FILE=str(tmp_path / "gateway-secret"),
        PI_GLINK_MAX_TENANT_RUNS="2",  # 两个并发追问都进得到锁内复核
    )
    store = RunStore(config.data_root / "runs")
    context = TenantContext(
        tenant_id="tenant-alpha",
        user_id="user-a",
        role="member",
        audience="pi-glink-test",
        expires_at=time.time() + 600,
    )
    manager = RunManager(config, EventBus(), store)
    monkeypatch.setattr(
        "pi_glink.kernel.harness_pi.PiExecutionAdapter",
        lambda spec, **kwargs: _ResumingAdapter(spec.expected_session_id),
    )

    # 父 run：一个已终态、带会话文件的历史轮次（不需要真的执行）。
    parent_id = "parentrun001"
    workspace = config.workspace_root / parent_id
    workspace.mkdir(parents=True, exist_ok=True)
    store.create(
        "第一轮",
        run_id=parent_id,
        workspace=workspace,
        tenant_id=context.tenant_id,
        creator_user_id=context.user_id,
        spec=RunSpec(family="pi", pack="pi", executor="inproc"),
    )
    session_dir = store.session_dir(parent_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / "parent.jsonl"
    session_file.write_text(
        json.dumps({"id": "same-session", "text": f"OPENAI_API_KEY={secret}"}) + "\n",
        encoding="utf-8",
    )
    store.update(parent_id, session_id="same-session", session_file=str(session_file))
    finish(store, parent_id, FINISHED, finished_at=time.time())

    both_snapshotting = threading.Barrier(2, timeout=10)
    # 锁边界在 pi 族的种子读取上：RunAdmission.resume_seed 只做共享门禁与委托，
    # 快照 IO（读取 + 脱敏）在族方法里，仍在准入锁外。
    family = manager._admission.family_for("pi")  # noqa: SLF001 - the lock boundary under test
    snapshot = family.resume_seed

    def gated_snapshot(parent, *, redactor):
        seed = snapshot(parent, redactor=redactor)
        both_snapshotting.wait()
        return seed

    monkeypatch.setattr(family, "resume_seed", gated_snapshot)
    outcomes: list[tuple[str, str]] = []
    crashes: list[BaseException] = []
    collected = threading.Lock()

    def follow_up(index: int) -> None:
        try:
            record = manager.start(f"追问 {index}", context, parent_run_id=parent_id)
        except FollowupUnavailable as refused:
            with collected:
                outcomes.append(("refused", refused.code))
        except BaseException as exc:  # noqa: BLE001 - reported, never hidden
            with collected:
                crashes.append(exc)
        else:
            with collected:
                outcomes.append(("admitted", record.run_id))

    threads = [threading.Thread(target=follow_up, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not crashes, f"两个追问必须能同时在锁外取快照；调用方异常退出: {crashes!r}"
    admitted = [value for kind, value in outcomes if kind == "admitted"]
    refused = [value for kind, value in outcomes if kind == "refused"]
    assert len(admitted) == 1, f"只有一个追问能建立子 run: {outcomes}"
    assert refused == ["stale_run"], f"另一个必须在锁内复核处被拦下: {outcomes}"

    children = [
        record
        for record in store.list_for(context.tenant_id, limit=None)
        if record.parent_run_id == parent_id
    ]
    assert [record.run_id for record in children] == admitted
    assert children[0].tenant_id == context.tenant_id
    assert children[0].creator_user_id == context.user_id

    # 被接受的追问照常继续：子 run 拿到的是脱敏后的父会话快照。
    final = manager.wait(admitted[0], timeout=30)
    assert final.status == FINISHED and final.session_id == "same-session"
    seeded_file = (config.data_root / "runs" / admitted[0] / "sessions") / "continued.jsonl"
    seeded = seeded_file.read_text(encoding="utf-8")
    assert secret not in seeded and "[REDACTED]" in seeded
    assert manager.shutdown(timeout=10) == []


def test_run_data_directories_are_created_private(tmp_path: Path) -> None:
    """The workspace, runtime config and session directories are host data."""
    import stat  # noqa: PLC0415

    manager, store = build_manager(tmp_path, write_report=True)
    record = manager.start("权限")
    final = manager.wait(record.run_id, timeout=30)
    assert final.status == FINISHED

    run_dir = store.run_dir(record.run_id)
    for path in (
        Path(final.workspace),
        run_dir / "pi-agent",
        (manager.config.data_root / "runs" / record.run_id / "sessions"),
        manager.config.workspace_root,
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, f"{path} 应为 0700"


# -- child-run documents: typed events → child.json → terminal summary (§3.5) ---
#
# The typed ``subagent_started``/``subagent_finished`` events are the projection
# layer's output; the runner's seam under test here starts at their commit
# (``_emit_frame`` → ``_emit`` → document upsert → verdict sweep → summary).


def open_execution(manager: RunManager, store: RunStore, workspace: Path, run_id: str):
    """An open execution without a kernel: the typed-event commit seam."""
    from pi_glink.runs.state import RunExecution as _Execution  # noqa: PLC0415

    # The run's spec is the manager's own admission spec: the family that
    # handles its frames is the one its record names.
    record = store.create(
        "子代理", run_id=run_id, workspace=workspace, spec=manager._admission.spec(None)  # noqa: SLF001
    )
    execution = _Execution(record=record, workspace=workspace)
    manager.bus.open_run(record.run_id)
    return execution


def emit(manager: RunManager, execution, event_type: str, payload: Mapping[str, Any]) -> None:
    with execution.lock:
        assert manager._pipeline.channel.emit_frame(execution, event_type, payload) is True  # noqa: SLF001


def test_pi_subagent_frames_flow_through_the_real_projection_into_documents(
    tmp_path: Path,
) -> None:
    """End to end through the real projector: raw pi ``tool_execution_*`` frames
    for a ``subagent`` call become typed events, child documents and the
    terminal summary — the generic card and the ``(running...)`` update vanish."""
    from pi_glink.kernel.projection import Projector  # noqa: PLC0415

    manager, store = build_manager(tmp_path)
    execution = open_execution(manager, store, tmp_path / "ws", "subdoc0")
    execution.projector = Projector(manager.redactor)
    frames = [
        {
            "type": "tool_execution_start",
            "toolCallId": "call_1",
            "toolName": "subagent",
            "args": {"tasks": [{"agent": "researcher", "task": "调研甲"}, {"agent": "researcher", "task": "调研乙"}]},
        },
        {
            "type": "tool_execution_update",
            "toolCallId": "call_1",
            "toolName": "subagent",
            "partialResult": "(running...)",
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "call_1",
            "toolName": "subagent",
            "result": {"content": [{"type": "text", "text": "两份结论"}]},
            "isError": False,
        },
    ]
    for frame in frames:
        manager._pipeline.channel.on_frame(execution, frame)  # noqa: SLF001

    logged = read_events(store, "subdoc0")
    assert [event["type"] for event in logged] == [
        "subagent_started",
        "subagent_started",
        "subagent_finished",
    ]
    page = store.read_children("subdoc0")
    assert [(doc["n"], doc["status"]) for doc in page.documents] == [(0, "finished"), (1, "finished")]
    assert all(doc["resultPreview"] == "两份结论" for doc in page.documents)

    finalize_with(manager, execution, outcome=ExecutionOutcome(settled=True))
    terminal = store.committed_terminal("subdoc0")
    assert terminal["payload"]["children"] == [
        {"n": 0, "agent": "researcher", "task": "调研甲", "status": "finished"},
        {"n": 1, "agent": "researcher", "task": "调研乙", "status": "finished"},
    ]


def test_subagent_started_event_commits_the_child_document(tmp_path: Path) -> None:
    manager, store = build_manager(tmp_path)
    execution = open_execution(manager, store, tmp_path / "ws", "subdoc1")

    emit(
        manager,
        execution,
        "subagent_started",
        {"n": 0, "toolCallId": "call_1", "agent": "researcher", "task": "调研甲"},
    )

    document = store.read_child("subdoc1", 0)
    assert document == {
        "n": 0,
        "toolCallId": "call_1",
        "agent": "researcher",
        "task": "调研甲",
        "status": "running",
        "resultPreview": None,
        "enrichment": "none",
    }
    logged = read_events(store, "subdoc1")
    assert logged[-1]["type"] == "subagent_started"


def test_subagent_finished_closes_every_child_of_the_call(tmp_path: Path) -> None:
    manager, store = build_manager(tmp_path)
    execution = open_execution(manager, store, tmp_path / "ws", "subdoc2")
    emit(manager, execution, "subagent_started", {"n": 0, "toolCallId": "call_1", "agent": "researcher", "task": "调研甲"})
    emit(manager, execution, "subagent_started", {"n": 1, "toolCallId": "call_1", "agent": "researcher", "task": "调研乙"})

    emit(
        manager,
        execution,
        "subagent_finished",
        {
            "toolCallId": "call_1",
            "ns": [0, 1],
            "status": "finished",
            "isError": False,
            "resultPreview": "两份结论的摘要",
        },
    )

    for n, task in ((0, "调研甲"), (1, "调研乙")):
        document = store.read_child("subdoc2", n)
        assert document["status"] == "finished"
        assert document["resultPreview"] == "两份结论的摘要"
        assert document["task"] == task, "started 落档的事实必须保留"
        assert document["enrichment"] == "none"


def test_subagent_finished_without_a_started_document_records_only_what_it_proves(
    tmp_path: Path,
) -> None:
    """A finished event for a child whose started write failed must not invent facts."""
    manager, store = build_manager(tmp_path)
    execution = open_execution(manager, store, tmp_path / "ws", "subdoc3")

    emit(
        manager,
        execution,
        "subagent_finished",
        {
            "toolCallId": "call_9",
            "ns": [4],
            "status": "failed",
            "isError": True,
            "resultPreview": "失败输出",
        },
    )

    document = store.read_child("subdoc3", 4)
    assert document["status"] == "failed"
    assert document["agent"] == "" and document["task"] == ""
    assert document["toolCallId"] == "call_9"


def test_child_documents_carry_only_redacted_values(tmp_path: Path) -> None:
    secret = "sk-child-0123456789abcdef"
    manager, store = build_manager(tmp_path, OPENAI_API_KEY=secret)
    execution = open_execution(manager, store, tmp_path / "ws", "subdoc4")

    emit(
        manager,
        execution,
        "subagent_started",
        {"n": 0, "toolCallId": "call_1", "agent": "researcher", "task": f"读取 {secret} 后总结"},
    )

    document = store.read_child("subdoc4", 0)
    assert secret not in json.dumps(document, ensure_ascii=False)
    assert "[REDACTED]" in document["task"]
    logged = read_events(store, "subdoc4")
    assert secret not in json.dumps(logged, ensure_ascii=False)


def test_a_document_write_failure_is_a_warning_not_a_blocked_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The document is an enhancement: a failed write never retracts the committed event."""
    manager, store = build_manager(tmp_path)
    execution = open_execution(manager, store, tmp_path / "ws", "subdoc5")

    def failing_write(run_id: str, document: Mapping[str, Any]) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(store, "write_child", failing_write)
    emit(manager, execution, "subagent_started", {"n": 0, "toolCallId": "call_1", "agent": "a", "task": "t"})

    logged = read_events(store, "subdoc5")
    types = [event["type"] for event in logged]
    assert "subagent_started" in types, "事件已提交，文档失败不能撤回它"
    warnings = [event for event in logged if event["type"] == "kernel_status"]
    assert warnings and warnings[-1]["payload"]["message"] == "child_doc_failed"


def finalize_with(manager: RunManager, execution, **facts) -> None:
    """Drive the real finalizer on a kernel-less execution with chosen facts.

    Fact names route to their owner group (execution / artifact / resource);
    anything else (e.g. ``cancel_requested``) stays on the execution itself.
    """
    groups = {
        "outcome": execution.execution_facts,
        "failure": execution.execution_facts,
        "event_log_error": execution.execution_facts,
        "model_stop_reason": execution.execution_facts,
        "model_error": execution.execution_facts,
        "usage_total": execution.execution_facts,
        "usage_keys": execution.execution_facts,
        "redacted": execution.artifact_facts,
        "report_path": execution.artifact_facts,
        "report_status": execution.artifact_facts,
        "artifact_failures": execution.artifact_facts,
        "cleanup": execution.resource_facts,
    }
    for name, value in facts.items():
        setattr(groups.get(name, execution), name, value)
    manager._pipeline.close_out(execution)  # noqa: SLF001 - the verdict path under test


def test_open_children_follow_a_cleanly_finished_run(tmp_path: Path) -> None:
    manager, store = build_manager(tmp_path)
    execution = open_execution(manager, store, tmp_path / "ws", "subdoc6")
    emit(manager, execution, "subagent_started", {"n": 0, "toolCallId": "call_1", "agent": "researcher", "task": "调研甲"})
    emit(manager, execution, "subagent_finished", {"toolCallId": "call_1", "ns": [0], "status": "finished", "isError": False, "resultPreview": "摘要"})
    emit(manager, execution, "subagent_started", {"n": 1, "toolCallId": "call_2", "agent": "researcher", "task": "调研乙"})

    finalize_with(manager, execution, outcome=ExecutionOutcome(settled=True))

    assert store.read_child("subdoc6", 0)["status"] == "finished", "已收口的保持原状态"
    assert store.read_child("subdoc6", 1)["status"] == "finished", "未收口的跟随 run"
    terminal = store.committed_terminal("subdoc6")
    assert terminal["type"] == "run_finished"
    assert terminal["payload"]["children"] == [
        {"n": 0, "agent": "researcher", "task": "调研甲", "status": "finished"},
        {"n": 1, "agent": "researcher", "task": "调研乙", "status": "finished"},
    ]


def test_open_children_follow_a_cancelled_run(tmp_path: Path) -> None:
    manager, store = build_manager(tmp_path)
    execution = open_execution(manager, store, tmp_path / "ws", "subdoc7")
    emit(manager, execution, "subagent_started", {"n": 0, "toolCallId": "call_1", "agent": "researcher", "task": "调研甲"})

    finalize_with(manager, execution, cancel_requested=True)

    assert store.read_child("subdoc7", 0)["status"] == "cancelled"
    terminal = store.committed_terminal("subdoc7")
    assert terminal["type"] == "run_cancelled"
    assert terminal["payload"]["children"] == [
        {"n": 0, "agent": "researcher", "task": "调研甲", "status": "cancelled"}
    ]


def test_open_children_follow_a_failed_run(tmp_path: Path) -> None:
    manager, store = build_manager(tmp_path)
    execution = open_execution(manager, store, tmp_path / "ws", "subdoc8")
    emit(manager, execution, "subagent_started", {"n": 0, "toolCallId": "call_1", "agent": "researcher", "task": "调研甲"})

    finalize_with(manager, execution, failure="RuntimeError: boom")

    assert store.read_child("subdoc8", 0)["status"] == "interrupted"
    terminal = store.committed_terminal("subdoc8")
    assert terminal["type"] == "run_failed"
    assert terminal["payload"]["children"][0]["status"] == "interrupted"


def test_a_run_without_subagents_carries_an_empty_children_summary(tmp_path: Path) -> None:
    manager, store = build_manager(tmp_path, write_report=True)
    record = manager.start("没有子代理的普通 run")
    final = manager.wait(record.run_id, timeout=30)
    assert final.status == FINISHED
    terminal = store.committed_terminal(record.run_id)
    assert terminal["payload"]["children"] == []


def _pi_runtime(tmp_path: Path, run_id: str):
    from pi_glink.kernel.agent_env import AgentRuntime  # noqa: PLC0415

    session_dir = tmp_path / "sessions" / run_id
    session_dir.mkdir(parents=True, exist_ok=True)
    return AgentRuntime(
        directory=tmp_path / "pi-agent",
        provider_id="fake",
        model_id="fake/model",
        models_path=tmp_path / "pi-agent" / "models.json",
        session_dir=session_dir,
    )


def _write_subagent_artifacts(session_dir: Path, run_prefix: str, agent: str, index: int, meta: dict) -> None:
    artifacts = session_dir / "subagent-artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    base = f"{run_prefix}_{agent}_{index}"
    (artifacts / f"{base}_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (artifacts / f"{base}_output.md").write_text("# 调研笔记\n", encoding="utf-8")
    (artifacts / f"{base}_input.md").write_text("# Task\n", encoding="utf-8")


def test_children_are_enriched_from_pi_subagent_artifacts(tmp_path: Path) -> None:
    """R2：收尾时产物按（agent, 序数）关联进 children/<n>/，_meta.json 白名单增强。"""
    manager, store = build_manager(tmp_path)
    execution = open_execution(manager, store, tmp_path / "ws", "enrich1")
    execution.runtime = _pi_runtime(tmp_path, "enrich1")
    emit(manager, execution, "subagent_started", {"n": 0, "toolCallId": "call_1", "agent": "researcher", "task": "调研甲"})
    _write_subagent_artifacts(
        execution.runtime.session_dir, "a1b2c3d4", "researcher", 0,
        {"exitCode": 0, "durationMs": 43210, "toolCount": 6, "model": "test/model:high",
         "usage": {"input": 1234, "output": 567, "cacheRead": 0, "cacheWrite": 0, "turns": 4, "cost": 0.01}},
    )

    finalize_with(manager, execution, outcome=ExecutionOutcome(settled=True))

    document = store.read_child("enrich1", 0)
    assert document["enrichment"] == "meta"
    assert document["durationMs"] == 43210 and document["toolCount"] == 6
    assert document["usage"] == {"input": 1234, "output": 567, "cacheRead": 0, "cacheWrite": 0, "turns": 4}
    assert "cost" not in json.dumps(document), "估费不是宿主事实"
    child_dir = store.run_dir("enrich1") / "children" / "0"
    assert (child_dir / "output.md").read_text(encoding="utf-8") == "# 调研笔记\n"
    assert (child_dir / "meta.json").is_file()
    names = {entry["name"] for entry in document["artifacts"]}
    assert {"meta.json", "output.md", "input.md"} <= names
    summary = store.children_summary("enrich1")
    assert summary[0]["durationMs"] == 43210
    assert summary[0]["artifacts"] == document["artifacts"]
    terminal = store.committed_terminal("enrich1")
    assert terminal["payload"]["children"][0]["enrichment"] == "meta"


def test_children_enrichment_marks_unmatched_honestly(tmp_path: Path) -> None:
    """同名 agent 两次派发产生 index 冲突：不配对，标 unmatched，不伪造。"""
    manager, store = build_manager(tmp_path)
    execution = open_execution(manager, store, tmp_path / "ws", "enrich2")
    execution.runtime = _pi_runtime(tmp_path, "enrich2")
    emit(manager, execution, "subagent_started", {"n": 0, "toolCallId": "call_1", "agent": "researcher", "task": "甲"})
    emit(manager, execution, "subagent_started", {"n": 1, "toolCallId": "call_2", "agent": "researcher", "task": "乙"})
    artifacts = execution.runtime.session_dir / "subagent-artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "a1b2c3d4_researcher_0_meta.json").write_text('{"exitCode": 0}', encoding="utf-8")
    (artifacts / "b2c3d4e5_researcher_0_meta.json").write_text('{"exitCode": 0}', encoding="utf-8")

    finalize_with(manager, execution, outcome=ExecutionOutcome(settled=True))

    first = store.read_child("enrich2", 0)
    second = store.read_child("enrich2", 1)
    assert first["enrichment"] == "unmatched" and second["enrichment"] == "unmatched"
    assert "durationMs" not in first and "artifacts" not in first


def test_children_enrichment_skips_the_acp_family(tmp_path: Path) -> None:
    """ACP 族没有 pi-subagents 布局：enrichment 保持 none，目录不被触碰。"""
    manager, store = build_manager(tmp_path, PI_GLINK_HARNESS="acp", TEST_ACP_COMMAND="true")
    execution = open_execution(manager, store, tmp_path / "ws", "enrich3")
    execution.runtime = _pi_runtime(tmp_path, "enrich3")
    emit(manager, execution, "subagent_started", {"n": 0, "toolCallId": "call_1", "agent": "", "task": "调研"})
    _write_subagent_artifacts(
        execution.runtime.session_dir, "a1b2c3d4", "researcher", 0, {"exitCode": 0}
    )

    finalize_with(manager, execution, outcome=ExecutionOutcome(settled=True))

    document = store.read_child("enrich3", 0)
    assert document["enrichment"] == "none"
    assert not (store.run_dir("enrich3") / "children" / "0" / "meta.json").exists()


# -- run 记录落 harness/executor/agent 事实 + 追问门禁比字段 ----------------------


def test_admission_records_the_harness_and_executor_facts(tmp_path: Path) -> None:
    """准入时 harness/executor 随 meta 原子写入；pi 面 agent 恒为 None。

    pi 协议不报 agent 身份：``session_ready`` 载荷只有 model/thinkingLevel，
    模型 id 不冒充 agent。红→绿机制：旧代码 ``RunRecord`` 无此三字段，断
    言对象不存在（AttributeError）。
    """
    manager, _store = build_manager(tmp_path, write_report=True)
    record = manager.start("记录本 run 的事实")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == FINISHED
    assert final.spec.family == manager.config.harness == "pi"
    assert final.spec.executor == manager.config.executor == "inproc"
    assert final.agent is None


def _write_parent_meta(
    config: HostConfig,
    store: RunStore,
    parent_id: str,
    *,
    with_session_file: bool = True,
    status: str = FINISHED,
    **meta_fields: Any,
) -> None:
    """准备一份父记录（与合法 pi 会话文件），不经过 RunManager 的准入。

    准入事实默认 spec = pi/pi/inproc；``family`` / ``pack`` / ``executor`` 覆盖
    ``spec`` 的对应字段，``tenant_id`` / ``creator_user_id`` 随 create 写入。
    ``status`` 为终态时提交对应终帧（终态只能随终帧进入记录）；``RUNNING``
    则保持未终态。
    """
    spec = {"family": "pi", "pack": "pi", "executor": "inproc"}
    for name in ("family", "pack", "executor"):
        if name in meta_fields:
            spec[name] = meta_fields.pop(name)
    store.create(
        "第一轮",
        run_id=parent_id,
        spec=RunSpec(**spec),
        tenant_id=meta_fields.pop("tenant_id", None),
        creator_user_id=meta_fields.pop("creator_user_id", None),
    )
    changes: dict[str, Any] = dict(meta_fields)
    if with_session_file:
        session_dir = store.session_dir(parent_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / "parent.jsonl"
        session_file.write_text(
            json.dumps({"id": "same-session", "text": "第一轮"}) + "\n",
            encoding="utf-8",
        )
        changes["session_id"] = "same-session"
        changes["session_file"] = str(session_file)
    if changes:
        store.update(parent_id, **changes)
    if status != RUNNING:
        finish(store, parent_id, status, cleanup={"released": True})


def test_same_family_pi_parent_follow_up_is_admitted_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同族合法输入保护：父记录 harness="pi" 时 pi→pi 追问逐断言不变。

    红→绿机制：旧代码 ``store.create`` 不接受 harness 参数（TypeError）；
    转绿后本测试钉住 pi→pi 的既有受理路径（读脱敏会话文件、--session 续接）。
    """
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    monkeypatch.setattr(
        "pi_glink.kernel.harness_pi.PiExecutionAdapter",
        lambda spec, **kwargs: _ResumingAdapter(spec.expected_session_id),
    )
    parent_id = "parentrun001"
    store.create("第一轮", run_id=parent_id, spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    session_dir = store.session_dir(parent_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / "parent.jsonl"
    session_file.write_text(
        json.dumps({"id": "same-session", "text": "第一轮"}) + "\n", encoding="utf-8"
    )
    store.update(parent_id, session_id="same-session", session_file=str(session_file))
    finish(store, parent_id, FINISHED, finished_at=time.time())

    child = manager.start("追问", parent_run_id=parent_id)
    final = manager.wait(child.run_id, timeout=30)
    assert final.status == FINISHED
    assert final.session_id == "same-session"
    assert final.parent_run_id == parent_id
    assert final.spec.family == "pi" and final.spec.executor == "inproc"
    assert manager.shutdown(timeout=10) == []


def test_known_cross_family_parent_is_refused_before_its_session_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """宿主从 acp 切到 pi 后，追问已知 ACP 父 run：fail-closed，且不读其会话。

    父记录的会话文件被刻意布置成完全合法：旧代码（门禁不含族检查，读回时
    harness 键被未知键过滤）会把这次追问放行到 ``RunAdmission.resume_seed`` 的会话读
    取并受理——本测试因没有任何拒绝而变红；新代码在读取之前的族判定处拒
    绝（绿）。
    """
    config = make_config(tmp_path)  # harness="pi"
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    admitted: list[str] = []

    def recording_adapter(spec, **kwargs):
        admitted.append(spec.run_id)
        return _ResumingAdapter(spec.expected_session_id)

    monkeypatch.setattr("pi_glink.kernel.harness_pi.PiExecutionAdapter", recording_adapter)
    _write_parent_meta(
        config, store, "parentrun001", family="acp", executor="inproc"
    )

    with pytest.raises(FollowupUnavailable) as excinfo:
        manager.start("追问", parent_run_id="parentrun001")
    assert excinfo.value.code == "session_unavailable"
    # 拒绝发生在会话读取与子 run 建立之前：没有任何子 run 被创建。
    assert admitted == []
    assert [record.run_id for record in store.list(limit=None)] == ["parentrun001"]


def test_cross_family_refusal_precedes_the_state_checks(tmp_path: Path) -> None:
    """族判定先于状态检查：run_busy 状态的跨族父 run 仍得 session_unavailable。

    红→绿机制：旧代码不含族检查，未终态父 run 先得到 ``run_busy``，与本
    测试断言的 ``session_unavailable`` 不同而变红。
    """
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    # 手写一份 RUNNING 状态的跨族父记录（未终态 → 旧顺序下是 run_busy）。
    _write_parent_meta(
        config,
        store,
        "busyparent01",
        with_session_file=False,
        status=RUNNING,
        family="acp",
        executor="inproc",
    )

    with pytest.raises(FollowupUnavailable) as excinfo:
        manager.start("追问", parent_run_id="busyparent01")
    assert excinfo.value.code == "session_unavailable"


def test_foreign_parent_stays_unknown_even_when_its_family_is_known(
    tmp_path: Path,
) -> None:
    """门禁顺序：owner 判定先于一切事实读取。

    跨租户父记录即便 harness 已知且跨族，仍先得到 ``unknown_run``——不泄
    露存在性，也不泄露 ``session_unavailable`` 这一族判定结果。owner 门
    禁本身未变，本测试新旧代码同绿，钉住这一不变式。
    """
    config = make_config(
        tmp_path,
        LOCAL_WEB_EXECUTOR="docker",
        PI_GLINK_TENANCY="multitenant",
        PI_GLINK_MODEL_CREDENTIALS="shared",
        PI_GLINK_TENANCY_AUDIENCE="pi-glink-test",
        PI_GLINK_GATEWAY_SECRET_FILE=str(tmp_path / "gateway-secret"),
    )
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    _write_parent_meta(
        config,
        store,
        "foreignparent",
        with_session_file=False,
        tenant_id="tenant-alpha",
        creator_user_id="user-a",
        family="acp",
        executor="docker",
    )
    stranger = TenantContext(
        tenant_id="tenant-beta",
        user_id="user-b",
        role="member",
        audience="pi-glink-test",
        expires_at=time.time() + 600,
    )

    with pytest.raises(FollowupUnavailable) as excinfo:
        manager.start("追问", stranger, parent_run_id="foreignparent")
    assert excinfo.value.code == "unknown_run"


# -- finalize 编排：拆出的步骤按契约顺序被驱动 ------------------------------------


def test_finalize_drives_its_steps_in_the_contract_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """顺序探针：观察结束 → 回收 → 关闭 → 清扫 → 增强 → 报告 → 提交。

    收尾由 ``RunPipeline.close_out`` 按 Observe → Recover → Seal →
    Decide/Commit 编排；本测试用记录型替身替换各阶段的命名步骤，钉住编排
    次序（步骤本身的行为由本文件其余 finalize 用例覆盖）。
    """
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    record = store.create("顺序探针", run_id="orderprobe01", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    from pi_glink.runs.state import ReportFacts, RunExecution  # noqa: PLC0415

    execution = RunExecution(record=record, workspace=workspace)
    pipeline = manager._pipeline  # noqa: SLF001

    calls: list[str] = []

    def observe(execution_):
        calls.append("observe")

    def recover(execution_, adapter):
        calls.append("recover")
        return ()

    def close(execution_, adapter):
        calls.append("close")
        return CleanupReport(released=True)

    def sweep(execution_, failures):
        calls.append("sweep")
        return False

    def enrich(execution_, failures):
        calls.append("enrich")
        return False

    def report(execution_):
        calls.append("report")
        return ReportFacts(status="missing")

    def commit(execution_):
        calls.append("commit")
        execution_.verdict = Verdict(FINISHED, None, None)
        return True

    monkeypatch.setattr(pipeline.observe, "finish", observe)
    monkeypatch.setattr(pipeline.recover, "face_artifacts", recover)
    monkeypatch.setattr(pipeline.recover, "close_face", close)
    monkeypatch.setattr(pipeline.seal, "sweep", sweep)
    monkeypatch.setattr(pipeline.seal, "enrich", enrich)
    monkeypatch.setattr(pipeline.seal, "report", report)
    monkeypatch.setattr(pipeline.commit, "run", commit)

    pipeline.close_out(execution)

    assert calls == ["observe", "recover", "close", "sweep", "enrich", "report", "commit"]
    assert execution.finalized.is_set()
