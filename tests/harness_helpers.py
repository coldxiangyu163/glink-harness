"""Compose a family's :class:`Launch` / :class:`Spawn` the way production does.

The launch description belongs to the harness family and the carrier to the
sandbox (``harness.driver(sandbox.spawn(harness.launch(spec)))``); tests that
check argv/env/mounts go through that same composition instead of a
test-only builder, so a drift between the two cannot hide behind a helper.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pi_glink.config import HostConfig
from pi_glink.kernel.execution import ExecutionSpec
from pi_glink.kernel.harness import HarnessFamily, select_family
from pi_glink.kernel.sandbox import Launch, Sandbox, open_sandbox
from pi_glink.store import RunStore


def execution_spec(
    config: HostConfig,
    runtime: Any,
    *,
    workspace: Path,
    run_id: str = "run-1",
    prompt: str = "prompt",
    system_prompt: str = "",
    timeout_seconds: int = 60,
    extra_argv: tuple[str, ...] = (),
    resume_session: str | None = None,
    expected_session_id: str | None = None,
) -> ExecutionSpec:
    return ExecutionSpec(
        config=config,
        runtime=runtime,
        run_id=run_id,
        prompt=prompt,
        system_prompt=system_prompt,
        workspace=workspace,
        timeout_seconds=timeout_seconds,
        extra_argv=tuple(extra_argv),
        resume_session=resume_session,
        expected_session_id=expected_session_id,
    )


def family(name: str, config: HostConfig, store_root: Path) -> HarnessFamily:
    return select_family(name, config=config, store=RunStore(store_root))


def compose_launch(
    family_name: str,
    executor: str,
    spec: ExecutionSpec,
    *,
    store_root: Path,
    **sandbox_kwargs: Any,
) -> tuple[Launch, Sandbox]:
    """The family's launch as placed by the ``executor`` sandbox (not opened)."""
    sandbox = open_sandbox(
        executor,
        config=spec.config,
        run_id=spec.run_id,
        workspace=spec.workspace,
        **sandbox_kwargs,
    )
    launch = family(family_name, spec.config, store_root).launch(spec, sandbox)
    return launch, sandbox
