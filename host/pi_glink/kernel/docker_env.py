"""Docker execution face: pi runs inside a disposable container.

The host stays outside the boundary. It starts ``docker run --rm -i`` and speaks
the same RPC protocol over the container's stdio, so the runner, the event
projection and the browser contract are untouched by the change of face.

Only data crosses the boundary:

* the run workspace — read-write bind mount at ``/workspace`` (the agent's cwd);
* the runtime config directory rendered by the host — at ``/config``;
* the pi session directory — at ``/sessions``;
* credentials — environment variables only, never argv and never the image.

The image is self-contained (Node + pi + the tools the agent uses); a container
installed-runtime dependency would defeat the point of the isolation, so this
module deliberately refuses to pass through host paths that are not mounts.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from ..config import HostConfig
from .agent_env import AgentRuntime

#: Default image; override with LOCAL_WEB_WORKER_IMAGE.
WORKER_IMAGE_DEFAULT = "pi-glink-worker:latest"
CONTAINER_WORKSPACE = "/workspace"
CONTAINER_CONFIG = "/config"
CONTAINER_SESSIONS = "/sessions"
#: Host environment variables forwarded by name (value stays out of argv).
_FORWARDED_ENV = ("OPENAI_API_KEY",)


@dataclass(frozen=True, slots=True)
class ContainerPlan:
    """Everything the runner needs to start one containerised run."""

    argv: list[str]
    env: dict[str, str]
    container_name: str
    image: str


def worker_image(config: HostConfig) -> str:
    return (
        os.environ.get("LOCAL_WEB_WORKER_IMAGE", "").strip()
        or WORKER_IMAGE_DEFAULT
    )


def is_available() -> bool:
    """True when a docker CLI and a reachable daemon are present."""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool(result.stdout.strip())


def image_present(image: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def build_plan(
    config: HostConfig,
    runtime: AgentRuntime,
    *,
    run_id: str,
    workspace: Path,
    extra: Sequence[str] = (),
) -> ContainerPlan:
    """Compose ``docker run`` for one run.

    ``-i`` keeps stdin open for the RPC command stream and ``--rm`` removes the
    container when pi exits. Credentials are forwarded by *name* so their values
    never appear in the process list.
    """
    image = worker_image(config)
    container_name = f"pi-glink-{run_id}"
    argv: list[str] = [
        "docker", "run", "--rm", "-i",
        "--name", container_name,
        "--label", f"pi-glink.run={run_id}",
        "-v", f"{workspace}:{CONTAINER_WORKSPACE}",
        "-w", CONTAINER_WORKSPACE,
        "-v", f"{runtime.directory}:{CONTAINER_CONFIG}",
        "-v", f"{config.session_dir}:{CONTAINER_SESSIONS}",
        "-e", f"PI_CODING_AGENT_DIR={CONTAINER_CONFIG}",
        "-e", f"PI_CODING_AGENT_SESSION_DIR={CONTAINER_SESSIONS}",
        "-e", "PI_SKIP_VERSION_CHECK=1",
        "-e", "PI_TELEMETRY=0",
    ]
    for name in _FORWARDED_ENV:
        argv.extend(["-e", name])
    argv.extend(
        [
            image,
            "pi", "--mode", "rpc",
            "--provider", runtime.provider_id,
            "--model", runtime.model_id,
        ]
    )
    argv.extend(extra)
    return ContainerPlan(
        argv=argv,
        env=build_env(config),
        container_name=container_name,
        image=image,
    )


def build_env(
    config: HostConfig, *, base_env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Environment for the *docker CLI*, not for the container.

    The container's environment is defined by the ``-e`` flags in :func:`build_plan`.
    Here we only make sure the CLI itself can see the credential it is told to
    forward by name.
    """
    env = dict(base_env) if base_env is not None else dict(os.environ)
    if config.openai_api_key:
        env["OPENAI_API_KEY"] = config.openai_api_key
    return env


def stop_argv(container_name: str) -> list[str]:
    """Force-remove a container after a cancelled or timed-out run."""
    return ["docker", "rm", "-f", container_name]
