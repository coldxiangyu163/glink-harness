# ACP 集成笔记

[多 harness 方案](multi-harness-plan.md) · [pi 接入边界](pi-boundary.md) · [架构](architecture.md)

本文记录 ACP（Agent Client Protocol）族接入的协议事实与实现口径。**每条标注来源**：

| 来源标记 | 含义 |
|---|---|
| `schema` | ACP v1 的 JSON Schema / 官方 schema 页（本机核对副本：`@agentclientprotocol/sdk` 1.4.0 的 `schema/schema.json`，`$defs` 中的 v1 定义） |
| `docs` | `agentclientprotocol.com/protocol/v1/*` 官方文档 |
| `dsh 源码` | 只读参考 `/Users/coldxiangyu/AI/git/deepseek-harness/packages/acp/acp`（M1 不接真实 dsh；不作为运行时事实） |
| `合成实测` | 本仓库 M1 回归（`tests/test_kernel_acp*.py` + `tests/fixtures/fake_acp.py`）。**只证明宿主侧客户端/投影/生命周期行为，不证明任何真实 agent 的实现** |
| `待 M2` | 需要真实 agent 与授权才能确认 |

状态（2026-09-24）：M1 合成验证完成（GLINK-ACP-01，commit `0f42f38..da75b45`）。真实
dsh / codex-acp / claude-code-acp 冒烟与真实模型调用属 M2，未执行。

## 1. 协议事实（v1）

### 1.1 传输与分帧

* ACP 是 **JSON-RPC 2.0**，stdio 传输：客户端启动 agent 子进程，消息走 agent 的
  stdin/stdout；一条消息一行，**LF 分隔且消息内不得含裸换行**，UTF-8 编码。agent 可以
  往 stderr 写日志，但 stdout 上的任何非 ACP 消息都是违规。（`docs`，transports）
* 正因如此，宿主复用 `host/pi_glink/kernel/framing.py` 的 LF-only 拆分：U+2028/U+2029
  是 JSON 字符串内容而非分帧边界，`\r\n` 写入者的尾部 `\r` 单独剥离。（`合成实测`：
  ACP 客户端跑共享语料 `tests/fixtures/lf-framing.json` 的每条记录，与 pi 客户端一致）
* 请求/响应按 `id` 关联，id 可以是数字或字符串；本客户端只发数字 id，回显 agent 的请求
  id 原样（三种 JSON-RPC id 类型都可能出现）。（`schema`）

### 1.2 初始化与能力协商

* `initialize` 请求：`protocolVersion`（必填）、`clientCapabilities`、`clientInfo`。
  协议版本是**单个整数（MAJOR）**，v1 即 `1`；版本协商规则是「agent 支持则回同一个，
  否则回自己最新的，客户端不支持就应关闭连接」。（`schema`/`docs`；SDK 1.4.0 的
  `PROTOCOL_VERSION = 1`）
* `initialize` 响应：`protocolVersion`（必填）、`agentCapabilities`、`agentInfo`、
  `authMethods`。省略的能力位按「不支持」处理（introspection 不成立）。（`docs`/`schema`）
* 宿主客户端声明：`fs.readTextFile=false`、`fs.writeTextFile=false`、`terminal=false`、
  `auth.terminal=false`，即**不提供任何客户端方法**：ACP agent 在自己的文件系统里干活
  （inproc 面就是沙箱/宿主工作区），与方案 §3 的拓扑一致。（`合成实测`：fake agent 回读
  到的 clientCapabilities 正是这四个值）
* 版本不一致时宿主在 `session/new` 之前失败收口（run `failed/execution_error`），不带着
  未知线格式继续。（`合成实测`）
* `initialize` 报错（如 `auth_required`）走同一条失败路径，不静默降级。（`合成实测`）
* 认证能力（`authMethods` 的 `terminal`/`env_var` 等）M1 不消费，只把前 20 条并入 run 记录；
  dsh 当前返回空数组。（`dsh 源码`/`待 M2`）

### 1.3 会话与 prompt

* `session/new` 请求：`cwd`（必填，绝对路径）、`mcpServers`（必填数组，可以为空）、可选
  `additionalDirectories`（能力位 `sessionCapabilities.additionalDirectories`）；响应
  `sessionId`（必填）、可选 `modes`/`configOptions`。宿主的 `cwd` 与 `mcpServers` 分别是
  本 run 工作区与空数组。（`schema`）
* `session/prompt` 请求：`sessionId`、`prompt`（`ContentBlock[]`）。基线保证 agent 支持
  text 与 resource_link；image/audio/embeddedContext 由 agent 的 `promptCapabilities`
  决定。M1 只发一个 text block（本 run 的 prompt 本身就是纯文本）。（`schema`/`docs`）
* `session/prompt` 响应：`stopReason`（必填）与一个 **UNSTABLE** 的可选 `usage`。M1 不投影
  该字段（用量只走 `usage_update`）。（`schema`）
* prompt 请求是**长请求**：turn 结束（或被取消）才回响应，期间的输出全走
  `session/update` 通知。宿主因此把 prompt 放在驱动线程里等，run 截止时间由 `wait()` 管。
  （`docs`/`合成实测`）

### 1.4 `session/update` 变体

v1 完整变体集（`docs`/`schema`）：`user_message_chunk`、`agent_message_chunk`、
`agent_thought_chunk`、`tool_call`、`tool_call_update`、`plan`、
`available_commands_update`、`current_mode_update`、`config_option_update`、
`session_info_update`、`usage_update`（另有 UNSTABLE 的 `plan_update`/`plan_removed`）。

* 内容块消息（`ContentChunk`）携带一个 `ContentBlock`，可带 `messageId` 标识同一消息；
  M1 只投影 text 块。（`schema`）
* `plan` 每次携带**完整条目列表**（客户端整体替换），条目含 `content`/`priority`/`status`。
  （`schema`）
* 未投影的变体（`user_message_chunk`、`current_mode_update`、`config_option_update`、
  `session_info_update` 等）在 M1 **不伪造产品事件**，按类型一次性写一条 `kernel_status`
  提示（`acp_update_not_projected`），避免静默丢事实。（`合成实测`）

### 1.5 工具调用

* `tool_call`：`toolCallId` 必填；`title`（人类可读）始终可用，`name` 是 **UNSTABLE** 的可选
  程序名，`kind` 是类别枚举（read/edit/delete/move/search/execute/think/fetch/switch_mode/other）；
  `status` ∈ pending/in_progress/completed/failed；`rawInput`/`rawOutput` 为任意 JSON。
  （`schema`）
* `tool_call_update`：除 `toolCallId` 外全部可选；`content` 是**整体替换**语义；状态推进到
  completed/failed 即终态。（`schema`）
* `ToolCallContent` 有三种：`content`（包 `ContentBlock`）、`diff`（`path`/`oldText`/`newText`）、
  `terminal`（`terminalId`，需要 `terminal/*` 能力——宿主不声明，出现只作占位呈现）。（`schema`）
* 产品工具卡片口径（`合成实测`）：`toolName` 取 `name → kind → title`（程序名优先）；
  `args` 走与 pi 相同的限幅投影（`projection._project_args`，同一份数组/对象契约）；
  `result` 取 `content` 文本（diff 渲染为 `diff <path>` + 新文本），为空时回退 `rawOutput`
  的紧凑 JSON，最终整体限幅；`tool_updated.output` 同样是「到目前为止的累计内容」。

### 1.6 用量

* `usage_update`：`used`（当前上下文中的 token）与 `size`（上下文窗口）必填且非负，
  `cost` 可选且必须同时含 `amount`（数值）与 `currency`（ISO 4217）；`cost` 是**会话累计**。
  （`schema`/`docs`）
* 这是「上下文窗口占用 + 累计成本」视角，**不是** pi 的逐请求 token 账本。宿主不做逐请求
  求和（那会伪造增长曲线），而是保留 ACP 口径并标记：

  ```json
  {"usage": {"totalTokens": <used>, "contextSize": <size>,
             "basis": "acp_usage_update",
             "cost": {"amount": …, "currency": …}}}
  ```

  终态记录的 `usage` 采用同一次转换的最后一个快照（替换语义，不是累加）。（`合成实测`）
* `used`/`size` 非法或 `cost` 形状不对时不猜数：写一条 `kernel_status`（`acp_usage_invalid` /
  `acp_usage_cost_invalid`）并跳过相应字段。（`合成实测`）
* `usage_update` 是可选能力，真实 agent 是否发送、粒度如何，待 M2 实测。（`待 M2`）

### 1.7 权限

* `session/request_permission` 是 **agent → client** 请求：`sessionId`、`toolCall`（至少含
  `toolCallId`）、`options[{optionId, name, kind}]`；`kind` ∈ allow_once/allow_always/
  reject_once/reject_always。响应 `outcome` 只有两种：`selected{optionId}` 或 `cancelled`。
  （`schema`）
* M1 无人值守，策略是 **fail-closed 全拒**：优先选 `reject_once`，其次 `reject_always`；
  没有可用的拒绝项（或缺 `optionId`）时回 `cancelled`；若 `session/cancel` 已经发出，按规范
  必须回 `cancelled`。（`合成实测`）
* 每次拒绝都写 `kernel_status`（`acp_permission_denied`，warning），带 `decision`、
  `toolCallId`、选中的 `optionId`/`optionKind`——拒绝不允许静默。（`合成实测`；方案 §4.2）
* dsh 侧的权限请求只给 `allow-once`/`reject-once` 两个一次性选项（`dsh 源码`，待实测确认）。

### 1.8 取消

* `session/cancel` 是**通知**（无响应）。agent 应尽快停止模型与工具调用，把待发的
  `session/update` 发完，并**必须**用 `stopReason=cancelled` 回应原来的 `session/prompt`。
  客户端在发出取消时应把未完成的工具标记为 cancelled，并**必须**用 `cancelled` 回应所有
  待答的权限请求。（`docs`/`schema`）
* 宿主实现：`cancel()` 幂等，只发一次通知；run 的结论由宿主的取消标志决定（adapter 的
  cancel 只缩短等待）。**超时触发的收尾取消不算用户取消**：结论保持 `failed/timeout`，
  不会因为 agent 回了 `cancelled` 而被改写成 `cancelled`。（`合成实测`）

## 2. 产品事件映射（实现口径）

| ACP | 产品事件 | 实现口径 |
|---|---|---|
| `initialize` 响应 | `session_ready` | payload 带 `harness: "acp"`、`protocolVersion`、`sessionId`、`agentInfo`、`agentCapabilities`、`authMethods`、`cwd`；`sessionFile` 为 null |
| `agent_message_chunk` | `assistant_delta` | 只投影 text 块；其他块型记一次性 `kernel_status`；文本走与 pi 相同的 `StreamRedactor` |
| `agent_thought_chunk` | `thinking_delta` | 同上 |
| `tool_call` | `tool_started`（若已含终态再补 `tool_finished`） | `toolName`/`args` 见 §1.5 |
| `tool_call_update`（pending/in_progress） | `tool_updated` | `output` = 累计内容 |
| `tool_call_update`（completed/failed） | `tool_finished` | `isError = status == "failed"`，`result` = 内容或 `rawOutput` |
| `plan` | `run_stage`（`stage: "plan"` + `entries`） | 条目限 20 条、每条 300 字符 |
| `usage_update` | `usage_reported` | 见 §1.6；口径标注 `basis` |
| `available_commands_update` | `kernel_status`（warning） | 记命令数与名称（限幅）；ACP 门禁一律拒绝 `/` 开头 prompt |
| `session/prompt` 的 `stopReason` | 终态判定输入 | `end_turn`→正常；`max_tokens`→`finished/length`（交付不完整）；`max_turn_requests`→`finished/max_turn_requests`；`refusal`→`failed/model_error`；`cancelled`→取消路径；未知/缺字段→`failed/execution_error` |
| `session/request_permission` | `kernel_status`（warning） | 不投影成交互，自动应答见 §1.7 |
| 其余变体 | 不投影 | 一次性 `kernel_status` 提示，词表不变（前端不感知 harness 差异） |

`modelStopReason` 对 ACP run 保留 ACP 的原词（`end_turn`/`max_tokens`/…），`stopReason`
仍是产品终态词表里的值。

## 3. M1 集成决定与已知偏差

1. **harness 选择只在配置层**：`PI_GLINK_HARNESS=pi|acp`（默认 `pi`）+
   `PI_GLINK_ACP_COMMAND`（ACP agent 命令行，`shlex.split` 后直接 exec，不经 shell）。
   没有 API/前端参数，`public_view()` 不含 harness，现有部署零变化。（与方案 §5/§6 一致）
2. **执行面**：M1 只接 `inproc`；`PI_GLINK_HARNESS=acp` 与 docker/e2b 组合在启动即拒绝
   （M3 做容器/沙箱 stdio 与状态目录回收）。
3. **续接未实现**：M1 不实现 `session/resume`（能力位 `sessionCapabilities.resume`）。
   ACP run 不落会话文件，追问在**准入层**以 `session_unavailable` 拒绝；适配器另外对
   任何 `resume_session` 输入直接报错，绝不以新会话冒充追问。（方案 §4.4 的「能力缺失
   显式拒绝」）
4. **状态目录**：每 run 渲染 `<run_dir>/acp-agent/home` 并作为子进程 `HOME`，agent 的
   `~/.claude`、`~/.codex`、dsh sessions 与 npm 状态都落在本 run 内；环境沿用 pi 面的最小
   透传清单，**不注入任何宿主凭据**（凭据注入点逐个实测属 M2，方案 §4.5）。
5. **未投影不等于丢失**：`max_turn_requests` 目前在记录里保留原词，前端尚无对应提示
   （只有 `length` 会显示「交付不完整」）；`current_mode_update`/`session_info_update`
   等只留一条 `kernel_status` 提示。
6. **`session/close` 未调用**：M1 用一个 run 一个进程，teardown 由进程退出完成；agent 是否
   需要显式关闭会话（`sessionCapabilities.close`）待 M2 实测后决定。

## 4. M1 合成验证覆盖

`tests/test_kernel_acp.py`（客户端/投影）与 `tests/test_kernel_acp_runner.py`（run 生命周期），
fixture 为 `tests/fixtures/fake_acp.py`（真实子进程、真实 stdio）：

* LF 分帧：共享语料 `lf-framing.json`（U+2028/U+2029 为内容）与 fake agent 的单次 CRLF 大块写；
* `initialize` 协商 → `session_ready`（能力矩阵入 run 记录）、版本不一致与初始化报错的失败收口；
* `session/new` → `session/prompt` → `session/update` 流式投影（§2 全表）与 fake agent 侧
  收到的 `cwd`/`mcpServers`/clientCapabilities 断言；
* `usage_update` 换算、`basis` 标注与替换（非累加）语义；
* `stopReason` 六个分支 + 缺字段 fail-closed；
* `session/cancel` 取消链路（含超时不改写为 cancelled）与授权取消；
* `session/request_permission` 全拒（含无拒绝项 → cancelled）并记 `kernel_status`；
* `/` 开头 prompt 准入拒绝（含前导空白、`/run --bg`、裸 `/`），普通题面照常；
* 流式与工具结果中的凭据脱敏、每 run 状态根隔离、宿主无模型 key 也能跑 ACP；
* `build_adapter` 的 harness 分派与 `ExecutionAdapter` 契约（pi 路径默认行为不变）。

## 5. 待 M2 实测项（未验证不能当成已成立）

| 待测事实 | 为什么重要 | 验证方式（M2） |
|---|---|---|
| dsh / codex-acp / claude-code-acp 的实际能力矩阵（loadSession、resume、usage_update、available_commands、promptCapabilities） | 决定哪些映射真有数据、哪些必须显式降级 | `initialize` 响应原文 + 各 agent 版本记录 |
| 各 agent 的凭据注入点（`ANTHROPIC_API_KEY` env、`OPENAI_API_KEY`/`auth.json`、dsh credentials） | 方案 §4.5；错了就会把 key 交给错误的进程或根本不生效 | 逐 agent 一次性真实 run + 环境/文件核对 |
| `HOME` 重定向是否被各 agent 尊重；是否需要 `CODEX_HOME`/`CLAUDE_CONFIG_DIR` 等专用变量 | 每 run 状态隔离与 M3 的状态目录回收都依赖它 | 真实 run 后检查状态文件落点 |
| `usage_update` 真实字段与频率；`cost.currency` 取值 | 前端口径与成本展示 | 真实 run 事件原文 |
| 权限请求真实选项种类与频次；白名单策略 | 现在全拒，真实研究 run 会被大量拒绝 | 真实 run 日志 + 逐 harness 策略记录 |
| `stopReason` 的真实取值分布（`max_turn_requests`/`refusal` 是否出现） | 终态提示是否需要前端补充 | 真实 run 记录 |
| `session/cancel` 的真实响应时延与终态 | 取消体验 | 真实 run + 计时 |
| docker/e2b 面的 stdio 桥接与帧完整性（M3 前置） | 传输面扩展 | 逐面 smoke |
| 各适配器版本钉版、来源、license、升级冒烟步骤 | 方案 §5/§7 纪律 | 集成笔记逐项补齐（本节升级为版本记录） |
