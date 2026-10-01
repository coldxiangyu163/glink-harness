#!/usr/bin/env python3
"""阶段 2（ADR 0020）真实 E2B 文件级冒烟：pause → resume → 代数 → 销毁。

不跑 agent、不打模型，只验证宿主热路径依赖的 SDK 事实链：

1. ``create``（公共 base 模板）→ 写代数标记文件；
2. ``pause(keep_memory=False)`` → 返回 True（刚暂停）且 ``get_info`` 的
   ``state`` 为 PAUSED；
3. ``Sandbox.connect(sandbox_id, on_resume="reboot")`` → 恢复为 RUNNING 且
   sandbox_id 不变；
4. 标记文件字节一致（文件系统跨 pause/resume 保留）；
5. ``pause`` 幂等（已暂停返回 False）与 killed 后 connect 报 not-found；
6. ``Sandbox.kill(sandbox_id)`` 销毁确认。

凭据从仓库 ``.env`` 的 ``E2B_API_KEY`` 读取，不打印、不落日志。网络或
控制面不可达时如实非零退出（不伪造证据）。

用法：``python scripts/smoke-e2b-lease-pause.py``（无参数）。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "host"))

MARKER = "/home/user/.glink-harness-state-generation"
CONVERSATION = "smoke-conv"
GENERATION = 7


def read_api_key() -> str:
    """Read E2B_API_KEY from the repo .env without printing it."""
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        raise SystemExit("[FAIL] 仓库缺少 .env，无法读取 E2B_API_KEY")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("E2B_API_KEY="):
            key = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            if not key:
                raise SystemExit("[FAIL] .env 的 E2B_API_KEY 为空")
            return key
    raise SystemExit("[FAIL] .env 未定义 E2B_API_KEY")


def step(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    suffix = f" — {detail}" if detail else ""
    print(f"[{status}] {time.strftime('%H:%M:%S')} {name}{suffix}")
    if not ok:
        raise SystemExit(f"[FAIL] 冒烟中止于：{name}{suffix}")


def state_text(info: Any) -> str:
    """The sandbox state as text: the enum's value, its name, or the raw str."""
    state = getattr(info, "state", None)
    value = getattr(state, "value", None)
    if isinstance(value, str) and value:
        return value
    name = getattr(state, "name", None)
    if isinstance(name, str) and name:
        return name
    return str(state or "")


def main() -> None:
    from e2b import Sandbox, SandboxNotFoundException

    api_key = read_api_key()
    print(f"# 冒烟开始 {time.strftime('%Y-%m-%d %H:%M:%S')}（key 已读取，不打印）")
    sandbox = Sandbox.create(api_key=api_key)
    sandbox_id = sandbox.sandbox_id
    step("create 沙箱", bool(sandbox_id), f"sandbox_id={sandbox_id}")

    marker = json.dumps(
        {"version": 1, "conversation": CONVERSATION, "generation": GENERATION},
        sort_keys=True,
    ).encode("utf-8")
    sandbox.files.write(MARKER, marker)
    read_marker = sandbox.files.read(MARKER)
    if isinstance(read_marker, str):
        read_marker = read_marker.encode("utf-8")
    step("写代数标记文件", read_marker == marker)

    paused = sandbox.pause(keep_memory=False)
    step("pause(keep_memory=False)", paused is True, f"return={paused!r}")
    info = sandbox.get_info()
    paused_state = state_text(info)
    step("暂停态 get_info", paused_state.lower().endswith("paused"),
         f"state={paused_state!r}")

    again = sandbox.pause(keep_memory=False)
    step("pause 幂等（已暂停返回 False）", again is False, f"return={again!r}")

    resumed = Sandbox.connect(sandbox_id, timeout=300, on_resume="reboot", api_key=api_key)
    step("connect(on_resume='reboot')", resumed.sandbox_id == sandbox_id,
         f"sandbox_id 不变: {resumed.sandbox_id == sandbox_id}")
    running_state = state_text(resumed.get_info())
    step("恢复后 get_info 为 RUNNING", running_state.lower().endswith("running"),
         f"state={running_state!r}")

    read_back = resumed.files.read(MARKER)
    if isinstance(read_back, bytes):
        pass
    else:
        read_back = str(read_back).encode("utf-8")
    document = json.loads(read_back.decode("utf-8"))
    step("标记文件跨 pause/resume 保留且代数一致",
         document.get("conversation") == CONVERSATION
         and document.get("generation") == GENERATION)

    killed = Sandbox.kill(sandbox_id, api_key=api_key)
    step("kill 销毁确认", killed is True, f"return={killed!r}")
    try:
        Sandbox.connect(sandbox_id, timeout=30, on_resume="reboot", api_key=api_key)
    except SandboxNotFoundException:
        step("销毁后 connect 报 not found", True)
    except Exception as exc:  # noqa: BLE001 - 如实报告不可达/其他错误
        step("销毁后 connect 报 not found", False, f"{type(exc).__name__}: {exc}")
    else:
        step("销毁后 connect 报 not found", False, "connect 意外成功")

    print(f"# 冒烟全部通过 {time.strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
