# 权威事件样例库（ADR 0021）

本目录是产品事件流的**权威样例库**（golden fixtures）：每个事件形态至少一个样例，
每个样例是**语义手工可审的最小序列**（从真实或 fake 运行裁剪合成，注释头注明来源）。
样例是跨仓契约载体——**改样例 = 改契约**；演进护栏：新事件形态先入本库、再进实现。

## 约定

- 样例是持久化信封 `{type, v, runId, seq, ts, payload}` 的 JSONL，逐帧过
  `host/glink_harness/event_schema.py` 校验（词汇/版本/字段）。
- 以 `#` 开头的行是**注释头**（说明覆盖的语义点与来源），不是事件帧；所有消费方
  的加载器跳过注释行。
- `legacy-unversioned` 是唯一的负例样例：schema 引入之前的旧日志（无 `v`），
  消费方必须整日志拒收（没有迁移），拒收仍推进 `seq` 水位。
- `manifest.json` 是机器可读清单：每个样例的来源、语义点、事件形态，以及两侧
  fold 的期望输出（`web` 快照不变量 / `trajectory` 投影行与终帧展示快照）。
  两侧共享的核心不变量（`status`/`texts`/`toolCount`）必须一致。

## 消费方契约测试

| 消费方 fold | 契约测试 |
|---|---|
| glink web reducer（`web/modules/run-reducer.mjs` 块模型） | `tests/web/fixture-contract.test.mjs` |
| ULegal trajectory 语义（`tests/trajectory_reference.py` 参考 fold：业务里程碑 + 终帧展示快照；delta/直播帧不投影） | `tests/test_event_fixture_contract.py` |

## 形态 → 样例对照

| 事件形态 | 样例 |
|---|---|
| run_started | tool-snapshot、stages、follow-up-turns 等全部终结样例 |
| run_stage（agent_start/agent_end/compaction） | stages |
| run_stage（auto_retry，含 429） | rate-limit-retry、failed-running-tool |
| run_stage（plan，ACP） | acp-presentation |
| session_ready | tool-snapshot、follow-up-turns |
| session_ready（开放 payload 透传内核会话事实） | session-ready-passthrough |
| turn_started / turn_finished | 全部含回合的样例 |
| assistant_delta / thinking_delta | tool-snapshot、acp-presentation 等 |
| assistant_message（pi 族逐 message） | tool-snapshot、follow-up-turns |
| assistant_message（truncated 以权威截断帧为准 + 交付不完整标记） | truncated-message |
| assistant_message（stopReason/usage 可选字段） | assistant-message-meta |
| assistant_message（turnScoped 回合合并，ACP） | acp-turn-scoped-message |
| tool_started / tool_updated / tool_finished | tool-snapshot、failed-running-tool、redacted-terminal |
| tool presentation 提示（ACP） | acp-presentation |
| subagent_started / updated / finished + 终帧 children 摘要 | subagents、run-cancelled |
| subagent_finished（failed 形态） | subagent-failed |
| deliverables_declared + 终帧逐文件回收结果 | deliverables-declared |
| usage_reported | tool-snapshot、rate-limit-retry、acp-presentation（ACP 计账） |
| kernel_status | failed-running-tool |
| kernel_status（info 会话事实 / warning 权限应答可选字段） | kernel-status-forms |
| run_finished | tool-snapshot 等 |
| run_failed | failed-running-tool |
| run_cancelled | run-cancelled |
| 终帧 redacted 脱敏标记 | redacted-terminal |
| 终帧可选字段（modelStopReason/kernelUsage/artifactFailures/external） | terminal-optional-fields |
| 多轮追问链（续接身份 + 多轮独立正文） | follow-up-turns |
| 无 v 旧日志（负例） | legacy-unversioned |

## 新增事件形态的流程

1. 先在本目录加样例（`.jsonl` + 注释头）并在 `manifest.json` 登记期望输出；
2. 两侧契约测试同步更新到红（消费方 fold 尚未实现新形态）；
3. 实现进 `event_schema.py`（必要时 bump 该类型的 `v`）与各消费方 fold，测试转绿。
