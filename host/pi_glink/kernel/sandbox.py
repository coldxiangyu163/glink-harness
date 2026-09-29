"""Sandbox: the resource-and-transport dimension of one run's execution.

A run's adapter is composed from two orthogonal choices (docs/architecture.md
§4, docs/multi-harness-plan.md §4):

* the **harness** (:class:`~pi_glink.kernel.harness.HarnessFamily`: pi, ACP)
  decides *what* runs — the launch argv and environment, the protocol driver,
  the stop vocabulary, continuation, subagent enrichment and usage;
* the **sandbox** (this module: in-process, Docker, E2B) decides *where* it
  runs — creating and destroying the resource, spawning the process on it,
  mapping paths between the harness's view and the host, recovering the
  workspace before the resource dies, and the resource's durable identity.

The composition is ``harness.driver(sandbox.spawn(harness.launch(spec)))``:
the harness describes the process as a :class:`Launch` (placing its own paths
through :meth:`Sandbox.place`), the sandbox turns it into a concrete
:class:`Spawn` for its carrier, and the harness's driver speaks the protocol
over it. Neither side branches on the other's name: a sandbox never knows which
harness it carries, and a harness only asks the sandbox facts
(``isolated``/``remote``/``place``/``handle``).

The sandbox is chosen from the run's frozen :class:`~pi_glink.store.RunSpec`
(``spec.executor``) by :func:`open_sandbox`, never from the host's current
configuration: a run keeps the face it was admitted on.
"""

from __future__ import annotations

import shlex
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..config import ConfigError, HostConfig
from .acp_e2b_env import SANDBOX_ACP_HOME
from .agent_env import passthrough_env
from .docker_env import (
    CONTAINER_CONFIG,
    CONTAINER_HOME,
    CONTAINER_SESSIONS,
    CONTAINER_WORKSPACE,
    cli_env as docker_cli_env,
    DOCKER_HOST_GATEWAY,
    container_name_for,
    is_available as docker_available,
    run_argv as docker_run_argv,
    stop_argv as docker_stop_argv,
    translate_container_path,
)
from .e2b_env import (
    SANDBOX_CONFIG,
    SANDBOX_SESSIONS,
    SANDBOX_WORKSPACE,
    SandboxCommandProcess,
    SandboxError,
    _create_sandbox,
    _recover_workspace,
    kill_within_budget,
)


#: Path roles a harness may place. ``workspace`` is the run workspace, the
#: others are harness-owned state the sandbox only has to make reachable.
ROLES = ("workspace", "config", "sessions", "home")


@dataclass(frozen=True, slots=True)
class Mount:
    """One host directory/file the process must see at ``target``."""

    source: Path
    target: str
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class Launch:
    """What a harness wants to run, independent of where it runs.

    ``env`` holds non-secret literals; ``secret_env`` holds values that must
    never appear in argv (the Docker face forwards them by name). ``mounts``
    are only meaningful to a sandbox that mounts (Docker); a sandbox that
    uploads (E2B) leaves delivery to the harness driver. ``guard`` is the
    image's package guard (run before the argv on isolated sandboxes);
    ``image``/``template`` name the harness's Docker image and E2B template;
    ``create_env`` is the environment the E2B sandbox itself is created with.
    """

    argv: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    secret_env: Mapping[str, str] = field(default_factory=dict)
    mounts: tuple[Mount, ...] = ()
    guard: str | None = None
    image: str = ""
    template: str = ""
    create_env: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class Spawn:
    """The concrete process a driver starts.

    ``process`` is ``None`` when the protocol client spawns ``argv`` on this
    host itself (in-process, and the ``docker run -i`` CLI); otherwise it is a
    factory returning an already-started ``subprocess.Popen``-shaped process
    (the E2B background command).
    """

    argv: list[str]
    env: dict[str, str]
    cwd: Path | None
    process: Callable[..., Any] | None = None


@dataclass(frozen=True, slots=True)
class CleanupReport:
    """Whether the face's resources are actually released."""

    released: bool
    failures: tuple[str, ...] = ()
    external: Mapping[str, str] = field(default_factory=dict)


IdentityCallback = Callable[[Mapping[str, str]], None]


class Sandbox(ABC):
    """One run's execution resource. Created per run, closed exactly once."""

    kind: str = "unknown"
    #: The kernel runs inside a self-contained image/template (package paths
    #: are image-local, argv[0] is the image's command).
    isolated: bool = False
    #: The workspace and harness state live away from the host until
    #: recovered (the resource is destroyed with them).
    remote: bool = False

    def __init__(
        self,
        *,
        config: HostConfig,
        run_id: str,
        workspace: Path,
        sandbox_factory: Callable[..., Any] | None = None,
        on_identity: IdentityCallback | None = None,
    ) -> None:
        self.config = config
        self.run_id = run_id
        self.workspace = Path(workspace)
        self._sandbox_factory = sandbox_factory
        self._on_identity = on_identity

    @abstractmethod
    def place(self, role: str, host: Path) -> str:
        """The path under which the process sees the host path ``host``."""

    def planned_identity(self) -> Mapping[str, str]:
        """Identity known before :meth:`open` (persisted before any start step)."""
        return {}

    def open(self, launch: Launch, *, timeout_seconds: int) -> None:
        """Create the resource. Raises when the face is unavailable."""
        # Hook 默认空实现：本地 face 无资源可建（inproc 不覆写）；显式 return None
        # 而非留空 body，避免被当成遗漏的 @abstractmethod（B027）。
        return None

    @abstractmethod
    def spawn(self, launch: Launch) -> Spawn: ...

    @property
    def handle(self) -> Any | None:
        """The live remote resource (E2B SDK sandbox), ``None`` elsewhere."""
        return None

    def recover_workspace(
        self, destination: Path, *, declared: tuple[str, ...] = ()
    ) -> tuple[list[Path], list[str]]:
        """Pull the remote workspace back (a no-op where it already is the host's)."""
        return [], []

    def host_path(self, reported: str, *, role: str, host_root: Path) -> str | None:
        """Map a path the process reported under ``role`` back to the host."""
        return reported

    def session_facts(self) -> dict[str, Any]:
        """Facts about the carrier a ``session_ready`` frame records."""
        return {}

    def identity(self) -> Mapping[str, str]:
        return {}

    def egress_origin(self, port: int) -> str:
        """The host's egress proxy origin as this face's process reaches it.

        ``port`` is the proxy's bound listener port. ``PI_GLINK_EGRESS_PUBLIC_URL``
        (when set) always wins: it is the operator's statement of where the
        proxy is reachable from outside the host (docs/adr/0008).
        """
        public = self.config.egress_public_url
        if public:
            return public
        return self._default_egress_origin(port)

    def _default_egress_origin(self, port: int) -> str:
        host = self.config.egress_host
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        if ":" in host:
            host = f"[{host}]"
        return f"http://{host}:{port}"

    def close(self) -> CleanupReport:
        return CleanupReport(released=True, external=dict(self.identity()))


class InprocSandbox(Sandbox):
    """The kernel runs as a child process of the host, in the host workspace."""

    kind = "inproc"

    def place(self, role: str, host: Path) -> str:
        return str(host)

    def spawn(self, launch: Launch) -> Spawn:
        env = passthrough_env()
        env.update(launch.env)
        env.update(launch.secret_env)
        return Spawn(argv=list(launch.argv), env=env, cwd=self.workspace)


class DockerSandbox(Sandbox):
    """A disposable ``docker run --rm -i`` container named after the run."""

    kind = "docker"
    isolated = True

    _PLACES = {
        "workspace": CONTAINER_WORKSPACE,
        "config": CONTAINER_CONFIG,
        "sessions": CONTAINER_SESSIONS,
        "home": CONTAINER_HOME,
    }

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.container_name = container_name_for(self.run_id)
        self._opened = False

    def place(self, role: str, host: Path) -> str:
        return self._PLACES[role]

    def planned_identity(self) -> Mapping[str, str]:
        # The name is fixed by the run id before ``docker run`` executes, so a
        # failure anywhere in start() still leaves a named leftover.
        return {"container": self.container_name}

    def open(self, launch: Launch, *, timeout_seconds: int) -> None:
        if not docker_available():
            raise ConfigError("LOCAL_WEB_EXECUTOR=docker requires a reachable Docker daemon")
        self._opened = True

    def spawn(self, launch: Launch) -> Spawn:
        return Spawn(
            argv=self.command(launch),
            env=docker_cli_env(launch),
            cwd=None,
        )

    def command(self, launch: Launch) -> list[str]:
        """The ``docker run`` argv for ``launch`` (see :func:`docker_env.run_argv`)."""
        return docker_run_argv(
            self.config, launch, run_id=self.run_id, workspace=self.workspace
        )

    def host_path(self, reported: str, *, role: str, host_root: Path) -> str | None:
        mapped = translate_container_path(reported, ((self._PLACES[role], host_root),))
        return str(mapped) if mapped is not None else None

    def identity(self) -> Mapping[str, str]:
        return {"container": self.container_name}

    def _default_egress_origin(self, port: int) -> str:
        # The container reaches the host through the gateway alias that
        # run_argv adds (``--add-host host.docker.internal:host-gateway``).
        # The listener must be bound where the bridge can reach it
        # (PI_GLINK_EGRESS_HOST; loopback works on Docker Desktop only).
        return f"http://{DOCKER_HOST_GATEWAY}:{port}"

    def close(self) -> CleanupReport:
        failures: list[str] = []
        if self._opened:
            # Closing stdin ends the kernel and ``--rm`` removes the container;
            # the force-remove confirms nothing keeps running, addressed by the
            # one identity this run persisted — never a name pattern.
            self._opened = False
            try:
                result = subprocess.run(
                    docker_stop_argv(self.container_name),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                stderr = (result.stderr or "").strip()
                if result.returncode != 0 and "No such container" not in stderr:
                    failures.append(f"删除容器失败: exit {result.returncode}: {stderr[-200:]}")
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                failures.append(f"删除容器失败: {type(exc).__name__}: {exc}")
        return CleanupReport(
            released=not failures, failures=tuple(failures), external=dict(self.identity())
        )


class E2BSandbox(Sandbox):
    """One E2B cloud sandbox: created per run, destroyed by ``kill()``."""

    kind = "e2b"
    isolated = True
    remote = True

    _PLACES = {
        "workspace": SANDBOX_WORKSPACE,
        "config": SANDBOX_CONFIG,
        "sessions": SANDBOX_SESSIONS,
        "home": SANDBOX_ACP_HOME,
    }

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._handle: Any | None = None
        #: Kept after ``close()``: a later failure must not erase the identity.
        self.sandbox_id = ""
        self.template = ""
        #: Lifetime the sandbox was created with; the kernel command must be
        #: allowed to run at least that long (see :meth:`command_timeout`).
        self.sandbox_timeout = 0

    @property
    def handle(self) -> Any | None:
        return self._handle

    def place(self, role: str, host: Path) -> str:
        return self._PLACES[role]

    def _default_egress_origin(self, port: int) -> str:
        # A cloud sandbox cannot reach the host's loopback. Without an explicit
        # public URL the run is refused — never handed the real key instead
        # (fail closed, docs/adr/0008).
        raise ConfigError(
            "LOCAL_WEB_EXECUTOR=e2b requires PI_GLINK_EGRESS_PUBLIC_URL: the "
            "sandbox must reach the host's credential egress proxy"
        )

    def open(self, launch: Launch, *, timeout_seconds: int) -> None:
        if self._handle is not None:
            raise SandboxError("sandbox already started")
        self.template = launch.template
        handle, self.sandbox_timeout = _create_sandbox(
            self.config,
            template=launch.template,
            timeout_seconds=timeout_seconds,
            sandbox_factory=self._sandbox_factory,
            envs=launch.create_env,
        )
        self._handle = handle
        self.sandbox_id = str(getattr(handle, "sandbox_id", ""))
        # The sandbox exists from here on: hand its identity to the host before
        # any later step can fail, so a partial start still leaves a named
        # leftover the operator and the online sweep can act on.
        if self.sandbox_id and self._on_identity is not None:
            self._on_identity({"sandbox": self.sandbox_id})

    def command(self, launch: Launch) -> str:
        """The sandbox command line: optional package guard, then ``exec`` the argv.

        A template older than the host has no guard script: the command fails
        with exit 78 and a named cause instead of starting pi without
        web_search. ``exec`` replaces the shell so the kernel owns stdin, and
        :func:`shlex.join` quotes every argument (``--append-system-prompt``
        text included); the prompt itself travels as a protocol frame on stdin
        and never appears on the command line.
        """
        workspace = shlex.quote(SANDBOX_WORKSPACE)
        if not launch.guard:
            return f"cd {workspace} && exec {shlex.join(launch.argv)}"
        guard = launch.guard
        return (
            f"test -x {guard} || {{ "
            f"echo 'pi-glink: 沙箱模板缺少预装包守卫 {guard}，"
            "请用 scripts/build-e2b-template.py 重建模板' >&2; exit 78; }"
            f" && cd {workspace}"
            f" && exec {shlex.join([guard, *launch.argv])}"
        )

    def command_timeout(self) -> int:
        """How long the kernel command may run: the sandbox's own lifetime.

        A shorter bound would kill the kernel while the sandbox is still usable
        (the SDK default is 60 s); a longer one is meaningless because the
        sandbox dies first. Frozen from the same calculation that created it.
        """
        if self.sandbox_timeout > 0:
            return self.sandbox_timeout
        return int(self.config.task_timeout_seconds) + 300

    def spawn(self, launch: Launch) -> Spawn:
        handle = self._handle
        if handle is None:
            raise SandboxError("sandbox 未启动")
        envs = dict(launch.env)
        envs.update(launch.secret_env)
        command = handle.commands.run(
            self.command(launch),
            background=True,
            stdin=True,
            envs=envs,
            cwd=SANDBOX_WORKSPACE,
            timeout=self.command_timeout(),
        )
        process = SandboxCommandProcess(command, sandbox_id=self.sandbox_id)
        # The client's own env/cwd are only used when it spawns a host
        # process, which this face never does.
        return Spawn(argv=list(launch.argv), env={}, cwd=None, process=lambda *a, **k: process)

    def recover_workspace(
        self, destination: Path, *, declared: tuple[str, ...] = ()
    ) -> tuple[list[Path], list[str]]:
        """Pull declared files first, then ordinary direct children within shared limits."""
        if self._handle is None:
            return [], []
        return _recover_workspace(self._handle, destination, declared=declared)

    def session_facts(self) -> dict[str, Any]:
        return {"sandboxId": self.sandbox_id, "template": self.template}

    def identity(self) -> Mapping[str, str]:
        return {"sandbox": self.sandbox_id} if self.sandbox_id else {}

    def close(self) -> CleanupReport:
        failures: list[str] = []
        handle = self._handle
        self._handle = None
        if handle is not None:
            # 销毁是释放权威，但 kill 本身不能无界：SDK 的 command kill 不接受
            # 请求超时，运输楔死时宿主必须放弃等待并记失败（不谎报已释放）。
            try:
                kill_within_budget(handle.kill, what="sandbox")
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                failures.append(f"sandbox kill failed: {type(exc).__name__}: {exc}")
        return CleanupReport(
            released=not failures, failures=tuple(failures), external=dict(self.identity())
        )


SANDBOXES: Mapping[str, type[Sandbox]] = {
    "inproc": InprocSandbox,
    "docker": DockerSandbox,
    "e2b": E2BSandbox,
}


def open_sandbox(
    executor: str,
    *,
    config: HostConfig,
    run_id: str,
    workspace: Path,
    sandbox_factory: Callable[..., Any] | None = None,
    on_identity: IdentityCallback | None = None,
) -> Sandbox:
    """The sandbox for a run's frozen ``RunSpec.executor`` (not yet opened)."""
    try:
        cls = SANDBOXES[executor]
    except KeyError as exc:
        raise ConfigError(f"未知的执行面 executor: {executor!r}") from exc
    return cls(
        config=config,
        run_id=run_id,
        workspace=workspace,
        sandbox_factory=sandbox_factory,
        on_identity=on_identity,
    )
