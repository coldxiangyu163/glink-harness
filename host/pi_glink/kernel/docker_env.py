"""Docker execution face: pi runs inside a disposable container.

The host stays outside the boundary. It starts ``docker run --rm -i`` and speaks
the same RPC protocol over the container's stdio, so the runner, the event
projection and the browser contract are untouched by the change of face.

Only data crosses the boundary:

* the run workspace — read-write bind mount at ``/workspace`` (the agent's cwd);
* the runtime config directory rendered by the host — at ``/config``;
* the pi session directory — at ``/sessions``;

The container's configuration — including the model credential, which lives in
``auth.json`` inside the runtime directory — arrives through those mounts, so
nothing credential-shaped travels through argv or the CLI environment at all.
A shell tool inside the container cannot read the key with ``env``.

The image is self-contained (Node + pi + the tools the agent uses); a container
installed-runtime dependency would defeat the point of the isolation, so this
module deliberately refuses to pass through host paths that are not mounts.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

from ..config import DEFAULT_WORKER_IMAGE, HostConfig
from .agent_env import PASSTHROUGH_ENV, AgentRuntime

#: Default image; frozen at configuration time (see ``HostConfig.worker_image``).
WORKER_IMAGE_DEFAULT = DEFAULT_WORKER_IMAGE
CONTAINER_WORKSPACE = "/workspace"
CONTAINER_CONFIG = "/config"
CONTAINER_SESSIONS = "/sessions"
#: Host environment variables forwarded by name (value stays out of argv).
#: Docker CLI needs these beyond the shared kernel whitelist (Docker Desktop / Colima).
_DOCKER_CLI_ENV = (
    "DOCKER_HOST",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
)


@dataclass(frozen=True, slots=True)
class ContainerPlan:
    """Everything the runner needs to start one containerised run."""

    argv: list[str]
    env: dict[str, str]
    container_name: str
    image: str


def worker_image(config: HostConfig) -> str:
    """The frozen worker image for every run of this host process.

    The value is resolved once at configuration time (``LOCAL_WEB_WORKER_IMAGE``
    is part of the frozen environment). Re-reading ``os.environ`` here would let
    an environment change after a run was accepted silently relabel that run to
    a different image.
    """
    return config.worker_image


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
    session_dir: Path,
    extra: Sequence[str] = (),
) -> ContainerPlan:
    """Compose ``docker run`` for one run.

    ``-i`` keeps stdin open for the RPC command stream and ``--rm`` removes the
    container when pi exits. Credentials are forwarded by *name* so their values
    never appear in the process list. ``session_dir`` is the run's own session
    directory: a shared mount would let two runs write into each other's session
    files.
    Every run gets explicit resource ceilings and a reduced privilege profile
    (``--cpus`` / ``--memory`` / ``--pids-limit``, all capabilities dropped,
    ``no-new-privileges``). The image and its template are never changed here;
    the limits apply to the container instance only. They are contract, not
    measured facts: no live Docker run is performed by the default test suite,
    so the host cannot claim what the daemon actually enforced.
    """
    image = worker_image(config)
    container_name = f"pi-glink-{run_id}"
    argv: list[str] = [
        "docker", "run", "--rm", "-i",
        "--name", container_name,
        "--label", f"pi-glink.run={run_id}",
        "--cpus", str(config.docker_cpus),
        "--memory", str(config.docker_memory),
        "--pids-limit", str(config.docker_pids_limit),
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "-v", f"{workspace}:{CONTAINER_WORKSPACE}",
        "-w", CONTAINER_WORKSPACE,
        "-v", f"{runtime.directory}:{CONTAINER_CONFIG}",
        "-v", f"{session_dir}:{CONTAINER_SESSIONS}",
        "-e", f"PI_CODING_AGENT_DIR={CONTAINER_CONFIG}",
        "-e", f"PI_CODING_AGENT_SESSION_DIR={CONTAINER_SESSIONS}",
        "-e", "PI_SKIP_VERSION_CHECK=1",
        "-e", "PI_TELEMETRY=0",
    ]
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

    The container's configuration — including the credential in ``auth.json`` —
    arrives through the mounted config directory, so nothing credential-shaped
    is placed on the CLI's environment or argv. The whitelist additionally keeps
    a host shell full of unrelated tokens out of this child process.
    """
    source = dict(base_env) if base_env is not None else dict(os.environ)
    names = (*PASSTHROUGH_ENV, *_DOCKER_CLI_ENV)
    return {name: source[name] for name in names if name in source}


def stop_argv(container_name: str) -> list[str]:
    """Force-remove a container after a cancelled or timed-out run."""
    return ["docker", "rm", "-f", container_name]


def host_session_path(raw: str, session_dir: Path) -> Path | None:
    """Map a session path pi reported inside the container to its host file.

    pi only knows container paths (``/sessions/<file>.jsonl``); the host mounted
    the run's own session directory at :data:`CONTAINER_SESSIONS`. Without this
    translation the host-side sanitizer reads a path that does not exist and
    silently skips the file — the leak the audit reproduced. Only the session
    mount is translated on purpose: the credential lives in ``/config/auth.json``
    and must keep its real value.
    """
    return translate_container_path(raw, ((CONTAINER_SESSIONS, session_dir),))


def translate_container_path(
    raw: str, mounts: Sequence[tuple[str, Path]]
) -> Path | None:
    """Map one container path back through the mounts that created it.

    Purely lexical (no filesystem access): relative paths, prefix look-alikes
    (``/sessions-other``) and traversal (``..``) are refused rather than guessed.
    """
    candidate = PurePosixPath(raw or "")
    if not candidate.is_absolute():
        return None
    for container_root, host_root in mounts:
        try:
            relative = candidate.relative_to(PurePosixPath(container_root))
        except ValueError:
            continue
        if ".." in relative.parts:
            return None
        return host_root.joinpath(*relative.parts)
    return None
