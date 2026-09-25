# ACP 集成笔记

[多 harness 方案](multi-harness-plan.md) · [pi 接入边界](pi-boundary.md) · [架构](architecture.md)

本文记录 ACP（Agent Client Protocol）族接入的协议事实与实现口径。**每条标注来源**：

| 来源标记 | 含义 |
|---|---|
| `schema` | ACP v1 的 JSON Schema / 官方 schema 页（本机核对副本：`@agentclientprotocol/sdk` 1.4.0 的 `schema/schema.json`，`$defs` 中的 v1 定义） |
| `docs` | `agentclientprotocol.com/protocol/v1/*` 官方文档 |
| `dsh 源码` | 只读参考 `/Users/coldxiangyu/AI/git/deepseek-harness/packages/acp/acp`（M1 不接真实 dsh；不作为运行时事实） |
| `合成实测` | 本仓库 M1/M2 回归（`tests/test_kernel_acp*.py` + `tests/fixtures/fake_acp.py`）。**只证明宿主侧客户端/投影/生命周期行为，不证明任何真实 agent 的实现** |
| `dsh 实测` | dsh 0.1.5-rc.3 的真实 initialize/session/new 与真实运行（证据 `docs/evidence/acp-m2-handshake.txt`、`acp-m2-smoke-dsh.txt`） |
| `codex-acp 实测` | codex-acp 0.16.0 的真实 initialize/session/new 与真实运行（证据 `docs/evidence/acp-m2-handshake.txt`、`acp-m2-smoke-codex-acp.txt`） |
| `npm` | npm registry 元数据（`npm view`） |
| `宿主代码` | 本仓库实现（`host/pi_glink/kernel/`、`config.py`） |
| `待实测` | 需要真实 agent/授权或其它执行面才能确认 |

状态（2026-09-25）：M1 合成验证完成（GLINK-ACP-01）；**M2 真实冒烟完成**（GLINK-ACP-02）：
`@deepseek-ai/dsh@0.1.5-rc.3` 与 `@zed-industries/codex-acp@0.16.0` 均完成真实
initialize + session/new（不消耗模型）并经宿主 HTTP API 各跑一次最小真实研究 run。
实测结论、能力矩阵、凭据注入点与权限决策见 §4；`claude-code-acp` 与 docker/e2b 面仍属后续
里程碑。

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
* 认证能力（`authMethods` 的 `terminal`/`env_var` 等）宿主不消费，只把前 20 条并入 run 记录。
  M2 实测：dsh 返回 `[]`（`--profile acp` 组合无需认证）；codex-acp 返回 `chatgpt` 与两个
  `env_var` 方法（`CODEX_API_KEY`/`OPENAI_API_KEY`），另声明可选 `auth.logout`；未注入凭据时
  `session/new` 以 `-32000 Authentication required` 拒绝，是凭据注入生效的直接观测点。
  （`dsh 实测`/`codex-acp 实测`；证据 §4.4）

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
* **回合边界**：ACP 没有「turn 开始/结束」更新，但产品的流式模型靠 `turn_finished` 关闭
  文本/思考块（关闭后才按 Markdown 渲染，思考行才从「正在思考」转为「思考过程」）。适配器
  因此在发出 `session/prompt` 前与回合结束时各发一个合成帧（`acp_turn_started` /
  `acp_turn_finished`），由投影层译为 `turn_started` / `turn_finished`；前端无需知道 harness
  差异。（`合成实测`；前端语义由 `tests/web/run-stream.test.mjs` 的既有断言钉住）

### 1.5 工具调用

* `tool_call`：`toolCallId` 必填；`title`（人类可读）始终可用，`name` 是 **UNSTABLE** 的可选
  程序名，`kind` 是类别枚举（read/edit/delete/move/search/execute/think/fetch/switch_mode/other）；
  `status` ∈ pending/in_progress/completed/failed；`rawInput`/`rawOutput` 为任意 JSON。
  （`schema`）
* `tool_call_update`：除 `toolCallId` 外全部可选；`content` 是**整体替换**语义；状态推进到
  completed/failed 即终态。（`schema`）
* `ToolCallContent` 有三种：`content`（包 `ContentBlock`）、`diff`（`path`/`oldText`/`newText`）、
  `terminal`（`terminalId`，需要 `terminal/*` 能力——宿主不声明，出现只作占位呈现）。（`schema`）
* 产品工具卡片口径（`合成实测`）：`toolName` 取 `name → title → kind`（程序名优先；`title` 是人类可读描述且总存在，dsh 把工具名放在这里而 `kind` 固定为 `other`；类别仅作最后回退），`tool_started` 额外带 `kind`；`args` 走与 pi 相同的限幅投影（`projection._project_args`，同一份数组/对象契约）；
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
* `usage_update` 是可选能力。M2 实测两 agent 都发送，频率与数值见 §4.5；宿主仍按替换语义
  记录终态快照。（`dsh 实测`/`codex-acp 实测`）

### 1.7 权限

* `session/request_permission` 是 **agent → client** 请求：`sessionId`、`toolCall`（至少含
  `toolCallId`）、`options[{optionId, name, kind}]`；`kind` ∈ allow_once/allow_always/
  reject_once/reject_always。响应 `outcome` 只有两种：`selected{optionId}` 或 `cancelled`。
  （`schema`）
* M1/M2 默认策略是 **fail-closed 全拒**：优先选 `reject_once`，其次 `reject_always`；
  没有可用的拒绝项（或缺 `optionId`）时回 `cancelled`；若 `session/cancel` 已经发出，按规范
  必须回 `cancelled`。（`合成实测`）
* M2 增加了一个显式配置门控的白名单：`PI_GLINK_ACP_PERMISSION_ALLOW`（option kind，
  可选 tool kind）。默认空集不改变 fail-closed 行为；只有配置了 `allow_once`/`allow_always`
  时才会选择匹配的 allow 选项，且当同时配置了 tool kind 时请求的 `toolCall.kind` 必须命中
  （没有 kind 的请求一律拒绝）。决定与策略都进事件流（`acp_permission_allowed` /
  `acp_permission_denied`）。（`宿主代码`/`合成实测`；M2 未启用，理由见 §4.6）
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
  不会因为 agent 回了 `cancelled` 而被改写成 `cancelled`。产品侧在终态为 `cancelled` 时
  把未完成的工具卡片标为已取消（前端 `run-stream.mjs` 的既有行为），适配器不为凑规范
  伪造额外的 `tool_call_update`。（`合成实测`）

## 2. 产品事件映射（实现口径）

| ACP | 产品事件 | 实现口径 |
|---|---|---|
| `initialize` 响应 | `session_ready` | payload 带 `harness: "acp"`、`protocolVersion`、`sessionId`、`agentInfo`、`agentCapabilities`、`authMethods`、`cwd`，以及 `session/new` 的 `modes` 与 `configOptions`（会话选项状态，含宿主应用 `PI_GLINK_ACP_SESSION_CONFIG` 后的值）；`sessionFile` 为 null |
| `agent_message_chunk` | `assistant_delta` | 只投影 text 块；其他块型记一次性 `kernel_status`；文本走与 pi 相同的 `StreamRedactor` |
| `agent_thought_chunk` | `thinking_delta` | 同上 |
| `tool_call` | `tool_started`（若已含终态再补 `tool_finished`） | `toolName`/`args` 见 §1.5 |
| `tool_call_update`（pending/in_progress） | `tool_updated` | `output` = 累计内容 |
| `tool_call_update`（completed/failed） | `tool_finished` | `isError = status == "failed"`，`result` = 内容或 `rawOutput` |
| `plan` | `run_stage`（`stage: "plan"` + `entries`） | 条目限 20 条、每条 300 字符 |
| `usage_update` | `usage_reported` | 见 §1.6；口径标注 `basis` |
| `available_commands_update` | `kernel_status`（warning） | 记命令数与名称（限幅）；ACP 门禁一律拒绝 `/` 开头 prompt |
| 适配器合成的回合边界（`session/prompt` 前后） | `turn_started` / `turn_finished` | ACP 没有 turn 更新；宿主补上这两个产品回合边界，让流式文本/思考块按产品既有语义关闭与渲染（见 §1.4） |
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
   透传清单。M2 补上显式凭据注入：`PI_GLINK_ACP_CREDENTIAL_FILES` 把 `source=dest` 的源
   文件**只读复制**进 run HOME（父目录 0700、文件 0600），`PI_GLINK_ACP_ENV` 注入显式 env
   （不允许覆盖 `HOME`）；注入值抽入该 run 的 redactor，源文件不被修改（sha256 前后一致）。
   逐 agent 实测落点见 §4.4。（`宿主代码`/`dsh 实测`/`codex-acp 实测`）
5. **未投影不等于丢失**：`max_turn_requests` 目前在记录里保留原词，前端尚无对应提示
   （只有 `length` 会显示「交付不完整」）；`current_mode_update`/`session_info_update`
   等只留一条 `kernel_status` 提示。
6. **`session/close` 未调用**：M1 用一个 run 一个进程，teardown 由进程退出完成；agent 是否
   需要显式关闭会话（`sessionCapabilities.close`）待 M2 实测后决定。

## 4. M2 实测（2026-09-25）

本节每条都标来源。`dsh 实测` / `codex-acp 实测` 的证据文件：
`docs/evidence/acp-m2-handshake.txt`（真实 initialize/session/new，未消耗模型）、
`docs/evidence/acp-m2-smoke-dsh.txt`、`docs/evidence/acp-m2-smoke-codex-acp.txt`（各一次
经宿主 HTTP API 的最小真实研究 run）。真实模型调用仅限这两次冒烟。

### 4.1 版本与入口

| agent | 钉版与入口 | 实测事实 |
|---|---|---|
| dsh | `@deepseek-ai/dsh@0.1.5-rc.3`（npm dist-tag `latest`），入口 `dsh --profile acp` | 0.1.5-rc.3 的 dsh CLI 没有 `acp` 子命令；brief 里的 “dsh acp” 是简写。长驻 stdio 服务器，`--help` 自述 “Serve automation clients over Agent Client Protocol stdio.”（`npm`/`dsh 实测`） |
| codex-acp | `@zed-industries/codex-acp@0.16.0`，入口 `codex-acp`（随平台可选包带二进制） | 无参数即 stdio ACP server；支持 `-c key=value` 覆盖 `~/.codex/config.toml`。npm 安装时告警：该包已被 `@agentclientprotocol/codex-acp` 取代，后继包迁移评估留给 M4（`npm`/`codex-acp 实测`） |

两者都只用钉版本地安装（`npm install --prefix`）或 npx 钉版调用，未全局安装。

### 4.2 能力矩阵（initialize / session/new 响应原文）

| 字段 | dsh 0.1.5-rc.3 | codex-acp 0.16.0 |
|---|---|---|
| `agentInfo` | `{"name": "deepseek-harness-acp", "version": "0.0.1"}` | `{"name": "codex-acp", "title": "Codex", "version": "0.16.0"}` |
| `protocolVersion` | `1` | `1` |
| `loadSession` | 未声明（不支持） | `true` |
| `sessionCapabilities` | `close` / `list` / `resume` | `list` / `resume` / `close` |
| `promptCapabilities` | image=false, audio=false, embeddedContext=false | image=true, audio=false, embeddedContext=true |
| `mcpCapabilities` | `{http: true}` | `{http: true, sse: false, acp: false}` |
| `authMethods` | `[]`（无需认证） | `chatgpt` + `codex-api-key`/`openai-api-key`（`env_var`，各带变量名） |
| `session/new` 返回 | `sessionId` + `configOptions`（`model`、`reasoning_effort`） | `sessionId` + `modes`（read-only/auto/full-access）+ `configOptions`（`mode`、`model`、`reasoning_effort`） |
| 附件能力位 | prompt 不接受图片 | image/embeddedContext 已声明（宿主只发 text block） |

补充实测事实：

* dsh 的 `model` 取值是 **JSON 编码字符串**（`["deepseek-official","deepseek-v4-flash"]`），且
  select 选项按 provider 分组（`options[].options[]`）；宿主的选择值校验需要展平分组。
  （`dsh 实测`/`宿主代码`）
* dsh 不返回 `modes`：ACP 的 modes 面不在它的 automation 契约里。（`dsh 实测`/`dsh 源码`）
* codex-acp 在 `session/new` 后立即发 `available_commands_update`（本次 6 条：review、
  review-branch、review-commit、init 等）。宿主对 ACP 族仍拒绝一切 `/` 开头 prompt——
  agent 声明命令不改变准入门禁。（`codex-acp 实测`；§2 词表不变）
* 未认证时 codex-acp 的 `session/new` 回 `-32000 Authentication required`；dsh 无此要求。
  （`codex-acp 实测`）
* 两侧都接受 `session/set_config_option`（`configId`+`value`）：codex-acp `mode=auto` 与
  dsh `reasoning_effort=low` 均生效，且 codex-acp 的 `modes.currentModeId` 随之更新。
  （`合成实测`/`dsh 实测`/`codex-acp 实测`）

### 4.3 system prompt 通道（决定与实测）

ACP v1 没有 system role：`session/new` 只有 `cwd`/`mcpServers`/`additionalDirectories`
与保留字段 `_meta`，而 schema 明确 “Implementations MUST NOT make assumptions about values
at these keys”；modes/configOptions 是审批与模型选择器，不是 prompt。

**决定（M2）**：工作约定作为 `session/prompt` 文本块的**前置围栏段**送达：
`compose_prompt()` 把 `spec.system_prompt` 包在 `<system-prompt>…</system-prompt>` 中，
后接 `--- 用户请求 ---` 与原始题面。理由：text block 是 ACP 基线能力（agent MUST
support），不依赖可选能力位、不依赖任何 agent 私有扩展；围栏让模型能区分工作约定与用户
请求。系统提示为空时保持 M1 的原样单文本块。（`宿主代码`/`合成实测`）

真实接收证据：两次真实 run 中 agent 都按工作约定把交付写成工作区 `report.md`、回复里只
给结论文本（codex-acp 出现对 `report.md` 的 `edit` 工具调用与短回复；dsh 出现 `write`/
`edit` 与 “报告写入 report.md” 回复），且报告合约（来源列表）生效。（证据时间线，
`dsh 实测`/`codex-acp 实测`）

未采用的通道（逐条实测）：

* `_meta` 注入 systemPrompt：schema 保留、无 agent 消费者；dsh 源码不读 `_meta`，codex-acp
  也未声明该约定。（`schema`/`dsh 源码`）
* agent 私有配置：codex 有 `-c developer_instructions=…`、dsh 有 `--patch` 改 system-prompt
  插件的路径，但都属于单 harness 的配置渲染器（方案 §4.5 已有预留），M2 选协议通用通道，
  不把产品契约挂在某个 agent 的私有面。（`npm`/`dsh 源码`）

### 4.4 凭据注入（逐 agent 实测落点）

| agent | 注入材料 | 每 run 落点 | 生效证据 |
|---|---|---|---|
| codex-acp | `~/.codex/auth.json`（ChatGPT 登录） | `HOME/.codex/auth.json`（0600） | 无该文件时 `session/new` 报 “Authentication required”；注入后握手成功并完成真实 run；`~/.codex` 状态（cache/config.toml/sqlite）均落在 run HOME，真实目录未被修改 |
| dsh | `~/.dsh/.credentials.yaml`（harness home 凭据） | `HOME/.dsh/.credentials.yaml`（0600） | harness home 默认 `$DSH_HOME` 或 `~/.dsh`（dsh docs）；实测 profiles/sessions/storages 都落在 run HOME/.dsh，真实 `~/.dsh` 未被修改 |

* 注入用 `PI_GLINK_ACP_CREDENTIAL_FILES`（`;` 分隔的 `source=dest`）：源文件只读，目标必须是
  run HOME 内相对路径；目标文件 0600、父目录 0700，已存在或为符号链接则失败（不覆盖、不
  跟随）。（`宿主代码`/`合成实测`）
* `PI_GLINK_ACP_ENV` 提供命令级 env（`NAME=value`），不允许覆盖 `HOME`（隔离边界）。（`宿主代码`）
* 注入值抽取（JSON 凭据键、YAML 标量行、provider shape）并入**该 run** 的 redactor：
  两个真实 run 的 events/report/API 下载对真实凭据值 0 命中；源文件 sha256 前后一致。
  （`合成实测`/`dsh 实测`/`codex-acp 实测`）
* 真实凭据 mtime/hash 在握手与冒烟前后均未变化；凭据值不落日志、不落 evidence、不进命令
  行或进程环境（文件形态注入）。（`握手中脚本输出`/`宿主代码`）

### 4.5 usage 口径（实测）

* 两 agent 都发送 `usage_update`，宿主按 M1 口径**替换**快照并标 `basis: "acp_usage_update"`；
  终态记录采用最后一个快照。（`dsh 实测`/`codex-acp 实测`）
* dsh 本次 run：`used` 7,962 → 27,328，`size` 1,000,000；每个模型 step 一条。
  codex-acp：`used` 45,799 → 45,116（上下文压缩/重算导致回落），`size` 258,400；每轮助手
  消息前后一条。（证据 events.jsonl）
* 两次 run 都未出现 `cost`（订阅制/会话计数不提供金额）；schema 允许省略，宿主不伪造。
  （`dsh 实测`/`codex-acp 实测`）
* `session/prompt` 响应里的 UNSTABLE `usage` 字段仍未观测到。

### 4.6 权限（决策：保持 fail-closed，不启用白名单）

* 两次真实 run 都在 fail-closed 默认下完成，`acp_permission_*` 事件 0 条：codex-acp 以
  `mode=auto`（workspace-write，会话配置显式指定）运行，编辑/命令/内置 web 搜索未触发审批；
  dsh 的默认组合是 `workspace-write + approval=ask`（dsh 源码），实测 web_search、web_fetch、
  write、bash、edit、read 也都未被要求审批。（`dsh 实测`/`codex-acp 实测`）
* **决定**：默认继续全拒；不启用任何白名单。`PI_GLINK_ACP_PERMISSION_ALLOW` 作为配置级
  门控已实现并有合成回归（默认空集 = 原行为），留给将来某个 agent 在真实 run 证明全拒不可
  用时按最小集合启用，并须回写本节。
* 依据：dsh 的权限请求只给 `allow-once`/`reject-once` 且 `toolCall` 不含 `kind`（`dsh 源码`/
  `合成实测`），tool-kind 白名单对 dsh 不可用；codex-acp 的 `mode` 会话选项是比逐次放行更
  窄、更可审计的政策面。（`宿主代码`）

### 4.7 会话配置与模式

* `PI_GLINK_ACP_SESSION_CONFIG`（`id=value`）在 `session/new` 后应用；agent 未声明该选项或
  取值不在其声明集合内则 run 失败闭环，不静默忽略。（`宿主代码`/`合成实测`）
* 真实应用记录：codex-acp `mode=auto`（`modes.currentModeId` 同步为 auto），dsh
  `reasoning_effort=low`（握手时验证）。冒烟 run 使用 codex-acp `mode=auto`、dsh 默认。
  （`dsh 实测`/`codex-acp 实测`）
* 客户端能力声明仍是全空（无 `fs/*`、无 `terminal/*`、无 `auth.terminal`）：agent 在自己的
  文件系统上工作（`合成实测`；§1.2）。

### 4.8 两个真实 run 的其余事实

* 终态：均为 `finished`，`modelStopReason=end_turn`，`cleanup.released=true`；
  `reportStatus=ok`，`report.md` 经 `/api/runs/<id>/report` 200 回收，工作区/
  artifacts/API 下载三者内容一致。（`dsh 实测`/`codex-acp 实测`）
* 流式：run 期间 `assistant_delta`/`thinking_delta`/`tool_*` 按 seq/ts 逐条到达（codex-acp 73
  条事件/54s；dsh 60 条/43s），结束时 `turn_finished` 先于 `run_finished`。（证据 events.jsonl）
* 宿主清理：宿主 PID 由 `$!` 记录，运行结束后只对已知 PID 发 `kill -TERM`（未用 `pkill -f` 等
  宽匹配）。（证据 summary.txt）

## 5. M1 合成验证覆盖

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

## 6. 剩余待实测项（§4 已覆盖的不再列）

| 待测事实 | 为什么重要 | 验证方式 |
|---|---|---|
| `session/resume` / `session/load` 真实续接（含 dsh 的持久会话语义） | M1 起追问仍显式拒绝；M2 只确认能力位存在 | 授权后的真实续接 run + 终态/状态目录核对 |
| `session/cancel` 真实响应时延与终态（含工具收敛） | 取消体验；M2 两次 run 未触发取消 | 真实 run + 计时 |
| claude-code-acp 能力矩阵与凭据（`ANTHROPIC_API_KEY` env） | M4 接入 | 同 M2 握手 + 冒烟 |
| docker/e2b 面的 stdio 桥接与帧完整性（M3 前置） | 传输面扩展 | 逐面 smoke |
| 各适配器版本升级冒烟步骤与 license 记录 | 方案 §5/§7 纪律 | 集成笔记逐项补齐 |
| codex-acp 后继包（`@agentclientprotocol/codex-acp`）迁移评估 | 0.16.0 已被标记 deprecated | 版本对比握手 + 冒烟 |
