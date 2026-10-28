"""Live 验收入口的隔离/错误/清理回归，不启动真实 E2B 或模型。"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def smoke_module():
    path = ROOT / "scripts/spikes/acp_e2b/deepseek_web_smoke.py"
    spec = importlib.util.spec_from_file_location("deepseek_web_smoke", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def arguments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    env = tmp_path / "input.env"
    env.write_text("E2B_API_KEY=synthetic-cloud\nOPENROUTER_API_KEY=synthetic-model\n"
                   "DEEPSEEK_API_KEY=synthetic-search\nJINA_API_KEY=never-deliver\n"
                   "OPENAI_MODEL=openai/gpt-5.6-sol\nOPENAI_REASONING_EFFORT=xhigh\n"
                   "LOCAL_WEB_DATA_ROOT=/do/not/use\nGLINK_TENANCY=multitenant\n")
    out = tmp_path / "evidence"
    monkeypatch.setattr(sys, "argv", ["smoke", "--template", "synthetic-candidate",
                                    "--env-file", str(env), "--out", str(out)])
    return out


def test_live_probe_does_not_echo_external_exception_or_inherit_service_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    module = smoke_module()
    out = arguments(tmp_path, monkeypatch)

    def fail(config, destination):
        assert destination == out
        assert config.data_root == out / "data"
        assert config.tenancy_mode == "local"
        assert config.acp_credential_env == ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY")
        raise RuntimeError("external exception with synthetic-search")

    monkeypatch.setattr(module, "preflight", fail)
    assert module.main() == 1
    summary = (out / "summary.json").read_text()
    assert json.loads(summary)["errorType"] == "RuntimeError"
    assert "synthetic-search" not in summary + capsys.readouterr().out


def test_live_probe_shuts_down_then_reconciles_only_its_recorded_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = smoke_module()
    out = arguments(tmp_path, monkeypatch)
    order = []
    server = SimpleNamespace(
        serve_forever=lambda: None,
        manager=SimpleNamespace(shutdown=lambda: order.append("manager-stop")),
        store=SimpleNamespace(list=lambda **kw: order.append("identities") or [SimpleNamespace(sandbox_id="owned")]),
        shutdown=lambda: order.append("http-stop"),
        server_close=lambda: order.append("close"),
    )
    monkeypatch.setattr(module, "preflight", lambda *args: None)
    monkeypatch.setattr(module, "create_server", lambda *args: server)

    def fail(*args, **kwargs):
        raise module.ProbeFailure("synthetic check failed")

    monkeypatch.setattr(module, "round_trip", fail)
    monkeypatch.setattr(module, "confirm_sandbox_absent", lambda identity, **kw: (order.append(identity) or True, "ok"))
    assert module.main() == 1
    assert order == ["manager-stop", "identities", "http-stop", "close", "owned"]
    summary = json.loads((out / "summary.json").read_text())
    assert summary["cleanup"] == [{"sandboxId": "owned", "confirmedAbsent": True}]
    assert summary["passed"] is False


def test_live_probe_rejects_existing_evidence_before_any_cloud_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = smoke_module()
    out = arguments(tmp_path, monkeypatch)
    out.mkdir()
    (out / "keep.txt").write_text("existing")
    with pytest.raises(module.ProbeFailure, match="empty"):
        module.main()
    assert (out / "keep.txt").read_text() == "existing"
