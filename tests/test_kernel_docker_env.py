"""Regressions for the Docker execution face (no daemon required).

These assert the *contract* of the boundary: what is mounted, what is passed by
name (never by value), how the command line is shaped, and that the image is
frozen at configuration time. A real container run is covered separately by the
task acceptance evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pi_glink.config import build_config
from pi_glink.kernel import agent_env, docker_env

REPO_ROOT = Path(__file__).resolve().parents[1]


def _prepare(config, tmp_path: Path, name: str = "run-1"):
    run_dir = tmp_path / "runs" / name
    return agent_env.prepare_agent_dir(
        config,
        runtime_dir=run_dir / "pi-agent",
        session_dir=run_dir / "sessions",
    )


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
            "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent-web-search.json"),
        },
        REPO_ROOT,
    )


def test_plan_mounts_data_and_never_the_host_filesystem(config, tmp_path: Path) -> None:
    runtime = _prepare(config, tmp_path)
    workspace = tmp_path / "workspace"
    plan = docker_env.build_plan(
        config,
        runtime,
        run_id="abc123",
        workspace=workspace,
        session_dir=runtime.session_dir,
        extra=["--name", "n"],
    )

    argv = plan.argv
    assert argv[:4] == ["docker", "run", "--rm", "-i"]
    assert "--name" in argv and plan.container_name in argv
    assert f"{workspace}:{docker_env.CONTAINER_WORKSPACE}" in argv
    assert f"{runtime.directory}:{docker_env.CONTAINER_CONFIG}" in argv
    assert f"{runtime.session_dir}:{docker_env.CONTAINER_SESSIONS}" in argv
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


def test_no_credential_travels_through_argv_or_the_cli_environment(
    config, tmp_path: Path
) -> None:
    runtime = _prepare(config, tmp_path)
    plan = docker_env.build_plan(
        config,
        runtime,
        run_id="abc123",
        workspace=tmp_path / "ws",
        session_dir=runtime.session_dir,
    )
    joined = " ".join(plan.argv)
    assert "OPENAI_API_KEY" not in plan.argv, "the container must get no credential env var"
    assert "sk-docker-secret" not in joined, "the key must never appear in argv"
    assert "OPENAI_API_KEY" not in plan.env
    assert "sk-docker-secret" not in plan.env.values()

    # The credential reaches the container as a file inside the mounted config dir.
    assert (runtime.directory / agent_env.AUTH_FILENAME).is_file()


def test_runtime_config_directory_carries_the_generated_models_file(config, tmp_path: Path) -> None:
    runtime = _prepare(config, tmp_path)
    assert (runtime.directory / "models.json").is_file()
    assert (runtime.directory / "settings.json").is_file()


def test_missing_docker_cli_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docker_env.shutil, "which", lambda _name: None)
    assert docker_env.is_available() is False


def test_stop_argv_is_a_force_remove() -> None:
    assert docker_env.stop_argv("pi-glink-abc") == ["docker", "rm", "-f", "pi-glink-abc"]


def test_container_session_paths_map_back_to_the_run_mount(config, tmp_path: Path) -> None:
    """Audit regression: pi reports container paths; the host must translate them.

    Without this mapping the closing sanitise pass read ``/sessions/…`` on the
    host, found nothing, and returned silently — a raw credential echoed by a
    tool could stay in the host-side session log forever. The mapping targets the
    *run's own* session directory now, so two runs cannot read each other's logs.
    """
    session_dir = tmp_path / "runs" / "abc123" / "sessions"
    assert (
        docker_env.host_session_path("/sessions/abc.jsonl", session_dir)
        == session_dir / "abc.jsonl"
    )
    assert (
        docker_env.host_session_path("/sessions/nested/abc.jsonl", session_dir)
        == session_dir / "nested" / "abc.jsonl"
    )
    # Prefix look-alikes, relative paths and traversal are refused, not guessed.
    assert docker_env.host_session_path("/sessions-other/abc.jsonl", session_dir) is None
    assert docker_env.host_session_path("sessions/abc.jsonl", session_dir) is None
    assert docker_env.host_session_path("/sessions/../etc/passwd", session_dir) is None
    assert docker_env.host_session_path("", session_dir) is None
    # Only the session mount is translated: /config/auth.json keeps its value.
    assert docker_env.host_session_path("/config/auth.json", session_dir) is None


def test_plan_applies_resource_ceilings_and_drops_privileges(config, tmp_path: Path) -> None:
    """Every container gets explicit limits and a reduced privilege profile.

    The image/template are untouched; these are per-instance ``docker run``
    arguments. No live daemon is contacted here, so this asserts the command
    contract, not what a daemon enforced.
    """
    runtime = _prepare(config, tmp_path)
    plan = docker_env.build_plan(
        config,
        runtime,
        run_id="abc123",
        workspace=tmp_path / "ws",
        session_dir=runtime.session_dir,
    )
    argv = plan.argv
    assert argv[argv.index("--cpus") + 1] == "2"
    assert argv[argv.index("--memory") + 1] == "2g"
    assert argv[argv.index("--pids-limit") + 1] == "512"
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    assert plan.image in argv, "the frozen image stays the one selected at startup"


def test_resource_ceilings_are_configurable_at_startup(tmp_path: Path) -> None:
    config = build_config(
        {
            "OPENAI_API_KEY": "sk-docker-secret",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "OPENAI_MODEL": "test/model",
            "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
            "LOCAL_WEB_EXECUTOR": "docker",
            "LOCAL_WEB_DOCKER_CPUS": "1.5",
            "LOCAL_WEB_DOCKER_MEMORY": "1g",
            "LOCAL_WEB_DOCKER_PIDS_LIMIT": "128",
        },
        REPO_ROOT,
    )
    runtime = _prepare(config, tmp_path)
    plan = docker_env.build_plan(
        config,
        runtime,
        run_id="abc123",
        workspace=tmp_path / "ws",
        session_dir=runtime.session_dir,
    )
    argv = plan.argv
    assert argv[argv.index("--cpus") + 1] == "1.5"
    assert argv[argv.index("--memory") + 1] == "1g"
    assert argv[argv.index("--pids-limit") + 1] == "128"


def test_worker_image_is_frozen_at_configuration_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run must not be relabelled by an environment change after acceptance."""
    config = build_config(
        {
            "OPENAI_API_KEY": "sk-docker-secret",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "OPENAI_MODEL": "test/model",
            "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
            "LOCAL_WEB_EXECUTOR": "docker",
            "LOCAL_WEB_WORKER_IMAGE": "pi-glink-worker:pinned",
        },
        REPO_ROOT,
    )
    assert docker_env.worker_image(config) == "pi-glink-worker:pinned"
    assert config.worker_image == "pi-glink-worker:pinned"
    # Building a plan at run time never re-reads the live environment.
    monkeypatch.setenv("LOCAL_WEB_WORKER_IMAGE", "pi-glink-worker:something-else")
    runtime = _prepare(config, tmp_path)
    plan = docker_env.build_plan(
        config,
        runtime,
        run_id="abc123",
        workspace=tmp_path / "ws",
        session_dir=runtime.session_dir,
    )
    assert plan.image == "pi-glink-worker:pinned"
    assert docker_env.worker_image(config) == "pi-glink-worker:pinned"


def test_runner_refuses_docker_when_daemon_is_missing(config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
