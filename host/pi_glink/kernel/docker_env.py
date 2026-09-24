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
from typing import Any, Callable, Mapping, Sequence

from ..config import DEFAULT_WORKER_IMAGE, HostConfig
from .agent_env import IMAGE_PACKAGE_GUARD, PASSTHROUGH_ENV, AgentRuntime

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

    ``--entrypoint`` is the image-local prebaked-package guard
    (:data:`agent_env.IMAGE_PACKAGE_GUARD`), not the image default: the mounted
    ``settings.json`` points at ``/opt/pi-packages`` instead of declaring an
    ``npm:`` install, and pi silently ignores a local package path that does not
    exist — the guard turns that silent loss into a clear failure (exit 78).
    """
    image = worker_image(config)
    container_name = container_name_for(run_id)
    argv: list[str] = [
        "docker", "run", "--rm", "-i",
        "--name", container_name,
        "--label", f"pi-glink.run={run_id}",
        "--cpus", str(config.docker_cpus),
        "--memory", str(config.docker_memory),
        "--pids-limit", str(config.docker_pids_limit),
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        # The container faces load pi packages from the image instead of running
        # npm inside the run, so the entrypoint is the image's package guard:
        # it exits 78 when a package the mounted settings point at is missing.
        # A pre-change image has no such file and fails at container start
        # instead of silently running without web_search.
        "--entrypoint", IMAGE_PACKAGE_GUARD,
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


#: Bound on one identity probe (inspect / force-remove). The ledger sweep skips
#: a tick while a probe runs, so this bounds the call, not a whole sweep.
PROBE_TIMEOUT_SECONDS = 20


def container_name_for(run_id: str) -> str:
    """The container name of one run — fixed before ``docker run`` executes.

    The runner persists it before starting the container, so a failure inside
    ``start()`` cannot leave an unconfirmed hold without its identity.
    """
    return f"pi-glink-{run_id}"


def confirm_absent(
    container_name: str, *, runner: Callable[..., Any] = subprocess.run
) -> tuple[bool, str]:
    """Confirm by persisted name that no container remains; remove it if it does.

    Only the named identity is ever addressed: no name pattern, no listing and
    no other container. ``(True, detail)`` means the container is proven gone;
    a missing identity, an unclear CLI answer or a timeout keeps the hold. A
    container that is still there is removed with the same idempotent
    force-remove the cancel path uses, then confirmed again.
    """
    if not container_name:
        return False, "缺少持久化的容器名"
    present, detail = _container_present(container_name, runner)
    if present is None or present is False:
        return present is False, detail
    try:
        result = runner(
            stop_argv(container_name),
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"删除容器失败: {type(exc).__name__}: {exc}"
    stderr = (result.stderr or "").strip()
    if result.returncode != 0 and "No such container" not in stderr:
        return False, f"删除容器失败: exit {result.returncode}: {stderr[-200:]}"
    present, detail = _container_present(container_name, runner)
    if present is False:
        return True, f"已按名字强制删除并确认容器不存在（{detail}）"
    return False, f"删除后仍无法确认容器不存在: {detail}"


def _container_present(
    container_name: str, runner: Callable[..., Any]
) -> tuple[bool | None, str]:
    """``True``/``False`` when the daemon answered, ``None`` when it did not."""
    try:
        result = runner(
            [
                "docker",
                "inspect",
                "--type",
                "container",
                "--format",
                "{{.Id}}",
                container_name,
            ],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"docker inspect 不可用: {type(exc).__name__}: {exc}"
    if result.returncode == 0:
        return True, "docker inspect 确认容器仍存在"
    message = ((result.stderr or "") + (result.stdout or "")).strip()
    if "no such" in message.lower():
        return False, "docker inspect 确认容器不存在"
    return None, f"docker inspect 无法判定: exit {result.returncode}: {message[-200:]}"


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
