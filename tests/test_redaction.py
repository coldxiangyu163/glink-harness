"""Credential-boundary regressions (audit reproduction included).

The completion audit reproduced a leak: a `tool_execution_end` frame carrying
``OPENAI_API_KEY=AUDIT_DUMMY_SECRET_…`` survived projection verbatim, which means
a bash `env` call (or a config-file read, or the model quoting a key back) would
reach `events.jsonl`, the SSE stream and the browser. These tests pin the fix at
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

from pi_glink.app import PiGlinkServer, create_server
from pi_glink.config import build_config
from pi_glink.events import EventBus
from pi_glink.kernel.projection import project_event
from pi_glink.kernel.rpc import PiRpcClient
from pi_glink.kernel.runner import RunManager
from pi_glink.redaction import REPLACEMENT, SecretRedactor, redactor_from_config
from pi_glink.store import RunStore

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_PI = Path(__file__).resolve().parent / "fixtures" / "fake_pi.py"

#: The exact shape the auditor used.
AUDIT_SECRET = "AUDIT_DUMMY_SECRET_0123456789abcdef"


def make_config(tmp_path: Path, **overrides: str):
    values = {
        "OPENAI_API_KEY": AUDIT_SECRET,
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
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


def test_redactor_learns_the_web_search_file(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runtime_dir = tmp_path / "agent-runtime"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "web-search.json").write_text(
        json.dumps({"exaApiKey": "exa-unknown0123456789abcdef", "workflow": "none"}),
        encoding="utf-8",
    )
    redactor = redactor_from_config(config)
    assert redactor.contains_secret("exa-unknown0123456789abcdef") is not None
    redactor.add_from_file(runtime_dir / "web-search.json")
    assert redactor.redact("token exa-unknown0123456789abcdef") == f"token {REPLACEMENT}"


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
    log_text = store.events_path(record.run_id).read_text(encoding="utf-8")
    assert AUDIT_SECRET not in log_text, "event log leaked the model credential"
    assert REPLACEMENT in log_text, "redaction marker missing: was the echo even emitted?"
    stream_text = json.dumps(seen, ensure_ascii=False)
    assert AUDIT_SECRET not in stream_text, "SSE payload leaked the model credential"


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
    session = tmp_path / "session.jsonl"
    session.write_text(
        json.dumps({"type": "message", "text": f"OPENAI_API_KEY={AUDIT_SECRET}"}) + "\n",
        encoding="utf-8",
    )

    manager._sanitize_session_file(str(session))  # noqa: SLF001 - the boundary unit itself

    text = session.read_text(encoding="utf-8")
    assert AUDIT_SECRET not in text
    assert REPLACEMENT in text
    # The file stays valid JSONL so pi can still read its own history.
    assert json.loads(text.splitlines()[0])["text"].endswith(REPLACEMENT)


def test_session_file_without_secrets_is_left_untouched(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    manager = RunManager(config, EventBus(), RunStore(config.data_root / "runs"))
    session = tmp_path / "clean.jsonl"
    original = json.dumps({"type": "message", "text": "普通内容"}) + "\n"
    session.write_text(original, encoding="utf-8")
    manager._sanitize_session_file(str(session))  # noqa: SLF001
    assert session.read_text(encoding="utf-8") == original


def test_minimal_child_environment_does_not_inherit_host_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pi process gets a whitelisted environment, not the operator's shell."""
    from pi_glink.kernel import agent_env

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-host-secret-0123456789")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-host-secret-0123456789")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    config = make_config(tmp_path)
    runtime = agent_env.prepare_agent_dir(config)
    env = agent_env.build_pi_env(config, runtime)

    assert "ANTHROPIC_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert env["PATH"] == "/usr/bin:/bin"
    assert "OPENAI_API_KEY" not in env, "the model key travels via auth.json, not the environment"


# -- report download ----------------------------------------------------------


def _get(server: PiGlinkServer, path: str):
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
        # A run whose report contains the credential (as a model echo would).
        store = server.store
        record = store.create("含密钥的报告", run_id="leakreport", workspace=tmp_path / "ws")
        report = tmp_path / "ws"
        report.mkdir(parents=True, exist_ok=True)
        (report / "report.md").write_text(
            f"# 报告\n\n调试时我看到了 OPENAI_API_KEY={AUDIT_SECRET}\n", encoding="utf-8"
        )
        store.update("leakreport", status="finished", report_path=str(report / "report.md"))

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
        workspace = tmp_path / "clean"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "report.md").write_text("# 干净报告\n\n没有凭据。\n", encoding="utf-8")
        store.create("干净", run_id="cleanreport", workspace=workspace)
        store.update("cleanreport", status="finished", report_path=str(workspace / "report.md"))

        status, headers, body = _get(server, "/api/runs/cleanreport/report")
        assert status == 200
        assert headers.get("X-Redacted") == "false"
        assert "没有凭据" in body.decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# -- config boundary ----------------------------------------------------------


def test_config_repr_hides_all_credentials(tmp_path: Path) -> None:
    config = make_config(tmp_path, E2B_API_KEY="e2b-secret-key-1234567890", PI_GLINK_WEB_SEARCH_KEY="exa-secret-key-1234567890")
    rendered = repr(config)
    for secret in (AUDIT_SECRET, "e2b-secret-key-1234567890", "exa-secret-key-1234567890"):
        assert secret not in rendered
