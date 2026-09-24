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
from pi_glink.events import EventBus
from pi_glink.kernel.rpc import PiRpcClient
from pi_glink.kernel.execution import (
    ArtifactRecovery,
    CleanupReport,
    ExecutionOutcome,
    SessionInfo,
)
from pi_glink.kernel.runner import (
    FollowupUnavailable,
    RunBusy,
    RunManager,
    UnsupportedCommand,
    prompt_names_unsupported_command,
)
from pi_glink.store import CANCELLED, FAILED, FINISHED, RunStore
from pi_glink.tenancy import TenantContext

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_PI = Path(__file__).resolve().parent / "fixtures" / "fake_pi.py"


def make_config(tmp_path: Path, **overrides: str) -> HostConfig:
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        # Never fall back to the operator's own credential file in a test.
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent-web-search.json"),
    }
    values.update(overrides)
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
    path = store.events_path(run_id)
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
    meta = json.loads((store.run_dir(record.run_id) / "meta.json").read_text(encoding="utf-8"))
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
    monkeypatch.setattr("pi_glink.kernel.runner.docker_available", lambda: True)

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
        session_dir = config.session_dir / match.group(1)
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
    # The research contract travels as an appended system prompt, read from the
    # repository template (offline smoke for the `--append-system-prompt` path).
    assert "--append-system-prompt" in argv
    contract = (REPO_ROOT / "agent" / "research-prompt.md").read_text(encoding="utf-8").strip()
    assert contract in argv, "the rendered research prompt must reach the kernel verbatim"


# -- slash 命令门禁（GLINK-SUB-02 评审修复） ---------------------------------


def _run_dirs(config: HostConfig) -> list[str]:
    runs = config.data_root / "runs"
    return sorted(path.name for path in runs.glob("*")) if runs.is_dir() else []


@pytest.mark.parametrize(
    "text",
    [
        "/run researcher --bg 查跨境资金监管",
        "/subagents-detach",
        "   /chain scout -> planner",
        "\n\t/parallel researcher --bg",
        "/",
        # pi 的模板匹配精确且大小写敏感：这些既不是模板，也不是普通文本。
        "/DeepResearch 题目",
        "/deepresearch2 题目",
        "/skill:pi-subagents",
    ],
)
def test_the_command_gate_refuses_before_any_run_exists(tmp_path: Path, text: str) -> None:
    """载入 pi-subagents 后，`/run --bg` 等命令能绕过 tool_call 守卫。

    准入层只放行从 `agent/prompts/` 推导出的模板命令（当前只有 deepresearch）；
    拒绝必须发生在凭据解析、父会话读取、run 目录与执行面创建之前。
    """
    manager, _store = build_manager(tmp_path)
    with pytest.raises(UnsupportedCommand) as excinfo:
        manager.start(text)
    assert excinfo.value.allowed == ("deepresearch",)
    assert _run_dirs(manager.config) == [], "拒绝必须早于 run 目录创建"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("普通问题", False),
        ("正文里出现 /run --bg 也不算命令", False),
        ("  普通问题前导空白", False),
        ("/deepresearch 题目", False),
        ("/deepresearch", False),
        ("\t/deepresearch 题目", False),
        ("/run --bg x", True),
        ("  /subagents-detach", True),
        ("/", True),
        ("/DeepResearch x", True),
        ("/deepresearch2 x", True),
        ("/skill:pi-subagents", True),
    ],
)
def test_command_detection_follows_pi_semantics(text: str, expected: bool) -> None:
    assert prompt_names_unsupported_command(text, ("deepresearch",)) is expected


def test_the_command_gate_reads_the_template_directory(tmp_path: Path) -> None:
    """命令白名单不硬编码：模板目录里有什么，就放行什么。"""
    template = tmp_path / "agent-template"
    (template / "prompts").mkdir(parents=True)
    (template / "prompts" / "deepresearch.md").write_text("题目：$@\n", encoding="utf-8")
    manager, _store = build_manager(tmp_path, PI_CODING_AGENT_DIR=str(template))

    record = manager.start("/deepresearch 监管新规")
    assert manager.wait(record.run_id, timeout=30).status == FINISHED

    with pytest.raises(UnsupportedCommand):
        manager.start("/notes 还没有这个模板")

    (template / "prompts" / "notes.md").write_text("题目：$@\n", encoding="utf-8")
    record = manager.start("/notes 现在可以了")
    assert manager.wait(record.run_id, timeout=30).status == FINISHED


def test_two_runs_do_not_share_runtime_configuration_or_credentials(tmp_path: Path) -> None:
    """B2: each run renders its own config, session and credential files."""
    import json as json_module

    store = RunStore(tmp_path / "data" / "runs")

    with_key = make_config(tmp_path, PI_GLINK_WEB_SEARCH_KEY="exa-secret-key-1234567890")
    manager_a = RunManager(with_key, EventBus(), store, client_factory=fake_factory(write_report=True))
    run_a = manager_a.start("第一个 run 带搜索凭据")
    final_a = manager_a.wait(run_a.run_id, timeout=30)
    assert final_a.status == FINISHED

    without_key = make_config(tmp_path)
    manager_b = RunManager(without_key, EventBus(), store, client_factory=fake_factory(write_report=True))
    run_b = manager_b.start("第二个 run 不带搜索凭据")
    final_b = manager_b.wait(run_b.run_id, timeout=30)
    assert final_b.status == FINISHED

    runtime_a = store.run_dir(run_a.run_id) / "pi-agent"
    runtime_b = store.run_dir(run_b.run_id) / "pi-agent"
    assert runtime_a != runtime_b
    assert (runtime_a / "web-search.json").is_file()
    assert not (runtime_b / "web-search.json").exists(), (
        "a credential removed from the configuration must not survive in another run"
    )
    # No file from run A leaks into run B's runtime directory.
    names_a = {path.name for path in runtime_a.rglob("*")}
    names_b = {path.name for path in runtime_b.rglob("*")}
    assert "web-search.json" not in names_b
    # Session directories are per run too (subdirectory of the configured root).
    assert without_key.session_dir / run_a.run_id != without_key.session_dir / run_b.run_id
    assert (without_key.session_dir / run_a.run_id).is_dir()
    assert (without_key.session_dir / run_b.run_id).is_dir()
    # The generated model credential is per run and never shared.
    for run_id in (run_a.run_id, run_b.run_id):
        auth = json_module.loads(
            (store.run_dir(run_id) / "pi-agent" / "auth.json").read_text(encoding="utf-8")
        )
        assert "sk-test" in json_module.dumps(auth)


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
    from pi_glink.kernel.runner import _Execution  # noqa: PLC0415 - private unit under test

    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    record = store.create("用量", run_id="usage", workspace=tmp_path / "ws")
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

    manager._on_frame(execution, turn(1, 100))  # noqa: SLF001
    manager._on_frame(execution, turn(2, 200))  # noqa: SLF001
    manager._on_frame(execution, turn(2, 200))  # duplicate identity: ignore

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


def test_settle_failure_still_releases_the_slot_and_the_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit regression (A3) on the committed-terminal model.

    The verdict is committed by the terminal frame; copying it into meta.json
    afterwards may fail without keeping the slot or the stream, and without
    changing what any reader sees. Reconciliation settles the committed verdict
    instead of rewriting it as ``interrupted``.
    """
    from pi_glink.ownership import DataRootOwnership  # noqa: PLC0415

    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    ownership = DataRootOwnership.acquire(config.data_root)
    try:
        manager = RunManager(
            config, EventBus(), store,
            client_factory=fake_factory(write_report=True), ownership=ownership,
        )

        def failing_settle(run_id: str):
            raise OSError("simulated disk failure")

        original_settle = store.settle
        monkeypatch.setattr(store, "settle", failing_settle)
        record = manager.start("收尾写盘失败也要释放槽位")
        assert wait_for(lambda: manager.active_run_id is None, timeout=30)

        # The slot is released at the commit point, before settle (which fails
        # here and logs) and before the stream is closed: wait for the close
        # itself instead of assuming it already happened.
        assert wait_for(
            lambda: manager.bus.is_open(record.run_id) is False, timeout=30
        ), "the live stream must be closed"
        meta = json.loads((store.run_dir(record.run_id) / "meta.json").read_text(encoding="utf-8"))
        assert meta["status"] == "running", "the settled copy was never written"
        seen = store.get(record.run_id)
        assert seen.status == FINISHED and seen.report_path, "readers see the committed verdict"
        assert read_events(store, record.run_id)[-1]["type"] == "run_finished"

        monkeypatch.setattr(store, "settle", original_settle)
        assert manager.reconcile() == [], "a committed verdict is never interrupted"
        meta = json.loads((store.run_dir(record.run_id) / "meta.json").read_text(encoding="utf-8"))
        assert meta["status"] == FINISHED
        assert [event["type"] for event in read_events(store, record.run_id)].count("run_finished") == 1
        assert not any(event["type"] == "run_failed" for event in read_events(store, record.run_id))
    finally:
        ownership.release()


def test_cleanup_failure_is_recorded_not_hidden(tmp_path: Path) -> None:
    """A sandbox that cannot be destroyed must not be reported as released."""
    config = make_config(
        tmp_path,
        LOCAL_WEB_EXECUTOR="e2b",
        E2B_API_KEY="e2b-secret",
        PI_GLINK_WEB_SEARCH_CONFIG=str(tmp_path / "absent.json"),
    )
    sandbox = _PoorlyClosingSandbox()
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


class _PoorlyClosingSandbox:
    """Minimal E2B stand-in whose ``kill()`` fails."""

    def __init__(self) -> None:
        self.sandbox_id = "poorly-closing"

        class _Files:
            def make_dir(self, path: str) -> None:
                pass

            def write(self, path: str, content: object) -> None:
                pass

            def read(self, path: str, format: str | None = None) -> Any:
                if path == "/home/user/events.jsonl":
                    data: Any = b'{"type": "agent_settled"}\n'
                elif path == "/home/user/workspace/report.md":
                    data = b"# report\n"
                else:
                    raise FileNotFoundError(path)
                if format == "stream":
                    raw = data if isinstance(data, bytes) else data.encode("utf-8")
                    return iter([raw])
                return data

            def get_info(self, path: str) -> Any:
                data = self.read(path)
                size = len(data) if isinstance(data, bytes) else len(data.encode("utf-8"))

                class _Info:
                    pass

                info = _Info()
                info.size = size
                return info

            def list(self, path: str) -> list:
                class _Entry:
                    name = "report.md"
                    path = "/home/user/workspace/report.md"
                    type = "file"

                return [_Entry()]

        self.files = _Files()

        class _Commands:
            def run(self, cmd: str, **kwargs: object):
                class _Handle:
                    def wait(self):
                        class _Result:
                            exit_code = 0

                        return _Result()

                return _Handle()

        self.commands = _Commands()

    def kill(self) -> None:
        raise RuntimeError("kill failed")


def test_missing_credential_refuses_to_start(tmp_path: Path) -> None:
    manager, _store = build_manager(tmp_path, OPENAI_API_KEY="")
    with pytest.raises(Exception) as excinfo:
        manager.start("没有密钥")
    assert "OPENAI_API_KEY" in str(excinfo.value)


# -- R4: resource release is its own fact ---------------------------------------


class ScriptedAdapter:
    """External execution stand-in: settles at once, blocks where told to.

    It writes a report during recovery like a sandbox pull-back would, and its
    ``close()`` result is scripted. ``gates`` maps a phase name to an event the
    call waits on after signalling ``entered[phase]``.
    """

    def __init__(self, *, close=None, gates=None) -> None:
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
    monkeypatch.setattr("pi_glink.kernel.runner.build_adapter", lambda spec, **kwargs: adapter)
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
        original = manager._recover_report  # noqa: SLF001 - timing hook only

        def slow_report(execution):
            entered.set()
            assert gate.wait(10)
            original(execution)

        monkeypatch.setattr(manager, "_recover_report", slow_report)
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
    monkeypatch.setattr("pi_glink.kernel.runner.build_adapter", build)
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
        "pi_glink.kernel.runner.build_adapter",
        lambda spec, **kwargs: _ResumingAdapter(spec.expected_session_id),
    )

    # 父 run：一个已终态、带会话文件的历史轮次（不需要真的执行）。
    parent_id = "parentrun001"
    session_dir = config.session_dir / parent_id
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / "parent.jsonl"
    session_file.write_text(
        json.dumps({"id": "same-session", "text": f"OPENAI_API_KEY={secret}"}) + "\n",
        encoding="utf-8",
    )
    workspace = config.workspace_root / parent_id
    workspace.mkdir(parents=True, exist_ok=True)
    store.create(
        "第一轮",
        run_id=parent_id,
        workspace=workspace,
        tenant_id=context.tenant_id,
        creator_user_id=context.user_id,
    )
    store.update(
        parent_id,
        status=FINISHED,
        session_id="same-session",
        session_file=str(session_file),
        finished_at=time.time(),
    )

    both_snapshotting = threading.Barrier(2, timeout=10)
    snapshot = manager._resume_seed  # noqa: SLF001 - the lock boundary under test

    def gated_snapshot(context_, parent_run_id, redactor):
        seed = snapshot(context_, parent_run_id, redactor)
        both_snapshotting.wait()
        return seed

    monkeypatch.setattr(manager, "_resume_seed", gated_snapshot)
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
    seeded_file = config.session_dir / admitted[0] / "continued.jsonl"
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
        Path(manager.config.session_dir) / record.run_id,
        manager.config.workspace_root,
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, f"{path} 应为 0700"
