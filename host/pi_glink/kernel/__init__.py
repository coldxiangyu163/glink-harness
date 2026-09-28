"""Kernel adapters: harness-specific protocol clients plus event projection.

Each harness family owns exactly two jobs:

* run its kernel as a child process and speak the documented protocol —
  :mod:`pi_glink.kernel.rpc` for pi's JSONL RPC, :mod:`pi_glink.kernel.acp_rpc`
  for ACP's JSON-RPC over stdio;
* translate that protocol's frames into the product vocabulary
  (:mod:`pi_glink.kernel.projection`, :mod:`pi_glink.kernel.acp_projection`).

The per-harness *policy* of one run lifecycle (admission allowlists, follow-up
gates, runtime rendering, launch description and protocol driver,
stop-reason normalization and subagent enrichment) lives behind the family
interface in :mod:`pi_glink.kernel.harness`, with the two implementations in
:mod:`pi_glink.kernel.harness_pi` and :mod:`pi_glink.kernel.harness_acp`. The
execution face (in-process / Docker / E2B) is the orthogonal
:mod:`pi_glink.kernel.sandbox` dimension; a run's adapter is
``harness.driver(sandbox.spawn(harness.launch(spec)))``.

It deliberately owns no research logic, no prompts, no file layout policy and
no run orchestration: run admission, the execution skeleton, the finalize
commit and the external-resource ledger live in :mod:`pi_glink.runs`, above
this layer. The kernel package keeps harness-specific protocol clients plus
event projection, and must not import the orchestration layer or host
orchestration modules (store/tenancy/ownership/blueprints/web_*/app/cli;
``store`` is whitelisted for the three harness-family modules only —
``harness``/``harness_pi``/``harness_acp`` read ``RunRecord`` and resume seeds).
"""

from __future__ import annotations

from .acp_e2b_env import SANDBOX_ACP_HOME
from .acp_env import AcpRuntime, prepare_acp_runtime
from .acp_execution import AcpExecutionAdapter
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
    prepare_agent_dir,
    template_manifest,
)
from .execution import (
    ArtifactRecovery,
    CleanupReport,
    ExecutionAdapter,
    ExecutionOutcome,
    ExecutionSpec,
    PiExecutionAdapter,
    SessionInfo,
)
from .harness import HarnessFamily, PreparedRuntime, select_family
from .harness_acp import AcpHarnessFamily
from .harness_pi import PiHarnessFamily
from .sandbox import (
    DockerSandbox,
    E2BSandbox,
    InprocSandbox,
    Launch,
    Mount,
    Sandbox,
    Spawn,
    open_sandbox,
)
from .projection import extract_text, project_event, project_frames
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
    "AcpHarnessFamily",
    "AcpProjector",
    "AcpProtocolError",
    "AcpRequestError",
    "AcpRpcClient",
    "AcpRuntime",
    "AcpTimeout",
    "AgentRuntime",
    "ArtifactRecovery",
    "CleanupReport",
    "DockerSandbox",
    "E2BSandbox",
    "ExecutionAdapter",
    "ExecutionOutcome",
    "ExecutionSpec",
    "HarnessFamily",
    "InprocSandbox",
    "Launch",
    "Mount",
    "PiExecutionAdapter",
    "PiHarnessFamily",
    "PiRpcClient",
    "PiRpcClosed",
    "PiRpcError",
    "PiRpcTimeout",
    "PreparedRuntime",
    "RpcResponse",
    "SANDBOX_ACP_HOME",
    "Sandbox",
    "SessionInfo",
    "Spawn",
    "extract_text",
    "prepare_acp_runtime",
    "prepare_agent_dir",
    "project_event",
    "open_sandbox",
    "project_frames",
    "select_family",
    "template_manifest",
    "wait_for_idle",
]
