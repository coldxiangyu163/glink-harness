# 0002 追问门禁与状态根按父记录自身事实判定

- 状态：已接受（2026-09-28，架构重设计 S1；替代此前的帧标记证明与执行面回退）
- 范围：`runs/runner.py::_resumable_parent`、`kernel/harness_acp.py::recorded_capabilities`、
  `AcpHarness.state_home`

## 背景

追问要求父 run 与本宿主同族同包，ACP 追问还要继承父 run 的 agent 状态根。此前族由 `session_ready`
载荷中的 `harness: "acp"` 标记或记录字段二者之一证明；状态根在记录缺 `executor` 时按宿主**当前**
执行面推导，宿主从 e2b 切到 docker 后，历史 e2b run 的合法追问会被指向不存在的 `acp-agent/home`。

## 决定

- 门禁顺序：owner（多租户）→ `parent.harness == 本族` → `parent.harness_pack == 本包` →
  终态/最新轮次等状态检查。族或包不同一律 `session_unavailable`（fail-closed），且先于任何会话读取。
- `recorded_capabilities` 只对 `harness == "acp"` 的父记录扫描 `session_ready` 帧取能力矩阵；帧内
  `harness` 标记继续写出，但不再作为证明。
- `state_home` 只看父记录自己的 `executor`：`e2b` → `acp-agent/recovered`，其余 → `acp-agent/home`。

## 后果

- 同一问题只有一条判定路径，不随宿主配置漂移。
- 依赖 0001：字段缺失的记录根本不进入门禁。
- 回归：`tests/test_kernel_harness_acp.py`、`tests/test_kernel_acp_resume.py`、
  `tests/test_runner.py`（跨族拒绝先于状态检查、owner 门禁先于一切事实读取）。
