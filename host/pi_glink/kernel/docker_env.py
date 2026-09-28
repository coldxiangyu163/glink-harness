"""Docker execution face: a harness runs inside a disposable container.

The host stays outside the boundary. It starts ``docker run --rm -i`` and speaks
the harness's own stdio protocol over the container's stdio, so the runner, the
event projection and the browser contract are untouched by the change of face:

the pi family speaks its bidirectional RPC protocol, the ACP family ACP
JSON-RPC. :func:`run_argv` composes the command from the harness's
:class:`~pi_glink.kernel.sandbox.Launch`; the lifecycle (availability check,
persisted name, force-remove) is :class:`~pi_glink.kernel.sandbox.DockerSandbox`.

Only data crosses the boundary:

* the run workspace — read-write bind mount at ``/workspace`` (the agent's cwd);
* pi's runtime config directory rendered by the host — at ``/config``;
* the pi session directory — at ``/sessions``;
* the ACP family's per-run ``HOME`` (agent state root, with the injected
  credentials inside it) — at :data:`CONTAINER_HOME`, with every credential file
  additionally mounted ``:ro`` so the container can keep state under ``HOME``
  but cannot modify or delete the injected credentials.

The pi container's configuration — including the model credential, which lives
in ``auth.json`` inside the runtime directory — arrives through those mounts, so
nothing credential-shaped travels through argv or the CLI environment at all.
A shell tool inside the container cannot read the key with ``env``.

Resource ceilings and the reduced privilege profile (``--cpus`` / ``--memory``
/ ``--pids-limit``, all capabilities dropped, ``no-new-privileges``) are
identical for both faces (:func:`_isolation_argv`) — contract, not measured
fact: the default test suite performs no live Docker run, so the host cannot
claim what the daemon actually enforced.

The image is self-contained (Node + the harness and the tools it uses); a
container installed-runtime dependency would defeat the point of the isolation,
so this module deliberately refuses to pass through host paths that are not
mounts.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from ..config import DEFAULT_WORKER_IMAGE, HostConfig
from .agent_env import PASSTHROUGH_ENV

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from .sandbox import Launch

#: Default image; frozen at configuration time (see ``HostConfig.worker_image``).
WORKER_IMAGE_DEFAULT = DEFAULT_WORKER_IMAGE
CONTAINER_WORKSPACE = "/workspace"
CONTAINER_CONFIG = "/config"
CONTAINER_SESSIONS = "/sessions"
#: The ACP face's ``HOME`` inside the container: the per-run agent state root
#: (``<run>/acp-agent/home``) is mounted here and the container's ``HOME`` is
#: pointed at it, so ``~/.codex`` / ``~/.dsh`` and every cache stay inside the
#: run instead of the operator's home. Mirrors ``HOME=/home/acp`` in
#: ``docker/Dockerfile.acp-worker``.
CONTAINER_HOME = "/home/acp"
#: Host environment variables forwarded by name (value stays out of argv).
#: Docker CLI needs these beyond the shared kernel whitelist (Docker Desktop / Colima).
_DOCKER_CLI_ENV = (
    "DOCKER_HOST",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
)


def worker_image(config: HostConfig) -> str:
    """The frozen pi worker image for every run of this host process.

    The value is resolved once at configuration time (``LOCAL_WEB_WORKER_IMAGE``
    is part of the frozen environment). Re-reading ``os.environ`` here would let
    an environment change after a run was accepted silently relabel that run to
    a different image.
    """
    return config.worker_image


def acp_worker_image(config: HostConfig) -> str:
    """The frozen ACP worker image (``LOCAL_WEB_ACP_WORKER_IMAGE``).

    Frozen at configuration time for the same reason as :func:`worker_image`:
    the ACP image carries different agents than the pi image, so re-reading the
    environment at run time could relabel an accepted run.
    """
    return config.acp_worker_image


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


#: Hostname under which a container reaches the host (the egress proxy).
DOCKER_HOST_GATEWAY = "host.docker.internal"


def run_argv(
    config: HostConfig,
    launch: "Launch",
    *,
    run_id: str,
    workspace: Path,
) -> list[str]:
    """Compose ``docker run -i`` for one run from a harness's :class:`Launch`.

    The container's stdio is the harness's own transport: ``-i`` keeps stdin
    open and ``--rm`` removes the container when the kernel exits. The name is
    fixed by the run id (:func:`container_name_for`) and labelled, so the
    resource ledger can address exactly this container. Resource ceilings and
    the reduced privilege profile (:func:`_isolation_argv`) are identical for
    every harness.

    The workspace is always mounted read-write at :data:`CONTAINER_WORKSPACE`
    and is the working directory; every other mount comes from the launch (the
    pi config and session directories, the ACP ``HOME`` with each injected
    credential re-mounted ``:ro`` on top so the container can keep state under
    ``HOME`` but cannot modify the credential). ``launch.guard`` becomes the
    entrypoint: the image's package guard verifies the prebaked pi packages
    and then ``exec``\\ s the argv, because pi silently ignores a missing local
    package. ``launch.env`` holds non-secret literals (``-e NAME=value``);
    ``launch.secret_env`` is passed by *name* only (``-e NAME``) with the value
    on the docker CLI's own environment (:func:`cli_env`), so it never appears
    in argv (the process list).
    """
    argv: list[str] = [
        "docker", "run", "--rm", "-i",
        "--name", container_name_for(run_id),
        "--label", f"pi-glink.run={run_id}",
        *_isolation_argv(config),
        # The egress proxy runs in the host process; the container reaches it
        # by this alias (docs/adr/0008). It is the only host route handed in.
        "--add-host", f"{DOCKER_HOST_GATEWAY}:host-gateway",
    ]
    if launch.guard:
        argv.extend(["--entrypoint", launch.guard])
    argv.extend(["-v", f"{workspace}:{CONTAINER_WORKSPACE}", "-w", CONTAINER_WORKSPACE])
    for mount in launch.mounts:
        suffix = ":ro" if mount.read_only else ""
        argv.extend(["-v", f"{mount.source}:{mount.target}{suffix}"])
    for name, value in launch.env.items():
        argv.extend(["-e", f"{name}={value}"])
    for name in launch.secret_env:
        argv.extend(["-e", name])
    argv.append(launch.image)
    argv.extend(launch.argv)
    return argv


def cli_env(
    launch: "Launch", *, base_env: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The docker CLI's environment for one launch: :func:`build_env` plus the
    values of ``launch.secret_env`` (forwarded by name in :func:`run_argv`)."""
    env = build_env(base_env=base_env)
    env.update(launch.secret_env)
    return env


def _isolation_argv(config: HostConfig) -> list[str]:
    """Per-container resource ceilings and reduced privileges (both faces).

    Shared so the ACP face cannot drift from the pi face on the isolation
    contract. The image is never changed here; these are ``docker run``
    instance arguments.
    """
    return [
        "--cpus", str(config.docker_cpus),
        "--memory", str(config.docker_memory),
        "--pids-limit", str(config.docker_pids_limit),
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
    ]


def build_env(*, base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for the *docker CLI*, not for the container.

    The container's configuration — including the run's egress token in
    ``auth.json`` — arrives through the mounted config directory, so nothing
    credential-shaped is placed on the CLI's environment or argv. The whitelist additionally keeps
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
