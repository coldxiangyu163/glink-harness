"""Kernel adapters: harness-specific protocol clients plus event projection.

Each harness family owns exactly two jobs:

* run its kernel as a child process and speak the documented protocol —
  :mod:`pi_glink.kernel.rpc` for pi's JSONL RPC, :mod:`pi_glink.kernel.acp_rpc`
  for ACP's JSON-RPC over stdio;
* translate that protocol's frames into the product vocabulary
  (:mod:`pi_glink.kernel.projection`, :mod:`pi_glink.kernel.acp_projection`).

It deliberately owns no research logic, no prompts, and no file layout policy:
those belong to the per-harness configuration (the pi template in ``agent/``,
and the ACP agent's own configuration) and its official extension points.
"""

from __future__ import annotations

from .acp_e2b_env import E2BAcpSandboxClient, SANDBOX_ACP_HOME
from .acp_env import AcpRuntime, prepare_acp_runtime
from .acp_execution import AcpExecutionAdapter, AcpSandboxExecutionAdapter
from .acp_projection import ACP_STOP_REASONS, AcpProjector
from .acp_rpc import (
    AcpClosed,
    AcpError,
    AcpProtocolError,
    AcpRequestError,
    AcpRpcClient,
    AcpTimeout,
)
from .agent_env import (
    AgentRuntime,
    build_pi_argv,
    build_pi_env,
    prepare_agent_dir,
    template_manifest,
)
from .execution import (
    ArtifactRecovery,
    CleanupReport,
    ExecutionAdapter,
    ExecutionOutcome,
    ExecutionSpec,
    SessionInfo,
    build_adapter,
)
from .projection import extract_text, project_event, project_frames
from .runner import REPORT_FILENAME, RESEARCH_PROMPT_FILENAME, RunBusy, RunManager
from .rpc import (
    PiRpcClient,
    PiRpcClosed,
    PiRpcError,
    PiRpcTimeout,
    RpcResponse,
    wait_for_idle,
)

__all__ = [
    "ACP_STOP_REASONS",
    "AcpClosed",
    "AcpError",
    "AcpExecutionAdapter",
    "AcpProjector",
    "AcpProtocolError",
    "AcpRequestError",
    "AcpRpcClient",
    "AcpRuntime",
    "AcpSandboxExecutionAdapter",
    "AcpTimeout",
    "AgentRuntime",
    "ArtifactRecovery",
    "CleanupReport",
    "E2BAcpSandboxClient",
    "ExecutionAdapter",
    "ExecutionOutcome",
    "ExecutionSpec",
    "PiRpcClient",
    "PiRpcClosed",
    "PiRpcError",
    "PiRpcTimeout",
    "REPORT_FILENAME",
    "RESEARCH_PROMPT_FILENAME",
    "RpcResponse",
    "RunBusy",
    "RunManager",
    "SANDBOX_ACP_HOME",
    "SessionInfo",
    "build_adapter",
    "build_pi_argv",
    "build_pi_env",
    "extract_text",
    "prepare_acp_runtime",
    "prepare_agent_dir",
    "project_event",
    "project_frames",
    "template_manifest",
    "wait_for_idle",
]
