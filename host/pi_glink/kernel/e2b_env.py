"""E2B execution face: pi runs inside a cloud sandbox.

**Why this face speaks a different protocol.** The E2B SDK exposes one-shot
command execution with streamed stdout, not a bidirectional pipe, so this face
uses pi's single-shot JSON event stream (``pi --mode json "<prompt>"``) instead
of the RPC protocol used by the in-process and Docker faces. That is the
documented protocol fallback in the objective, and the event vocabulary and
projection are identical, so the browser contract does not change.

**Credential flow (recorded per the task contract).**

* The model credential is uploaded inside ``auth.json`` — pi's official credential
  store — so the sandbox kernel never receives it through its environment: a shell
  tool inside the sandbox cannot read it with ``env``. The file exists only for the
  sandbox lifetime (destroyed on ``kill()``).
* The pi-web-access credential is written to ``/home/user/config/web-search.json``
  inside the sandbox. That file lives for the sandbox lifetime only (destroyed on
  ``kill()``); the host keeps just its own local copy. This is deliberate: the
  plugin resolves that file from ``PI_CODING_AGENT_DIR`` and has no
  environment-variable form for it. A sandbox without the file still works
  (keyless Exa MCP search).

**Configuration delivery.** The sandbox receives the *whole* per-run runtime
directory (generated files plus the reviewed template entries: ``AGENTS.md``,
``settings.json``, ``extensions/``, ``skills/``, ``prompts/``), with the same
relative paths the in-process and Docker faces use. The upload records a manifest
of ``(relative path, sha256)`` so the delivery is verifiable instead of "the
current directory happened to be empty".

**Known limitation (honest, not papered over).** The E2B SDK 2.x does not fire
its stdout callback for ``commands.run(background=True)``, so the sandbox writes
its event stream to a file and the host replays it after the command settles.
The order and vocabulary match the other faces; the difference is that this face
is **not live**. See ``docs/architecture.md`` §5 and ``docs/evidence/e2b-run.txt``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Callable, Mapping

from ..artifacts import ArtifactError, ArtifactTree
from ..config import ConfigError, HostConfig
from .agent_env import AgentRuntime
from .framing import strip_trailing_cr

#: Sandbox paths (E2B runs as a non-root user with /home/user as home).
SANDBOX_HOME = "/home/user"
SANDBOX_CONFIG = f"{SANDBOX_HOME}/config"
SANDBOX_WORKSPACE = f"{SANDBOX_HOME}/workspace"
SANDBOX_SESSIONS = f"{SANDBOX_HOME}/sessions"
REPORT_FILENAME = "report.md"
#: The sandbox writes its JSON event stream here (see module docstring).
_EVENTS_FILE = f"{SANDBOX_HOME}/events.jsonl"
_STDERR_FILE = f"{SANDBOX_HOME}/pi-stderr.txt"
#: One uploaded config file / recovered artifact may not exceed this.
UPLOAD_MAX_BYTES = 8 * 1024 * 1024
ARTIFACT_MAX_BYTES = 32 * 1024 * 1024


class SandboxError(RuntimeError):
    """Raised when the sandbox cannot be created or the run cannot start."""


class E2BSandboxClient:
    """Own one sandbox for one run.

    ``sandbox_factory`` is injectable so the runner can be exercised without a
    cloud dependency.
    """

    def __init__(
        self,
        config: HostConfig,
        runtime: AgentRuntime,
        *,
        run_id: str,
        workspace: Path,
        sandbox_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.runtime = runtime
        self.run_id = run_id
        self.workspace = workspace
        self._sandbox_factory = sandbox_factory
        self.sandbox: Any | None = None
        self.uploaded: list[str] = []
        #: ``(relative path, sha256)`` for everything delivered to the sandbox.
        self.upload_manifest: list[tuple[str, str]] = []
        self.sandbox_id: str = ""

    # -- lifecycle ------------------------------------------------------------

    def start(self, *, timeout_seconds: int | None = None) -> None:
        if self.sandbox is not None:
            raise SandboxError("sandbox already started")
        template = self.config.e2b_template
        if not template:
            raise ConfigError("LOCAL_WEB_E2B_TEMPLATE 未配置")
        if not self.config.has_e2b_key:
            raise ConfigError("E2B_API_KEY 未配置")
        factory = self._sandbox_factory or _default_sandbox_factory()
        # ``Sandbox.create`` takes the credential through ``**opts`` (ApiParams),
        # so an operator who only filled ``.env`` still authenticates: the host
        # passes the key it read at startup instead of relying on the SDK having
        # inherited E2B_API_KEY from the launching shell.
        create_kwargs: dict[str, Any] = {
            "template": template,
            "timeout": timeout_seconds or self.config.task_timeout_seconds + 300,
            "envs": self._model_envs(),
        }
        if self.config.e2b_api_key:
            create_kwargs["api_key"] = self.config.e2b_api_key
        try:
            self.sandbox = factory(**create_kwargs)
        except Exception as exc:  # noqa: BLE001 - surfaced to the run record
            raise SandboxError(f"sandbox 创建失败: {type(exc).__name__}: {exc}") from exc
        self.sandbox_id = str(getattr(self.sandbox, "sandbox_id", ""))

    def upload_config(self) -> None:
        """Upload the run's rendered configuration directory (see module docstring).

        Every file in the runtime directory is delivered with its relative path
        intact, so the sandboxed kernel reads the same configuration the local
        faces read. The manifest lets a test (or an operator) verify what was
        actually delivered.
        """
        sandbox = self._require_sandbox()
        sandbox.files.make_dir(SANDBOX_CONFIG)
        sandbox.files.make_dir(SANDBOX_WORKSPACE)
        sandbox.files.make_dir(SANDBOX_SESSIONS)
        manifest = _runtime_manifest(self.runtime.directory)
        if not manifest:
            return
        try:
            tree = ArtifactTree(self.runtime.directory)
        except ArtifactError as exc:
            raise SandboxError(f"配置交付失败: {exc}") from exc
        with tree:
            for relative, is_dir in manifest:
                sandbox_path = f"{SANDBOX_CONFIG}/{relative}"
                if is_dir:
                    sandbox.files.make_dir(sandbox_path)
                    continue
                try:
                    data = tree.read(relative, max_bytes=UPLOAD_MAX_BYTES)
                except ArtifactError as exc:
                    raise SandboxError(f"配置交付失败: {relative}: {exc}") from exc
                sandbox.files.write(sandbox_path, data)
                self.uploaded.append(sandbox_path)
                self.upload_manifest.append((relative, hashlib.sha256(data).hexdigest()))

    def run_prompt(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
    ) -> tuple[int, str]:
        """Run ``pi --mode json`` once inside the sandbox.

        The sandbox writes its stdout/stderr to files instead of relying on the
        SDK's streaming callback: in e2b 2.x, ``commands.run(background=True,
        on_stdout=…)`` never fires (verified against the SDK), while the command
        result carries the whole buffer only at the end. Writing a JSONL file and
        replaying it once the command settles keeps every frame — the trade-off
        is that this face replays the run *after* it ends rather than live, which
        ``docs/architecture.md`` records as a known difference.
        """
        sandbox = self._require_sandbox()
        command = self._command(prompt, system_prompt)
        handle = sandbox.commands.run(command, background=True, envs=self._model_envs())
        result = handle.wait()
        exit_code = int(getattr(result, "exit_code", 1))
        return exit_code, self.fetch_stderr_tail()

    def fetch_events(self) -> list[str]:
        """Return the JSONL records the sandbox wrote for this run.

        Framing is LF-only (``pi`` documents strict JSONL): ``splitlines`` would
        also split on U+2028/U+2029, tearing a valid JSON string in two and
        losing both halves.
        """
        sandbox = self._require_sandbox()
        try:
            content = sandbox.files.read(_EVENTS_FILE)
        except Exception:  # noqa: BLE001 - absent stream means no frames
            return []
        text = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
        return [
            strip_trailing_cr(line)
            for line in text.split("\n")
            if line.strip()
        ]

    def fetch_stderr_tail(self, limit: int = 20) -> str:
        sandbox = self._require_sandbox()
        try:
            content = sandbox.files.read(_STDERR_FILE)
        except Exception:  # noqa: BLE001
            return ""
        text = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
        return "\n".join(text.split("\n")[-limit:])

    def recover_artifacts(self, destination: Path) -> tuple[list[Path], list[str]]:
        """Pull the sandbox workspace back into the host run workspace.

        Only direct children with simple names are recovered, each one bounded;
        anything unreadable (a directory), oversized, or named with a path
        separator is recorded as a failure instead of being guessed at.
        """
        sandbox = self._require_sandbox()
        destination.mkdir(parents=True, exist_ok=True)
        try:
            entries = sandbox.files.list(SANDBOX_WORKSPACE)
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            return [], [f"无法列出沙箱工作区: {type(exc).__name__}: {exc}"]
        try:
            tree = ArtifactTree(destination)
        except ArtifactError as exc:
            return [], [f"宿主工作区不可用: {exc}"]
        files: list[Path] = []
        failures: list[str] = []
        with tree:
            for entry in entries:
                name = str(getattr(entry, "name", None) or "")
                path = str(getattr(entry, "path", None) or f"{SANDBOX_WORKSPACE}/{name}")
                if not name:
                    continue
                # Path semantics are checked before the hidden-file skip: a name
                # that tries to escape ("../x") starts with a dot and must be
                # reported, not silently ignored.
                if "/" in name or "\\" in name or name in {".", ".."}:
                    failures.append(f"跳过非法产物名: {name!r}")
                    continue
                if name.startswith("."):
                    continue
                try:
                    content = sandbox.files.read(path)
                except Exception:  # noqa: BLE001 - directories and unreadable entries
                    continue
                data = content if isinstance(content, bytes) else str(content).encode("utf-8")
                if len(data) > ARTIFACT_MAX_BYTES:
                    failures.append(f"产物超过上限: {name}")
                    continue
                try:
                    tree.replace(name, data, max_bytes=ARTIFACT_MAX_BYTES)
                except (ArtifactError, OSError) as exc:
                    failures.append(f"产物写入失败: {name}: {exc}")
                    continue
                files.append(destination / name)
        return files, failures

    def close(self) -> list[str]:
        """Destroy the sandbox. Returns the failures instead of hiding them."""
        sandbox = self.sandbox
        self.sandbox = None
        if sandbox is None:
            return []
        try:
            sandbox.kill()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            return [f"sandbox kill failed: {type(exc).__name__}: {exc}"]
        return []

    # -- internals ------------------------------------------------------------

    def _require_sandbox(self) -> Any:
        if self.sandbox is None:
            raise SandboxError("sandbox 未启动")
        return self.sandbox

    def _model_envs(self) -> dict[str, str]:
        """Environment for the sandboxed kernel — deliberately credential-free.

        The model credential travels in the uploaded ``auth.json`` (pi's own
        store), exactly like the in-process face, so ``env`` inside the sandbox
        cannot print it.
        """
        return {
            "PI_CODING_AGENT_DIR": SANDBOX_CONFIG,
            "PI_CODING_AGENT_SESSION_DIR": SANDBOX_SESSIONS,
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
        }

    def _command(self, prompt: str, system_prompt: str) -> str:
        """Compose the sandbox command.

        Both the prompt and the research contract are base64-encoded before they
        are interpolated into the shell line, so quoting, newlines, backticks,
        ``$(…)`` and non-ASCII text cannot break the command or execute anything:
        the shell only ever sees a base64 alphabet. The command is asserted by
        the adapter tests against exactly those inputs.
        """
        encoded_prompt = base64.b64encode(prompt.encode("utf-8")).decode("ascii")
        parts = [
            f"mkdir -p {SANDBOX_CONFIG} {SANDBOX_WORKSPACE} {SANDBOX_SESSIONS}",
            f"cd {SANDBOX_WORKSPACE}",
            f"PROMPT=$(printf '%s' '{encoded_prompt}' | base64 -d)",
        ]
        pi = [
            "pi",
            "--mode", "json",
            "--provider", self.runtime.provider_id,
            "--model", self.runtime.model_id,
            "--name", f"pi-glink-{self.run_id}",
        ]
        if system_prompt:
            encoded_system = base64.b64encode(system_prompt.encode("utf-8")).decode("ascii")
            parts.append(f"SYSTEM=$(printf '%s' '{encoded_system}' | base64 -d)")
            pi.extend(["--append-system-prompt", '"$SYSTEM"'])
        parts.append(f"{' '.join(pi)} \"$PROMPT\" > {_EVENTS_FILE} 2> {_STDERR_FILE}")
        return " && ".join(parts)


def _runtime_manifest(runtime_dir: Path) -> list[tuple[str, bool]]:
    """Every file/directory to deliver, as ``(relative, is_dir)``.

    The runtime directory is host-rendered before the sandbox exists; the bytes
    are still read through :class:`~pi_glink.artifacts.ArtifactTree`.
    """
    manifest: list[tuple[str, bool]] = []
    if not runtime_dir.is_dir():
        return manifest
    for child in sorted(runtime_dir.rglob("*")):
        if child.is_symlink():
            continue
        manifest.append((child.relative_to(runtime_dir).as_posix(), child.is_dir()))
    return manifest


def parse_json_stream_line(line: str) -> Mapping[str, Any] | None:
    """Parse one ``--mode json`` frame; returns None for non-JSON output."""
    text = line.strip()
    if not text or not text.startswith("{"):
        return None
    try:
        frame = json.loads(text)
    except json.JSONDecodeError:
        return None
    return frame if isinstance(frame, dict) else None


def _default_sandbox_factory() -> Callable[..., Any]:
    def factory(**kwargs: Any) -> Any:
        from e2b import Sandbox

        return Sandbox.create(**kwargs)

    return factory


def sandbox_available() -> tuple[bool, str]:
    """Best-effort preflight used by the host before it accepts a run."""
    try:
        import e2b  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        return False, f"e2b SDK 不可用: {exc}"
    if shutil.which("ssh") is None:  # pragma: no cover - ssh is ubiquitous
        return True, ""
    return True, ""
