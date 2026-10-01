"""ACP-15 R1/B1: reject linked remote roots before traversal or target I/O."""
from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest

from pi_glink.events import EventBus
from pi_glink.kernel.acp_e2b_env import SANDBOX_ACP_HOME
from pi_glink.kernel.acp_state_compat import AcpStateCompatError, repair_resume_home
from pi_glink.runs.runner import RunManager
from pi_glink.store import FAILED, FINISHED, RunStore
from test_dsh_session_config import MODEL_OPTIONS, REASONING_OPTION, wired_config
from test_kernel_acp_hot_path import ParkableFiles, ParkableSandbox
from test_kernel_acp_runner import read_log
from test_kernel_acp_state_compat import CompatCommands, rows


class TrackingFiles(ParkableFiles):
    """Real files; SDK entry kinds use lstat, and outside effects are counted."""
    def __init__(self, sandbox, outside):
        super().__init__(sandbox)
        self.outside = outside
        self.external_log_reads = 0
        self.external_scratch_writes = 0
        self.external_directory_lists = 0

    def _outside(self, path):
        return self._real(path).resolve().is_relative_to(self.outside)

    def read(self, path, **kwargs):
        if self._outside(path) and path.endswith("session.v3.jsonl.zstd"):
            self.external_log_reads += 1
        return super().read(path, **kwargs)

    def write(self, path, data, **kwargs):
        if self._outside(path) and ".acp15-" in path:
            self.external_scratch_writes += 1
        return super().write(path, data, **kwargs)

    def list(self, path, **kwargs):
        if self._outside(path):
            self.external_directory_lists += 1
        entries = super().list(path, **kwargs)
        for entry in entries:
            real = self._real(entry.path)
            if real.is_symlink():
                entry.type = "symlink"
                entry.size = 0
        return entries


@pytest.mark.parametrize("linked", ["home", "parent"])
def test_hot_manager_rejects_linked_home_before_external_io_and_spawn(tmp_path, linked):
    outside = tmp_path / "outside"
    outside.mkdir()
    log = tmp_path / "agent.jsonl"
    config = wired_config(tmp_path, {
        "FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True,
        "FAKE_ACP_DSH_V3_LOG": True, "FAKE_ACP_DSH_WRITE_V3_ROWS": rows()[1:],
        "FAKE_ACP_CONFIG_OPTIONS": MODEL_OPTIONS + [REASONING_OPTION],
    }, env={
        "LOCAL_WEB_EXECUTOR": "e2b", "E2B_API_KEY": "e2b-synthetic-key",
        "PI_GLINK_EGRESS_PUBLIC_URL": "https://egress.example.test",
        "PI_GLINK_CONVERSATION_LEASE_TTL_SECONDS": "7200",
    })
    sandboxes = []

    class LinkedParkableSandbox(ParkableSandbox):
        def pause(self, **kwargs):
            # The parent recovered, stopped its agent, wrote generation/lease.
            # Poison its filesystem immediately before pause. Reboot reconnect
            # must not treat disk affinity as proof of path ownership.
            target = self.root / ("home/user/acp/home" if linked == "home" else "home/user/acp")
            shutil.move(str(target), outside / "state")
            target.symlink_to(outside / "state", target_is_directory=True)
            return super().pause(**kwargs)

    def factory(**_kwargs):
        sandbox = LinkedParkableSandbox(tmp_path, sandbox_id="linked-park", report=None)
        sandbox.files = TrackingFiles(sandbox, outside)
        sandbox.commands = CompatCommands(sandbox, sandbox.commands)
        sandboxes.append(sandbox)
        return sandbox

    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store, sandbox_factory=factory,
                         sandbox_resume_factory=ParkableSandbox.connect)
    try:
        first = manager.start("父轮保存旧审计会话")
        parent = manager.wait(first.run_id, timeout=30)
        assert parent is not None and parent.status == FINISHED, parent.error
        sandbox = sandboxes[0]
        assert sandbox.state == "paused"
        external = next(outside.glob("**/session.v3.jsonl.zstd"))
        before = external.read_bytes()
        authority = store.conversation_dir(first.run_id) / "state"
        manifest = (authority / "manifest.json").read_bytes()
        child = manager.start("同沙箱追问必须安全拒绝", parent_run_id=first.run_id)
        final = manager.wait(child.run_id, timeout=30)
        # These assertions intentionally precede status: a failure after I/O is
        # still unsafe, and normal failure recovery must not traverse it either.
        print("B1", linked, "external_log_reads=", sandbox.files.external_log_reads,
              "external_scratch_writes=", sandbox.files.external_scratch_writes,
              "external_directory_lists=", sandbox.files.external_directory_lists,
              "status=", final.status)
        assert sandbox.files.external_log_reads == 0
        assert sandbox.files.external_scratch_writes == 0
        assert sandbox.files.external_directory_lists == 0
        assert final is not None and final.status == FAILED
        assert final.stop_reason == "execution_error"
        assert sandbox.connect_calls[-1]["on_resume"] == "reboot"
        assert len(sandboxes) == 1
        assert len([e for e in read_log(log) if e["event"] == "initialize"]) == 1
        assert not [e for e in read_log(log) if e["event"] == "session_resume"]
        assert external.read_bytes() == before
        assert (authority / "manifest.json").read_bytes() == manifest
        assert final.cleanup["released"] is True
    finally:
        for sandbox in sandboxes:
            sandbox.kill()


@pytest.mark.parametrize("linked", ["/home", "/home/user", "/home/user/acp", SANDBOX_ACP_HOME])
def test_direct_remote_validation_stops_at_first_link(tmp_path, linked):
    # Thin SDK descriptor fixture: root is trusted, one directory entry is a
    # symlink. Accessing that entry as a directory, or payload I/O, is forbidden.
    parents = ["/", "/home", "/home/user", "/home/user/acp", SANDBOX_ACP_HOME]
    calls = []

    def listing(path, **kwargs):
        calls.append(path)
        if path == linked or path.startswith(linked + "/"):
            pytest.fail("listed through an unverified link")
        i = parents.index(path)
        return [SimpleNamespace(path=parents[i + 1], type=("symlink" if parents[i + 1] == linked
                                                          else "dir"))]

    def forbidden(*_args, **_kwargs):
        pytest.fail("payload I/O before directory validation")

    handle = SimpleNamespace(files=SimpleNamespace(list=listing, read=forbidden, write=forbidden))
    with pytest.raises(AcpStateCompatError):
        repair_resume_home(SANDBOX_ACP_HOME, handle=handle)
    assert linked not in calls
