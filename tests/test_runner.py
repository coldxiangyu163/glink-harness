"""Regressions for run orchestration, driven by the fake pi protocol stand-in."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import pytest

from pi_glink.config import HostConfig, build_config
from pi_glink.events import EventBus
from pi_glink.kernel.rpc import PiRpcClient
from pi_glink.kernel.runner import RunBusy, RunManager
from pi_glink.store import CANCELLED, FAILED, FINISHED, RunStore

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_PI = Path(__file__).resolve().parent / "fixtures" / "fake_pi.py"


def make_config(tmp_path: Path, **overrides: str) -> HostConfig:
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
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

    assert manager.stop(record.run_id) is True
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
    assert manager.stop("does-not-exist") is False


def test_docker_session_file_is_mapped_back_to_the_host_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit regression: a container ``/sessions/…`` path must be read on the host.

    pi reports container paths on the Docker face while the file itself lives
    under the mounted host session directory. Reading the raw container path
    found nothing and the closing sanitise pass returned silently, leaving a
    tool-echoed credential in the host session log.
    """
    monkeypatch.setattr("pi_glink.kernel.runner.docker_available", lambda: True)
    monkeypatch.setattr("pi_glink.kernel.runner.subprocess.run", lambda *a, **k: None)

    secret = "sk-test-0123456789abcdef"
    config = make_config(tmp_path, LOCAL_WEB_EXECUTOR="docker", OPENAI_API_KEY=secret)
    session_file = config.session_dir / "fake-session.jsonl"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(
        f'{{"role":"tool","text":"OPENAI_API_KEY={secret}"}}\n', encoding="utf-8"
    )

    def factory(argv, *, cwd=None, env=None, on_event=None):
        merged = dict(env or {})
        merged["FAKE_PI_WRITE_REPORT"] = "0"
        merged["FAKE_PI_SESSION_FILE"] = "/sessions/fake-session.jsonl"
        return PiRpcClient(
            [sys.executable, str(FAKE_PI)], cwd=cwd, env=merged, on_event=on_event
        )

    bus = EventBus()
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, bus, store, client_factory=factory)
    record = manager.start("docker 会话文件映射回归")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == FINISHED
    # The stored path points at the host file, and that file was sanitized.
    assert final.session_file == str(session_file)
    text = session_file.read_text(encoding="utf-8")
    assert secret not in text
    assert "[REDACTED]" in text
    assert final.redacted is True


def test_missing_credential_refuses_to_start(tmp_path: Path) -> None:
    manager, _store = build_manager(tmp_path, OPENAI_API_KEY="")
    with pytest.raises(Exception) as excinfo:
        manager.start("没有密钥")
    assert "OPENAI_API_KEY" in str(excinfo.value)
