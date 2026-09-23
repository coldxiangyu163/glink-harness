"""Regressions for the durable event log as the replay source of truth.

The audit reproduced two failure modes:

* ``_emit`` published to the live bus *before* appending to ``events.jsonl``: a
  failed append left a frame the browser had seen and the log did not;
* ``is_open`` stayed true after a run closed and replay came from the in-memory
  ring, so a small ring silently served a suffix of the real history.

The replacement is "commit before publish" plus "replay from the log, follow the
bus": sequence assignment, durable append, live notification and the final count
all describe the same set of frames. These tests exercise over-capacity history,
append failures, live reconnects, same-process finishes, process restarts and
the slow-subscriber bound.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.request
from http.client import HTTPConnection
from pathlib import Path

import pytest

from pi_glink import events as events_module
from pi_glink.app import PiGlinkServer, create_server
from pi_glink.config import build_config
from pi_glink.events import RUN_STARTED, EventBus
from pi_glink.kernel.rpc import PiRpcClient
from pi_glink.kernel.runner import RunManager
from pi_glink.static_files import WebAssets
from pi_glink.store import FAILED, RUNNING, RunStore

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_PI = Path(__file__).resolve().parent / "fixtures" / "fake_pi.py"


def make_config(tmp_path: Path, **overrides: str):
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_PORT": "0",
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent.json"),
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


def fake_factory(*, write_report: bool = True):
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


def build_server(config, *, capacity: int | None = None) -> PiGlinkServer:
    bus = EventBus(capacity=capacity) if capacity is not None else EventBus()
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, bus, store, client_factory=fake_factory())
    return PiGlinkServer(config, bus, store, manager, WebAssets(config.web_root))


def wait_for(predicate, *, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def read_log(store: RunStore, run_id: str) -> list[dict]:
    path = store.events_path(run_id)
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def sse_frames(port: int, run_id: str, *, last_event_id: str | None = None) -> list[dict]:
    connection = HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        headers = {}
        if last_event_id is not None:
            headers["Last-Event-ID"] = last_event_id
        connection.request("GET", f"/api/runs/{run_id}/events", headers=headers)
        response = connection.getresponse()
        assert response.status == 200, response.status
        body = response.read().decode("utf-8")
    finally:
        connection.close()
    frames: list[dict] = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                frames.append(json.loads(line[len("data: ") :]))
    return frames


# -- the bus commit contract --------------------------------------------------


def test_commit_persists_before_publishing_and_persist_failure_publishes_nothing() -> None:
    bus = EventBus()
    bus.open_run("run-1")
    subscriber = bus.subscribe("run-1")
    seen: list[object] = []

    def collector() -> None:
        for event in subscriber:
            seen.append(event)

    thread = threading.Thread(target=collector, daemon=True)
    thread.start()
    time.sleep(0.1)

    def failing_persist(event) -> None:
        raise OSError("disk full")

    with pytest.raises(OSError):
        bus.commit("run-1", "assistant_delta", {"text": "lost"}, persist=failing_persist)

    committed = bus.commit("run-1", "assistant_delta", {"text": "kept"})
    assert committed.seq == 1, "a failed append must not consume a sequence number"
    bus.close("run-1")
    thread.join(timeout=5)

    assert [event.seq for event in seen] == [1]
    assert seen[0].payload == {"text": "kept"}, "only the committed frame was published"


def test_event_log_failure_fails_the_run_without_phantom_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store, client_factory=fake_factory())
    original_append = store.append_event
    failures = {"remaining": 3}

    def flaky_append(run_id: str, event) -> None:
        if failures["remaining"] > 0 and event.get("type") == "assistant_delta":
            failures["remaining"] -= 1
            raise OSError("simulated append failure")
        original_append(run_id, event)

    monkeypatch.setattr(store, "append_event", flaky_append)
    record = manager.start("事件日志写盘失败")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == FAILED
    assert final.stop_reason == "event_log_failed"
    assert final.error and "事件日志写入失败" in final.error

    persisted = read_log(store, record.run_id)
    seqs = [event["seq"] for event in persisted]
    assert seqs == list(range(1, len(seqs) + 1)), "the committed log must have no gaps"
    # The frame that could not be appended is absent from the log *and* was not
    # published: a reader cannot observe it on any surface.
    assert store.count_events(record.run_id) == len(persisted)
    assert persisted[-1]["type"] == "run_failed"
    assert final.event_count == len(persisted)


def test_terminal_frame_append_failure_still_lands_a_terminal_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken log at the very last frame must not leave the run "running"."""
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store, client_factory=fake_factory())
    original_append = store.append_event

    def flaky_append(run_id: str, event) -> None:
        if event.get("type") == "run_finished":
            raise OSError("simulated append failure on the terminal frame")
        original_append(run_id, event)

    monkeypatch.setattr(store, "append_event", flaky_append)
    record = manager.start("终帧写盘失败")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == "finished", final.error
    assert final.error and "事件日志" in final.error, "the broken log must be visible"
    assert final.event_count == len(read_log(store, record.run_id))
    assert all(event["type"] != "run_finished" for event in read_log(store, record.run_id))
    assert manager.active_run_id is None
    assert manager.bus.is_open(record.run_id) is False


# -- replay boundaries --------------------------------------------------------


def test_history_replay_is_not_bounded_by_the_bus_ring(tmp_path: Path) -> None:
    """A 3-frame ring must not truncate a 12-frame run's history."""
    config = make_config(tmp_path)
    server = build_server(config, capacity=3)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/runs",
            data=json.dumps({"prompt": "多帧 run"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            run_id = json.loads(response.read())["run"]["runId"]

        def finished() -> bool:
            record = server.store.get(run_id)
            return record is not None and record.status != RUNNING

        assert wait_for(finished)
        persisted = read_log(server.store, run_id)
        assert len(persisted) > 3, "the fixture must exceed the ring capacity"

        frames = sse_frames(server.server_address[1], run_id)
        assert [frame["seq"] for frame in frames] == [
            event["seq"] for event in persisted
        ], "replay must read the durable log, not the ring"
        assert frames[-1]["type"] == "run_finished"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_open_run_replays_history_then_follows_live_frames(tmp_path: Path) -> None:
    """Active → finished: a late subscriber sees every committed frame once."""
    config = make_config(tmp_path)
    server = build_server(config, capacity=3)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/runs",
            data=json.dumps({"prompt": "NOSETTLE 保持运行"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            run_id = json.loads(response.read())["run"]["runId"]
        assert wait_for(lambda: len(read_log(server.store, run_id)) >= 5)
        before = read_log(server.store, run_id)
        assert len(before) > 3, "the run must have outgrown the ring before attach"

        result: dict[str, list[dict]] = {}

        def stream() -> None:
            result["frames"] = sse_frames(server.server_address[1], run_id)

        streamer = threading.Thread(target=stream, daemon=True)
        streamer.start()
        time.sleep(0.3)
        server.manager.stop(run_id)
        streamer.join(timeout=20)
        assert not streamer.is_alive(), "the live tail never ended after the run closed"

        frames = result["frames"]
        seqs = [frame["seq"] for frame in frames]
        assert seqs == sorted(seqs), "frames must arrive in sequence order"
        assert len(seqs) == len(set(seqs)), "no frame may be delivered twice"
        assert seqs[0] == 1, "the replay must start at the beginning"
        persisted = read_log(server.store, run_id)
        assert seqs == [event["seq"] for event in persisted]
        assert frames[-1]["type"] == "run_cancelled"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_last_event_id_resumes_without_replaying_committed_frames(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    server = build_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/runs",
            data=json.dumps({"prompt": "断线接续"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            run_id = json.loads(response.read())["run"]["runId"]
        assert wait_for(
            lambda: (server.store.get(run_id) or None) and server.store.get(run_id).is_terminal
        )
        frames = sse_frames(server.server_address[1], run_id, last_event_id="2")
        assert [frame["seq"] for frame in frames][0] == 3
        assert all(frame["seq"] > 2 for frame in frames)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_all_readers_observe_the_same_committed_facts(tmp_path: Path) -> None:
    """events.jsonl, the run view and the SSE replay agree frame for frame."""
    config = make_config(tmp_path)
    server = build_server(config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/runs",
            data=json.dumps({"prompt": "一致事实"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            run_id = json.loads(response.read())["run"]["runId"]
        assert wait_for(
            lambda: (server.store.get(run_id) or None) and server.store.get(run_id).is_terminal
        )
        persisted = read_log(server.store, run_id)
        record = server.store.get(run_id)
        assert record.event_count == len(persisted)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_address[1]}/api/runs/{run_id}", timeout=10
        ) as response:
            view = json.loads(response.read())["run"]
        assert view["eventCount"] == len(persisted)
        frames = sse_frames(server.server_address[1], run_id)
        assert [(f["seq"], f["type"]) for f in frames] == [
            (e["seq"], e["type"]) for e in persisted
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# -- restart ------------------------------------------------------------------


def test_restart_reconciles_a_run_left_running_by_a_previous_process(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    store.create("上一次进程遗留", run_id="abandoned", workspace=tmp_path / "ws")
    store.update(
        "abandoned",
        status=RUNNING,
        container_name="pi-glink-abandoned",
        sandbox_id="sandbox-xyz",
    )
    store.append_event(
        "abandoned",
        {
            "type": "run_started",
            "runId": "abandoned",
            "seq": 1,
            "ts": time.time(),
            "payload": {"prompt": "上一次进程遗留"},
        },
    )

    # Creating the host is the restart: reconcile runs before history is served.
    server = create_server(config, client_factory=fake_factory())
    record = server.store.get("abandoned")
    assert record.status == FAILED
    assert record.stop_reason == "interrupted"
    assert record.error and "重启" in record.error
    assert record.event_count == 2

    persisted = read_log(store, "abandoned")
    assert [event["type"] for event in persisted] == ["run_started", "run_failed"]
    assert [event["seq"] for event in persisted] == [1, 2]
    assert persisted[-1]["payload"]["stopReason"] == "interrupted"
    assert persisted[-1]["payload"]["external"] == {
        "container": "pi-glink-abandoned",
        "sandbox": "sandbox-xyz",
    }
    # External leftovers are reported for an operator decision, never touched.
    orphans = server.manager.orphaned_resources()
    assert any(item["runId"] == "abandoned" for item in orphans)
    assert orphans[0]["cleanup"]["released"] is False


# -- slow subscribers ---------------------------------------------------------


def test_a_slow_subscriber_is_disconnected_instead_of_growing_without_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(events_module, "_SUBSCRIBER_CAPACITY", 2)
    bus = EventBus()
    bus.open_run("run-1")
    subscriber = bus.subscribe("run-1")
    for index in range(10):
        bus.commit("run-1", RUN_STARTED, {"index": index})

    received: list[object] = []
    started = time.monotonic()
    for event in subscriber:
        received.append(event)
    elapsed = time.monotonic() - started

    assert elapsed < 5, "the consumer must be released, not left waiting forever"
    assert len(received) <= 2, "a subscriber that stopped reading is disconnected"
    assert bus.is_open("run-1") is True


def test_run_view_uses_the_persisted_count_once_the_bus_window_is_gone(tmp_path: Path) -> None:
    """``/api/runs`` must not report ``eventCount: 0`` for a finished run."""
    config = make_config(tmp_path)
    server = build_server(config, capacity=2)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/runs",
            data=json.dumps({"prompt": "结束后计数"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            run_id = json.loads(response.read())["run"]["runId"]
        assert wait_for(
            lambda: (server.store.get(run_id) or None) and server.store.get(run_id).is_terminal
        )
        assert server.bus.is_open(run_id) is False
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_address[1]}/api/runs", timeout=10
        ) as response:
            runs = json.loads(response.read())["runs"]
        view = next(item for item in runs if item["runId"] == run_id)
        assert view["eventCount"] == len(read_log(server.store, run_id))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
