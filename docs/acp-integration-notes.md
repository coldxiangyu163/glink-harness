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
| `docker 实测` | M3a 的真实容器证据（`docs/evidence/acp-m3-docker-*.txt`）：镜像构建与容器内自检、容器内 ACP 握手、经宿主 HTTP API 的一次最小真实 run；M4 重建镜像后的三个 agent 容器内握手见 `docs/evidence/acp-m4-claude-docker-handshake.txt` |
| `e2b 帧实测` | M3b 帧完整性探针的真实沙箱输出（`docs/evidence/acp-m3-e2b-framing.txt`，base 模板，e2b SDK 2.51.0）：保序、字节一致、LF 分帧、大帧与 `close_stdin` |
| `e2b 模板实测` | M3b 的 dev 模板 CLI 构建与真实沙箱自检（`docs/evidence/acp-m3-e2b-template.txt`）：`glink-acp-worker`、构建日志、两次失败与修复 |
| `e2b 握手实测` | M3b 用生产装配路径在两个 agent 上的真实沙箱 `initialize`/`session/new`（`docs/evidence/acp-m3-e2b-agent-check.txt`）；M4 的同路径 claude-code-acp 沙箱握手见 `docs/evidence/acp-m4-claude-e2b-handshake.txt` |
| `e2b 实测` | M3b 经宿主 HTTP API 的一次最小真实 run（`docs/evidence/acp-m3-e2b-smoke.txt`）：SSE 流式、终态、report 回收、沙箱按 id 终止 |
| `claude 实测` | M4 的真实 claude-code-acp 事实（`docs/evidence/acp-m4-claude-handshake.txt`、`acp-m4-claude-smoke.txt`）：钉版入口、能力矩阵、每 run HOME/凭据落点、源码级权限 kind 观测、一次真实模型的 inproc 最小 run |
| `opencode 实测` | M5 的真实 opencode 事实（`docs/evidence/acp-m5-opencode-handshake.txt`、`acp-m5-opencode-smoke.txt`、`acp-m5-opencode-docker-handshake.txt`、`acp-m5-opencode-e2b-handshake.txt`）：钉版入口、能力矩阵、每 run HOME/凭据落点（inproc/docker/e2b）、一次真实模型的 inproc 最小 run 与 0 条权限请求 |
| `opencode 源码` | 只读参考 opencode 1.18.32 的上游源码（`anomalyco/opencode` v1.18.32 的 `packages/opencode/src/acp/{permission,event,tool}.ts` 等）；不作为运行时事实，用于解释真实观测 |
| `镜像自检` | `pi-glink-acp-worker` 镜像内各组件的版本输出（构建期日志与本机 `docker run --rm` 复验，M4 起含 claude 双包；证据同 `docker 实测` 与 `docs/evidence/acp-m4-claude-docker-handshake.txt`） |
| `npm` | npm registry 元数据（`npm view`） |
| `宿主代码` | 本仓库实现（`host/pi_glink/kernel/`、`config.py`） |
| `待实测` | 需要真实 agent/授权或其它执行面才能确认 |

状态（2026-09-25）：M1 合成验证完成（GLINK-ACP-01）；**M2 真实冒烟完成**（GLINK-ACP-02）：
`@deepseek-ai/dsh@0.1.5-rc.3` 与 `@zed-industries/codex-acp@0.16.0` 均完成真实
initialize + session/new（不消耗模型）并经宿主 HTTP API 各跑一次最小真实研究 run。
**M3a 完成（GLINK-ACP-03）**：ACP 族的 Docker 执行面可用（容器 stdio 桥接同一 M1 客户端、
每 run HOME 与只读凭据挂载、资源限制与 pi 面对齐），镜像与实测见 §5。
**M3b 完成（GLINK-ACP-04）**：ACP 族的 E2B 执行面可用——先在真实沙箱完成双向 stdio
帧完整性实测（§6.1），再用独立 dev 模板 `glink-acp-worker` 跑通握手（§6.4）与一次最小
真实 run（§6.5）；沙箱后台命令的 stdio 交给同一 M1 客户端，每 run HOME/凭据、产物回收与
`sandbox_id` 对账沿用 pi 面约定。
**M4 完成（GLINK-ACP-05，2026-09-25）**：claude-code-acp（`@zed-industries/claude-code-acp`
0.16.2）接入：inproc 握手与一次最小真实 run 完成；每 run 凭据形态、权限白名单、镜像与 dev
模板重建（含 claude 双包钉版）均已实测，见 §7。ACP 续接仍属后续里程碑。
**M5 完成（GLINK-ACP-06，2026-09-25）**：opencode（`opencode-ai` 1.18.32，入口
`opencode acp`）接入：inproc 握手、每 run `auth.json` 注入（OAuth access/refresh 并入
redactor）、状态目录落点、权限形态与一次最小真实 run 均已实测；镜像与 dev 模板加
opencode 钉版层并完成容器/沙箱握手，见 §8。

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
* `clientInfo` 是 `Implementation`，`name` 与 `version` 都是必填。M4 实测：claude-code-acp
  0.16.2 严格校验 initialize 参数，缺 `version` 直接回 `-32602 Invalid params`（连 session
  都不会建立）；宿主因此补上自身版本，合成回归钉住该字段。（`schema`/`claude 实测`）

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
* M4 补上 tool kind 解析：`session/request_permission` 的 `toolCall.kind` 是**可选**字段，
  claude-code-acp 0.16.2 的请求就不带它（实测）。宿主按同一 `toolCallId` 记住 agent 已经
  在 `tool_call`/`tool_call_update` 通知里公布的 kind，用它给请求分类；两处都没有 kind 时
  仍视为未分类，配了 tool kind 的白名单继续拒绝。事件流里 `toolKindSource` 标明 kind 来自
  `request` 还是 `tool_call`。（`claude 实测`/`合成实测`）
* dsh 侧的权限请求只给 `allow-once`/`reject-once` 两个一次性选项（`dsh 源码`，待实测确认）。
* opencode 的请求选项固定为 `once`/`always`/`reject`，且 `toolCall` 自带 `kind`
  （`opencode 源码`）；M5 的真实 run 未触发请求，原因与触发面记在 §8.4。

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
2. **执行面**：M1 只接 `inproc`；M3a 起 `PI_GLINK_HARNESS=acp` 同时支持
   `LOCAL_WEB_EXECUTOR=inproc|docker`，M3b 起再加上 `e2b`（§6；沙箱后台命令的
   `stdin=True` + `send_stdin` 承载同一份 stdio）。三个执行面共用同一个 ACP 适配器与
   `AcpRpcClient`，不为 e2b 另写协议代码。
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
* 本机 dsh 的真实状态目录是 `~/.dsh`（`$DSH_HOME` 未设置时的默认值），**`~/.config/dsh`
  不存在**（`ls` 实测）；任务描述里的 `~/.config/dsh` 在本机不是 dsh 的读写路径，两种路径
  都未被本实现或冒烟修改。（`dsh 实测`）
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
  用时按最小集合启用，并须回写本节。**M4 满足了启用条件**：claude-code-acp 的安全写入
  （Write/Edit）与 WebFetch 会逐次询问，研究 run 无法在 fail-closed 下产出 report.md；
  M4 定义的最小集合与真实 run 证据见 §7.4（dsh/codex-acp 的默认策略不变，仍不需要白名单）。
  **M5 反向确认无需扩展**：opencode 的真实 run 在 `webfetch`/`apply_patch` 上 0 条权限请求
  （白名单参数未被引用），继续沿用 fail-closed 默认，见 §8.4。
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

## 5. M3a 实测（2026-09-25，Docker 执行面）

M3a 把 ACP 族从 inproc 扩展到 Docker 执行面。协议侧零新增：容器只是另一个 stdio 承载，
ACP 客户端、投影、生命周期判定继续复用 M1/M2 的同一份实现。

### 5.1 镜像与钉版

* 新增 `docker/Dockerfile.acp-worker`，`FROM node:24.21.0-bookworm-slim`——与 pi worker
  同一 Node 精确 tag（`宿主代码`/`镜像自检`）。镜像自包含：构建期装好 Node 与两个 agent，
  容器内不安装任何运行时依赖。
* 镜像钉版（可机检：`tests/test_kernel_acp_docker.py` 强制 Dockerfile ARG 等于本节记录的
  实测版本）：`@deepseek-ai/dsh@0.1.5-rc.3`、
  `@agentclientprotocol/codex-acp@1.13.1`。
* dsh 保持 M2 实测版本（`dsh 实测`/`npm`）。codex-acp 改钉**后继包**
  `@agentclientprotocol/codex-acp`：旧包 `@zed-industries/codex-acp`（0.16.0，M2 实测）
  已被 npm 标记 deprecated（“replaced by @agentclientprotocol/codex-acp”，`npm`），
  后继包 1.13.1 用仓库 M1 客户端实测 `initialize` 协商回 `protocolVersion=1`
  并声明 `sessionCapabilities`（`docker 实测`）；本次只验证握手，真实模型 run 仍列在 §10。
* 镜像默认命令即自检：`dsh --version && codex-acp --version`；构建期还用 ARG 精确比对
  两个 `--version` 输出（不一致即构建失败），因此“镜像里装的是声明版本”是可复核事实
  （`镜像自检`/`宿主代码`）。实测：`pi-glink-acp-worker:latest`（sha256:4268dca9d9ed…，
  linux/arm64，1.37 GB）默认 CMD 输出 `0.1.5-rc.3` 与
  `@agentclientprotocol/codex-acp 1.13.1`，镜像内 `node -v` = v24.21.0（`docker 实测`）。
  构建日志出现 npm 11 的 install-scripts 警告（dsh-subprocess-local/koffi/node-pty 等
  未执行）；本次 ACP stdio 链路与真实 run 不依赖它们，但若未来某个 agent 需要原生脚本，
  必须在 ARG 层显式 `--allow-scripts` 并重建（`docker 实测`/`npm`）。M4 新增的 claude CLI
  就属于这种情况：npm 11 默认没跑它的 postinstall，构建里显式执行了包内的 `install.cjs`
  放置原生二进制（§7.5），并保留“四个组件版本精确比对”的自检。
* 命名与冻结：`LOCAL_WEB_ACP_WORKER_IMAGE`（默认 `pi-glink-acp-worker:latest`）与 pi 面的
  `LOCAL_WEB_WORKER_IMAGE`分开；值在配置期冻结，运行期不再读环境（`宿主代码`/`合成实测`）。
  升级路径：改 ARG → 重建 → 边界测试（ARG 必须重新等于 §5.1 记录值）→ 重建后的容器内
  握手/最小 run。M4 把默认 CMD 从「两个 `--version` 的 `sh -c` 一行」换成镜像内自检脚本
  `acp-self-check`（与 e2b 模板同形），覆盖四个组件；边界测试同步更新，见 §7.5。

### 5.2 传输、挂载与容器身份

* `docker run --rm -i`：容器 stdio 就是 ACP 传输（`-i` 保持 stdin，无 TTY——TTY 会破坏
  LF 分帧）。宿主把 `docker run` 的 argv 交给 M1 的 `AcpRpcClient`，不新增第二套协议代码
  （`宿主代码`/`合成实测`/`docker 实测`）。
* 工作区：宿主 run 工作区以读写挂载到 `/workspace` 并作为容器工作目录；`session/new` 的
  `cwd` 是容器路径 `/workspace`（宿主路径在容器内不存在），run 记录的 `session_ready.cwd`
  也是该容器路径（`宿主代码`/`合成实测`）。产物因此直接落在宿主工作区，不需要回收通道。
* 每 run HOME：`<run>/acp-agent/home` 挂载到 `/home/acp` 并 `-e HOME=/home/acp`；
  `~/.codex`、`~/.dsh`、cache 都落在本 run 目录内，随 run 销毁，不碰操作员目录
  （`宿主代码`/`合成实测`/`docker 实测`）。
* 凭据形态：沿用 M2 的“只读复制进 run HOME”，再在容器内对每个凭据文件做**嵌套只读挂载**
  （`-v <run-home>/<dest>:/home/acp/<dest>:ro`）。HOME 本身保持可写（agent 必须能写会话与
  缓存），但凭据文件在容器内不可改写/删除（`宿主代码`/`合成实测`/`docker 实测`）。
* `PI_GLINK_ACP_ENV`：按**名字**传（`-e NAME`），值放在 docker CLI 自己的环境里，不进 argv
  （进程列表）；这是与文件注入并列的显式进程环境通道（`宿主代码`/`合成实测`）。
* 容器身份与对账：容器名、停止、对账完全复用 pi 面的 `container_name_for` / `stop_argv` /
  `confirm_absent` 约定；名字在 `docker run` 执行前持久化到 run 记录，在线 ledger 只按
  这个持久化身份探测（`宿主代码`/`合成实测`）。

### 5.3 资源限制与降权

* 与 pi 面共用同一份 `--cpus` / `--memory` / `--pids-limit` / `--cap-drop ALL` /
  `--security-opt no-new-privileges`（`_isolation_argv`），合成测试逐项比对两面 argv 相等
  （`宿主代码`/`合成实测`）。这些都是 `docker run` 实例参数，镜像本身不被改写。
* 容器内进程仍是 root（与 pi 面一致）：降权靠 cap-drop + no-new-privileges；没有
  `--user`。限制是**契约**而非实测 enforcement——默认测试集不连 Docker daemon，宿主不会
  声称 daemon 实际执行了哪个值。

### 5.4 实测结论（2026-09-25）

证据文件：`docs/evidence/acp-m3-docker-build.txt`（镜像）、`acp-m3-docker-agent-check.txt`
（容器内握手与挂载契约）、`acp-m3-docker-smoke.txt`（最小真实 run）。本节每条都标来源。

* **容器内握手**：用生产路径（`prepare_acp_runtime` + `build_acp_plan` + `AcpRpcClient`）
  在容器里跑 initialize/session/new（不发 prompt，不耗模型）：codex-acp 1.13.1 与
  dsh 0.1.5-rc.3 都协商回 `protocolVersion=1` 并拿到 `sessionId` 与
  configOptions（codex-acp：mode/collaboration_mode/model/reasoning_effort/fast-mode；
  dsh：model/reasoning_effort）。无凭据时 codex-acp 会报
  `-32000 Authentication required`，挂载生效后握手成功——与 M2 的凭据注入观测点一致
  （`docker 实测`）。
* **挂载契约**：同一 plan 的挂载下，容器内写 `/home/acp/.codex/auth.json` 得
  “Read-only file system”（tamper_exit=2），写 HOME 其它位置成功（home_write_exit=0），
  宿主上的凭据副本 sha256 不变；client.close 后按名 force-remove 报 “No such container”，
  `docker ps -a` 无同名残留（`docker 实测`）。
* **一次最小真实 run（dsh）**：宿主 HTTP API 创建 run（临时数据根、随机端口、300s 有界
  超时），终态 `finished`、`modelStopReason=end_turn`、`reportStatus=ok`，
  `/api/runs/<id>/report` 200（1025 bytes），`turn_finished` 先于 `run_finished`；
  28 条事件含 thinking/tool（web_search/web_fetch/write）/usage（替换口径，
  `basis=acp_usage_update`）与 session_ready（`cwd=/workspace`）；清理
  `released=true`、容器名持久化且 `docker ps -a` 无残留；宿主只按 `$!` 记录的 PID 发
  `kill -TERM`（退出状态 143），未用 `pkill -f`、未做名字通配删除（`docker 实测`）。
* **冒烟次数说明**：授权为“一次最小真实 run”；第一次题面是对话式短问，模型未写
  report.md（`stopReason=no_report`，链路正常），于是用显式要求写 report.md 的短题补跑
  一次，两次都记录在证据文件里（`docker 实测`）。
* **环境约束（Colima/本机）**：Colima 只把 home 树共享给 VM；数据根/工作区放在 `/tmp`
  时，绑定的文件与目录会被静默创建成空目录（首次探针即复现：容器内凭据变成目录，
  `session/new` 因缺少凭据失败）。本机验证因此全部放在 `$HOME` 下的临时目录；该约束对
  pi docker 面同样适用（它一直靠默认数据根 `~/.pi-glink` 在 home 下而没暴露）。
  约束本身不是代码行为，但会把“静默空挂载”变成运行期失败，因此写在这里（`docker 实测`）。

### 5.5 与 M2 的差异与风险

* **codex-acp 包替换**：0.16.0（旧包）→ 1.13.1（后继包）。M3a 只验证了容器内
  initialize/session/new；M3b 在 e2b 沙箱内也只验证握手；后继包的真实模型 run 仍待授权（§10）。
* **只读范围**：只读的是凭据文件（嵌套 `:ro` 挂载），HOME 本身可写——agent 必须能写
  sessions/cache；不是整个 HOME 只读（`宿主代码`/`docker 实测`）。
* **容器 root 与宿主挂载**：容器内是 root，但 macOS/Colima 下 bind mount 的属主仍是宿主
  用户；Linux 宿主上 root 写 bind mount 的 uid 归属与文件属主变化**待实测**（§10）。
* **共享路径约束**：Colima 只共享 home 树，数据根必须在共享路径内（见 §5.4）；
  不同 Docker 后端（Docker Desktop/Linux 原生）的共享范围与“非共享路径静默空挂载”行为
  待逐环境实测。
## 6. M3b 实测（2026-09-25，E2B 执行面）

M3b 把 ACP 族从 inproc/Docker 扩到 E2B 云沙箱。协议侧零新增：沙箱后台命令的 stdio 交给
同一个 M1 `AcpRpcClient`，投影、权限应答、回合边界与终态判定继续是同一份实现。本节每条
都标来源；证据文件：`docs/evidence/acp-m3-e2b-framing.txt`（帧完整性）、
`acp-m3-e2b-template.txt`（模板）、`acp-m3-e2b-agent-check.txt`（沙箱内握手）、
`acp-m3-e2b-smoke.txt`（一次真实 run）。

### 6.1 帧完整性（先行实测，结论正面）

E2B SDK 不给双向管道，只有 `commands.run(background=True, stdin=True)` 的
`CommandHandle`：宿主用 `send_stdin` 写、迭代 handle 读 stdout 分块。M3b 的第一步就是用
仓库 LF-only 分帧（`kernel/framing.py`）验证这个通道能不能承载 ACP 的 JSON-RPC；探针
（`scripts/spikes/acp_e2b/frame_integrity_probe.py` + 沙箱端回显 agent）在真实
`base` 沙箱测了 20 项，全部通过（`e2b 帧实测`）：

* **保序**：64 条逐条写入与 128 条突发写入（不等回包）都按调用顺序得到回包，agent 侧
  记录序号 `n` 连续、`id`/内容/原始字节哈希逐条一致。
* **字节一致**：每条记录的 `raw_sha256`（不含结尾 LF）与宿主发送的字节相等；`send_stdin`
  接受 `str` 与 `bytes`，没有截断或改写。
* **LF 分帧**：半帧 + 半帧（中间有 0.3s 间隔）合成为一条记录；一次写入两帧被拆成两条；
  agent 单次 `os.write` 连发三帧也被拆成三条；CRLF 写入者的尾部 CR 单独剥离；共享语料
  `tests/fixtures/lf-framing.json`（U+2028/U+2029 为内容）双向逐帧一致。
* **大帧**：64 KiB / 256 KiB / 1 MiB 单条记录双向通过（host→agent 记录 65598 / 262207 /
  1048640 字节，回包哈希与正文长度全等；agent→host 记录 65835 / 262444 / 1048877 字节）。
* **EOF**：`close_stdin()` 可用，agent 读到 EOF 后正常退出（exit 0）。
* **分块不按帧对齐**：两百多条小帧的测试里 stdout 共 293 个 chunk，max_chunk 32768，
  有 84 个 chunk 停在帧中间、2 个 chunk 含多帧——所以承载方**必须**保留 LF 分帧器，
  不能假设一块等于一帧。

结论正面，M3b 按计划实现（brief 的“结论负面就停止”未触发）。沙箱只按 `sandbox_id`
终止，无模式匹配清理（`e2b 帧实测`）。

### 6.2 dev 模板与钉版（E2B CLI 构建）

新增 `docker/e2b/Dockerfile.acp-e2b`，构建**独立 dev 模板** `glink-acp-worker`；生产模板
`glink-frontier-e2b` 与其任何 tag 均未被读取写入（只有 `template list` 的只读列出），
pi 面模板 `pi-glink-worker` 也未动（`e2b 模板实测`）。

* 钉版与 M3a 镜像一致：`@deepseek-ai/dsh@0.1.5-rc.3`、
  `@agentclientprotocol/codex-acp@1.13.1`，`FROM node:24.21.0-bookworm-slim`；构建期用
  ARG 精确比对两个 `--version`，默认 CMD 也是同一个自检（`e2b 模板实测`）。
* 构建必须用 E2B CLI（历史事故：Python SDK 的 `Template().from_dockerfile()` 会丢多行
  ENV）：`E2B_API_KEY=… npx -y @e2b/cli template create glink-acp-worker --dockerfile …
  --cpu-count 2 --memory-mb 4096`。实测 Template ID `oen0rq47o2sizakry5v3`，
  Build ID `3a75d2b3-57aa-4876-b2fd-fee5276155b5`，构建 36s（`e2b 模板实测`）。
* 两个真实构建坑（均写进 Dockerfile 注释与证据）：① E2B 构建会把 CMD 数组用空格拼成
  一行再交给 shell（日志 `Running start command: sh -c dsh --version && codex-acp
  --version`），`sh -c dsh --version` 实际是“启动 dsh、把 --version 当 $0”，报
  `--profile <name> is required`；修复：CMD 指向镜像内自检脚本 `acp-self-check`。
  ② E2B 构建解析会把 Dockerfile 文本里的 `\n` 吃成 `n`（日志里的 RUN 命令即为
  `printf '%sn' …`），生成的脚本没有换行、execve 报 “cannot execute: required file
  not found”；修复：改用逐行 `echo` 生成脚本。
* 构建后真实沙箱自检（`scripts/spikes/acp_e2b/template_selfcheck.py`）：沙箱默认用户
  `user`（uid 1001）、`HOME=/home/user`、工作目录 `/home/user/workspace`，`node` v24.21.0，
  `dsh --version` = 0.1.5-rc.3，`codex-acp --version` = `@agentclientprotocol/codex-acp
  1.13.1`；`/home/user/acp` 与 `/home/user/workspace` 可写，每 run 状态根
  `/home/user/acp/home` 可创建可写；envd 0.9.0（`e2b 模板实测`）。
* 升级路径：改 ARG → CLI 重建 → 边界测试（ARG 必须重新等于本节记录值）→ 沙箱自检 →
  最小 run。模板名/ID/build ID 是本节的事实源，`tests/test_kernel_acp_e2b.py` 卡住 ARG
  与本节记录的版本相等。M4 用同一模板 ID 重建、加入 claude 双包（新 Build ID 与四个组件
  的自检输出见 §7.5 与 `docs/evidence/acp-m4-claude-e2b-handshake.txt`）。

### 6.3 执行面实现与合成回归

* **沙箱生命周期沿用 pi 面**：`E2BAcpSandboxClient` 与 pi 的 `E2BSandboxClient` 共用
  `_create_sandbox` / `_recover_workspace` 与同一份冻结配置（`E2B_API_KEY` 走 `Sandbox.create`
  参数，不进环境）；销毁前把沙箱工作区拉回宿主工作区，`sandbox_id` 在沙箱创建后**立即**
  持久化（上传失败也保留身份，供台账对账）（`宿主代码`/`合成实测`/`e2b 实测`）。
* **每 run HOME 与凭据**：复用 `prepare_acp_runtime` 渲染的 `runs/<id>/acp-agent/home`
  （凭据源只读复制、0600），再上传到沙箱状态根 `/home/user/acp/home`：先把状态根
  `chmod 0700` 再上传（不让文件落在可遍历目录里），上传后把创建的目录 `chmod 0700`、
  每个文件 `chmod 0600`；agent 命令的 `HOME` 指向该根，
  `PI_GLINK_ACP_ENV` 作为显式进程环境通道（值不进命令行），凭据值并入该 run 的 redactor
  （`宿主代码`/`合成实测`/`e2b 实测`）。
* **stdio 桥接**：`SandboxCommandProcess` 把 `CommandHandle` 适配成 `subprocess` 形状
  （`send_stdin`/`close_stdin`/分块读/`poll`/`wait`/`kill`），`AcpRpcClient` 新增可注入的
  `spawn`，e2b 面把 `E2BAcpSandboxClient.spawn` 交给它——同一份客户端、同一份 LF 分帧，
  没有第二套协议实现（`宿主代码`/`合成实测`）。
* **装配**：`build_adapter(harness="acp", executor="e2b")` → `AcpSandboxExecutionAdapter`
  （`runner._acp_e2b_plan` 提供沙箱客户端）；`session/new` 的 `cwd` 是沙箱路径
  `/home/user/workspace`（宿主路径在沙箱内不存在），`session_ready` 额外带 `sandboxId` 与
  `template`（`宿主代码`/`合成实测`/`e2b 实测`）。
* **配置**：解除 acp+e2b 的启动拒绝；新增冻结旋钮 `LOCAL_WEB_E2B_ACP_TEMPLATE`
  （默认 `glink-acp-worker`），与 pi 面的 `LOCAL_WEB_E2B_TEMPLATE` 分开；`public_view()`
  仍不暴露 harness/模板（`宿主代码`/`合成实测`）。
* **合成回归**（`合成实测`）：新增 `tests/test_kernel_acp_e2b.py` 15 项——fake SDK
  组合（模板/key/超时/上传清单/0600 加固/命令与 env/生命周期）、用真实子进程 fake ACP
  agent 走完整桥接的端到端适配器与 run 生命周期（终态、report 回收、身份持久化、上传失败
  仍保留身份、云端失败不上报身份）、模板 ARG 钉版边界。既有断言按新义务更新并在注释里
  写明原义务/新义务（配置与装配层的 e2b 拒绝改为接受）。

### 6.4 真实沙箱握手（两个 agent，不消耗模型）

用生产装配路径（`build_config` → `prepare_acp_runtime` → `E2BAcpSandboxClient` →
`build_adapter`）在 `glink-acp-worker` 沙箱内只做 `initialize` + `session/new`
（`scripts/spikes/acp_e2b/sandbox_handshake.py`，不发 `session/prompt`）：

* dsh 0.1.5-rc.3：`protocolVersion=1`，`agentInfo={"name":"deepseek-harness-acp",
  "version":"0.0.1"}`，`sessionCapabilities` 等能力位，`configOptions` = model /
  reasoning_effort，`authMethods=[]`，`cwd=/home/user/workspace`（`e2b 握手实测`）。
* codex-acp 1.13.1：`protocolVersion=1`，`agentInfo={"name":"@agentclientprotocol/codex-acp",
  "version":"1.13.1"}`，`authMethods` = api-key / chat-gpt，`configOptions` 5 个
  （mode / collaboration_mode / model / reasoning_effort / fast-mode），
  `cwd=/home/user/workspace`（`e2b 握手实测`）。
* 两侧凭据源文件 sha256 前后一致、源 mtime 早于本次运行；沙箱按 id 终止、无残留
  （`e2b 握手实测`）。与 M2/M3a 观测一致：无凭据时 codex-acp 的 `session/new` 会以
  Authentication required 拒绝（M3a 观测点，本次注入后握手成功）。

### 6.5 一次真实冒烟（dsh，经宿主 HTTP API）

`harness=acp + executor=e2b`、临时数据根、随机端口 55500、300s 有界超时、明确要求写
`report.md` 的短题（`scripts/spikes/acp_e2b/smoke_host.py`，`e2b 实测`）。该 run 在实现
提交 `235b86a` 的代码上产生；此后只调整了凭据上传前的状态根 `chmod` 顺序（§6.3/§6.6），
不影响本小节断言的事实（流式/终态/report 回收/沙箱按 id 对账）；最终代码的沙箱内凭据路径
由 §6.4 的握手重新实测（`e2b 握手实测`）：

* 终态 `finished`、`modelStopReason=end_turn`、`reportStatus=ok`，时长 29.6s、28 条事件；
  `GET /api/runs/<id>/report` 200（994 bytes），报告内容为 dsh 写的两点结论 + 1 条已核验
  来源；`turn_finished` 先于 `run_finished`。
* 流式：SSE 在 run 期间收到全部 28 帧（`run_started` → `session_ready`（8.1s，cwd 为沙箱
  路径）→ `turn_started` → thinking/usage/tool（web_search + 3 次 web_fetch）→
  `assistant_delta` → `turn_finished` → `run_finished`），与 `events.jsonl` 时间线一致。
* 沙箱：`sandbox_id=i9865hh296nza5lt7az2l` 持久化；`cleanup={"released":true,
  "external":{"sandbox":"i9865hh296nza5lt7az2l"}}`；宿主日志有 `DELETE /v2/sandboxes/<id>
  → 204`，随后 `confirm_sandbox_absent(id)` 返回“SDK 确认沙箱不存在”。
* 清理只操作已知身份：宿主进程由脚本持有的 PID `terminate()`（exit -15），沙箱按 id；
  未用 `pkill -f`、无名字通配；临时数据根（含每 run HOME 的凭据副本）取证后删除。
* 脱敏：`scripts/check-no-secrets.py` 对该数据根退出码 0（扫描文件 4 个、真实命中 0 处）；
  证据文件本身密钥形状 0 命中（`e2b 实测`）。
* 次数说明：本任务授权“一次最小真实冒烟 run”，本文件即那一次；运行前的管道演练使用不
  存在的模板（不消耗模型、不创建沙箱），不计入通过项（`e2b 实测`）。

### 6.6 与 M2/M3a 的差异与风险

* **承载面差异只在传输**：e2b 面没有容器挂载，所以每 run HOME 靠“上传 + 沙箱内 chmod”，
  凭据副本在上传后仍留在宿主 run 目录（0600，随 run 目录生命周期），不是只读挂载；
  `files.write` 没有 mode 参数，0600 由紧随其后的 `chmod` 施加（已先把状态根 0700，且沙箱
  本身是单 run 单用户），因此没有容器面的嵌套只读挂载这一层——沙箱内无法验证“凭据文件不可
  改写/删除”的容器级只读语义（`宿主代码`/`e2b 实测`）。
* **资源上限**：E2B SDK 这条路径只暴露 lifetime（与 pi e2b 面相同），没有 cpus/memory
  参数；宿主只声明它真的要求过的边界（`宿主代码`）。
* **模板独立性**：`glink-acp-worker` 是 dev 模板，未发布/未移动任何 tag；升级同样需要
  重新构建与沙箱自检（§6.2）。
* **仍未实测**：`@agentclientprotocol/codex-acp@1.13.1` 在 e2b 面的真实模型 run、
  e2b 面的真实 `session/cancel` 响应时延（§10）。

## 7. M4 实测（2026-09-25，claude-code-acp 接入）

本节每条都标来源。证据文件：`docs/evidence/acp-m4-claude-handshake.txt`（inproc 握手与
凭据/首启形态，不消耗模型）、`acp-m4-claude-smoke.txt`（一次最小真实 run）、
`acp-m4-claude-docker-handshake.txt`（镜像重建与三个 agent 容器内握手）、
`acp-m4-claude-e2b-handshake.txt`（dev 模板重建与沙箱握手）。协议侧零新增：claude-code-acp
走的是与 dsh/codex-acp 同一份 `AcpRpcClient`、投影与生命周期判定。

### 7.1 版本与入口

| 组件 | 钉版与入口 | 实测事实 |
|---|---|---|
| claude-code-acp | `@zed-industries/claude-code-acp@0.16.2`（npm `latest`），入口 `claude-code-acp`（无参数即 stdio ACP server） | npm 告警该包已 deprecated（"renamed to @agentclientprotocol/claude-agent-acp"），本任务按 brief 钉 0.16.2，后继包迁移评估留后续（`npm`/`claude 实测`） |
| 被驱动的 claude CLI | 适配器依赖 `@anthropic-ai/claude-agent-sdk@0.2.44`，后者自带 vendor `cli.js`（版本 2.1.44）；环境变量 `CLAUDE_CODE_EXECUTABLE` 可覆盖 | SDK 按路径后缀识别“原生二进制”并直接 spawn；指向本机 claude（2.1.281）时子进程命令行实测为 `…/bin/claude --output-format stream-json … --permission-mode default`（`claude 实测`/`宿主代码`） |
| 镜像/模板内 claude CLI | `@anthropic-ai/claude-code@2.1.282`（构建时 npm `latest`） | 官方包用平台可选依赖 + postinstall 放置原生二进制；npm 11 默认不跑生命周期脚本，构建里显式执行包内 `install.cjs`（`镜像自检`/`e2b 模板实测`） |

inproc 面的钉版调用是 `npx -y @zed-industries/claude-code-acp@0.16.2`（不全局安装）；因为
每 run 的 `HOME` 是新的，npx 缓存也随 run 新建，探针用 `PI_GLINK_ACP_ENV` 注入
`npm_config_cache` 共享一份缓存，避免冷下载超过 initialize 的 60s 上限（`宿主代码`/`claude 实测`）。

### 7.2 能力矩阵（initialize / session/new 响应原文）

| 字段 | dsh 0.1.5-rc.3 | codex-acp 0.16.0 / 1.13.1 | claude-code-acp 0.16.2 | opencode 1.18.32 |
|---|---|---|---|---|
| `protocolVersion` | 1 | 1 | 1 | 1 |
| `agentInfo` | `{"name":"deepseek-harness-acp","version":"0.0.1"}` | `{"name":"@agentclientprotocol/codex-acp","version":"1.13.1"}` | `{"name":"@zed-industries/claude-code-acp","title":"Claude Code","version":"0.16.2"}` | `{"name":"OpenCode","version":"1.18.32"}`（`opencode 实测`） |
| `loadSession` | 未声明 | `true` | `true` | `true`（`opencode 实测`） |
| `sessionCapabilities` | `close`/`list`/`resume` | `list`/`resume`/`close` | `fork`/`list`/`resume`（无 `close`） | `close`/`fork`/`list`/`resume`，值均为空对象 `{}`（`opencode 实测`） |
| `promptCapabilities` | image=false, audio=false, embeddedContext=false | image=true, embeddedContext=true | image=true, embeddedContext=true | image=true, embeddedContext=true（未声明 audio）（`opencode 实测`） |
| `mcpCapabilities` | `{http: true}` | `{http, sse, acp 各布尔}` | `{http: true, sse: true}` | `{http: true, sse: true}`（无 `acp` 键）（`opencode 实测`） |
| `authMethods` | `[]` | api-key / chat-gpt（env_var） | `[claude-login]`（描述为“Run `claude /login` in the terminal”） | `[opencode-login]`（描述为“Run `opencode auth login` in the terminal”）（`opencode 实测`） |
| `session/new` 返回 | `sessionId` + `configOptions`（model、reasoning_effort） | `sessionId` + `modes` + `configOptions`（5 项） | `sessionId` + `models`（default/opus/opus[1m]/haiku）+ `modes`（default/acceptEdits/plan/dontAsk/bypassPermissions）；**`configOptions` 为空数组** | `sessionId` + `modes`（空对象）+ `configOptions`（`model`、`mode`；选中具体模型后联出 `effort`）（`opencode 实测`） |
| 会话配置面 | `session/set_config_option` 可用 | `session/set_config_option` 可用 | 无 `configOptions`：`PI_GLINK_ACP_SESSION_CONFIG` 对它不适用（配了会失败闭环，缺省不配即是无操作）（`claude 实测`） | `session/set_config_option` 可用；实测 `model=openai/gpt-5.6-sol-fast` 生效，`model=…` 后 `configOptions` 多出 `effort`（`opencode 实测`） |

补充实测：

* `session/new` 后立即广播 `available_commands_update`（claude 本次 47 条；opencode 3 条：
  customize-opencode、init、review）；宿主对 ACP 族仍拒绝一切 `/` 开头 prompt，agent 声明命令
  不改变准入门禁（`claude 实测`/`opencode 实测`；§2 词表不变）。
* opencode 的真实模式选择不在 `modes`（空对象）而在 `configOptions.mode`（build/plan，
  默认 `build`）——同一份 `session_ready` 记录里两者都落地，所以“模式读哪一处”不靠猜
  （`opencode 实测`）。
* 宿主客户端声明仍是全空（无 `fs/*`、无 `terminal/*`、无 `auth.terminal`），agent 在自己的
  文件系统上工作；`clientInfo` 从 M4 起带 `version`（见 §7.7）（`claude 实测`/`宿主代码`）。

### 7.3 凭据与首启形态（inproc 面实测）

这是 M4 需要重新测量的部分，因为 claude 的登录态与其他 agent 的“凭据文件”形态不同。
旧假设（brief 背景里写的“OAuth 在 macOS Keychain、Keychain 按用户不按 HOME”）**部分不成立**，
实测事实如下（每条都有现场命令，值不落证据；`claude 实测`）：

1. **Keychain 可达但本机条目为空。** CLI 用的读取命令
   `security find-generic-password -a "$USER" -w -s "Claude Code-credentials"` 退出码 0、无交互提示；
   但条目值里 `claudeAiOauth.accessToken` / `refreshToken` 长度均为 0——它是空壳，不提供可用登录态。
2. **真正的 inproc 凭据是 `~/.claude/settings.json` 的 `env` 块**（本机指向第三方 Anthropic
   兼容端点 + `ANTHROPIC_AUTH_TOKEN`）。同一份 settings 下：本机 `claude` 2.1.281 `auth status`
   报 `loggedIn=true / authMethod=oauth_token`；去掉复制件里的 `ANTHROPIC_AUTH_TOKEN`/
   `ANTHROPIC_BASE_URL` 立刻变 `loggedIn=false`。即登录态来自 settings 的 env，不来自 Keychain。
3. **真实 settings.json 不能整文件复制进 run。** 它含约 30 条 hooks（Muxy/Otty/Orca/Herdr/
   harness notify）、`enabledPlugins` 与 `statusLine`；实测整文件复制后，SessionStart hook 立即在
   宿主上拉起 `~/.harness/cli/notify.mjs` 并把操作员真实的 `~/.claude/projects` 等目录传进去。
   正确形态是只把 `env` 块抽成一份运行期 settings（0600），经 `PI_GLINK_ACP_CREDENTIAL_FILES`
   只读复制进 `HOME/.claude/settings.json`（`source=dest`），源文件 sha256 前后一致。
4. **`~/.claude.json` 不需要复制。** 全新 HOME 首次启动时 CLI 自行创建 `.claude.json` 与
   `.claude.json.backup.*`（含 firstStartTime/userID/GrowthBook 缓存），不依赖预先存在的文件；
   initialize + session/new 在无任何凭据时也能完成。
5. **驱动哪个 CLI 会影响 auth 判定。** SDK 自带 cli.js 2.1.44 在同一份运行期 settings 下
   `auth status` 报 `loggedIn=false`，本机 2.1.281 报 `true`；inproc 冒烟因此按适配器官方通道设
   `CLAUDE_CODE_EXECUTABLE=本机 claude`（2.1.281），而不是赌 2.1.44 是否会在真实请求上应用
   settings env（`claude 实测`）。
6. **每 run HOME 落点**：`.claude/settings.json`（0600，注入值并入该 run 的 redactor）、
   `.claude.json`（0600）、`.claude/{backups,debug,sessions,projects}` 与 `Library/Caches`
   都在 run HOME 内，随 run 销毁；docker 面再对凭据文件加嵌套只读挂载，e2b 面在沙箱内
   0600 加固（§5.2/§6.3 的同一机制）（`claude 实测`/`docker 实测`/`e2b 握手实测`）。

结论：inproc 形态 = **运行期 `.claude/settings.json`（env 块，CREDENTIAL_FILES 0600 复制）+
`CLAUDE_CODE_EXECUTABLE` 指向宿主 claude CLI**；`PI_GLINK_ACP_ENV` 的
`ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` 是等价的环境变量通道（值同样并入 redactor，见 §7.7）。
macOS Keychain 只在 inproc 面存在，docker/e2b 没有它——那两面的真实模型 run 需要宿主显式提供
API key，否则属预期阻塞。

### 7.4 权限白名单（决策：claude 研究 run 启用最小集合）

* claude-code-acp 的 `canUseTool` 对 WebFetch 与 Write/Edit 逐次发 `session/request_permission`
  （选项 `allow_always`/`allow_once`/`reject_once`），但请求的 `toolCall` **不带** `kind`
  （适配器源码只传 `toolCallId`/`rawInput`/`title`；`claude 实测`/`npm`）。M2 的
  “无 kind 即拒绝”门控因此会让研究 run 写不出 report.md。
* **决策**：给 ACP 族定义最小白名单 `PI_GLINK_ACP_PERMISSION_ALLOW=allow_once,read,search,fetch,edit`
  ——只放行研究 run 需要的 read/search/fetch 与写（edit）四类工具，且只用一次性放行
  （`allow_once`）；`execute`/`think`/`switch_mode`/`other` 保持拒绝，未分类请求（请求与
  `tool_call` 通知都没有 kind）仍拒绝。默认（未配置）行为不变：fail-closed 全拒。
* 为使 kind 可用，宿主按同一 `toolCallId` 把 agent 已在 `tool_call` 通知里公布的 kind 用于
  权限分类（§1.7）；事件 `kernel_status` 记录 `decision`/`policy`/`toolKind`/`toolKindSource`。
  依据：kind 是 agent 自己发布的 ACP 字段，比按 `title` 或 `rawInput` 猜更硬；按标题匹配会
  把 i18n/文案变化引入安全决策（`claude 实测`/`宿主代码`）。
* 真实 run 证据：`acp-m4-claude-smoke.txt` 的 2 条 `acp_permission_allowed`（`kind=fetch`
  与 `kind=edit`，`toolKindSource=tool_call`）；本次未出现 execute 类请求，execute 仍被拒绝
  由合成回归
  （`tests/test_kernel_acp_runner.py::test_acp_permission_allowlist_refuses_a_published_execute_kind`）
  与默认空集路径钉住（`claude 实测`/`合成实测`）。
* 粒度限制（如实记录）：白名单按 **tool kind** 放行，不能限定“只准写 report.md”；
  需要路径级策略时属后续能力（dsh 的请求不含 kind，codex-acp 的 `mode` 会话选项仍是更窄
  的替代面，两者默认策略不变）（`宿主代码`）。

### 7.5 镜像与 dev 模板（本地重建 + 无模型握手自检）

* `docker/Dockerfile.acp-worker` 新增 claude 双包层：`@zed-industries/claude-code-acp@0.16.2` +
  `@anthropic-ai/claude-code@2.1.282`，与既有 dsh/codex-acp 同一镜像；npm 11 默认跳过生命周期
  脚本，构建里显式跑 `install.cjs` 放好原生二进制，并保留四个组件的构建期版本比对。默认 CMD
  从两个 `--version` 的一行改为镜像内自检脚本 `acp-self-check`（与 e2b 模板同形，
  claude-code-acp 用 `npm ls -g` 复验）。重建后实测：`docker run --rm` 输出
  `0.1.5-rc.3` / `@agentclientprotocol/codex-acp 1.13.1` / `2.1.282 (Claude Code)` / `0.16.2`；
  容器内握手（生产装配路径）claude-code-acp 成功、dsh 回归成功、codex-acp 的 session/new 在
  无凭据时报 `Authentication required`（与 M3a 观测一致，本任务禁触 `~/.codex` 未注入凭据）；
  三个容器都按持久化名字确认不存在（`docker 实测`）。
* `docker/e2b/Dockerfile.acp-e2b` 同步同一对钉版并用 E2B CLI 重建 dev 模板 `glink-acp-worker`
  （同一 Template ID `oen0rq47o2sizakry5v3`，新 Build ID `4a5fbaea-d46b-44fe-81ef-0255d854c6d7`，
  构建 1m43s）；构建期与 finalize 的 start command 都跑了 `acp-self-check`，四个组件版本正确；
  真实沙箱内 claude-code-acp 完成 initialize + session/new（cwd `/home/user/workspace`，
  `authMethods=[claude-login]`，`configOptions` 空），沙箱按 id 终止无残留；生产模板
  `glink-frontier-e2b` 与 pi 模板 `pi-glink-worker` 未被触碰（`e2b 模板实测`/`e2b 握手实测`）。
* CMD/自检形态的既有断言按新义务更新（`tests/test_kernel_acp_docker.py` 注释写明原义务：
  两个 `--version`；新义务：脚本覆盖四组件），ARG 钉版边界测试扩展到 claude 双包（`合成实测`）。

### 7.6 一次 inproc 最小真实冒烟

`harness=acp + executor=inproc`、临时数据根、随机端口、300s 有界超时、明确要求写 `report.md`
的短题；宿主 HTTP API 创建 run，SSE 与终态从 HTTP 面核对（`claude 实测`，证据
`acp-m4-claude-smoke.txt`，授权 ≤2 次中的第 1 次）：

* 终态 `finished`、`modelStopReason=end_turn`、`reportStatus=ok`，时长 47.6s、141 条事件；
  `GET /api/runs/<id>/report` 200（714 bytes），报告为 2 条要点 + 1 条可核验来源；
  `turn_finished` 先于 `run_finished`。
* 流式：SSE 收到全部 141 帧（`run_started` → `session_ready`（2.2s）→ `turn_started` →
  thinking/`assistant_delta` → `tool_started`/`tool_updated`/`tool_finished`（Fetch、Write）→
  `turn_finished` → `run_finished`），与 `events.jsonl` 时间线一致。
* 权限：2 条 `acp_permission_allowed`（fetch、edit；kind 均来自 `tool_call` 通知），无拒绝。
* 清理：宿主进程按脚本持有的 PID `terminate()`（exit -15）；run 期间采样的 4 个后代进程
  （npm exec → node claude-code-acp → claude → claude.exe）结束后全部退出，无 leftover；
  `cleanup={"released":true,...}`；容器/沙箱身份均为 None；未用 `pkill -f` 或名字通配。
* 脱敏：`scripts/check-no-secrets.py` 对该数据根退出码 0（扫描 4 个文件、真实命中 0 处）；
  events 里 `ANTHROPIC` 0 命中；真实 `~/.claude/settings.json` sha256 前后一致。
* 本机语义如实记录：该 run 由宿主 claude 配置的第三方 Anthropic 兼容端点驱动（settings env
  的 AUTH_TOKEN + BASE_URL），不是 Claude 订阅；brief 的“Claude 订阅”描述与实测不符，
  以本节为准（`claude 实测`）。

### 7.7 宿主侧实现变更（M4）

* `clientInfo` 补 `version`（§1.2）；合成回归
  `test_acp_initialize_identifies_the_client_with_a_version` 钉住。
* 权限 kind 关联（§1.7/§7.4）：`AcpExecutionAdapter` 记住每个 `toolCallId` 的 kind（上限 512），
  `acp_permission` 帧加 `toolKindSource`；合成回归用 fake agent 复刻 claude-code-acp 的
  “先 `tool_call` 通知、后无 kind 权限请求”顺序（`FAKE_ACP_PERMISSION_AFTER_TOOL_CALL`）。
* `PI_GLINK_ACP_ENV` 里凭据形状的取值并入该 run 的 redactor（`_env_credential_values`）：
  兑现 §4.5/方案 §4.5 已声明的“注入值并入 redactor”，只按变量名（`key/token/secret/…`）
  认定，普通旋钮（如 `ACME_MODE=workspace`）不参与脱敏（`宿主代码`/`合成实测`）。
* live 探针：`scripts/spikes/acp_inproc/{handshake,smoke_host}.py`、
  `scripts/spikes/acp_docker/handshake.py`，e2b 探针扩展 `--agent claude-code-acp`
  （`agent 文档/消费脚本同步更新`）。

### 7.8 与既有面的差异与风险

* **claude-code-acp 与它的后继包**：0.16.2 已被 npm 标记 deprecated（改名
  `@agentclientprotocol/claude-agent-acp`）；本任务按要求钉旧包，升级路径是改 ARG →
  重建 → 边界测试 → 握手/冒烟（§7.5）。
* **usage 面**：本次 claude 的 141 条事件里没有 `usage_update`（终态 `usage=null`），适配器
  不发就该如实空着，不伪造；token 用量在 claude 的 ACP 面上暂不可得（`claude 实测`）。
* **重复的 `tool_call` 通知**：claude-code-acp 对同一个 `toolCallId` 会先后发两条
  `tool_call`（先程序名、后完整标题），产品面因此出现成对的 `tool_started`；这是适配器
  行为，不是宿主丢帧或重复执行，本次如实记录，未改投影语义（`claude 实测`/`宿主代码`）。
* **`session/cancel`、续接、docker/e2b 真实模型 run**：仍未实测，见 §10。
* **Keychain 形态因机而异**：本机条目为空壳，其他机器若真用订阅登录，keychain 读路径同样
  “按用户不按 HOME”，但 inproc 之外仍不可用；升级/换机时应重跑 §7.3 的检查（`claude 实测`）。

## 8. M5 实测（2026-09-25，opencode 接入）

本节每条都标来源。证据文件：`docs/evidence/acp-m5-opencode-handshake.txt`（inproc 握手与
凭据/状态目录形态，不消耗模型）、`acp-m5-opencode-smoke.txt`（inproc 一次最小真实 run）、
`acp-m5-opencode-docker-handshake.txt`（镜像重建、容器内握手与凭据只读挂载）、
`acp-m5-opencode-docker-smoke.txt`（docker 面一次最小真实 run）、
`acp-m5-opencode-e2b-handshake.txt`（dev 模板重建与沙箱握手）。协议侧零新增：opencode
走的是与前三个 agent 同一份 `AcpRpcClient`、投影与生命周期判定。

### 8.1 版本与入口

| 组件 | 钉版与入口 | 实测事实 |
|---|---|---|
| opencode | `opencode-ai@1.18.32`（npm `latest`），入口 `opencode acp` | 本机 npm 全局装的是同族的 1.18.18；两者的 `initialize` 能力矩阵一致，只有 `configOptions.model` 的候选列表随版本/凭据变化。inproc 探针用 `npx -y opencode-ai@1.18.32 acp`（不全局安装）；镜像与 dev 模板用同一 ARG 安装（`npm`/`opencode 实测`） |
| 包形态 | wrapper + 平台可选包，`bin.opencode` 指向 `bin/opencode.exe` | `postinstall.mjs` 从平台包复制二进制并自验；镜像/模板显式再跑一次（npm 生命周期脚本不保证执行），构建期与 ARG 精确比对（`npm`/`镜像自检`） |

### 8.2 握手与事件变体

* inproc 握手（生产装配路径，不发 prompt）：`protocolVersion=1`、
  `agentInfo={"name":"OpenCode","version":"1.18.32"}`，能力矩阵见 §7.2；`cwd` 是 run
  工作区；初始 `configOptions` 是 `model`（默认 `opencode/big-pickle`，OpenCode Zen）+ `mode`
  （build/plan）；`authMethods` 只有 `opencode-login`（`opencode 实测`）。
* **无凭据也能握手**：`session/new` 不需要先登录（与 codex-acp 不同）；注入 `auth.json` 后
  `model` 选项里出现 OpenAI 系模型，这是凭据生效的直接观测点（`opencode 实测`）。
* 一次真实 run 观测到的 `session/update` 变体：`agent_message_chunk`、`tool_call`、
  `tool_call_update`、`config_option_update`、`available_commands_update`、`usage_update`
  （本次未出现计划/思考块）。其中 **`config_option_update` 是 §4.1 未定义的变体**：宿主按
  「不猜语义」原则记一条 `kernel_status`（`acp_update_not_projected`，info），不伪造产品
  事件；本次它出现在 `session_ready` 之前（宿主应用 `model=…` 的联动），产品词表不变
  （`opencode 实测`）。
* `available_commands_update` 本次 3 条（customize-opencode、init、review），走 §2 的
  warning 提示；`/` 开头 prompt 的准入门禁不因 agent 声明命令而放宽（`opencode 实测`）。
* 工具调用形状：`tool_call` 先到（`title` 为 webfetch 的 URL、`kind` 显式给出），随后
  `tool_call_update` 推进 status；`apply_patch` 成功写 `report.md` 后以 completed 收口
  （`opencode 实测`/`宿主代码`）。

### 8.3 凭据与状态目录（inproc 面实测）

* **注入**：`PI_GLINK_ACP_CREDENTIAL_FILES=~/.local/share/opencode/auth.json=.local/share/opencode/auth.json`
  ——源只读（探针前后 sha256 一致）、目标文件 0600、父目录 0700（`opencode 实测`）。
* **redactor**：opencode 的 OAuth 字段名是 `access`/`refresh`（不带 `token` 后缀），通用规则
  抓不到；M5 按**精确字段名**扩充（`openai.access` 命中、`openai.accessMode` 不命中），本次
  4 条值并入该 run 的 redactor（openai/codex 各一对 access/refresh）。`type`/`expires`/
  `accountId` 不并入（账号标识不是凭据）（`宿主代码`/`合成实测`/`opencode 实测`）。
* **每 run HOME 落点**（inproc 实测，全部在 run 目录内、随 run 销毁）：
  `.local/share/opencode/auth.json`（0600，注入副本）、`.local/share/opencode/{log/opencode.log,
  opencode.db, opencode.db-shm, opencode.db-wal, repos/}`、`.local/state/opencode/locks/<hash>.lock/`、
  `.config/opencode/{.gitignore, opencode.jsonc}`（自建空配置，50 字节）、`.cache/opencode/bin`
  （`opencode 实测`）。
* **真实全局配置不进入 run**：`~/.config/opencode/opencode.json`（操作员自定义 provider 模型
  与插件列表）在 run HOME 里不存在，run 内也没有读它——用只放 `auth.json` 的假 HOME 直跑
  `opencode models openai`，模型列表与真实 run 一致；`gpt-6-astra` 系列是 1.18.32 的内置模型，
  不是配置泄漏（`opencode 实测`）。
* **run HOME 的配置通道可用**（实测登记，本任务未据此预置渲染器）：把一份只定义
  `zzz-marker-6` 的 `opencode.json` 经 CREDENTIAL_FILES 放进 `HOME/.config/opencode/`，该模型
  随即出现在 `configOptions.model` 里（`opencode 实测`）。
* **凭据刷新实测（inproc）**：真实 run 结束后 run HOME 的 `auth.json` 副本与源不同
  （4300 vs 4285 字节）：openai 的 `access`/`refresh` 都被刷新、`expires` 从 2026-09-18
  更新到 2026-10-05；`codex` 未变（本次未用）；**源文件 sha256 前后一致**。即刷新写回只落
  在 run 的副本上，不碰操作员文件（`opencode 实测`）。
* 环境隔离沿用既有清单：`PASSTHROUGH_ENV` 只透传 `XDG_CONFIG_HOME`/`XDG_CACHE_HOME`（本机
  未设置），`XDG_DATA_HOME` 不在清单——所以 `~/.local/share`、`~/.config`、`~/.cache` 必然
  落在 run HOME，真实操作员目录不被 opencode 读写（`宿主代码`/`opencode 实测`）。

### 8.4 权限（决策：不需要白名单，fail-closed 默认不变）

* **实测**：一次真实 run 的 53 条事件里 `acp_permission_*` 0 条；两次 `webfetch` 与一次
  `apply_patch`（写 `report.md`）都直接执行，opencode 没有发 `session/request_permission`。
  该 run 虽然按 M4 最小集合传了 `PI_GLINK_ACP_PERMISSION_ALLOW=allow_once,read,search,fetch,edit`，
  但**白名单从未被引用**（没有请求就没有决策）：run 的成功不依赖它（`opencode 实测`）。
  这与 claude-code-acp（§7.4 逐次询问）形成对照，与 dsh/codex-acp 同类。
* **源码形态**（只读参考上溯版本）：请求选项固定为 `once`(allow_once)/`always`(allow_always)/
  `reject`(reject_once)；请求的 `toolCall` 由 `pendingToolCall` 生成、**带 `kind`**
  （webfetch→fetch、edit/write/apply_patch→edit、read→read、grep/glob→search、bash/shell→execute、
  task→think、其余 other），客户端未声明 `requestPermission` 时 opencode 自行按 reject 处理
  （fail-closed）。因此若将来出现请求，宿主的 tool-kind 白名单会以 `toolKindSource=request`
  分类，不需要 M4 那种按 `toolCallId` 回查通知（`opencode 源码`/`宿主代码`）。
* **决策**：**不为 opencode 启用白名单**；`PI_GLINK_ACP_PERMISSION_ALLOW` 默认空集不变，M4 的
  最小集合仍然只属于 claude-code-acp 的运行配置。哪些工具会触发 prompt（源码里的 `bash`、
  外部目录路径等）本轮未在真实 run 观测到，记入 §10（`opencode 实测`/`opencode 源码`）。

### 8.5 一次 inproc 最小真实冒烟

`harness=acp + executor=inproc`、临时数据根、随机端口、300s 有界超时、明确要求写 `report.md`
的短题；宿主 HTTP API 创建 run，SSE 与终态从 HTTP 面核对（`opencode 实测`，证据
`acp-m5-opencode-smoke.txt`；授权为 inproc 1 次，本文件即那一次）：

* 终态 `finished`、`modelStopReason=end_turn`、`reportStatus=ok`，时长 19.8s、53 条事件；
  `GET /api/runs/<id>/report` 200（336 bytes），报告为 2 条要点 + 1 条已核验来源；
  `turn_finished` 先于 `run_finished`。
* 流式：SSE 收到全部 53 帧（`run_started` → `kernel_status`（`config_option_update`）→
  `kernel_status`（available commands）→ `session_ready` → `turn_started` → 两个 `webfetch`、
  一个 `apply_patch` 的 `tool_started`/`tool_updated`/`tool_finished` → 36 条 `assistant_delta`
  → `usage_reported` → `turn_finished` → `run_finished`）。
* usage：`used=6145`、`size=1050000`、`cost={amount:0,currency:USD}`、`basis=acp_usage_update`
  （替换语义，终态采用最后一个快照）。
* 权限：0 条 `acp_permission_*`（见 §8.4）。
* 清理：宿主进程按脚本持有的 PID `terminate()`（exit -15）；运行期采样到 2 个后代
  （`npm exec opencode-ai@1.18.32 acp` → `opencode acp`），结束后都已退出；
  `cleanup={"released":true,...}`；容器/沙箱身份均为 None；未用 `pkill -f` 或名字通配。
* 脱敏：真实 `~/.local/share/opencode/auth.json` sha256 前后一致；证据文件与检查脚本对数据根
  0 命中（见证据文件尾部的扫描记录）。

### 8.6 镜像与 dev 模板（本地重建 + 无模型握手自检）

* `docker/Dockerfile.acp-worker` 新增 opencode 层：`ARG OPENCODE_AI_VERSION` 与既有四项同一次
  `npm install -g`，安装后显式再跑包内 `postinstall.mjs`（npm 11 的 install-scripts 策略会跳过
  它，构建日志里点名的就有 `opencode-ai (postinstall: node ./postinstall.mjs)`），并保留五组件的
  构建期版本比对；默认 CMD/`acp-self-check` 增至五个组件（`opencode --version`）。
* **本地重建实测**（Docker 29.2.1 / Colima）：镜像 `pi-glink-acp-worker:latest` =
  `sha256:fbf2707319e664ab91e8ecf5c23a2bfa9f42890da4acbba7dc53ef736fcb40e3`（linux/arm64，
  `docker inspect` 527357716 字节），构建期自检输出五组件版本且 `grep -qx "${OPENCODE_AI_VERSION}"`
  通过。本机到官方 deb/npm 源很慢（apt 阶段 8 分钟、npm 约 60 KB/s），按 Dockerfile 已声明的
  覆盖参数改用 `mirrors.aliyun.com` + `registry.npmmirror.com` 重建（组件版本不变）；第一次
  官方源构建在 npm 阶段中止、未产出镜像（`docker 实测`/`镜像自检`）。
* **容器内握手**（生产装配路径，不发 prompt）：`opencode acp` 在容器里完成 initialize +
  session/new，`cwd=/workspace`、`agentInfo={"name":"OpenCode","version":"1.18.32"}`、
  `PI_GLINK_ACP_SESSION_CONFIG=model=openai/gpt-5.6-sol-fast` 生效（`configOptions` 联动出
  `effort`）；容器按持久化名字对账无残留（`docker 实测`）。
* **凭据只读契约**（同一 plan 的挂载参数）：容器内 `HOME/.local/share/opencode/auth.json`
  0600、4285 字节、sha256 与宿主副本一致；追加写失败（`Read-only file system`，exit 2）、
  删除失败（`Device or resource busy`，exit 1），而 HOME 其它位置可写（exit 0）——与 M3a/M4
  的嵌套只读挂载语义一致（`docker 实测`）。
* **e2b dev 模板**：`docker/e2b/Dockerfile.acp-e2b` 同步同一钉版与同一套自检，用 E2B CLI 重建
  `glink-acp-worker`（同一 Template ID `oen0rq47o2sizakry5v3`，新 Build ID
  `ef0637fd-9b6d-47f4-bef0-de94bdb3ff23`，构建 2m15s）；构建期与 finalize 的 start command
  都打印五组件版本（opencode `1.18.32`），npm 同样跳过它的 postinstall、由 Dockerfile 显式
  补跑。真实沙箱内 opencode 完成 initialize + session/new（`cwd=/home/user/workspace`、
  `authMethods=[opencode-login]`、`configOptions` = model/mode），每 run HOME 的 auth.json
  上传后 sha256 与源一致、沙箱按 id 终止无残留；生产模板 `glink-frontier-e2b` 与 pi 模板
  `pi-glink-worker` 未被触碰（`e2b 模板实测`/`e2b 握手实测`）。

### 8.7 docker 面一次最小真实冒烟

`harness=acp + executor=docker`、临时数据根（$HOME 下，Colima 共享树）、随机端口 60831、
300s 有界超时、同一短题；授权为 docker 面最多 1 次，本小节即那一次（`opencode 实测`，证据
`acp-m5-opencode-docker-smoke.txt`）：

* 终态 `finished`、`modelStopReason=end_turn`、`reportStatus=ok`，26.2s、53 条事件；`/report`
  200（362 bytes）；事件构成与 inproc 同形（`config_option_update` 仍只记 `kernel_status`，
  0 条权限请求）。
* 容器 argv 由宿主日志留证（`--rm -i`、五项资源限制、`/workspace` 与 `/home/acp` 挂载、凭据
  `:ro` 嵌套挂载、image 后接 `opencode acp`）。
* 凭据用的是 inproc 冒烟刷新后的副本（token 有效期到 2026-10-05）：容器内是只读挂载，
  刷新写回在该面不可写；run 前后宿主副本与 run HOME 内副本 sha256 一致
  （`93d21eac7c800383…`，4300 字节），只读契约在真实 run 中成立（`opencode 实测`）。
* run HOME 落点（宿主侧可见）：`.config/opencode/node_modules/**`（opencode 在容器内为
  provider 装的依赖）、`.local/share/opencode/{auth.json, log, opencode.db*}`、
  `.local/state/opencode/locks/**`；workspace 里是 `report.md`；都随 run 销毁（`opencode 实测`）。
* 清理：宿主 PID `terminate()`（exit -15）；`cleanup.released=true`、
  `external.container=pi-glink-960dd5f9958e`；`docker ps -a` 无 `pi-glink-*` 残留；未用
  `pkill -f`/通配；数据根经 `scripts/check-no-secrets.py` 扫描 0 命中（`opencode 实测`）。

### 8.8 宿主侧实现变更（M5）

* `kernel/acp_env.py`：`credential_secrets_from_text` 增加 OAuth 精确字段规则
  （`_OAUTH_SECRET_FIELDS`），JSON 路径提示改为点分（既有的子串规则不受影响）；新回归
  `tests/test_kernel_acp_env.py` 钉住 opencode `auth.json` 的 4 条值与 `accessMode` 负例。
* live 探针参数化：inproc `handshake.py` 增 `--session-config`、`--watch`（子进程采样关键字，
  opencode 用 `opencode`）；inproc `smoke_host.py` 与 docker `handshake.py` 增
  `--session-config`，docker 探针另增 `--credential`（验证每 run HOME 的凭据与容器内嵌套只读
  挂载）。
* 钉版边界回归扩展到 `OPENCODE_AI_VERSION`（docker 与 e2b 两个 Dockerfile 都必须等于本节
  记录的实测版本），既有四项钉版断言不变。

### 8.9 与既有面的差异与风险

* **access token 的有效期与刷新**：本机 `auth.json` 的 openai access token 在实测时已过期
  （expires 早于实测日期），真实 run 靠 refresh token 刷新并把新值写回 **run HOME 的副本**
  ——inproc 面已实测发生（§8.3：副本被重写、源不变）。容器的只读挂载与 e2b 沙箱副本都
  不允许写回：docker 面的真实 run 因此用了 inproc 刷新后的副本（有效到 2026-10-05）并成功
  （§8.7）；e2b 面的真实 run 同理需宿主提供有效凭据，否则属预期阻塞（`opencode 实测`）。
* **刷新后的新令牌不在 redactor 快照里**：redactor 在 run 创建时取凭据值，而刷新发生在 run
  期间；新令牌若被 agent 回显不会被本 run 的 redactor 命中（旧值仍会被命中）。这是“值并入
  redactor”按 run 边界生效的直接推论，如实记录（`宿主代码`/`opencode 实测`）。
* **会话选项的实时变化不投影**：`config_option_update` 只记一条 `kernel_status`（§8.2）；
  产品侧看到的仍是 `session_ready` 时的快照（`宿主代码`/`opencode 实测`）。
* **权限请求的触发面未闭环**：本轮真实 run 没有触发任何请求，源码里的 `bash`/外部目录路径
  等触发点未在真实 run 观测（§10）。
* **能力位形态差异**：opencode 的 `sessionCapabilities` 值全是空对象、`modes` 也是空对象，
  宿主只按存在性/整体形状记录，不解析内部字段（`opencode 实测`）。

## 9. M1 合成验证覆盖

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

M3a 新增 `tests/test_kernel_acp_docker.py`（无 daemon）：ACP docker plan 组合（`docker run -i`、
`/workspace` 与 `HOME=/home/acp` 挂载、凭据嵌套只读挂载、资源限制与 pi 面逐项相等、
`PI_GLINK_ACP_ENV` 按名字传且值不进 argv）、ACP worker 镜像的钉版边界
（Dockerfile ARG == 本笔记记录的实测版本，浮动/缺失声明直接失败）、acp+e2b 在装配层交给沙箱
面（M3b 起）、容器身份在启动前持久化与按名对账。真实容器行为不在这个文件里，见 §5 与证据文件。

M3b 新增 `tests/test_kernel_acp_e2b.py`（无云调用）：ACP e2b 组合（冻结 ACP 模板与
`E2B_API_KEY` 到达 `Sandbox.create`、每 run HOME 上传清单与沙箱内 0700/0600 加固命令、
命令即冻结 argv 在沙箱工作区、`background=True`+`stdin=True`+超时等于沙箱寿命）、把
`CommandHandle` 适配成 subprocess 形状的桥接（真实子进程 fake ACP agent 走完
initialize/session/new/prompt、投影帧、产物回收与沙箱销毁）、run 生命周期（终态、report、
`sandbox_id` 持久化、上传失败仍保留身份、云端失败不上报身份）、模板 ARG 钉版与 dev 模板
独立性。真实沙箱行为不在这个文件里，见 §6 与证据文件。

M5 新增/更新（`合成实测`）：`tests/test_kernel_acp_env.py` 增三项 opencode 凭据回归
（JSON 的 `access`/`refresh` 命中且 `accountId`/`accessMode` 不命中、YAML 标量行同规则）；
`tests/test_kernel_acp_docker.py` 与 `tests/test_kernel_acp_e2b.py` 的钉版边界各扩展
`OPENCODE_AI_VERSION`（Dockerfile ARG 必须等于本笔记记录的实测版本）与五组件自检断言，
既有四项钉版、`acp-self-check`/CMD 与 dev 模板独立性断言不变。

## 10. 剩余待实测项（§4–§8 已覆盖的不再列）

| 待测事实 | 为什么重要 | 验证方式 |
|---|---|---|
| `session/resume` / `session/load` 真实续接（含 dsh 的持久会话语义；claude-code-acp 声明了 `resume`/`fork`/`list`） | M1 起追问仍显式拒绝；M2 只确认能力位存在，M4 只记录 claude 的能力位 | 授权后的真实续接 run + 终态/状态目录核对 |
| `session/cancel` 真实响应时延与终态（含工具收敛） | 取消体验；M2 两次 run 未触发取消；M3a docker 面、M3b e2b 面、M4 inproc 面同样未触发 | 真实 run + 计时 |
| claude-code-acp 的真实模型 run（docker / e2b 面） | M4 只验证了这两面的无模型握手：容器/沙箱没有 Keychain，宿主也未提供 `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` | 提供 API key 后按 §7.6 同形冒烟（需新授权） |
| `@zed-industries/claude-code-acp` 后继包 `@agentclientprotocol/claude-agent-acp` 的迁移评估 | 钉版 0.16.2 已被 npm 标记 deprecated（与 M3a 的 codex-acp 包替换同类问题） | 后继包版本核对 + 能力矩阵/握手/冒烟对照 |
| `@anthropic-ai/claude-agent-sdk` 自带 cli.js（2.1.44）是否会在真实请求上应用 settings 的 env | 本机实测它的 `auth status` 不认 settings env（2.1.281 认）；M4 因此用 `CLAUDE_CODE_EXECUTABLE` 走了本机 CLI | 无模型：换用 2.1.44 直接发一次请求/检查请求头；或升级适配器版本后重测 |
| claude-code-acp 的 `usage_update`（本次 141 条事件里 0 条）与重复 `tool_call` 通知（同一 `toolCallId` 两条） | 前者影响用量呈现，后者影响工具卡片时序；两者都是适配器行为，宿主如实记录 | 升级适配器版本后对照事件流；必要时与上游反馈 |
| opencode 的权限请求触发面（`bash`、外部目录路径等） | M5 真实 run 的 `webfetch`/`apply_patch` 未发请求，白名单未被引用（§8.4）；触发点来自上游源码而非实测 | 授权后一次会用到 shell/外部路径的真实 run，对照 `toolKindSource=request` 与决定记录 |
| opencode 的真实模型 run（e2b 面，含只读凭据下的刷新写回） | M5 已验证 docker 面（用 inproc 刷新后的副本，只读挂载成立）；e2b 面的真实 run 与容器/沙箱内能否在只读凭据下刷新 token 未实测 | 提供有效凭据后按 §8.7 同形冒烟（需新授权） |
| opencode 的 `session/cancel` 真实响应时延与终态 | M5 的 run 正常结束，未触发取消路径 | 真实 run + 计时 |
| `@agentclientprotocol/codex-acp@1.13.1` 的真实模型 run | M3a 只验证了容器内握手；M3b 只验证了 e2b 沙箱内握手与 dsh 的真实 run | 同 M2 冒烟（需新授权） |
| e2b 面的真实 `session/cancel` 响应时延与沙箱终止 | M3b 的 run 正常结束，未触发取消路径 | 真实 run + 计时 + `sandbox_id` 对账 |
| Linux 宿主上容器 root 写 bind mount 的 uid/属主行为 | macOS/Colima 下属主不变；Linux 原生 Docker 未测 | Linux 宿主上的 docker smoke + 文件属主核对 |
| 不同 Docker 后端（Docker Desktop 虚拟化 / Linux 原生）的共享路径与空挂载行为 | Colima 只共享 home 树；非共享路径会静默空挂载 | 逐环境 smoke + 挂载内容核对 |
| 各适配器版本升级冒烟步骤与 license 记录 | 方案 §5/§7 纪律 | 集成笔记逐项补齐 |
