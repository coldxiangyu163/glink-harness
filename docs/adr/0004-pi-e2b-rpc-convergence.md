# 0004 pi 的 E2B 面收敛到 RPC

- 状态：已接受（GLINK-ARCH-3）
- 范围：`kernel/harness_pi.py`、`kernel/harness.py`、`kernel/execution.py`、`kernel/e2b_env.py`、
  `runs/runner.py`

## 背景

pi 的 E2B 面曾经用 `--mode json` 加 tee 的单向流：session id 只能在流中发现，追问身份只能在回合后由
驱动线程比对 `expected_session_id`（模型调用已经发生）；取消只能杀进程；断流后的状态靠猜。

## 决定

- 三个执行面统一 `--mode rpc`。E2B 面以 `--session /home/user/sessions/continued.jsonl` 原地打开续接
  会话，RPC `get_state` 在 `session_ready` 时即知会话身份，追问身份不符在**回合前、第一次模型调用前**
  拒 `session_unavailable`；`session_ready` 在回合前发布。
- 取消走协议级 `abort`，不以杀进程代替。
- E2B 命令流断流即失败，不推测终态。
- 旧 `--mode json` 适配器与 tee 路径整体删除。

## 后果

pi 族对三个执行面只有一种续接身份验证时点与一种取消语义。live 验收见 `docs/acceptance.md`。
