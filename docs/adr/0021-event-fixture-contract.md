# 0021 事件样例库与消费方契约测试

- 状态：已接受（用户批准；方向固定，本文只做精确表述）
- 范围：`docs/architecture.md` §3.7（新增小节）、`docs/exec-contract.md` §2.5（锚点一句）；
  后续实现落在 `tests/fixtures/events/` 样例库的覆盖盘点与各消费方的契约测试
- 关联：[0011](0011-versioned-event-schema.md)（事件 schema 单一来源与版本）、
  [exec-contract.md](../exec-contract.md)（HTTP 面服务契约，本 ADR 补充 fold 语义一致性）、
  [multi-harness-plan.md](../multi-harness-plan.md) §6（R3 权威持久化接管，原文存档的归属点）

## 背景

ULegal↔glink 对接后，同一条执行事件流被两侧各自解释：glink web 端是
`run-reducer.mjs` 的块模型 fold，ULegal 是 trajectory 行投影 fold。两套 fold 各自
手写，语义漂移没有自动化护栏，靠真人走查才发现——近期实例包括 ACP 族缺权威
`assistant_message` 帧、回合范围合并与 subagent 误标等多轮修复（B3 评审 BLOCKER
系列）。

参照 dsh（deepseek-harness 仓库）的事件溯源设计：`packages/core/session` 的会话事件
日志是单一事实源，`Session.append()` 为唯一写入口（「模型可见即已记录」不变量）；
展示态由投影 seam 承载——`docs/architecture.md`「Projection seam」中注册单元增量 fold
已提交事件，各消费方经 `stateOf()`/`snapshot()` 读取类型化状态；事件信封（
`docs/persistence-catalog.zh.md`）带可选 `ignorable` 演进护栏，快照类事件按
whole-value replace 约定折叠。dsh 的一致性来自「共享纯 fold + 稳定业务 id +
whole-value 约定 + ignorable 护栏」，但它运行在同一语言栈内，fold 代码可以直接共享。

## 决定

1. **一致性机制 = 权威事件样例库 + 消费方契约测试**。glink 维护权威事件样例库
   （golden fixtures，覆盖事件词汇全形态；现有 `tests/fixtures/events/*.jsonl` 为其
   雏形）；各消费方 fold（glink web reducer、ULegal trajectory 投影）各自实现契约
   测试：同一份样例喂两侧，fold 输出快照必须一致。跨语言（glink Python/JS、ULegal
   Go/TS）**不共享 fold 代码，只共享样例与期望输出**。
2. **ULegal trajectory 定位：业务里程碑 + 展示 fold**。trajectory 只落业务里程碑
   （started/phase/终态）与展示所需快照；delta/直播帧**不落盘、不投影**——该取舍
   （对接讨论中的 D5 项）确认为正式设计，不是权宜。**「事件原文存档」归 R3** 存储
   边界重定义时一并实施（multi-harness-plan §6 的权威持久化接管），本期不建新表、
   不预建导出接口。
3. **演进护栏**：新事件形态**先入样例库、再进实现**；消费方 fold 不过样例库契约
   测试即红。

## 理由

- **样例库放 glink**：事件词汇、payload 字段与版本 `v` 的单一来源本就在 glink
  （`host/glink_harness/event_schema.py`，0011）；样例库是 schema 的可执行语义，
  与来源同仓才能随 schema 同步演进。
- **不移植 fold 代码**：两侧语言栈不同（glink 前端 JS/宿主 Python，ULegal Go/TS），
  共享 fold 需要跨语言移植与双端维护，成本高于共享样例；样例 + 期望快照是语言
  中立的契约载体。
- **trajectory 不做完整事件镜像**：ULegal 的职责是业务视图（open-items 已约定 R2
  业务视图消费、权威状态 R3 才转移）；直播帧落盘会预建一个本期无消费者的第二
  事件存储，与「不预建无消费者的调度器或空接口」的约束冲突。
- **样例先于实现**：B3 系列的漂移都是「实现先行、两侧各自解释」造成的；样例库
  前置把分歧暴露在契约测试里，而不是真人走查里。

## 后果

- `tests/fixtures/events/*.jsonl` 升格为跨仓契约载体：改样例 = 改契约，两侧消费方
  的契约测试同步更新；新增事件形态没有样例不得进实现。
- 各消费方（当前：glink web reducer、ULegal trajectory 投影）各自维护 fold 的快照
  测试，快照差异即红，不允许放宽断言掩盖漂移。
- **「事件原文存档」明确归 R3**：本期 ULegal 不建事件原文表，glink 不为消费方
  预建导出接口；R3 重定义存储边界时一并决定原文存档的形态与归属。
- 本 ADR 不替代 [exec-contract.md](../exec-contract.md) 的 HTTP 面契约；契约测试
  补充的是 fold 语义层面的一致性，HTTP 面语义仍以 exec-contract 与其钉住测试为准。
- 本 ADR 只固化方向，**不宣称实现已完成**：样例库的全形态覆盖盘点与两侧契约
  测试的落地是后续任务。
