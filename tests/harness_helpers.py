"""Compose a family's :class:`Launch` / :class:`Spawn` the way production does.

The launch description belongs to the harness family and the carrier to the
sandbox (``harness.driver(sandbox.spawn(harness.launch(spec)))``); tests that
check argv/env/mounts go through that same composition instead of a
test-only builder, so a drift between the two cannot hide behind a helper.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from glink_harness.config import HostConfig
from glink_harness.kernel.execution import ExecutionSpec
from glink_harness.kernel.harness import HarnessFamily, select_family
from glink_harness.kernel.sandbox import Launch, Sandbox, open_sandbox
from glink_harness.store import RunStore


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


#: A synthetic egress token for renders that never reach the proxy.
TEST_EGRESS_TOKEN = "pge_test-egress-token-0123456789abcdef"


def test_egress(
    *,
    origin: str = "http://127.0.0.1:9",
    token: str = TEST_EGRESS_TOKEN,
    search: bool = False,
):
    """The :class:`EgressEndpoint` a pi render gets (docs/adr/0008)."""
    from glink_harness.kernel.agent_env import EgressEndpoint

    return EgressEndpoint(
        model_base_url=f"{origin}/model",
        token=token,
        search_base_url=f"{origin}/search" if search else None,
    )


test_egress.__test__ = False  # not a pytest test despite the name
