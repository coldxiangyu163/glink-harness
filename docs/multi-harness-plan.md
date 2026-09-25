# 多 Harness 平台化：设计构想与推进计划

[架构](architecture.md) · [工程约束](engineering.md) · [pi 边界](pi-boundary.md)

本文记录「从单一 pi 内核走向多 harness 平台」的方向决定、目标架构、复用策略与里程碑。
状态（2026-09-25）：方向已经用户确认；**M1（GLINK-ACP-01）已完成合成验证**；
**M2（GLINK-ACP-02）已完成真实冒烟**：`dsh --profile acp`（dsh 0.1.5-rc.3）与
`codex-acp`（@zed-industries/codex-acp 0.16.0）各完成真实 initialize/session/new 握手，并经
宿主 HTTP API 各跑一次最小真实研究 run（流式事件、终态、`report.md` 回收全部验证）；
system prompt 送达通道与每 run 凭据注入已实现。逐条实测事实、能力矩阵与权限决策见
[ACP 集成笔记](acp-integration-notes.md) §4，证据在 `docs/evidence/acp-m2-*.txt`。
**M3a（GLINK-ACP-03）已完成**：ACP 族的 Docker 执行面可用——新增自包含的 ACP worker
镜像（`docker/Dockerfile.acp-worker`），容器 stdio 直接桥接 M1 客户端，每 run HOME 与只读
凭据挂载、cpus/memory/pids 限制与降权与 pi 面对齐，容器名/停止/对账复用既有的
`docker_env` 约定，并经宿主 HTTP API 跑过一次容器内最小真实 run；实测与镜像钉版见
[ACP 集成笔记](acp-integration-notes.md) §5，证据在 `docs/evidence/acp-m3-docker-*.txt`。
**M3b（GLINK-ACP-04）已完成**：ACP 族的 E2B 执行面可用——先在真实沙箱完成双向 stdio
帧完整性实测（保序/字节一致/LF 分帧/64 KiB–1 MiB 大帧/`close_stdin`，正面），再用独立
dev 模板 `glink-acp-worker`（E2B CLI 构建、ARG 钉版同 M3a）跑通两个 agent 的沙箱内握手与
dsh 的一次最小真实 run（SSE 流式、终态、`report.md` 回收、`sandbox_id` 对账无残留）；
实测与模板事实见 [ACP 集成笔记](acp-integration-notes.md) §6，证据在
`docs/evidence/acp-m3-e2b-*.txt`。
**M4（GLINK-ACP-05）已完成**：claude-code-acp（`@zed-industries/claude-code-acp@0.16.2`）
接入三个执行面的事实面——inproc 握手与一次最小真实 run（流式/终态/report.md/清理只操作
已知 PID）、每 run 凭据形态（运行期 `.claude/settings.json` 的 env 块，CREDENTIAL_FILES
0600）、权限最小白名单（`allow_once,read,search,fetch,edit`；execute 保持拒绝）、镜像与
dev 模板重建（docker 与 e2b 各加 claude 双包钉版层 + 无模型握手自检）；docker/e2b 面的
真实模型 run 因无 API key 属预期阻塞，详见 [ACP 集成笔记](acp-integration-notes.md) §7
与 `docs/evidence/acp-m4-claude-*.txt`。ACP 续接仍属后续里程碑。
宿主默认 harness 仍是 `pi`，ACP 族只在显式配置下启用，执行面已覆盖 inproc / docker / e2b；
本文是该方向的事实源：实现与本文出现偏差时回写本文，而不是让代码与文档各自漂移。

## 1. 决定与定位

* 宿主（run 编排、事件总线、存储、SSE、租户、脱敏、资源台账）保持 **harness 无关**；
  `kernel/` 从「pi 接入层」泛化为「harness 适配层」。
* 目标候选：deepseek-harness（dsh，原生 ACP server）、Claude Code、Codex，以及一切
  ACP 兼容 agent（Gemini CLI、OpenCode、Kimi 等）。
* **不变量**：产品事件词汇（`events.py`）语义不改；前端不感知 harness 差异；多租户、
  凭据边界、脱敏、资源台账的既有承诺不因 harness 增加而削弱；run 生命周期与终态判定表
  （架构 §4）继续是唯一定论。
* **明确不做**：
  * 不为尚无消费者的 harness 预建空适配器或抽象框架（工程约束：不预建无消费者的抽象）；
    抽象随第一个真实 harness（ACP 族）的接入而产生。
  * 不 fork / patch / vendor 任何 harness；一切接入走对方官方文档化的协议或扩展点。
  * 不以终端文本解析为接入机制（coder/agentapi 的 PTY 屏幕解析路线与本仓库
    「前端不解析终端文本」的底线冲突，排除）。
  * M1 不引入真实模型调用；合成验证先行，真实冒烟单独授权。

## 2. 现状接缝（为什么可行）

宿主约 12k 行中 pi 特异约 2.5k 行，全部集中在 `kernel/` 与 `agent/`，边界清晰：

| 已 harness 无关（不动） | pi 特异（泛化对象） | 泛化后的形态 |
|---|---|---|
| 产品事件词汇、EventBus、RunStore、提交水位、SSE 重放 | `kernel/projection.py`（pi 事件 → 产品事件） | 每族一个 projector |
| 租户、准入、资源台账、数据根所有权 | `kernel/agent_env.py`（渲染 models/auth/settings、argv、每 run 配置目录） | 每 harness 一个配置渲染器；**每 run 独立配置目录、凭据走文件不走 env 的模式照用** |
| `redaction.py` 流式脱敏（作用于文本） | `kernel/rpc.py` + `framing.py`（pi 双向 RPC） | 每族一个协议客户端；LF 分帧复用 |
| report.md 产物契约（prompt 约束） | `agent/`（pi settings、扩展、prompt 模板） | 每 harness 一份可版本化配置包 |
| `ExecutionAdapter` 契约（start / deliver_input / wait / cancel / recover_artifacts / close / identity） | 宿主 `/` 命令准入门禁、stopReason 词汇、会话文件复制续接 | 每 harness 各自的门禁/权限策略与续接能力声明 |

执行面（inproc / docker / e2b）与 harness 是**正交两维**：适配器数量是
「N 个 harness 族 × 2~3 个传输族」，不是全矩阵。

## 3. 目标架构：三个适配族

```
HarnessAdapter（抽象随 M1 落地）
├─ pi 族   — 现有 kernel/ 原样：rpc（双向 stdio NDJSON，inproc+docker 共用）、
│            json（单向流，e2b）；作为其它族的参照系与回归基线
├─ ACP 族  — 一次 ACP client（JSON-RPC over stdio）覆盖：
│            dsh 原生 acp server、claude-code-acp、codex-acp（@zed-industries/codex-acp）、
│            gemini --acp、opencode；适配器进程与 agent 同容器/沙箱，stdio 桥接
└─ SDK 直连族（可选，暂不建）— 如 claude-agent-sdk Python 进程内集成
```

ACP（Agent Client Protocol，Zed 主导的开放协议）关键能力（v1 schema 已核实）：
`initialize` 能力协商、`session/new`、`session/load`（带历史回放）、`session/resume`
（不回放，能力位 `sessionCapabilities.resume`）、`session/prompt`（返回 stopReason）、
`session/cancel`、`session/update` 通知（`agent_message_chunk` / `agent_thought_chunk` /
`tool_call` / `tool_call_update` / `plan` / `available_commands_update` /
`current_mode_update` / `usage_update`）、`session/request_permission`（agent → client）、
客户端方法 `fs/*` 与 `terminal/*`（默认全部不声明，agent 自行操作自身文件系统——
正好契合沙箱内运行拓扑）。

## 4. ACP 族设计要点

### 4.1 事件投影映射（ACP → 产品事件）

| ACP | 产品事件 | 语义备注 |
|---|---|---|
| `initialize` 响应（capabilities、agentInfo） | `session_ready` | 能力矩阵一并入 run 记录 |
| `agent_message_chunk` | `assistant_delta` | 流式正文 |
| `agent_thought_chunk` | `thinking_delta` | 思考折叠块 |
| `tool_call` / `tool_call_update`（status: pending→in_progress→completed/failed） | `tool_started` / `tool_updated` / `tool_finished` | kind/rawInput/rawOutput 入工具卡片 |
| `plan` | `run_stage` | 计划条目作为阶段行呈现 |
| `usage_update` | `usage_reported` | **语义换算**：ACP 是「上下文窗口占用 + 累计成本」视角，产品语义是「逐请求累计求和」；投影层如实换算并在 run 记录标注口径差异，不伪造逐请求 token 数 |
| `available_commands_update` | `kernel_status`（警告） | 见 §4.3 门禁 |
| `session/prompt` 返回 `stopReason` | 终态判定输入 | `end_turn`→正常；`max_tokens`≈ pi 的 `length`（finished 但标注交付不完整）；`refusal`→failed/model_error 类；`cancelled`→取消路径 |
| `session/request_permission` | 不投影为交互，自动应答（§4.2） | 应答结果记 `kernel_status` |

run 生命周期、终帧即终态、提交先于发布等全部沿用架构 §3/§4，不因 harness 改变。

### 4.2 权限自动应答（无人值守前提）

沙箱/无人场景没有人应答权限询问。默认 **fail-closed 全拒**；拒绝本身必须进入事件流
（`kernel_status`），不允许静默。M2 实测：dsh 0.1.5-rc.3 与 codex-acp 0.16.0 在
workspace-write 会话模式下跑完两个最小真实研究 run，均未发出权限请求，因此**保持默认、
不启用白名单**；配置级门控 `PI_GLINK_ACP_PERMISSION_ALLOW`（option kind，可选 tool kind）
已实现并默认空集。M4 首次真实启用：claude-code-acp 的研究 run 需要逐次放行 WebFetch 与
Write/Edit（它的权限请求不带 tool kind，宿主按同一 `toolCallId` 的 `tool_call` 通知解析），
最小集合为 `allow_once,read,search,fetch,edit`，execute/think/switch_mode/other 与未分类
请求继续拒绝；决定、机制与真实 run 证据见
[ACP 集成笔记](acp-integration-notes.md) §7.4。

### 4.3 准入与门禁

ACP prompt 是结构化 ContentBlock，不存在 pi 那样的「`/` 前缀先派发扩展命令」面；但
`available_commands_update` 表明 agent 可能自带 slash 命令——准入层对 ACP 族拒绝
以 `/` 开头的 prompt（同形复用现有门禁入口，允许清单为空集，从 harness 能力推导而非
硬编码模板目录）。M2 实测 codex-acp 确实在 `session/new` 后广播 6 条命令，准入拒绝不受
其影响。

### 4.4 续接（resume）

* pi 族：复制会话文件 + `--session`（现状，不动）。
* ACP 族：`session/resume`（能力位声明）；resume 依赖 agent 自身状态目录
  （`~/.claude`、`~/.codex`、dsh sessions）——inproc 面状态目录每 run 独立渲染；
  docker/e2b 面沿用「销毁前回收状态目录、追问时灌回新沙箱」的现有会话回收模式。
* 能力缺失时产品层显式拒绝追问（不冒充、不降级为静默新建）。

### 4.5 凭据与配置渲染

沿用 `agent_env.py` 模式：每 run 渲染独立配置目录，凭据写文件或注入命令级 env，
不进宿主进程环境，全部出站过 `redaction.py`。ACP 族的 M2 实现：
`PI_GLINK_ACP_CREDENTIAL_FILES` 把 `source=dest` 源文件**只读复制**进每 run HOME
（父目录 0700、文件 0600），`PI_GLINK_ACP_ENV` 注入显式 env（不允许覆盖 `HOME`），
注入值抽入该 run 的 redactor。已实测：codex-acp 读 `$HOME/.codex/auth.json`、dsh 读
`$HOME/.dsh/.credentials.yaml`（dsh harness home 默认 `$DSH_HOME` 或 `~/.dsh`）；两者都
尊重每 run `HOME`，真实用户目录未被修改。逐 agent 落点与证据见
[ACP 集成笔记](acp-integration-notes.md) §4.4。Docker 面沿用同一“只读复制进 run HOME”，
再在容器内对每个凭据文件加嵌套只读挂载（HOME 本身可写，agent 要写会话/缓存）；
`PI_GLINK_ACP_ENV` 在 docker 面按名字传给容器，值不进 argv（见集成笔记 §5.2）。E2B 面
没有宿主挂载，改为把同一份 run HOME 上传进沙箱状态根并在沙箱内重新施加 0700/0600，
`HOME` 指向该根、`PI_GLINK_ACP_ENV` 作为命令环境通道（值不进命令行；见集成笔记 §6.3）。
system prompt 的通道也已在 M2 定下：
作为 `session/prompt` 的首个围栏文本块送达（ACP v1 无 system role；见集成笔记 §4.3）。
M4 补上 claude-code-acp 的凭据形态：它的登录态在本机来自 `~/.claude/settings.json` 的
`env` 块（不是 Keychain 里的 OAuth），需把该 env 块抽成运行期 settings 经 CREDENTIAL_FILES
复制进每 run HOME；真实 settings.json 含 hooks/plugins，不得整文件复制。细节与证据见
[ACP 集成笔记](acp-integration-notes.md) §7.3。

## 5. 开源复用策略

| 项目 | 复用方式 | 约束 |
|---|---|---|
| ACP 适配器（claude-code-acp、@zed-industries/codex-acp、dsh acp） | 作为**钉版外部依赖**接入（npm 包/二进制），记录版本、来源、license | 版本钉死 + 冒烟 + 每 harness 一份集成笔记（沿用 pi-boundary 纪律）；适配器缺陷不改对方源码，反馈上游或换版本 |
| vibe-kanban（BloopAI，已停摆转社区维护） | **只读借鉴**：`crates/executors` 的 per-agent normalize 实现是事件归一化字典 | 不引入运行时依赖 |
| 官方 SDK（claude-agent-sdk Python、@openai/codex-sdk TS） | SDK 直连族的候选（暂不建）；其事件定义作为投影语义参照 | Python SDK 才可进程内；TS SDK 仅参考 |
| VibeKit / OpenHands / terminal-bench / gate4agent | 形态与传输矩阵的参照证据 | 不引入 |
| coder/agentapi | **排除**：PTY 屏幕解析机制违背本仓库事件契约底线 | — |

ACP 客户端本体用 Python 标准库实现（subprocess + json），不新增第三方依赖；
确需新增时论证并记录消费者。

## 6. 里程碑

| 里程碑 | 内容 | 验收 | 状态 |
|---|---|---|---|
| M1（GLINK-ACP-01） | ACP 族 inproc 骨架：ACP stdio JSON-RPC 客户端（复用 LF 分帧）、projector、harness 注册与配置级选择（默认仍为 pi，不动 API/前端）、run 全生命周期接入（受理/流式/取消/终态/产物回收）、fail-closed 权限应答、合成 fake ACP server 测试 | `bash scripts/check.sh` 全绿；合成测试覆盖 §4.1 映射、取消、stopReason 判定、权限拒绝、门禁；现有 pi 三面测试零回归 | 已完成（合成验证；实现口径与待实测项见 [ACP 集成笔记](acp-integration-notes.md)） |
| M2（GLINK-ACP-02） | 真实冒烟：`dsh --profile acp` 与 `codex-acp` 各跑一次真实研究 run（inproc）；system prompt 送达通道；per-agent 凭据注入（每 run HOME/env、0600、并入 redactor）；能力矩阵、usage 口径与权限决策回写 | 真实 run 报告产物 + 集成笔记；`bash scripts/check.sh` 全绿 | **已完成**（2026-09-25）：两侧握手与最小真实 run 均成功，fail-closed 默认无需白名单；实测细节见 [ACP 集成笔记](acp-integration-notes.md) §4，证据 `docs/evidence/acp-m2-*.txt` |
| M3a（GLINK-ACP-03） | Docker 执行面：`docker/Dockerfile.acp-worker`（自包含、ARG 钉版、镜像内自检）；`docker run -i` 双向 stdio 桥接复用 M1 客户端；每 run HOME 与只读凭据挂载；容器名/停止/对账复用 `docker_env` 约定；资源限制与降权与 pi 面对齐；解除 `harness=acp + executor=docker` 的启动拒绝 | `bash scripts/check.sh` 全绿；合成 plan/钉版边界回归；镜像自检 + 一次容器内最小真实 run（流式/终态/report.md/无容器残留） | **已完成**（2026-09-25）：docker 面可用，pi docker 面零回归；实测与钉版见 [ACP 集成笔记](acp-integration-notes.md) §5，证据 `docs/evidence/acp-m3-docker-*.txt` |
| M3b | e2b 执行面：双向 stdio（`stdin=True` + `send_stdin`，SDK 2.x 已具备）帧完整性实测先行；模板钉版；`harness=acp + executor=e2b` 的启动拒绝随之解除 | e2b smoke | **已完成**（2026-09-25）：帧完整性正面、dev 模板 `glink-acp-worker` 构建+自检、两 agent 沙箱内握手、dsh 最小真实 run（report.md 与沙箱按 id 无残留）；实测见 [ACP 集成笔记](acp-integration-notes.md) §6，证据 `docs/evidence/acp-m3-e2b-*.txt` |
| M4（GLINK-ACP-05） | claude-code-acp 接入（`@zed-industries/claude-code-acp@0.16.2`）；能力矩阵补齐；每 run 凭据与首启形态；权限最小白名单；镜像/dev 模板加 claude 双包钉版层 | inproc 握手 + 一次最小真实 run（流式/终态/report.md/清理）；容器与沙箱无模型握手自检；`bash scripts/check.sh` 全绿 | **已完成**（2026-09-25）：inproc 握手与真实 run 通过；docker 镜像重建后三个 agent 容器内握手（codex-acp 无凭据属预期）；e2b dev 模板重建后沙箱握手；docker/e2b 的真实模型 run 无 API key 属预期阻塞。详见 [ACP 集成笔记](acp-integration-notes.md) §7，证据 `docs/evidence/acp-m4-claude-*.txt` |

每个里程碑独立验收、独立提交边界；里程碑之间允许因实测结论回写本文。

### M1 落地偏差（相对 §3–§4；详细口径见 [ACP 集成笔记](acp-integration-notes.md)）

* **配置开关**：`PI_GLINK_HARNESS=pi|acp`（默认 `pi`）+ `PI_GLINK_ACP_COMMAND`；只在配置层，
  无 API/前端参数，`public_view()` 不暴露 harness。M1 落地时 ACP 只接 `inproc`；M3a 起
  `docker` 组合可用（镜像走独立的 `LOCAL_WEB_ACP_WORKER_IMAGE`，见 §5），`e2b` 组合继续在
  启动即拒绝（M3b）；M3b 起三个执行面（inproc / docker / e2b）都可用。
* **续接（§4.4）**：M1 不实现 ACP 的 `session/resume`；ACP run 不落会话文件，追问在准入层以
  `session_unavailable` 拒绝，适配器也不会以新会话冒充追问。
* **用量（§4.1）**：`usage_update` 保留 ACP 的「上下文窗口占用 + 累计成本」视角，投影为
  `used/size + cost` 并标注 `basis: "acp_usage_update"`；不做逐请求 token 求和，终态记录采用
  最后一个快照（替换而非累加）。
* **状态目录（§4.4/§4.5）**：inproc 面每 run 渲染 `runs/<id>/acp-agent/home` 作为子进程
  `HOME`，不注入任何宿主凭据；各 agent 是否还认专用变量（`CODEX_HOME` 等）与凭据注入点
  逐 harness 实测属 M2。
* **未投影的更新**：`user_message_chunk`、`current_mode_update`、`session_info_update` 等
  §4.1 未定义的变体不猜语义，按类型一次性记 `kernel_status` 提示；`max_turn_requests` 以原词
  进入 `stopReason`（前端目前只对 `length` 提示「交付不完整」）。
* **回合边界（§4.1 补充）**：ACP 没有 turn 更新，适配器在 `session/prompt` 前后补发合成的
  回合边界，投影为 `turn_started`/`turn_finished`，让流式文本/思考块按产品既有语义关闭与
  渲染；产品事件词表与前端契约不变。

## 7. 风险与纪律

* **ACP 能力参差**：`loadSession`/`resume`/`usage_update` 均为可选能力——以实测矩阵为准，
  能力缺失走显式降级，不伪造。
* **dsh 处于 developer preview**（官方明示 breaking changes）：钉版 + 集成笔记，同 pi 纪律。
* **事件语义鸿沟**（usage/stopReason/工具状态机）：产品事件语义由宿主定义，projector 换算；
  换算不了的如实标注，不塞进近似语义。
* **权限无人应答**：fail-closed 默认；任何放行白名单需逐 harness 论证并记录。
* **升级路径**：每个外部适配器/harness 的版本固定点与升级冒烟步骤，写进对应集成笔记，
  参照 [pi 边界](pi-boundary.md) §3 的三处固定模式。
* **E2B 传输与模板**：沙箱 stdio 是 `send_stdin` + `CommandHandle` 流（主机不可见容器级
  只读挂载），且 E2B 构建解析会改写 Dockerfile 文本（CMD 拼接、`\n` 转义）——模板必须
  CLI 构建 + 真实沙箱自检，帧完整性已有真实沙箱实测（集成笔记 §6.1/§6.2）。E2B 这条 SDK
  路径只暴露 lifetime，没有 cpus/memory 参数，宿主只声明它真的要求过的边界。
