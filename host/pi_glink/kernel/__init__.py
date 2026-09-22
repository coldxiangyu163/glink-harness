"""pi kernel adapter: procotol client plus event projection.

The adapter owns exactly two jobs:

* run ``pi --mode rpc`` as a child process and speak its documented JSONL
  protocol (:mod:`pi_glink.kernel.rpc`);
* translate pi session events into the product vocabulary
  (:mod:`pi_glink.kernel.projection`).

It deliberately owns no research logic, no prompts, and no file layout policy:
those belong to the pi configuration directory (``agent/``) and its official
extension points.
"""

from __future__ import annotations

from .agent_env import (
    AgentRuntime,
    build_pi_argv,
    build_pi_env,
    prepare_agent_dir,
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
    "AgentRuntime",
    "PiRpcClient",
    "PiRpcClosed",
    "PiRpcError",
    "PiRpcTimeout",
    "REPORT_FILENAME",
    "RESEARCH_PROMPT_FILENAME",
    "RpcResponse",
    "RunBusy",
    "RunManager",
    "build_pi_argv",
    "build_pi_env",
    "extract_text",
    "prepare_agent_dir",
    "project_event",
    "project_frames",
    "wait_for_idle",
]
