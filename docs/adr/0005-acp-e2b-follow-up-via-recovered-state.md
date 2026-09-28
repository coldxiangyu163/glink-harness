# 0005 ACP E2B 面追问经状态根回收后受理

- 状态：已接受（GLINK-ACP-13 / M8，替代 M7 的「e2b 面一律拒绝」；详见
  `docs/acp-integration-notes.md` M7、M8 与 §11）
- 范围：`kernel/harness_acp.py::check_continuation`、`state_home`、E2B 回收路径

## 背景

M7 实现 ACP 续接时，e2b 面的 agent 状态随沙箱销毁，因此 e2b 父 run 的追问一律拒绝。

## 决定

ACP e2b run 在销毁沙箱前把 agent 状态根有界镜像到宿主 `runs/<id>/acp-agent/recovered`（整份成功
才发布，越界/读失败拒绝）。e2b 父 run 的追问在 M7 条件（同族同包、已结束、最新轮次、有 sessionId、
声明续接能力）之上再要求「父状态根回收成功」，满足时把继承状态灌回新沙箱后走 `session/resume`；
否则按 `session_unavailable` 拒绝，不降格为新会话。
