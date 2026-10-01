"""Credential-boundary regressions (audit reproduction included).

The completion audit reproduced a leak: a `tool_execution_end` frame carrying
``OPENAI_API_KEY=AUDIT_DUMMY_SECRET_…`` survived projection verbatim, which means
a bash `env` call (or a config-file read, or the model quoting a key back) would
reach the stored events, the SSE stream and the browser. These tests pin the fix at
every egress: projection, the runner's emit path, the report download and the
repo scanner.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from glink_harness.app import GlinkHarnessServer, create_server
from glink_harness.config import build_config
from glink_harness.events import EventBus
from glink_harness.kernel.projection import project_event
from glink_harness.kernel.rpc import PiRpcClient
from glink_harness.runs.runner import RunManager
from glink_harness.runs.secrets import RunSecretBoundary
from glink_harness.redaction import REPLACEMENT, SecretRedactor, redactor_from_config
from glink_harness.store import RunStore, RunSpec
from store_helpers import events_text, finish, stored_record, stored_text

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_PI = Path(__file__).resolve().parent / "fixtures" / "fake_pi.py"

#: The exact shape the auditor used.
AUDIT_SECRET = "AUDIT_DUMMY_SECRET_0123456789abcdef"


def make_config(tmp_path: Path, **overrides: str):
    values = {
        "OPENAI_API_KEY": AUDIT_SECRET,
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "LOCAL_WEB_PORT": "0",  # ephemeral port: tests must not fight over 7860
    }
    values.update(overrides)
    return build_config(values, REPO_ROOT)


# -- projection boundary ------------------------------------------------------


def test_audit_reproduction_tool_output_is_redacted() -> None:
    """The auditor's case: a tool echoing the env var must not survive."""
    frame = {
        "type": "tool_execution_end",
        "toolCallId": "c1",
        "toolName": "bash",
        "result": {"content": [{"type": "text", "text": f"OPENAI_API_KEY={AUDIT_SECRET}"}]},
        "isError": False,
    }
    redactor = SecretRedactor([AUDIT_SECRET])
    (kind, payload), = project_event(frame, redactor)
    rendered = json.dumps(payload, ensure_ascii=False)
    assert kind == "tool_finished"
    assert AUDIT_SECRET not in rendered
    assert REPLACEMENT in rendered


def test_projection_without_an_explicit_redactor_is_fail_closed() -> None:
    """The auditor's exact repro must be masked even when no redactor is passed.

    ``project_event(frame)`` is the narrowest call shape there is; if it leaked,
    every caller that forgot the host redactor would leak too.
    """
    frame = {
        "type": "tool_execution_end",
        "toolCallId": "c1",
        "toolName": "bash",
        "result": {"content": [{"type": "text", "text": f"OPENAI_API_KEY={AUDIT_SECRET}"}]},
        "isError": False,
    }
    (_kind, payload), = project_event(frame)  # no redactor argument on purpose
    rendered = json.dumps(payload)
    assert AUDIT_SECRET not in rendered
    assert REPLACEMENT in rendered


def test_shape_detection_catches_secrets_the_host_never_saw() -> None:
    """A key pasted into a prompt (unknown to the host) is still masked."""
    frame = {
        "type": "tool_execution_end",
        "toolCallId": "c1",
        "toolName": "bash",
        "result": {"content": [{"type": "text", "text": "OPENAI_API_KEY=sk-unknown000111222333444555"}]},
        "isError": False,
    }
    (_kind, payload), = project_event(frame, SecretRedactor())
    rendered = json.dumps(payload)
    assert "sk-unknown000111222333444555" not in rendered
    assert REPLACEMENT in rendered


def test_assistant_text_args_and_headers_are_redacted() -> None:
    redactor = SecretRedactor([AUDIT_SECRET])
    cases = [
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": f"key: {AUDIT_SECRET}"}},
        {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": f"{AUDIT_SECRET}"}]}},
        {"type": "tool_execution_start", "toolCallId": "c", "toolName": "bash", "args": {"command": f"echo {AUDIT_SECRET}"}},
        {"type": "tool_execution_update", "toolCallId": "c", "toolName": "bash", "partialResult": f"Authorization: Bearer {AUDIT_SECRET}"},
        {"type": "extension_error", "error": f"failed with {AUDIT_SECRET}", "extensionPath": "/x"},
    ]
    for frame in cases:
        produced = project_event(frame, redactor)
        assert produced, frame["type"]
        rendered = json.dumps([payload for _kind, payload in produced], ensure_ascii=False)
        assert AUDIT_SECRET not in rendered, frame["type"]


def test_redactor_hides_values_in_repr_and_known_list(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    redactor = redactor_from_config(config)
    assert AUDIT_SECRET in redactor.known_values  # used for detection only
    assert AUDIT_SECRET not in repr(redactor.known_values - {AUDIT_SECRET} | set())


# -- runner egress ------------------------------------------------------------


def _fake_factory(*, echo_secret: bool):
    def factory(argv, *, cwd=None, env=None, on_event=None):
        merged = dict(env or {})
        merged["FAKE_PI_WRITE_REPORT"] = "1"
        merged["FAKE_PI_ECHO_SECRET"] = "1" if echo_secret else "0"
        return PiRpcClient(
            [sys.executable, str(FAKE_PI), *argv[1:]],
            cwd=cwd,
            env=merged,
            on_event=on_event,
        )

    return factory


def test_runner_redacts_a_tool_echo_before_the_log_and_the_bus(tmp_path: Path) -> None:
    """End-to-end: pi prints the key, the product surfaces must not."""
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    bus = EventBus()
    manager = RunManager(config, bus, store, client_factory=_fake_factory(echo_secret=True))

    seen: list[dict] = []
    record = manager.start("让工具回显环境变量")
    subscriber = bus.subscribe(record.run_id)
    collector = threading.Thread(
        target=lambda: seen.extend(event.as_dict() for event in subscriber), daemon=True
    )
    collector.start()
    final = manager.wait(record.run_id, timeout=30)
    bus.close(record.run_id)
    collector.join(timeout=5)

    assert final.status == "finished"
    log_text = events_text(store, record.run_id)
    assert AUDIT_SECRET not in log_text, "event log leaked the model credential"
    assert REPLACEMENT in log_text, "redaction marker missing: was the echo even emitted?"
    stream_text = json.dumps(seen, ensure_ascii=False)
    assert AUDIT_SECRET not in stream_text, "SSE payload leaked the model credential"


# -- streaming (delta) boundary -------------------------------------------------


def test_stream_redactor_catches_every_split_position_of_a_known_secret() -> None:
    """Audit regression: two deltas that each contain half a known secret.

    Redaction used to run per delta, so neither half matched and the browser
    could reconstruct the value by concatenating the frames it received.
    """
    secret = AUDIT_SECRET
    prefix, suffix = "审计记录：", "。引用结束"
    for split in range(1, len(secret)):
        stream = SecretRedactor([secret]).stream()
        emitted = [
            stream.push("assistant_delta", prefix + secret[:split]),
            stream.push("assistant_delta", secret[split:] + suffix),
            stream.flush("assistant_delta"),
        ]
        combined = "".join(emitted)
        assert secret not in combined, f"split at {split} leaked the credential"
        assert REPLACEMENT in combined, f"split at {split} was not masked"
        assert combined.startswith(prefix) and combined.endswith(suffix), (
            f"split at {split} changed the surrounding text"
        )


def test_stream_redactor_catches_a_split_credential_shape() -> None:
    """A shape-only token (the host never knew the value) split across deltas."""
    token = "sk-" + "a1b2c3d4e5f6g7h8i9j0"
    for split in range(1, len(token)):
        stream = SecretRedactor().stream()
        combined = "".join(
            [
                stream.push("assistant_delta", token[:split]),
                stream.push("assistant_delta", token[split:]),
                stream.flush("assistant_delta"),
            ]
        )
        assert token not in combined, f"split at {split} leaked the token"


def test_stream_redactor_holds_an_incomplete_assignment_value() -> None:
    """``NAME=value`` may be split anywhere; the value is masked as a whole."""
    value = "AUDIT_VALUE_0123456789abcdef"
    text = f"环境检查：OPENAI_API_KEY={value}"
    for split in range(1, len(text)):
        stream = SecretRedactor().stream()
        combined = "".join(
            [
                stream.push("assistant_delta", text[:split]),
                stream.push("assistant_delta", text[split:]),
                stream.flush("assistant_delta"),
            ]
        )
        assert value not in combined, f"split at {split} leaked the value"
        assert "OPENAI_API_KEY=[REDACTED]" in combined


def test_stream_redactor_preserves_legitimate_text_sharing_a_prefix() -> None:
    """Holding a prefix must delay text, never alter it."""
    stream = SecretRedactor([AUDIT_SECRET]).stream()
    emitted = [
        stream.push("assistant_delta", "关键词 AUDIT"),
        stream.push("assistant_delta", " 表示审计，不是凭据。"),
        stream.flush("assistant_delta"),
    ]
    assert "".join(emitted) == "关键词 AUDIT 表示审计，不是凭据。"


def test_stream_redactor_is_independent_per_thinking_and_text_channel() -> None:
    stream = SecretRedactor([AUDIT_SECRET]).stream()
    thinking = stream.push("thinking_delta", AUDIT_SECRET[:20])
    text = stream.push("assistant_delta", AUDIT_SECRET[20:])
    flushed_thinking = stream.flush("thinking_delta")
    flushed_text = stream.flush("assistant_delta")
    assert AUDIT_SECRET not in (thinking + text + flushed_thinking + flushed_text)


#: Credential structures that defeat a hold-the-tail-then-mask stream: a
#: suffix equal to its own prefix (the R1 reproduction), periodic repeats, a
#: value that is a prefix of another, and repeats of a short motif.
STRUCTURED_SECRETS = ("abcdEFGHabcd", "abcabcabcabc", "aaaaaaaaaa", "xyxyxyxyZ", "abcdEFGHabcdQQ")
STREAM_CORPUS = (
    "前缀 abcdEFGHabcd 后缀",
    "abcdEFGHabcdEFGHabcd",
    "abcdEFGHabc 只差一个字符",
    "abcdEFGHabcdX",
    "abcabcabcabcabcabc end",
    "xyxyxyxyxyxyZ and xyxyxyxyZ",
    "abcdEFGHabcdQQ 比 abcdEFGHabcd 更长",
    "环境检查：OPENAI_API_KEY=AUDIT_VALUE_0123456789abcdef 然后",
    'config {"exaApiKey": "exa-value 0123456789"} done',
    "token sk-a1b2c3d4e5f6g7h8i9j0K and tasks-a1b2c3d4e5f6g7h8i9j0 ok",
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123 end",
    "open the openai docs; OPENAI_API_KEY = 'quotedvalue123' fine",
    "关键词 AUDIT 表示审计，不是凭据。",
    'He said "hello" and "myToken": "zzzzzzzzzzzz"',
)


def _stream_all(redactor: SecretRedactor, text: str, cuts: list[int]) -> str:
    stream = redactor.stream()
    emitted: list[str] = []
    previous = 0
    for cut in [*cuts, len(text)]:
        emitted.append(stream.push("assistant_delta", text[previous:cut]))
        previous = cut
    emitted.append(stream.flush("assistant_delta"))
    return "".join(emitted)


@pytest.mark.parametrize("known", [(), STRUCTURED_SECRETS])
def test_streamed_redaction_equals_whole_text_redaction_for_every_split(known) -> None:
    """R1: the stream releases exactly what whole-text redaction would.

    Every single split position, many random multi-frame splits and a
    character-by-character stream of each corpus line must give the same text
    as redacting the whole line at once — so no split can reassemble a value
    the whole-text redaction masks, and legitimate neighbours are unchanged.
    """
    import random  # noqa: PLC0415

    redactor = SecretRedactor(known)
    rng = random.Random(20260923)
    for text in STREAM_CORPUS:
        expected = redactor.redact(text)
        for split in range(1, len(text)):
            assert _stream_all(redactor, text, [split]) == expected, (text, split)
        for _ in range(200):
            count = min(len(text) - 1, rng.randint(2, 7))
            cuts = sorted(rng.sample(range(1, len(text)), count))
            assert _stream_all(redactor, text, cuts) == expected, (text, cuts)
        assert _stream_all(redactor, text, list(range(1, len(text)))) == expected, text


def test_overlapping_repeats_are_masked_as_one_region() -> None:
    """A repeat that overlaps a complete match must not leave its tail visible."""
    secret = "abcdEFGHabcd"
    redactor = SecretRedactor([secret])
    assert redactor.redact(secret + "EFGHabcd") == REPLACEMENT
    for split in range(1, len(secret)):
        streamed = _stream_all(redactor, secret + "EFGHabcd", [split, len(secret)])
        assert streamed == REPLACEMENT, split


def test_projector_passes_raw_deltas_to_one_stream() -> None:
    """Per-delta redaction must not hide the raw text the stream needs."""
    from glink_harness.kernel.projection import Projector  # noqa: PLC0415

    secret = "abcdEFGHabcd"
    projector = Projector(SecretRedactor([secret]))
    produced = []
    for delta in (secret, "EFGHabcd", " 结束"):
        produced += projector.project(
            {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": delta}}
        )
    produced += projector.project({"type": "message_end", "message": {"role": "user"}})
    text = "".join(payload["text"] for kind, payload in produced if kind == "assistant_delta")
    assert text == f"{REPLACEMENT} 结束"


def test_runner_stream_never_reassembles_a_structured_secret_on_any_surface(
    tmp_path: Path,
) -> None:
    """R1 real path: fake pi deltas → projection → runner → log, bus and replay.

    The secret's suffix equals its prefix. Every split position is streamed in
    one run (plus a three-frame split and an overlapping continuation); the
    durable log and the live bus must equal the whole-text redaction, and no
    eight-character fragment of the secret may appear anywhere.
    """
    secret = "abcdEFGHabcd"
    pieces = ["审计开始："]
    for split in range(1, len(secret)):
        pieces += [secret[:split], secret[split:] + f" 第{split}段 "]
    pieces += [secret[:4], secret[4:8], secret[8:], "EFGHabcd", "（重叠续写）结束"]

    config = make_config(tmp_path, OPENAI_API_KEY=secret)
    store = RunStore(config.data_root / "runs")
    bus = EventBus()

    def factory(argv, *, cwd=None, env=None, on_event=None):
        merged = dict(env or {})
        merged["FAKE_PI_WRITE_REPORT"] = "1"
        merged["FAKE_PI_TEXT_DELTAS"] = json.dumps(pieces, ensure_ascii=False)
        return PiRpcClient(
            [sys.executable, str(FAKE_PI), *argv[1:]], cwd=cwd, env=merged, on_event=on_event
        )

    manager = RunManager(config, bus, store, client_factory=factory)
    record = manager.start("流式脱敏：结构化凭据")
    live: list[dict] = []
    subscriber = bus.subscribe(record.run_id)
    collector = threading.Thread(
        target=lambda: live.extend(event.as_dict() for event in subscriber), daemon=True
    )
    collector.start()
    final = manager.wait(record.run_id, timeout=30)
    collector.join(timeout=10)

    assert final.status == "finished"
    persisted = [
        json.loads(line)
        for line in events_text(store, record.run_id).splitlines()
        if line.strip()
    ]
    expected = SecretRedactor([secret]).redact("".join(pieces))
    for frames in (persisted, [event for event in live]):
        emitted = "".join(
            event["payload"]["text"] for event in frames if event["type"] == "assistant_delta"
        )
        rendered = json.dumps(frames, ensure_ascii=False)
        fragments = [
            secret[start : start + length]
            for length in range(8, len(secret) + 1)
            for start in range(len(secret) - length + 1)
        ]
        assert not [fragment for fragment in fragments if fragment in rendered]
        assert emitted == expected
    assert [e["seq"] for e in persisted if e["type"] == "assistant_delta"] == [
        e["seq"] for e in live if e["type"] == "assistant_delta"
    ]


# -- host-side artifacts ------------------------------------------------------


def test_runner_redacts_the_report_in_place_and_marks_the_run(tmp_path: Path) -> None:
    """When pi writes the credential into its report, the host masks it in place."""
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(
        config, EventBus(), store, client_factory=_fake_factory(echo_secret=True)
    )
    record = manager.start("让报告也包含密钥")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == "finished"
    assert final.redacted is True, "a sanitised artifact must be visible in the run record"
    report = Path(final.report_path).read_text(encoding="utf-8")
    assert AUDIT_SECRET not in report
    assert REPLACEMENT in report


def test_session_file_is_redacted_on_the_way_out(tmp_path: Path) -> None:
    """pi writes its own session log; the host masks credentials there too."""
    config = make_config(tmp_path)
    manager = RunManager(config, EventBus(), RunStore(config.data_root / "runs"))
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session = session_dir / "session.jsonl"
    session.write_text(
        json.dumps({"type": "message", "text": f"OPENAI_API_KEY={AUDIT_SECRET}"}) + "\n",
        encoding="utf-8",
    )

    boundary = RunSecretBoundary(manager.redactor)
    changed = boundary.sanitize_session_file(str(session), root=session_dir)

    assert changed is True
    text = session.read_text(encoding="utf-8")
    assert AUDIT_SECRET not in text
    assert REPLACEMENT in text
    # The file stays valid JSONL so pi can still read its own history.
    assert json.loads(text.splitlines()[0])["text"].endswith(REPLACEMENT)


def test_session_file_outside_the_run_directory_is_refused(tmp_path: Path) -> None:
    """A kernel-reported path elsewhere on the host is never rewritten."""
    config = make_config(tmp_path)
    manager = RunManager(config, EventBus(), RunStore(config.data_root / "runs"))
    outside = tmp_path / "elsewhere.jsonl"
    original = f'OPENAI_API_KEY={AUDIT_SECRET}\n'
    outside.write_text(original, encoding="utf-8")
    allowed = tmp_path / "sessions"
    allowed.mkdir()
    linked = allowed / "link.jsonl"
    linked.symlink_to(outside)

    assert (
        RunSecretBoundary(manager.redactor).sanitize_session_file(str(outside), root=allowed)
        is False
    )
    assert (
        RunSecretBoundary(manager.redactor).sanitize_session_file(str(linked), root=allowed)
        is False
    )
    assert outside.read_text(encoding="utf-8") == original, "the target bytes must not change"


def test_session_file_without_secrets_is_left_untouched(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    manager = RunManager(config, EventBus(), RunStore(config.data_root / "runs"))
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session = session_dir / "clean.jsonl"
    original = json.dumps({"type": "message", "text": "普通内容"}) + "\n"
    session.write_text(original, encoding="utf-8")
    assert (
        RunSecretBoundary(manager.redactor).sanitize_session_file(str(session), root=session_dir)
        is False
    )
    assert session.read_text(encoding="utf-8") == original


def test_minimal_child_environment_does_not_inherit_host_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pi process gets a whitelisted environment, not the operator's shell."""
    from harness_helpers import compose_launch, execution_spec, test_egress
    from glink_harness.kernel import agent_env

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-host-secret-0123456789")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-host-secret-0123456789")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    config = make_config(tmp_path)
    runtime = agent_env.prepare_agent_dir(
        config,
        runtime_dir=tmp_path / "runtime",
        session_dir=tmp_path / "sessions",
        egress=test_egress(),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = execution_spec(config, runtime, workspace=workspace)
    launch, sandbox = compose_launch("pi", "inproc", spec, store_root=tmp_path / "store")
    env = sandbox.spawn(launch).env

    assert "ANTHROPIC_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["PATH"] == "/usr/bin:/bin"
    assert "OPENAI_API_KEY" not in env, "the model key travels via auth.json, not the environment"


def test_pasted_credential_in_the_prompt_is_not_stored_in_metadata(tmp_path: Path) -> None:
    """A user-pasted key must not land in the stored run record (the kernel still gets it)."""
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    received: list[str] = []

    def factory(argv, *, cwd=None, env=None, on_event=None):
        client = PiRpcClient(
            [sys.executable, str(FAKE_PI), *argv[1:]],
            cwd=cwd,
            env={**(env or {}), "FAKE_PI_WRITE_REPORT": "1"},
            on_event=on_event,
        )
        original_request = client.request

        def request(command, **kwargs):
            if command.get("type") == "prompt":
                received.append(str(command.get("message", "")))
            return original_request(command, **kwargs)

        client.request = request  # type: ignore[method-assign]
        return client

    manager = RunManager(config, EventBus(), store, client_factory=factory)
    record = manager.start(f"请记住这个值 {AUDIT_SECRET}，然后写出 report.md。")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == "finished"
    meta_text = stored_text(store)
    assert AUDIT_SECRET not in meta_text, "metadata must not persist a pasted credential"
    assert REPLACEMENT in meta_text
    assert received and AUDIT_SECRET in received[0], "the kernel still receives the user's instruction"


def test_workspace_artifacts_are_swept_for_credentials(tmp_path: Path) -> None:
    """A `write` call can leak into any artifact, not just report.md."""
    config = make_config(tmp_path)
    manager = RunManager(config, EventBus(), RunStore(config.data_root / "runs"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    dirty = workspace / "notes.md"
    dirty.write_text(f"记录：OPENAI_API_KEY={AUDIT_SECRET}\n", encoding="utf-8")
    clean = workspace / "clean.md"
    clean.write_text("普通内容\n", encoding="utf-8")
    binary = workspace / "image.bin"
    binary.write_bytes(b"\x00\x01\x02binary")

    changed = RunSecretBoundary(manager.redactor).sweep_tree(workspace)  # the boundary unit itself

    assert changed is True
    dirty_text = dirty.read_text(encoding="utf-8")
    assert AUDIT_SECRET not in dirty_text
    assert REPLACEMENT in dirty_text
    assert clean.read_text(encoding="utf-8") == "普通内容\n"
    assert binary.read_bytes() == b"\x00\x01\x02binary"


def test_workspace_sweep_never_writes_through_a_symlink(tmp_path: Path) -> None:
    """Audit regression: a linked artifact must not rewrite its external target."""
    config = make_config(tmp_path)
    manager = RunManager(config, EventBus(), RunStore(config.data_root / "runs"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    original = f"OPENAI_API_KEY={AUDIT_SECRET}\n"
    outside.write_text(original, encoding="utf-8")
    (workspace / "linked.md").symlink_to(outside)
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "nested.md").write_text(original, encoding="utf-8")
    (workspace / "linked-dir").symlink_to(outside_dir, target_is_directory=True)

    changed = RunSecretBoundary(manager.redactor).sweep_tree(workspace)

    assert changed is False, "nothing readable through a link may be swept"
    assert outside.read_text(encoding="utf-8") == original
    assert (outside_dir / "nested.md").read_text(encoding="utf-8") == original


def test_workspace_sweep_cannot_be_redirected_by_an_ancestor_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2 write path: the directory is swapped between the sweep's read and rewrite.

    Re-resolving ``workspace/sub/notes.md`` by name after the swap followed the
    new directory link and replaced a file outside the workspace.
    """
    config = make_config(tmp_path)
    manager = RunManager(config, EventBus(), RunStore(config.data_root / "runs"))
    workspace = tmp_path / "workspace"
    (workspace / "sub").mkdir(parents=True)
    (workspace / "sub" / "notes.md").write_text(f"OPENAI_API_KEY={AUDIT_SECRET}\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    external = outside / "notes.md"
    external.write_text("外部原文，不应被改写\n", encoding="utf-8")
    original_redact = manager.redactor.redact
    swapped: list[bool] = []

    def swapping_redact(text):
        if AUDIT_SECRET in text and not swapped:
            swapped.append(True)
            (workspace / "sub").rename(workspace / "moved-sub")
            (workspace / "sub").symlink_to(outside, target_is_directory=True)
        return original_redact(text)

    monkeypatch.setattr(manager.redactor, "redact", swapping_redact)
    RunSecretBoundary(manager.redactor).sweep_tree(workspace)  # the sweep entry itself

    assert swapped == [True]
    assert external.read_text(encoding="utf-8") == "外部原文，不应被改写\n"
    assert sorted(path.name for path in outside.iterdir()) == ["notes.md"]


def test_run_error_in_metadata_is_redacted(tmp_path: Path) -> None:
    """A kernel error carrying a credential must not leak into the stored run record."""
    config = make_config(tmp_path)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(
        config, EventBus(), store, client_factory=_fake_factory(echo_secret=True)
    )

    record = manager.start("FAILSECRET 让内核报错并回显凭据")
    final = manager.wait(record.run_id, timeout=30)

    assert final.status == "failed"
    meta = stored_record(store, record.run_id)
    rendered = stored_text(store)
    assert AUDIT_SECRET not in rendered, "run metadata leaked the credential"
    assert REPLACEMENT in (meta.get("error") or ""), "the error should be visibly redacted"


# -- report download ----------------------------------------------------------


def _get(server: GlinkHarnessServer, path: str):
    port = server.server_address[1]
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def test_report_download_is_redacted_and_flagged(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    server = create_server(config, client_factory=_fake_factory(echo_secret=False))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # A run whose host-owned artifact contains the credential (as a model
        # echo would). The artifact lives under the run directory: the download
        # path only serves validated regular files from there.
        store = server.store
        store.create("含密钥的报告", run_id="leakreport", workspace=tmp_path / "ws", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
        artifact_dir = store.run_dir("leakreport") / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact = artifact_dir / "report.md"
        artifact.write_text(
            f"# 报告\n\n调试时我看到了 OPENAI_API_KEY={AUDIT_SECRET}\n", encoding="utf-8"
        )
        finish(store, "leakreport", "finished", report_path=str(artifact))

        status, headers, body = _get(server, "/api/runs/leakreport/report")
        text = body.decode("utf-8")
        assert status == 200
        assert AUDIT_SECRET not in text
        assert REPLACEMENT in text
        assert headers.get("X-Redacted") == "true"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_clean_report_is_not_flagged(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    server = create_server(config, client_factory=_fake_factory(echo_secret=False))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        store = server.store
        store.create("干净", run_id="cleanreport", workspace=tmp_path / "ws", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
        artifact_dir = store.run_dir("cleanreport") / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact = artifact_dir / "report.md"
        artifact.write_text("# 干净报告\n\n没有凭据。\n", encoding="utf-8")
        finish(store, "cleanreport", "finished", report_path=str(artifact))

        status, headers, body = _get(server, "/api/runs/cleanreport/report")
        assert status == 200
        assert headers.get("X-Redacted") == "false"
        assert "没有凭据" in body.decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_report_download_refuses_paths_outside_the_run_directory(tmp_path: Path) -> None:
    """Audit regression: `reportPath` is not a download capability."""
    config = make_config(tmp_path)
    server = create_server(config, client_factory=_fake_factory(echo_secret=False))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        store = server.store
        outside = tmp_path / "outside.md"
        original = f"# 外部文件\n\nOPENAI_API_KEY={AUDIT_SECRET}\n"
        outside.write_text(original, encoding="utf-8")
        # Direct escape: the stored path points outside the run directory.
        store.create("越界", run_id="escape", workspace=tmp_path / "ws", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
        finish(store, "escape", "finished", report_path=str(outside))
        assert _get(server, "/api/runs/escape/report")[0] == 404
        # Symlink escape: a link inside the run directory is refused too.
        store.create("链接", run_id="linked", workspace=tmp_path / "ws", spec=RunSpec(family="pi", pack="pi", executor="inproc"))
        artifact_dir = store.run_dir("linked") / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "report.md").symlink_to(outside)
        finish(store, "linked", "finished", report_path=str(artifact_dir / "report.md"))
        assert _get(server, "/api/runs/linked/report")[0] == 404
        # Neither read nor redaction may have touched the external bytes.
        assert outside.read_text(encoding="utf-8") == original
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# -- config boundary ----------------------------------------------------------


def test_config_repr_hides_all_credentials(tmp_path: Path) -> None:
    config = make_config(tmp_path, E2B_API_KEY="e2b-secret-key-1234567890", GLINK_WEB_SEARCH_KEY="exa-secret-key-1234567890")
    rendered = repr(config)
    for secret in (AUDIT_SECRET, "e2b-secret-key-1234567890", "exa-secret-key-1234567890"):
        assert secret not in rendered


# -- the hold bound (GLINK-HARNESS-STREAM-DURABILITY-20260923) ----------------------
#
# A stream holds undecided text only up to HOLD_CAP characters. The earlier
# overflow branch released the whole window once the hold passed the bound,
# so an accepted known value longer than the bound was emitted raw in two
# pieces. The bound must never release undecided text: past it the stream
# fails closed for the rest of the block (the authoritative assistant_message
# still carries the whole-text-redacted answer).


def _long_secret(length: int) -> str:
    return "q" * (length - 1) + "Z"


def _pushes(redactor: SecretRedactor, text: str, cuts: list[int]) -> tuple[str, int]:
    """Stream ``text`` split at ``cuts``; return the output and the peak hold."""
    from glink_harness.redaction import HOLD_CAP  # noqa: PLC0415

    stream = redactor.stream()
    emitted: list[str] = []
    peak = 0
    previous = 0
    for cut in [*cuts, len(text)]:
        emitted.append(stream.push("assistant_delta", text[previous:cut]))
        previous = cut
        held = sum(len(value) for value in stream._pending.values())  # noqa: SLF001
        peak = max(peak, held)
        assert held <= HOLD_CAP, "the stream may never hold more than its bound"
    emitted.append(stream.flush("assistant_delta"))
    return "".join(emitted), peak


@pytest.mark.parametrize("length", [8192, 8193, 8194, 20000])
def test_a_long_known_value_never_leaves_the_stream_raw(length: int) -> None:
    from glink_harness.redaction import HOLD_CAP  # noqa: PLC0415
    import random  # noqa: PLC0415

    secret = _long_secret(length)
    redactor = SecretRedactor([secret])
    before, after = "前文，", "，后文"
    text = before + secret + after
    decidable = length - 1 <= HOLD_CAP  # the longest proper prefix fits the bound
    rng = random.Random(length)
    cut_sets = [
        [len(before) + length - 1],  # the whole proper prefix, then the last character
        [len(before), len(before) + length - 1],
        [len(before) + 1, len(before) + length // 2, len(before) + length - 1],
        *(sorted(rng.sample(range(1, len(text)), 5)) for _ in range(20)),
    ]
    for cuts in cut_sets:
        output, peak = _pushes(redactor, text, cuts)
        assert secret not in output, cuts
        assert "q" * 64 not in output, "no long fragment of the value may be released"
        assert output.startswith(before), "decided text before the value is kept"
        if decidable:
            assert output == redactor.redact(text), cuts
        else:
            assert REPLACEMENT in output
        assert peak <= HOLD_CAP


def test_an_overlapping_long_value_is_bounded_and_never_released() -> None:
    """A self-overlapping value grows one merged span; the bound still holds."""
    secret = "ab" * 4097  # 8194 characters, period two: every repeat overlaps
    redactor = SecretRedactor([secret])
    text = "开头 " + secret + "ab" * 50 + " 结尾"
    for cuts in ([len(text) // 2], list(range(1000, len(text), 1000)), [5, 8199, 8200]):
        output, _peak = _pushes(redactor, text, cuts)
        assert secret not in output
        assert "ab" * 32 not in output
        assert output.startswith("开头 ")


def test_the_stream_recovers_after_a_fail_closed_block() -> None:
    """Failing closed is scoped to the block; the next block streams normally."""
    secret = _long_secret(8194)
    stream = SecretRedactor([secret]).stream()
    first = stream.push("assistant_delta", "前文 " + secret[:-1])
    first += stream.push("assistant_delta", secret[-1:] + " 同一段的后文")
    first += stream.flush("assistant_delta")
    assert first == "前文 " + REPLACEMENT
    second = stream.push("assistant_delta", "下一段正常文字。") + stream.flush("assistant_delta")
    assert second == "下一段正常文字。"


@pytest.mark.parametrize("length", [8193, 8194])
def test_runner_never_emits_a_long_known_value(tmp_path: Path, length: int) -> None:
    """Real path: fake pi deltas → Projector → RunManager → stored events, bus, SSE."""
    from glink_harness.app import GlinkHarnessServer  # noqa: PLC0415
    from glink_harness.static_files import WebAssets  # noqa: PLC0415

    secret = _long_secret(length)
    pieces = ["前文，", secret[:-1], secret[-1:], "，后文"]
    config = make_config(tmp_path, OPENAI_API_KEY=secret)
    store = RunStore(config.data_root / "runs")
    bus = EventBus()

    def factory(argv, *, cwd=None, env=None, on_event=None):
        merged = dict(env or {})
        merged["FAKE_PI_WRITE_REPORT"] = "0"
        merged["FAKE_PI_TEXT_DELTAS"] = json.dumps(pieces, ensure_ascii=False)
        return PiRpcClient(
            [sys.executable, str(FAKE_PI), *argv[1:]], cwd=cwd, env=merged, on_event=on_event
        )

    manager = RunManager(config, bus, store, client_factory=factory)
    server = GlinkHarnessServer(config, bus, store, manager, WebAssets(config.web_root))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        record = manager.start("长凭据流式回显")
        live: list[dict] = []
        subscriber = bus.subscribe(record.run_id)
        collector = threading.Thread(
            target=lambda: live.extend(event.as_dict() for event in subscriber), daemon=True
        )
        collector.start()
        final = manager.wait(record.run_id, timeout=60)
        collector.join(timeout=10)
        assert final.status == "finished"

        persisted = [
            json.loads(line)
            for line in events_text(store, record.run_id).splitlines()
            if line.strip()
        ]
        port = server.server_address[1]
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/runs/{record.run_id}/events")
        with urllib.request.urlopen(request, timeout=20) as response:
            replay = [
                json.loads(line[len("data: "):])
                for line in response.read().decode("utf-8").splitlines()
                if line.startswith("data: ")
            ]
        for frames in (persisted, live, replay):
            rendered = json.dumps(frames, ensure_ascii=False)
            assert secret not in rendered
            assert "q" * 64 not in rendered
            joined = "".join(e["payload"]["text"] for e in frames if e["type"] == "assistant_delta")
            assert joined.startswith("前文，")
            if length - 1 <= 8192:
                assert joined == SecretRedactor([secret]).redact("".join(pieces))
            else:
                assert REPLACEMENT in joined
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_hold_and_the_output_stay_bounded_for_an_arbitrarily_long_value() -> None:
    """Memory and output bound: a one-million-character value streamed in small
    deltas never makes the stream hold more than HOLD_CAP, and once the block
    failed closed nothing further is buffered or emitted."""
    from glink_harness.redaction import HOLD_CAP  # noqa: PLC0415

    secret = _long_secret(1_000_000)
    stream = SecretRedactor([secret]).stream()
    emitted: list[str] = []
    for start in range(0, len(secret), 997):
        emitted.append(stream.push("assistant_delta", secret[start : start + 997]))
        assert sum(len(v) for v in stream._pending.values()) <= HOLD_CAP  # noqa: SLF001
    emitted.append(stream.push("assistant_delta", " 同一段后续文字"))
    emitted.append(stream.flush("assistant_delta"))
    assert "".join(emitted) == REPLACEMENT
    assert stream._pending == {} and stream._context == {}  # noqa: SLF001
