"""R3 regressions: a second host must not rewrite a live owner's runs.

R1 review reproduction: a second host started against the same data root
reconciled the first host's still-executing run to ``failed/interrupted``
before its own bind failed. Reconciliation now requires exclusive ownership of
the data root, acquired before any record is read for recovery; a live owner —
in this process or another, on the same port or a different one — refuses the
second host without touching a record.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path

import pytest

from pi_glink.app import create_server
from pi_glink.config import build_config
from pi_glink.kernel.rpc import PiRpcClient
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


def fake_factory(argv, *, cwd=None, env=None, on_event=None):
    merged = dict(env or {})
    merged["FAKE_PI_WRITE_REPORT"] = "0"
    return PiRpcClient(
        [sys.executable, str(FAKE_PI), *argv[1:]], cwd=cwd, env=merged, on_event=on_event
    )


def wait_for(predicate, *, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _start_owned_run(server) -> str:
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    try:
        body = json.dumps({"prompt": "第一个宿主仍在执行 NOSETTLE"}).encode()
        connection.request("POST", "/api/runs", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 201
        run_id = json.loads(response.read())["run"]["runId"]
    finally:
        connection.close()
    # NOSETTLE: the fake emits its whole script except agent_settled, ending
    # with agent_end. Once that stage is logged, the owner writes nothing more
    # until it is stopped, so byte snapshots of its records are stable.
    assert wait_for(
        lambda: any(
            e["type"] == "run_stage" and e["payload"].get("stage") == "agent_end"
            for e in server.store.iter_events(run_id)
        )
    )
    return run_id


@pytest.mark.parametrize("same_port", [True, False])
def test_a_second_host_cannot_reconcile_a_live_owners_run(tmp_path: Path, same_port: bool) -> None:
    config = make_config(tmp_path)
    first = create_server(config, client_factory=fake_factory)
    thread = threading.Thread(target=first.serve_forever, daemon=True)
    thread.start()
    try:
        run_id = _start_owned_run(first)
        log_before = first.store.events_path(run_id).read_bytes()
        meta_before = (first.store.run_dir(run_id) / "meta.json").read_bytes()
        port = first.server_address[1] if same_port else 0

        refusal: Exception | None = None
        try:
            second = create_server(replace(config, port=port), client_factory=fake_factory)
        except Exception as exc:  # noqa: BLE001 - which failure it was is asserted below
            refusal = exc
        else:
            second.server_close()

        # The live owner's run is untouched, however the second start ended.
        observed = first.store.get(run_id)
        assert observed.status == RUNNING and observed.stop_reason is None
        assert first.store.events_path(run_id).read_bytes() == log_before
        assert (first.store.run_dir(run_id) / "meta.json").read_bytes() == meta_before
        assert first.manager.active_run_id == run_id
        # And it was refused for the ownership, not by a later bind failure.
        assert type(refusal).__name__ == "DataRootBusy", refusal
        assert "占用" in str(refusal)

        assert first.manager.stop(run_id).accepted is True
        final = first.manager.wait(run_id, timeout=30)
        assert final.status == "cancelled", "the real owner still decides its run"
    finally:
        first.shutdown()
        first.server_close()
        thread.join(timeout=5)


def test_ownership_is_exclusive_across_processes(tmp_path: Path) -> None:
    """The real deployment case: the second host is another process."""
    from pi_glink.ownership import DataRootOwnership  # noqa: PLC0415

    data_root = tmp_path / "data"
    ownership = DataRootOwnership.acquire(data_root)
    probe = (
        "import sys; from pathlib import Path\n"
        "from pi_glink.ownership import DataRootBusy, DataRootOwnership\n"
        "try:\n"
        "    DataRootOwnership.acquire(Path(sys.argv[1]))\n"
        "except DataRootBusy:\n"
        "    sys.exit(17)\n"
        "sys.exit(0)\n"
    )
    try:
        busy = subprocess.run([sys.executable, "-c", probe, str(data_root)], timeout=30)
        assert busy.returncode == 17, "a live owner in another process must be detected"
    finally:
        ownership.release()
    free = subprocess.run([sys.executable, "-c", probe, str(data_root)], timeout=30)
    assert free.returncode == 0, "a released (or dead) owner no longer blocks recovery"


def test_reconcile_without_ownership_is_refused_and_changes_nothing(tmp_path: Path) -> None:
    from pi_glink.events import EventBus  # noqa: PLC0415
    from pi_glink.runs.runner import RunManager  # noqa: PLC0415
    from pi_glink.ownership import OwnershipRequired  # noqa: PLC0415

    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    store.create("别的宿主的 run", run_id="theirs", workspace=tmp_path / "ws")
    meta_before = (store.run_dir("theirs") / "meta.json").read_bytes()
    manager = RunManager(config, EventBus(), store)
    with pytest.raises(OwnershipRequired):
        manager.reconcile()
    assert (store.run_dir("theirs") / "meta.json").read_bytes() == meta_before
    assert not store.events_path("theirs").exists()


def test_a_dead_owners_running_record_is_reconciled_by_the_next_owner(tmp_path: Path) -> None:
    """Once the owner is gone (lock released), recovery proceeds as before."""
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    store.create("上一次进程遗留", run_id="orphan", workspace=tmp_path / "ws")
    server = create_server(config, client_factory=fake_factory)
    try:
        record = server.store.get("orphan")
        assert record.status == FAILED and record.stop_reason == "interrupted"
        assert record.cleanup["released"] is None, "an unknown resource state is not 'released'"
    finally:
        server.server_close()


def test_resource_adjudication_requires_the_host_to_be_stopped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator command runs under the same ownership as the host."""
    from pi_glink.app import _resource_command  # noqa: PLC0415

    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    store.create("资源未确认", run_id="held", workspace=tmp_path / "ws")
    store.update(
        "held",
        status="finished",
        finished_at=time.time(),
        cleanup={"released": False, "failures": ["synthetic kill failed"], "external": {}},
    )
    host = create_server(config, client_factory=fake_factory)
    try:
        assert _resource_command(config, "held", "已核对") == 2
        assert store.get("held").resource_adjudication is None, "a live host keeps ownership"
    finally:
        host.server_close()

    assert _resource_command(config, None, None) == 0
    assert '"runId": "held"' in capsys.readouterr().out
    assert _resource_command(config, "held", "已在控制台确认资源不存在") == 0
    adjudication = store.get("held").resource_adjudication
    assert adjudication["decision"] == "released" and "控制台" in adjudication["note"]
    assert store.get("held").cleanup["released"] is False, "the cleanup fact is kept"
    assert _resource_command(config, "held", "重复裁决") == 1, "nothing left to adjudicate"


# -- data-root permissions (GLINK-HARDEN-01) -----------------------------------


def test_a_fresh_data_root_is_private(tmp_path: Path) -> None:
    import stat  # noqa: PLC0415

    from pi_glink.ownership import DataRootOwnership  # noqa: PLC0415

    root = tmp_path / "data"
    ownership = DataRootOwnership.acquire(root)
    try:
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
    finally:
        ownership.release()


@pytest.mark.parametrize("mode", [0o777, 0o770, 0o775, 0o757, 0o702])
def test_a_group_or_other_writable_data_root_is_refused_untouched(
    tmp_path: Path, mode: int
) -> None:
    """Fail closed and report; the host never chmods an existing root for you."""
    import stat  # noqa: PLC0415

    from pi_glink.ownership import DataRootOwnership, DataRootUnsafe  # noqa: PLC0415

    root = tmp_path / "wide"
    root.mkdir()
    root.chmod(mode)
    with pytest.raises(DataRootUnsafe) as refusal:
        DataRootOwnership.acquire(root)
    assert "权限过宽" in str(refusal.value)
    assert "700" in str(refusal.value), "错误信息要给出操作员的下一步"
    assert stat.S_IMODE(root.stat().st_mode) == mode, "不得自动 chmod"


def test_a_symlinked_or_non_directory_data_root_is_refused(tmp_path: Path) -> None:
    from pi_glink.ownership import DataRootOwnership, DataRootUnsafe  # noqa: PLC0415

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(DataRootUnsafe, match="符号链接"):
        DataRootOwnership.acquire(link)

    file_root = tmp_path / "not-a-dir"
    file_root.write_text("x", encoding="utf-8")
    with pytest.raises(DataRootUnsafe, match="不是目录"):
        DataRootOwnership.acquire(file_root)


def test_the_host_refuses_to_start_on_an_unsafe_data_root(tmp_path: Path) -> None:
    """The same rule applies to the real start path, not only to the lock class."""
    from pi_glink.ownership import DataRootUnsafe  # noqa: PLC0415

    config = make_config(tmp_path)
    config.data_root.mkdir(parents=True, exist_ok=True)
    config.data_root.chmod(0o777)
    with pytest.raises(DataRootUnsafe):
        create_server(config, client_factory=fake_factory)


def test_the_cli_reports_an_unsafe_data_root_and_exits_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from pi_glink.app import main as app_main  # noqa: PLC0415

    config = make_config(tmp_path)
    config.data_root.mkdir(parents=True, exist_ok=True)
    config.data_root.chmod(0o777)
    monkeypatch.setattr("pi_glink.app.load_config", lambda: config)
    assert app_main([]) == 2
    assert "权限过宽" in capsys.readouterr().err
