"""Regressions for the Docker execution face (no daemon required).

These assert the *contract* of the boundary: what is mounted, what is passed by
name (never by value), and how the command line is shaped. A real container run
is covered separately by the task acceptance evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pi_glink.config import build_config
from pi_glink.kernel import agent_env, docker_env

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def config(tmp_path: Path):
    return build_config(
        {
            "OPENAI_API_KEY": "sk-docker-secret",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "OPENAI_MODEL": "openai/gpt-5.6-sol",
            "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
            "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
            "LOCAL_WEB_EXECUTOR": "docker",
        },
        REPO_ROOT,
    )


def test_plan_mounts_data_and_never_the_host_filesystem(config, tmp_path: Path) -> None:
    runtime = agent_env.prepare_agent_dir(config)
    workspace = tmp_path / "workspace"
    plan = docker_env.build_plan(
        config, runtime, run_id="abc123", workspace=workspace, extra=["--name", "n"]
    )

    argv = plan.argv
    assert argv[:4] == ["docker", "run", "--rm", "-i"]
    assert "--name" in argv and plan.container_name in argv
    assert f"{workspace}:{docker_env.CONTAINER_WORKSPACE}" in argv
    assert f"{runtime.directory}:{docker_env.CONTAINER_CONFIG}" in argv
    assert f"{config.session_dir}:{docker_env.CONTAINER_SESSIONS}" in argv
    assert f"PI_CODING_AGENT_DIR={docker_env.CONTAINER_CONFIG}" in argv
    assert plan.image in argv
    # The kernel command inside the container mirrors the in-process face.
    tail = argv[argv.index(plan.image) :]
    assert tail[1:5] == ["pi", "--mode", "rpc", "--provider"]
    assert "--model" in tail
    # No host path outside the declared mounts may leak into the command line.
    host_paths = [item for item in argv if isinstance(item, str) and item.startswith("/Users/")]
    assert all(
        docker_env.CONTAINER_WORKSPACE in item
        or docker_env.CONTAINER_CONFIG in item
        or docker_env.CONTAINER_SESSIONS in item
        for item in host_paths
    )


def test_credentials_are_forwarded_by_name_not_by_value(config, tmp_path: Path) -> None:
    runtime = agent_env.prepare_agent_dir(config)
    plan = docker_env.build_plan(
        config, runtime, run_id="abc123", workspace=tmp_path / "ws"
    )
    joined = " ".join(plan.argv)
    assert "OPENAI_API_KEY" in plan.argv
    assert "sk-docker-secret" not in joined, "the key must never appear in argv"
    assert plan.env["OPENAI_API_KEY"] == "sk-docker-secret"


def test_runtime_config_directory_carries_the_generated_models_file(config) -> None:
    runtime = agent_env.prepare_agent_dir(config)
    assert (runtime.directory / "models.json").is_file()
    assert (runtime.directory / "settings.json").is_file()


def test_missing_docker_cli_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docker_env.shutil, "which", lambda _name: None)
    assert docker_env.is_available() is False


def test_stop_argv_is_a_force_remove() -> None:
    assert docker_env.stop_argv("pi-glink-abc") == ["docker", "rm", "-f", "pi-glink-abc"]


def test_runner_refuses_docker_when_daemon_is_missing(config, tmp_path: Path, monkeypatch) -> None:
    from pi_glink.events import EventBus
    from pi_glink.kernel.runner import RunManager
    from pi_glink.store import RunStore

    monkeypatch.setattr("pi_glink.kernel.runner.docker_available", lambda: False)
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    record = manager.start("docker 不可用时应当失败")
    final = manager.wait(record.run_id, timeout=30)
    assert final.status == "failed"
    assert final.error is not None and "Docker daemon" in final.error
