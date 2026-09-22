"""Prepare the pi configuration directory for a run.

pi reads its configuration from ``PI_CODING_AGENT_DIR``. The repository ships a
template directory (``agent/``) for the parts that should be reviewed and
versioned: settings, AGENTS.md, extensions, skills, prompts.

Operator-provided values cannot live in those files. pi resolves environment
interpolation for ``apiKey`` and ``headers`` only — not for ``baseUrl`` and not
for model ids (verified against pi 0.86.1: a ``$OPENAI_BASE_URL`` baseUrl
reaches the provider as a literal string and every request fails with
``Invalid URL``). So the host renders a small runtime directory:

    <data_root>/pi-agent/
    ├── models.json          generated: literal baseUrl + literal model id
    ├── auth.json            generated: model credential in pi's own store (0600)
    ├── web-search.json      generated: pi-web-access credential (0600), if configured
    └── settings.json, AGENTS.md, extensions/, …   copied from the template

**The model credential lives in ``auth.json``, not in the process environment.**
That is pi's official credential store — the same file ``/login`` writes — and
using it keeps the key out of the kernel's environment, where its shell tool
would otherwise read it with ``env``. The file sits in the runtime directory
(outside the repository) with mode 0600 and is rewritten on every run. The same
reasoning applies to the pi-web-access credential, which the plugin can only read
from a file anyway.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..config import ConfigError, HostConfig

#: Provider id used inside the generated models.json.
PROVIDER_ID = "frontier"
#: OpenRouter-style thinking controls (``reasoning: {effort}``).
#:
#: Without this, pi sends no thinking parameter for a custom openai-completions
#: provider, the upstream omits reasoning entirely, and the workbench has
#: nothing to render for ``thinking_delta``. Verified against
#: openrouter.ai/api/v1: with this format the same model returns reasoning.
THINKING_FORMAT = "openrouter"
#: Template entries copied verbatim into the runtime directory.
_TEMPLATE_ENTRIES = ("settings.json", "AGENTS.md", "extensions", "skills", "prompts")

#: Environment names handed to the pi process verbatim.
#:
#: The host deliberately does **not** inherit its whole environment: doing so
#: would hand pi's tools every credential the operator's shell happens to export
#: (other providers, cloud keys, CI tokens). Only what the kernel needs to run
#: and reach the network is passed through; every credential is injected
#: explicitly below and nothing else. (pi's tools can still read the model key
#: the kernel itself uses — that is inherent to running the agent in-process and
#: is recorded in docs/architecture.md §6.)
PASSTHROUGH_ENV = (
    "PATH", "HOME", "SHELL", "USER", "LOGNAME", "TMPDIR", "TERM",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "NODE_OPTIONS",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
)
#: pi-web-access credential file (read from PI_CODING_AGENT_DIR).
WEB_SEARCH_FILENAME = "web-search.json"
#: pi's own credential store (same file `/login` writes).
AUTH_FILENAME = "auth.json"
#: Default provider key used when the credential comes from the environment.
WEB_SEARCH_ENV_PROVIDER_KEY = "exaApiKey"


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    """Where pi will read its configuration for this host, and which model."""

    directory: Path
    provider_id: str
    model_id: str
    models_path: Path

    @property
    def model_ref(self) -> str:
        return f"{self.provider_id}/{self.model_id}"


def prepare_agent_dir(
    config: HostConfig, *, runtime_dir: Path | None = None
) -> AgentRuntime:
    """Render the runtime pi configuration directory and return its handle."""
    if not config.openai_base_url:
        raise ConfigError("OPENAI_BASE_URL is required to launch the pi kernel")
    if not config.openai_model:
        raise ConfigError("OPENAI_MODEL is required to launch the pi kernel")

    directory = runtime_dir if runtime_dir is not None else config.data_root / "pi-agent"
    directory.mkdir(parents=True, exist_ok=True)
    _sync_template(config.agent_config_dir, directory)
    _seed_web_search_config(config, directory)
    _write_auth_json(config, directory)

    models_path = directory / "models.json"
    models_path.write_text(
        json.dumps(_models_document(config), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return AgentRuntime(
        directory=directory,
        provider_id=PROVIDER_ID,
        model_id=config.openai_model,
        models_path=models_path,
    )


def build_pi_argv(
    config: HostConfig, runtime: AgentRuntime, *, extra: Sequence[str] = ()
) -> list[str]:
    """Command line for the headless pi process."""
    argv = [
        config.pi_bin,
        "--mode",
        "rpc",
        "--provider",
        runtime.provider_id,
        "--model",
        runtime.model_id,
    ]
    argv.extend(extra)
    return argv


def build_pi_env(
    config: HostConfig,
    runtime: AgentRuntime,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Child environment: minimal passthrough, and **no model credential**.

    The credential is not an environment variable any more. It lives in the
    runtime ``auth.json`` (mode 0600, outside the repository) that pi reads
    itself, which keeps it out of the kernel process environment — a shell tool
    running `env` cannot see it. ``base_env`` is still filtered through
    :data:`PASSTHROUGH_ENV` so a caller handing in ``os.environ`` cannot widen
    the child's view of host credentials.
    """
    source = dict(base_env) if base_env is not None else dict(os.environ)
    env = {name: source[name] for name in PASSTHROUGH_ENV if name in source}
    env["PI_CODING_AGENT_DIR"] = str(runtime.directory)
    env["PI_CODING_AGENT_SESSION_DIR"] = str(config.session_dir)
    env["PI_SKIP_VERSION_CHECK"] = "1"
    env["PI_TELEMETRY"] = "0"
    return env


def _models_document(config: HostConfig) -> dict[str, Any]:
    return {
        "providers": {
            PROVIDER_ID: {
                "baseUrl": config.openai_base_url,
                "api": "openai-completions",
                # No apiKey here on purpose: the credential comes from pi's own
                # auth.json (see _write_auth_json), so `env` inside the kernel
                # never sees it. pi resolves provider credentials from both
                # sources; leaving this field out selects the safe one.
                "models": [
                    {
                        "id": config.openai_model,
                        "name": f"Frontier research model ({config.openai_model})",
                        "reasoning": True,
                        "input": ["text"],
                        "contextWindow": config.context_window,
                        "maxTokens": config.max_output_tokens,
                        "compat": {
                            "thinkingFormat": THINKING_FORMAT,
                            "supportsReasoningEffort": True,
                        },
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                        },
                    }
                ],
            }
        }
    }


def _write_auth_json(config: HostConfig, directory: Path) -> Path | None:
    """Store the model credential the way pi itself stores it.

    ``auth.json`` is pi's official credential store — the same file ``/login``
    writes, mode 0600, inside the runtime config directory (which lives outside
    the repository). Using it instead of an environment variable is what keeps
    the key **out of the kernel's process environment**, and therefore out of
    reach of its shell tool: the objective allows "environment variables or pi's
    official credential mechanism", and this is the narrower one.

    The file is rewritten on every run, so a credential pi might clear while
    rotating its own state is always restored before the next kernel start.
    """
    if not config.openai_api_key:
        return None
    target = directory / AUTH_FILENAME
    payload = {PROVIDER_ID: {"type": "api_key", "key": config.openai_api_key}}
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    target.chmod(0o600)
    return target


def _sync_template(template_dir: Path, target_dir: Path) -> None:
    """Copy reviewable template entries next to the generated models.json."""
    if not template_dir.is_dir():
        return
    for entry in _TEMPLATE_ENTRIES:
        source = template_dir / entry
        if not source.exists():
            continue
        target = target_dir / entry
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)


def _seed_web_search_config(config: HostConfig, directory: Path) -> Path | None:
    """Seed pi-web-access credentials into the runtime config directory.

    pi-web-access resolves its config directory from ``PI_CODING_AGENT_DIR``
    (verified in its ``utils.ts``), so redirecting that variable would silently
    hide the operator's own ``web-search.json`` and downgrade search to the
    keyless fallback. Two sources, in order:

    1. ``PI_GLINK_WEB_SEARCH_KEY`` — rendered as ``{"exaApiKey": …}``;
    2. the operator's own file (``~/.pi/agent/web-search.json`` or
       ``~/.pi/web-search.json``), copied verbatim.

    Having no credential at all is a supported state: pi-web-access still works
    through keyless Exa MCP search, and the run contract does not require a key.
    """
    target = directory / WEB_SEARCH_FILENAME
    if config.web_search_key:
        payload = {WEB_SEARCH_ENV_PROVIDER_KEY: config.web_search_key}
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        target.chmod(0o600)
        return target
    source = config.web_search_config_path
    if source is not None and source.is_file():
        shutil.copy2(source, target)
        target.chmod(0o600)
        return target
    return None
