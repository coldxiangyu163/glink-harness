"""ACP-15: retired audit envelopes in a real inherited/rehydrated v3 log."""
from __future__ import annotations

import json

import pytest

from pi_glink.events import EventBus
from pi_glink.runs.runner import RunManager
from pi_glink.store import FAILED, FINISHED, RunStore
from test_dsh_session_config import (
    DEFAULT_MODEL, MODEL_OPTIONS, REASONING_OPTION, TARGET_MODEL, wired_config,
)
from test_kernel_acp_runner import payloads, read_events, read_log

zstd = pytest.importorskip("zstandard")
RELATIVE = ".dsh/sessions/--workspace--/s/session.v3.jsonl.zstd"
AUDIT = "web/jina-fetch-request"


def rows(session_id="s", *, unknown=False):
    return [
        {"type": "session", "version": 3, "id": session_id},
        {"type": AUDIT, "seq": 0, "time": 1,
         "data": {"url": "https://private.example/source", "chars": 123,
                  "body": "session-body-must-not-be-logged", "integer": 2**60 + 3}},
        {"type": "future/required" if unknown else "tool/result", "seq": 1, "time": 2,
         "sourceEventSeqs": [0], "surfaceOp": "append", "data": {"nested": [1, 2]}},
        {"type": AUDIT, "seq": 2, "time": 3, "ignorable": True, "data": {}},
    ]


def compressed(events):
    # Exactly one header row in the first frame; concatenated zstd frames.
    return b"".join(zstd.ZstdCompressor(write_checksum=True).compress(
        json.dumps(row, ensure_ascii=False).encode() + b"\n"
    ) for row in events)


def decoded(data):
    with zstd.ZstdDecompressor().stream_reader(data) as stream:
        return [json.loads(line) for line in stream.read().splitlines()]


def seed(home, session_id="s", *, unknown=False):
    relative = RELATIVE.replace("/s/", f"/{session_id}/")
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    data = compressed(rows(session_id, unknown=unknown))
    path.write_bytes(data)
    (path.parent / "session.lock").write_bytes(b"")
    return path, data


@pytest.mark.parametrize("unknown", [False, True])
def test_repaired_resume_still_applies_model_before_prompt(tmp_path, unknown):
    """Real Manager → inheritance → parser → setConfig → prompt/turn, not canned status."""
    log = tmp_path / "agent.jsonl"
    config = wired_config(tmp_path, {
        "FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True,
        "FAKE_ACP_DSH_V3_LOG": True,
        "FAKE_ACP_CONFIG_OPTIONS": MODEL_OPTIONS + [REASONING_OPTION],
    })
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    parent = manager.start("旧会话的首轮")
    first = manager.wait(parent.run_id, timeout=30)
    assert first is not None and first.status == FINISHED
    home = store.run_dir(parent.run_id) / "acp-agent/home"
    original, original_bytes = seed(home, first.session_id, unknown=unknown)
    parent_events = read_events(store, parent.run_id)
    child = manager.start("在原会话追问", parent_run_id=parent.run_id)
    final = manager.wait(child.run_id, timeout=30)
    assert final is not None and final.status == (FAILED if unknown else FINISHED)
    assert original.read_bytes() == original_bytes
    assert read_events(store, parent.run_id) == parent_events
    entries = read_log(log)
    resume = next(i for i, entry in enumerate(entries) if entry["event"] == "session_resume")
    mutations = [e for e in entries[resume:] if e["event"] == "config_option"]
    if unknown:
        assert "unknown to this harness" in final.error
        assert not mutations
        assert not [e for e in entries[resume:] if e["event"] == "prompt"]
        return
    assert [(e["configId"], e["value"]) for e in mutations] == [
        ("model", TARGET_MODEL), ("reasoning_effort", "xhigh"),
    ]
    prompt = next(i for i in range(resume, len(entries)) if entries[i]["event"] == "prompt")
    assert all(entries.index(e) < prompt for e in mutations)
    events = read_events(store, child.run_id)
    ready = payloads(events, "session_ready")[0]
    assert ready["sessionId"] == first.session_id
    assert ready["resumed"] is True
    assert ready["configOptions"][0]["currentValue"] == TARGET_MODEL != DEFAULT_MODEL
    assert payloads(events, "turn_started")
    copied = store.run_dir(child.run_id) / "acp-agent/home" / original.relative_to(home)
    expected = rows(first.session_id)
    expected[1]["ignorable"] = True
    assert decoded(copied.read_bytes()) == expected
    assert (copied.parent / "session.lock").read_bytes() == b""


def test_annotation_preserves_every_field_and_is_byte_idempotent(tmp_path, caplog, capsys):
    from pi_glink.kernel.acp_state_compat import annotate_v3_log, repair_resume_home

    home = tmp_path / "home"
    path, before = seed(home)
    expected = rows()
    expected[1]["ignorable"] = True
    repaired, size = annotate_v3_log(before)
    assert decoded(repaired) == expected
    assert size > 0
    assert len(decoded(repaired)) == len(rows())
    assert [e.get("seq") for e in decoded(repaired)] == [e.get("seq") for e in rows()]
    assert annotate_v3_log(repaired)[0] == repaired
    # Verify the first physical frame is still exactly one header line.
    first = zstd.ZstdDecompressor().decompressobj().decompress(repaired)
    assert first.count(b"\n") == 1 and json.loads(first) == rows()[0]
    lock = path.parent / "session.lock"
    lock_inode = lock.stat().st_ino
    repair_resume_home(home)
    published = path.read_bytes()
    inode = path.stat().st_ino
    repair_resume_home(home)
    assert path.read_bytes() == published and path.stat().st_ino == inode
    assert lock.read_bytes() == b"" and lock.stat().st_ino == lock_inode
    assert path.stat().st_mode & 0o777 == 0o600
    for private in ("private.example", "session-body-must-not-be-logged"):
        assert private not in caplog.text
        assert private not in capsys.readouterr().out


@pytest.mark.parametrize("case", ["truncated", "checksum", "bad-json", "duplicate", "false-flag", "header-frame"])
def test_malformed_log_is_not_reinterpreted_or_published(tmp_path, case):
    from pi_glink.kernel.acp_state_compat import AcpStateCompatError, repair_resume_home

    home = tmp_path / "home"
    path, data = seed(home)
    if case == "truncated":
        data = data[:-3]
    elif case == "checksum":
        data = data[:-1] + bytes([data[-1] ^ 1])
    elif case == "bad-json":
        data += zstd.ZstdCompressor().compress(b'{"broken":\n')
    elif case == "duplicate":
        data += zstd.ZstdCompressor().compress(b'{"type":"web/jina-fetch-request","type":"other"}\n')
    elif case == "false-flag":
        events = rows()
        events[1]["ignorable"] = False
        data = compressed(events)
    else:
        data = zstd.ZstdCompressor().compress(b"".join(json.dumps(r).encode() + b"\n" for r in rows()))
    path.write_bytes(data)
    with pytest.raises(AcpStateCompatError):
        repair_resume_home(home)
    assert path.read_bytes() == data
    assert not list(path.parent.glob("*.tmp"))


def test_decoder_bounds_apply_to_all_concatenated_frames(tmp_path):
    from pi_glink.kernel.acp_state_compat import AcpStateCompatError, annotate_v3_log

    data = compressed(rows())
    with pytest.raises(AcpStateCompatError, match="上限"):
        annotate_v3_log(data, decode_limit=100)


@pytest.mark.parametrize("link_at", [".dsh", ".dsh/sessions", ".dsh/sessions/--workspace--/s",
                                    RELATIVE])
def test_local_links_never_read_or_rewrite_the_target(tmp_path, link_at, monkeypatch):
    from pi_glink.kernel import acp_state_compat as compat

    outside = tmp_path / "outside"
    target, data = seed(outside)
    home = tmp_path / "home"
    link = home / link_at
    link.parent.mkdir(parents=True)
    link.symlink_to(outside / link_at)
    monkeypatch.setattr(compat, "_codec", lambda *_: pytest.fail("followed a symlink"))
    compat.repair_resume_home(home)
    assert target.read_bytes() == data


def test_unrelated_formats_and_paths_are_not_repaired(tmp_path, monkeypatch):
    from pi_glink.kernel import acp_state_compat as compat

    home = tmp_path / "home"
    for relative in (".codex/sessions/session.v3.jsonl.zstd", "other/session.v3.jsonl.zstd",
                     RELATIVE.replace("v3", "v4"), RELATIVE.replace(".zstd", "")):
        target = home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"not-a-zstd-log")
    monkeypatch.setattr(compat, "_codec", lambda *_: pytest.fail("unrelated file"))
    compat.repair_resume_home(home)


def test_non_v3_header_and_already_marked_log_keep_original_bytes():
    from pi_glink.kernel.acp_state_compat import annotate_v3_log

    events = rows()
    events[0]["version"] = 4
    data = compressed(events)
    assert annotate_v3_log(data)[0] == data
    events[0]["version"] = 3
    events[1]["ignorable"] = True
    data = compressed(events)
    assert annotate_v3_log(data)[0] == data


class CompatCommands:
    """Execute the real atomic publish command; only cloud/agent I/O is replaced."""
    def __init__(self, sandbox, delegate):
        self.sandbox = sandbox
        self.delegate = delegate
        self.repairs = []

    def __getattr__(self, key):
        return getattr(self.delegate, key)

    def run(self, command, **kwargs):
        if ".acp15-" not in command:
            return self.delegate.run(command, **kwargs)
        import subprocess
        from pi_glink.kernel.acp_e2b_env import SANDBOX_ACP_HOME
        from test_kernel_acp_e2b import _Result

        real_home = self.sandbox.root / "home/user/acp/home"
        mapped = command.replace(SANDBOX_ACP_HOME, str(real_home))
        self.repairs.append(command)
        result = subprocess.run(["sh", "-c", mapped], capture_output=True, timeout=kwargs["timeout"])
        return _Result(result.returncode)


@pytest.mark.parametrize("path_kind", ["files", "archive", "hot"])
def test_remote_repair_precedes_spawn_for_cold_and_reboot_paths(tmp_path, path_kind):
    from pi_glink.kernel.acp_e2b_env import SANDBOX_ACP_HOME
    from pi_glink.kernel.conversation_lease import read_lease
    from test_kernel_acp_hot_path import ParkableSandbox

    log = tmp_path / "remote-agent.jsonl"
    config = wired_config(tmp_path, {
        "FAKE_ACP_LOG": str(log), "FAKE_ACP_WRITE_REPORT": True,
        "FAKE_ACP_DSH_V3_LOG": True, "FAKE_ACP_DSH_WRITE_V3_ROWS": rows()[1:],
        "FAKE_ACP_CONFIG_OPTIONS": MODEL_OPTIONS + [REASONING_OPTION],
    }, env={
        "LOCAL_WEB_EXECUTOR": "e2b", "E2B_API_KEY": "e2b-synthetic-key",
        "PI_GLINK_EGRESS_PUBLIC_URL": "https://egress.example.test" if path_kind != "files" else "",
        "PI_GLINK_CONVERSATION_LEASE_TTL_SECONDS": "7200" if path_kind == "hot" else "0",
    })
    store = RunStore(config.data_root / "runs")
    sandboxes = []
    holder = {}

    def factory(**_):
        sandbox = ParkableSandbox(tmp_path, sandbox_id=f"compat-{len(sandboxes)}",
                                   report=None, conversation_dir=holder.get("conversation"))
        sandbox.commands = CompatCommands(sandbox, sandbox.commands)
        sandboxes.append(sandbox)
        return sandbox

    manager = RunManager(config, EventBus(), store, sandbox_factory=factory,
                         sandbox_resume_factory=ParkableSandbox.connect)
    first = manager.start("首轮旧审计写入")
    parent = manager.wait(first.run_id, timeout=30)
    assert parent is not None and parent.status == FINISHED, parent.error
    conversation = store.conversation_dir(first.run_id)
    holder["conversation"] = conversation
    authority = (store.run_dir(first.run_id) / "acp-agent/recovered"
                 if path_kind == "files" else conversation / "state")
    original = next(authority.glob(".dsh/sessions/*/*/session.v3.jsonl.zstd"))
    original_bytes = original.read_bytes()
    manifest = authority / "manifest.json"
    original_manifest = manifest.read_bytes() if manifest.exists() else None
    # Observe the boundary after start's compatibility, before prompt/recovery
    # can normally publish this new run. Authority must still be unchanged.
    from pi_glink.kernel.acp_execution import AcpExecutionAdapter
    real_deliver = AcpExecutionAdapter.deliver_input

    def deliver(adapter):
        if adapter.spec.resume_session:
            assert original.read_bytes() == original_bytes
            assert (manifest.read_bytes() if manifest.exists() else None) == original_manifest
        real_deliver(adapter)

    from unittest.mock import patch
    try:
        with patch.object(AcpExecutionAdapter, "deliver_input", deliver):
            second = manager.start("追问新模型配置", parent_run_id=first.run_id)
            final = manager.wait(second.run_id, timeout=30)
        assert final is not None and final.status == FINISHED, final.error
        ready = payloads(read_events(store, second.run_id), "session_ready")[0]
        assert ready["sessionId"] == parent.session_id and ready["resumed"] is True
        assert ready["configOptions"][0]["currentValue"] == TARGET_MODEL
        assert payloads(read_events(store, second.run_id), "turn_started")
        active = sandboxes[-1]
        assert len(active.commands.repairs) == 1
        copied = next((active.root / "home/user/acp/home").glob(
            ".dsh/sessions/*/*/session.v3.jsonl.zstd"))
        expected = rows(parent.session_id)
        expected[1]["ignorable"] = True
        assert decoded(copied.read_bytes()) == expected
        assert (copied.parent / "session.lock").read_bytes() == b""
        if path_kind == "hot":
            assert len(sandboxes) == 1
            assert active.connect_calls[-1]["on_resume"] == "reboot"
            assert active.commands.backfills == 0
            assert read_lease(conversation).generation == 2
        else:
            assert len(sandboxes) == 2
            assert active.commands.backfills == (1 if path_kind == "archive" else 0)
        assert SANDBOX_ACP_HOME in active.commands.repairs[0]
    finally:
        for sandbox in sandboxes:
            sandbox.kill()


@pytest.mark.parametrize("pack", ["codex", "opencode"])
def test_other_packs_do_not_run_dsh_compatibility(tmp_path, monkeypatch, pack):
    from acp_pack import write_acp_pack
    from pi_glink.config import build_config
    from pi_glink.kernel import acp_execution
    from test_kernel_acp_runner import acp_command, REPO_ROOT

    packs = write_acp_pack(tmp_path, acp_command(tmp_path, {}), name=pack)
    config = build_config({"PI_GLINK_HARNESS": pack, "PI_GLINK_HARNESSES_DIR": str(packs),
                           "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data")}, REPO_ROOT)
    monkeypatch.setattr(acp_execution, "repair_resume_home",
                        lambda *_args, **_kw: pytest.fail("non-dsh compatibility"))
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    parent = manager.start("首轮")
    first = manager.wait(parent.run_id, timeout=30)
    original, data = seed(store.run_dir(parent.run_id) / "acp-agent/home", first.session_id)
    child = manager.start("追问", parent_run_id=parent.run_id)
    final = manager.wait(child.run_id, timeout=30)
    assert final is not None and final.status == FINISHED
    copied = store.run_dir(child.run_id) / "acp-agent/home" / original.relative_to(
        store.run_dir(parent.run_id) / "acp-agent/home")
    assert copied.read_bytes() == data and original.read_bytes() == data


def test_remote_failed_atomic_publish_leaves_original_and_cleans_only_scratch(tmp_path, monkeypatch):
    from pi_glink.kernel.acp_e2b_env import SANDBOX_ACP_HOME
    from pi_glink.kernel.acp_state_compat import AcpStateCompatError, repair_resume_home
    from test_kernel_acp_hot_path import ParkableSandbox
    from test_kernel_acp_e2b import _Result

    sandbox = ParkableSandbox(tmp_path, sandbox_id="failed-publish", report=None)
    original, data = seed(sandbox.root / "home/user/acp/home")
    monkeypatch.setattr(sandbox.commands, "run", lambda *_a, **_k: _Result(9))
    try:
        with pytest.raises(AcpStateCompatError, match="发布失败"):
            repair_resume_home(SANDBOX_ACP_HOME, handle=sandbox)
        assert original.read_bytes() == data
        assert (original.parent / "session.lock").read_bytes() == b""
        assert not list(original.parent.glob("*.acp15-*"))
    finally:
        sandbox.kill()


def test_codec_failure_discards_process_output_without_logging_payload(monkeypatch, caplog, capsys):
    from types import SimpleNamespace
    from pi_glink.kernel import acp_state_compat as compat

    private = b"session-body-must-not-be-logged https://private.example/source"
    monkeypatch.setattr(compat.subprocess, "run", lambda *_a, **_k:
                        SimpleNamespace(returncode=1, stdout=private, stderr=private))
    with pytest.raises(compat.AcpStateCompatError) as error:
        compat.annotate_v3_log(compressed(rows()))
    assert "private.example" not in str(error.value) + caplog.text + capsys.readouterr().out
