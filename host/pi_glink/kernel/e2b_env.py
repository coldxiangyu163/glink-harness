"""E2B execution face: pi runs inside a cloud sandbox.

**Why this face speaks a different protocol.** The E2B SDK exposes one-shot
command execution with streamed stdout, not a bidirectional pipe, so this face
uses pi's single-shot JSON event stream (``pi --mode json "<prompt>"``) instead
of the RPC protocol used by the in-process and Docker faces. That is the
documented protocol fallback in the objective, and the event vocabulary and
projection are identical, so the browser contract does not change.

**Credential flow (recorded per the task contract).**

* Model credentials travel through ``Sandbox.create(envs=…)``: they exist only
  in the sandbox process environment and are never written to a file.
* The pi-web-access credential is written to ``/home/user/config/web-search.json``
  inside the sandbox. That file lives for the sandbox lifetime only (destroyed on
  ``kill()``); the host keeps just its own local copy. This is deliberate: the
  plugin resolves that file from ``PI_CODING_AGENT_DIR`` and has no
  environment-variable form for it. A sandbox without the file still works
  (keyless Exa MCP search).

Everything else is data: the runtime config rendered by the host (models.json,
settings.json) is uploaded, and the report is pulled back into the host run
workspace.
"""

from __future__ import annotations

import base64
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..config import ConfigError, HostConfig
from .agent_env import AgentRuntime, WEB_SEARCH_FILENAME

#: Sandbox paths (E2B runs as a non-root user with /home/user as home).
SANDBOX_HOME = "/home/user"
SANDBOX_CONFIG = f"{SANDBOX_HOME}/config"
SANDBOX_WORKSPACE = f"{SANDBOX_HOME}/workspace"
SANDBOX_SESSIONS = f"{SANDBOX_HOME}/sessions"
#: Uploaded config files; the credential file is handled separately.
_CONFIG_FILES = ("models.json", "settings.json")
REPORT_FILENAME = "report.md"
#: The sandbox writes its JSON event stream here (see module docstring).
_EVENTS_FILE = f"{SANDBOX_HOME}/events.jsonl"
_STDERR_FILE = f"{SANDBOX_HOME}/pi-stderr.txt"
#: Env vars the pi process inside the sandbox needs.
_SANDBOX_ENV_NAMES = ("PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR", "PI_SKIP_VERSION_CHECK", "PI_TELEMETRY")


class SandboxError(RuntimeError):
    """Raised when the sandbox cannot be created or the run cannot start."""


@dataclass(frozen=True, slots=True)
class SandboxOutcome:
    sandbox_id: str
    exit_code: int
    stderr_tail: str
    report_path: Path | None
    uploaded: list[str] = field(default_factory=list)


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
        """Upload the host-rendered config the kernel needs (see module docstring)."""
        sandbox = self._require_sandbox()
        sandbox.files.make_dir(SANDBOX_CONFIG)
        sandbox.files.make_dir(SANDBOX_WORKSPACE)
        sandbox.files.make_dir(SANDBOX_SESSIONS)
        for name in _CONFIG_FILES:
            source = self.runtime.directory / name
            if source.is_file():
                sandbox.files.write(f"{SANDBOX_CONFIG}/{name}", source.read_text(encoding="utf-8"))
                self.uploaded.append(name)
        credential = self.runtime.directory / WEB_SEARCH_FILENAME
        if credential.is_file():
            sandbox.files.write(
                f"{SANDBOX_CONFIG}/{WEB_SEARCH_FILENAME}", credential.read_text(encoding="utf-8")
            )
            self.uploaded.append(WEB_SEARCH_FILENAME)

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
        """Return the JSONL frames the sandbox wrote for this run."""
        sandbox = self._require_sandbox()
        try:
            content = sandbox.files.read(_EVENTS_FILE)
        except Exception:  # noqa: BLE001 - absent stream means no frames
            return []
        text = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
        return [line for line in text.splitlines() if line.strip()]

    def fetch_stderr_tail(self, limit: int = 20) -> str:
        sandbox = self._require_sandbox()
        try:
            content = sandbox.files.read(_STDERR_FILE)
        except Exception:  # noqa: BLE001
            return ""
        text = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
        return "\n".join(text.splitlines()[-limit:])

    def fetch_report(self) -> Path | None:
        """Pull ``report.md`` (and any other artifacts) back into the host workspace."""
        sandbox = self._require_sandbox()
        self.workspace.mkdir(parents=True, exist_ok=True)
        try:
            entries = sandbox.files.list(SANDBOX_WORKSPACE)
        except Exception:  # noqa: BLE001 - listing is best effort
            entries = []
        for entry in entries:
            name = getattr(entry, "name", None) or ""
            path = getattr(entry, "path", None) or f"{SANDBOX_WORKSPACE}/{name}"
            if not name or name.startswith("."):
                continue
            try:
                content = sandbox.files.read(path)
            except Exception:  # noqa: BLE001 - skip unreadable entries (dirs)
                continue
            target = self.workspace / Path(path).name
            target.write_bytes(content if isinstance(content, bytes) else str(content).encode("utf-8"))
        report = self.workspace / REPORT_FILENAME
        return report if report.is_file() else None

    def close(self) -> None:
        sandbox = self.sandbox
        self.sandbox = None
        if sandbox is None:
            return
        try:
            sandbox.kill()
        except Exception:  # noqa: BLE001 - teardown is best effort
            pass

    # -- internals ------------------------------------------------------------

    def _require_sandbox(self) -> Any:
        if self.sandbox is None:
            raise SandboxError("sandbox 未启动")
        return self.sandbox

    def _model_envs(self) -> dict[str, str]:
        envs = {
            "PI_CODING_AGENT_DIR": SANDBOX_CONFIG,
            "PI_CODING_AGENT_SESSION_DIR": SANDBOX_SESSIONS,
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
        }
        if self.config.openai_api_key:
            envs["OPENAI_API_KEY"] = self.config.openai_api_key
        return envs

    def _command(self, prompt: str, system_prompt: str) -> str:
        """Compose the sandbox command.

        Both the prompt and the research contract are base64-encoded so quoting,
        newlines and non-ASCII text cannot break the shell line.
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
