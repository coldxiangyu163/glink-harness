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

import errno
import json
import os
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
from pi_glink.runs.runner import RunManager
from pi_glink.static_files import WebAssets
from pi_glink.store import FAILED, RUNNING, RunStore

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_PI = Path(__file__).resolve().parent / "fixtures" / "fake_pi.py"


def make_config(tmp_path: Path, **overrides: str):
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
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
    assert persisted[-1]["type"] == "run_failed"
    assert final.event_count == len(persisted)


def test_terminal_append_failure_leaves_every_reader_on_the_same_uncommitted_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R7: a verdict that cannot be committed is not shown on any surface.

    The WIP wrote ``meta.json`` as finished while the log had no terminal
    frame, so the detail view and the SSE replay disagreed. Now nothing is
    committed: detail, list, report and replay all still describe a running
    run; the run keeps its slot, refuses a stop as ``finalizing`` and is
    reported by shutdown. A restart (new owner) interrupts it consistently on
    every surface and keeps its resources held until adjudicated.
    """
    config = make_config(tmp_path)
    server = create_server(config, client_factory=fake_factory())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    original_commit = server.store.commit_terminal

    def failing_terminal(run_id: str, event) -> None:
        raise OSError("simulated append failure on the terminal frame")

    monkeypatch.setattr(server.store, "commit_terminal", failing_terminal)
    try:
        run_id = _post(port, "/api/runs", {"prompt": "终帧写盘失败"})[1]["run"]["runId"]
        execution = server.manager._executions[run_id]  # noqa: SLF001 - wait for the finalizer
        assert execution.finalized.wait(30), "the finalizer never finished"

        detail = _get_json(port, f"/api/runs/{run_id}")[1]["run"]
        listed = next(r for r in _get_json(port, "/api/runs")[1]["runs"] if r["runId"] == run_id)
        assert detail["status"] == listed["status"] == RUNNING
        assert detail["hasReport"] is False and listed["hasReport"] is False
        assert _get_status(port, f"/api/runs/{run_id}/report") == 404
        log = read_log(server.store, run_id)
        assert all(e["type"] not in events_module.TERMINAL_EVENT_TYPES for e in log)
        assert server.manager.active_run_id == run_id, "an uncommitted run keeps its slot"
        assert _post(port, "/api/runs", {"prompt": "第二个"})[0] == 409
        status, body = _post(port, f"/api/runs/{run_id}/stop", None)
        assert status == 409 and body["reason"] == "finalizing" and body["requested"] is False
        assert server.manager.shutdown(timeout=1) == [run_id]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    monkeypatch.setattr(server.store, "commit_terminal", original_commit)
    restarted = create_server(config, client_factory=fake_factory())
    thread = threading.Thread(target=restarted.serve_forever, daemon=True)
    thread.start()
    port = restarted.server_address[1]
    try:
        detail = _get_json(port, f"/api/runs/{run_id}")[1]["run"]
        assert detail["status"] == FAILED and detail["stopReason"] == "interrupted"
        assert detail["hasReport"] is False
        assert _get_status(port, f"/api/runs/{run_id}/report") == 404
        frames = sse_frames(port, run_id)
        assert frames[-1]["type"] == "run_failed"
        assert frames[-1]["payload"]["cleanup"]["released"] is None
        assert detail["eventCount"] == len(frames)
        status, body = _post(port, "/api/runs", {"prompt": "资源未确认时不放行"})
        assert status == 409 and body["error"] == "resources_unreleased"
        assert body["runIds"] == [run_id]
    finally:
        restarted.shutdown()
        restarted.server_close()
        thread.join(timeout=5)


def _post(port: int, path: str, body: dict | None) -> tuple[int, dict]:
    connection = HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        payload = json.dumps(body).encode() if body is not None else b"{}"
        connection.request("POST", path, body=payload, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        connection.close()


def _get_json(port: int, path: str) -> tuple[int, dict]:
    connection = HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        connection.close()


def _get_status(port: int, path: str) -> int:
    connection = HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


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
    assert record.error and "中断" in record.error
    assert record.event_count == 2

    persisted = read_log(store, "abandoned")
    assert [event["type"] for event in persisted] == ["run_started", "run_failed"]
    assert [event["seq"] for event in persisted] == [1, 2]
    assert persisted[-1]["payload"]["stopReason"] == "interrupted"
    assert persisted[-1]["payload"]["external"] == {
        "container": "pi-glink-abandoned",
        "sandbox": "sandbox-xyz",
    }
    # External leftovers are reported for an operator decision, never touched:
    # their state is unknown, so they keep holding capacity.
    unreleased = server.manager.unreleased_resources()
    assert [item["runId"] for item in unreleased] == ["abandoned"]
    assert unreleased[0]["cleanup"]["released"] is None
    assert unreleased[0]["container"] == "pi-glink-abandoned"
    server.server_close()


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
        # The verdict is visible from its commit; the live window closes after it.
        assert wait_for(lambda: server.bus.is_open(run_id) is False)
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


# -- R7: the terminal notification never precedes readable state -----------------


class _ObservingBus(EventBus):
    """Reads every HTTP surface the moment a terminal frame has been published.

    The observation runs inside the finalizer's commit call, before any later
    write: whatever is not visible here is not part of the committed fact.
    """

    def __init__(self) -> None:
        super().__init__()
        self.port: int | None = None
        self.seen: list[dict] = []

    def commit(self, run_id, event_type, payload=None, **kwargs):
        event = super().commit(run_id, event_type, payload, **kwargs)
        if event_type in events_module.TERMINAL_EVENT_TYPES and self.port is not None:
            detail = _get_json(self.port, f"/api/runs/{run_id}")[1]["run"]
            listed = next(
                r for r in _get_json(self.port, "/api/runs")[1]["runs"] if r["runId"] == run_id
            )
            self.seen.append(
                {
                    "frame": event_type,
                    "frameHasReport": bool((payload or {}).get("reportPath")),
                    "detail": detail,
                    "listed": listed,
                    "report": _get_status(self.port, f"/api/runs/{run_id}/report"),
                }
            )
        return event


def test_a_terminal_frame_is_only_published_once_every_reader_sees_it(tmp_path: Path) -> None:
    """R7: the WIP published run_finished while meta.json still said running."""
    config = make_config(tmp_path)
    bus = _ObservingBus()
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, bus, store, client_factory=fake_factory(write_report=True))
    server = PiGlinkServer(config, bus, store, manager, WebAssets(config.web_root))
    bus.port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        run_id = _post(bus.port, "/api/runs", {"prompt": "终帧与读面一致"})[1]["run"]["runId"]
        assert wait_for(lambda: bool(bus.seen))
        (seen,) = bus.seen
        assert seen["frame"] == "run_finished"
        for view in (seen["detail"], seen["listed"]):
            assert view["status"] == "finished"
            assert view["hasReport"] is True
        assert seen["frameHasReport"] is True
        assert seen["report"] == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_settle_failure_and_restart_keep_every_surface_on_the_committed_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R7 with a mid-way I/O failure: the settled copy is never written.

    Detail, list, report and replay agree before the restart (projected from
    the committed frame) and after it (settled by the next owner) — and the
    committed ``finished`` verdict is not rewritten as interrupted.
    """
    config = make_config(tmp_path)

    def surfaces(port: int, run_id: str) -> tuple:
        detail = _get_json(port, f"/api/runs/{run_id}")[1]["run"]
        listed = next(r for r in _get_json(port, "/api/runs")[1]["runs"] if r["runId"] == run_id)
        frames = sse_frames(port, run_id)
        return (
            detail["status"], listed["status"], frames[-1]["payload"]["status"],
            detail["hasReport"], listed["hasReport"], _get_status(port, f"/api/runs/{run_id}/report"),
            detail["eventCount"], len(frames),
        )

    server = create_server(config, client_factory=fake_factory())
    monkeypatch.setattr(server.store, "settle", lambda run_id: (_ for _ in ()).throw(OSError("disk full")))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        run_id = _post(port, "/api/runs", {"prompt": "落定副本写失败"})[1]["run"]["runId"]
        assert wait_for(lambda: server.manager.active_run_id is None)
        meta = json.loads((server.store.run_dir(run_id) / "meta.json").read_text(encoding="utf-8"))
        assert meta["status"] == RUNNING
        before = surfaces(port, run_id)
        assert before[:6] == ("finished", "finished", "finished", True, True, 200)
        assert before[6] == before[7]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    restarted = create_server(config, client_factory=fake_factory())
    thread = threading.Thread(target=restarted.serve_forever, daemon=True)
    thread.start()
    port = restarted.server_address[1]
    try:
        meta = json.loads((restarted.store.run_dir(run_id) / "meta.json").read_text(encoding="utf-8"))
        assert meta["status"] == "finished", "the next owner settles the committed verdict"
        assert surfaces(port, run_id) == before
        assert [f["type"] for f in sse_frames(port, run_id)].count("run_failed") == 0
    finally:
        restarted.shutdown()
        restarted.server_close()
        thread.join(timeout=5)


def test_report_write_failure_is_a_consistent_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R7 with the artifact copy failing: no surface offers a report."""
    from pi_glink.artifacts import ArtifactTree  # noqa: PLC0415

    config = make_config(tmp_path)
    original_replace = ArtifactTree.replace

    def failing_replace(self, path, data, **kwargs):
        if str(path).startswith("artifacts/"):
            raise OSError("simulated artifact write failure")
        return original_replace(self, path, data, **kwargs)

    monkeypatch.setattr(ArtifactTree, "replace", failing_replace)
    server = create_server(config, client_factory=fake_factory())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        run_id = _post(port, "/api/runs", {"prompt": "产物副本写失败"})[1]["run"]["runId"]
        assert wait_for(lambda: server.manager.active_run_id is None)
        detail = _get_json(port, f"/api/runs/{run_id}")[1]["run"]
        assert detail["status"] == "finished" and detail["stopReason"] == "report_rejected"
        assert detail["hasReport"] is False
        assert _get_status(port, f"/api/runs/{run_id}/report") == 404
        terminal = sse_frames(port, run_id)[-1]
        assert terminal["payload"]["reportStatus"] == "rejected"
        assert terminal["payload"]["reportPath"] is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# -- the terminal commit point (PI-GLINK-STREAM-DURABILITY-20260923) ------------
#
# The terminal line was written, then fsync'ed; readers could see it in between,
# and a failed fsync truncated it away again. A terminal state must become
# visible only once it is durable, and a failure must leave it never visible.


def _fd_path(fd: int) -> str:
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        import fcntl  # noqa: PLC0415 - macOS

        return fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0].decode()


class _FsyncGate:
    """Blocks the first fsync of an ``events.jsonl`` descriptor until released."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, fail: bool) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail = fail
        self._original = os.fsync
        monkeypatch.setattr(os, "fsync", self._fsync)

    def _fsync(self, fd: int) -> None:
        if not self.entered.is_set() and _fd_path(fd).endswith("events.jsonl"):
            self.entered.set()
            assert self.release.wait(30), "the test never released the fsync gate"
            if self.fail:
                raise OSError(errno.EIO, "synthetic fsync failure")
        self._original(fd)


def _has_terminal(store: RunStore, run_id: str) -> bool:
    return any(e["type"] in events_module.TERMINAL_EVENT_TYPES for e in store.iter_events(run_id))


@pytest.mark.parametrize("fail", [False, True], ids=["fsync-ok", "fsync-fails"])
def test_a_verdict_is_invisible_until_its_fsync_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    config = make_config(tmp_path)
    server = create_server(config, client_factory=fake_factory())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    gate = _FsyncGate(monkeypatch, fail=fail)
    samples: list[tuple[str, bool]] = []
    stop = threading.Event()
    try:
        run_id = _post(port, "/api/runs", {"prompt": "终帧落盘窗口"})[1]["run"]["runId"]

        def observe() -> None:
            while not stop.is_set():
                status = _get_json(port, f"/api/runs/{run_id}")[1]["run"]["status"]
                samples.append((status, _has_terminal(server.store, run_id)))
                time.sleep(0.005)

        observer = threading.Thread(target=observe, daemon=True)
        observer.start()
        assert gate.entered.wait(30), "the terminal commit never reached fsync"

        # The terminal line is written and fsync has not returned: nothing may show it.
        assert server.store.get(run_id).status == RUNNING
        assert server.store.committed_terminal(run_id) is None
        assert not _has_terminal(server.store, run_id)
        detail = _get_json(port, f"/api/runs/{run_id}")[1]["run"]
        listed = next(r for r in _get_json(port, "/api/runs")[1]["runs"] if r["runId"] == run_id)
        assert (detail["status"], listed["status"]) == (RUNNING, RUNNING)
        assert detail["hasReport"] is False and listed["hasReport"] is False
        assert _get_status(port, f"/api/runs/{run_id}/report") == 404
        seen_while_blocked = len(samples)
        gate.release.set()

        execution = server.manager._executions[run_id]  # noqa: SLF001 - wait for the finalizer
        assert execution.finalized.wait(30)
        time.sleep(0.1)
        stop.set()
        observer.join(5)
        assert seen_while_blocked and all(
            status == RUNNING and not terminal for status, terminal in samples[:seen_while_blocked]
        )
        if not fail:
            detail = _get_json(port, f"/api/runs/{run_id}")[1]["run"]
            assert detail["status"] == "finished" and detail["hasReport"] is True
            assert _get_status(port, f"/api/runs/{run_id}/report") == 200
            frames = sse_frames(port, run_id)
            assert [f["type"] for f in frames].count("run_finished") == 1
            assert frames[-1]["type"] == "run_finished"
            first = next(i for i, (status, _t) in enumerate(samples) if status != RUNNING)
            assert all(status == "finished" and terminal for status, terminal in samples[first:]), (
                "a committed verdict never disappears again"
            )
        else:
            assert all(status == RUNNING and not terminal for status, terminal in samples), (
                "a verdict whose fsync failed was never visible"
            )
            assert server.store.get(run_id).status == RUNNING
            assert not _has_terminal(server.store, run_id)
            assert _get_status(port, f"/api/runs/{run_id}/report") == 404
            log = read_log(server.store, run_id)
            assert server.bus.last_seq(run_id) == log[-1]["seq"], "no sequence was burned"
            assert server.store.events_path(run_id).read_bytes().endswith(b"\n")
    finally:
        stop.set()
        gate.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    if fail:
        restarted = create_server(config, client_factory=fake_factory())
        try:
            record = restarted.store.get(run_id)
            assert record.status == FAILED and record.stop_reason == "interrupted"
            types = [e["type"] for e in restarted.store.iter_events(run_id)]
            assert types.count("run_failed") == 1 and types[-1] == "run_failed"
            assert "run_finished" not in types
        finally:
            restarted.server_close()


@pytest.mark.parametrize("fail", [False, True], ids=["fsync-ok", "fsync-fails"])
def test_a_reconciled_interruption_is_invisible_until_its_fsync_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    """Restart path over a torn tail: the repair newline and the interrupted
    frame become visible together, after fsync — or not at all."""
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    store.create("上一个进程遗留", run_id="orphan", workspace=tmp_path / "ws")
    store.append_event(
        "orphan",
        {"type": "run_started", "runId": "orphan", "seq": 1, "ts": time.time(), "payload": {}},
    )
    with store.events_path("orphan").open("a", encoding="utf-8") as handle:
        handle.write('{"type": "assistant_delta", "runId": "orph')  # a crash left half a line
    before = store.events_path("orphan").read_bytes()
    gate = _FsyncGate(monkeypatch, fail=fail)
    started: dict[str, object] = {}

    def start_host() -> None:
        try:
            started["server"] = create_server(config, client_factory=fake_factory())
        except BaseException as exc:  # noqa: BLE001 - asserted below
            started["error"] = exc

    host = threading.Thread(target=start_host, daemon=True)
    host.start()
    try:
        assert gate.entered.wait(30), "reconciliation never reached fsync"
        observer = RunStore(config.data_root / "runs")
        assert observer.get("orphan").status == RUNNING
        assert observer.committed_terminal("orphan") is None
        assert [e["type"] for e in observer.iter_events("orphan")] == ["run_started"]
        gate.release.set()
        host.join(30)
        assert "error" not in started, started.get("error")
        if not fail:
            record = observer.get("orphan")
            assert record.status == FAILED and record.stop_reason == "interrupted"
            assert [e["type"] for e in observer.iter_events("orphan")] == ["run_started", "run_failed"]
        else:
            assert observer.get("orphan").status == RUNNING
            assert observer.committed_terminal("orphan") is None
            assert store.events_path("orphan").read_bytes() == before, "the repair was rolled back too"
    finally:
        gate.release.set()
        host.join(5)
        server = started.get("server")
        if server is not None:
            server.server_close()


# -- child-run documents vs. the committed terminal frame (§3.5) -----------------


def test_child_documents_and_the_terminal_summary_survive_a_restart(tmp_path: Path) -> None:
    """The committed terminal frame's ``children`` summary and the durable
    ``children/<n>/child.json`` documents are two read surfaces of the same
    facts: a fresh process over the same data root must read them identically."""
    config = make_config(tmp_path)
    bus = EventBus()
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, bus, store, client_factory=fake_factory())
    record = manager.start("NOSETTLE 挂住，等注入子代理事件")
    assert wait_for(
        lambda: any(event["type"] == "session_ready" for event in read_log(store, record.run_id))
    )
    execution = manager._executions[record.run_id]  # noqa: SLF001 - injection seam
    with execution.lock:
        # The typed events are the projection layer's output; the seam under
        # test starts at their commit (docs/architecture.md §3.5).
        assert manager._emit_frame(  # noqa: SLF001
            execution,
            "subagent_started",
            {"n": 0, "toolCallId": "call_1", "agent": "researcher", "task": "调研甲"},
        )
        assert manager._emit_frame(  # noqa: SLF001
            execution,
            "subagent_finished",
            {
                "toolCallId": "call_1",
                "ns": [0],
                "status": "finished",
                "isError": False,
                "resultPreview": "甲的结论摘要",
            },
        )
        assert manager._emit_frame(  # noqa: SLF001
            execution,
            "subagent_started",
            {"n": 1, "toolCallId": "call_2", "agent": "researcher", "task": "调研乙"},
        )
    assert manager.stop(record.run_id).accepted is True
    final = manager.wait(record.run_id, timeout=30)
    assert final.status == "cancelled"

    expected_children = [
        {"n": 0, "agent": "researcher", "task": "调研甲", "status": "finished"},
        # 未收口的子代理跟随 run 的终态（§3.4「工具终态跟随 run」）。
        {"n": 1, "agent": "researcher", "task": "调研乙", "status": "cancelled"},
    ]
    terminal = store.committed_terminal(record.run_id)
    assert terminal["payload"]["children"] == expected_children

    # The restart: a fresh store/manager over the same data root reads the
    # same committed frame and the same documents.
    restarted_store = RunStore(config.data_root / "runs")
    replayed_terminal = restarted_store.committed_terminal(record.run_id)
    assert replayed_terminal["payload"]["children"] == expected_children
    page = restarted_store.read_children(record.run_id)
    assert page.skipped == 0
    summary_from_docs = [
        {
            "n": document["n"],
            "agent": document["agent"],
            "task": document["task"],
            "status": document["status"],
        }
        for document in page.documents
    ]
    assert summary_from_docs == expected_children
    assert page.documents[0]["resultPreview"] == "甲的结论摘要"
    assert page.documents[0]["enrichment"] == "none"
    # The replayed log carries the typed events plus exactly one terminal frame.
    types = [event["type"] for event in restarted_store.iter_events(record.run_id)]
    assert "subagent_started" in types and "subagent_finished" in types
    assert types[-1] == "run_cancelled"
