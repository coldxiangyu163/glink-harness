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
    └── settings.json, AGENTS.md, extensions/, …   copied from the template

The API key is never written to disk: ``models.json`` carries the literal
string ``$OPENAI_API_KEY`` and pi resolves it from the child process
environment, which the host supplies in memory.
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
#: Literal pi resolves from the child environment; never a real secret.
API_KEY_PLACEHOLDER = "$OPENAI_API_KEY"
#: OpenRouter-style thinking controls (``reasoning: {effort}``).
#:
#: Without this, pi sends no thinking parameter for a custom openai-completions
#: provider, the upstream omits reasoning entirely, and the workbench has
#: nothing to render for ``thinking_delta``. Verified against
#: openrouter.ai/api/v1: with this format the same model returns reasoning.
THINKING_FORMAT = "openrouter"
#: Template entries copied verbatim into the runtime directory.
_TEMPLATE_ENTRIES = ("settings.json", "AGENTS.md", "extensions", "skills", "prompts")


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
    """Child environment: config redirection plus the operator's credential.

    The credential is passed through the process environment only. Nothing is
    written to disk, and the value never appears in an event payload or a log
    line produced by this host.
    """
    env = dict(base_env) if base_env is not None else dict(os.environ)
    env["PI_CODING_AGENT_DIR"] = str(runtime.directory)
    env["PI_CODING_AGENT_SESSION_DIR"] = str(config.session_dir)
    env["PI_SKIP_VERSION_CHECK"] = "1"
    env["PI_TELEMETRY"] = "0"
    if config.openai_api_key:
        env["OPENAI_API_KEY"] = config.openai_api_key
    return env


def _models_document(config: HostConfig) -> dict[str, Any]:
    return {
        "providers": {
            PROVIDER_ID: {
                "baseUrl": config.openai_base_url,
                "api": "openai-completions",
                "apiKey": API_KEY_PLACEHOLDER,
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
