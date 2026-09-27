# harnesses/pi —— pi 包（配置模板 + 扩展本体 + 能力提示）

本目录是一个 **harness 包**：`harness.toml` 声明 `family = "pi"`，它是选择依据（`PI_GLINK_HARNESS=pi`，默认）；其余目录内容是 pi 的**模板**，不是 pi 的运行期目录。

每个 run 开始时，宿主把这里的文件复制/渲染到该 run 自己的目录 `runs/<runId>/pi-agent/`
（仓库外），再把 `PI_CODING_AGENT_DIR` 指向那个目录，于是 pi 读取的是这份配置，而不是用户
`~/.pi/agent` 的个人配置，也不是其他 run 的目录。副本写在仓库外的原因：pi 会把运行期状态
（`auth.json`、模型缓存等）写进配置目录，那些内容既不能进版本库，也不该被一次 run 覆盖掉
用户的本机配置或其他 run 的凭据。每个 run 的渲染都是「先删再拷」，因此从模板里删掉的扩展/
凭据不会作为旧文件残留。目的有两个：

1. **可复现**：本机、容器、E2B 三种执行面拿到同一份内核配置。
2. **可升级**：对 pi 的一切定制都落在这个包（以及 `kernel/` 的协议消费层），内核包保持原样，升级 pi 只需要换版本号。

切换 harness 只改包名（`PI_GLINK_HARNESS=dsh|codex|opencode`）；宿主、事件契约、HTTP/SSE 与前端不随包改变（见 [多 harness 计划](../../docs/multi-harness-plan.md) §3）。

## 允许放什么

| 位置 | 用途 |
|---|---|
| `harness.toml` | 包清单：`format`/`name`/`family`（fail-closed 解析，未知键拒绝） |
| `capabilities.md` | 能力提示：拼进系统提示（工作约定之后、蓝图之前）；只写已知成立的事实 |
| `settings.json` | pi 的官方设置（模型、思考档位、工具开关等） |
| `AGENTS.md` | 目录级运行约定：工作区路径、报告产出位置、检索纪律 |
| `extensions/` | pi 扩展：自定义工具、provider、命令（官方 extension API） |
| `skills/` | 按需加载的能力包（官方 skill 机制） |
| `agents/` | pi-subagents 的 user 作用域子代理定义（`PI_CODING_AGENT_DIR/agents/*.md`，frontmatter + 正文）；当前只有 `researcher.md` |

## 不允许放什么

- **不放置 pi 的源码或补丁。** 任何需要改内核才能实现的需求，先在 `docs/pi-boundary.md` 记录并作为设计决定提出，不得直接改内核。
- **不放置密钥。** 凭据走 pi 官方凭据机制：`auth.json` 由宿主渲染到本 run 的运行期目录
  `runs/<runId>/pi-agent/`（0600，仓库外），不进进程环境变量；本目录下的文件会进版本库。
  检索凭据同理，渲染为同一个运行期目录下的 `web-search.json`。
- **不放置宿主实现。** 宿主逻辑在 `host/pi_glink/`，这里只描述内核侧行为。

本目录当前包含：

| 文件 | 内容 |
|---|---|
| `harness.toml` | 包清单：`format = "pi-glink-harness/v1"`、`name = "pi"`、`family = "pi"`；pi 的版本不在清单里（见 `docker/Dockerfile.worker` 的 `ARG PI_VERSION` 与 `settings.json`） |
| `capabilities.md` | pi 包能力提示（拼进系统提示，不落进运行期目录）：`subagent` / `researcher` 委派与子代理结论亲自核实 |
| `settings.json` | `defaultThinkingLevel`、两个精确 pin 的包声明（`npm:pi-web-access@0.31.0`、`npm:pi-subagents@0.40.0`）与关闭的 watchdog 设置 `subagents.watchdog`。本机（inproc）面按此在 run 启动时安装到运行期配置目录；Docker/E2B 面由宿主渲染成镜像内本地包 `/opt/pi-packages/node_modules/<name>`（`docs/architecture.md` §5.3），不在 run 里联网安装 |
| `agents/researcher.md` | 取证子代理定义：自包含任务、一手来源优先、打开原文核对、产物落工作区约定文件名、只回报要点与来源；`tools` 白名单里没有委派工具，子代理不得再派 |
| `extensions/subagent-foreground-guard.ts` | 本仓自有的前台守卫扩展：官方 `tool_call` 钩子把 pi-subagents 的 `async` 改写为 `false`，无法安全改写的后台/定时/控制/管理入口与工作区外路径直接阻止（fail-closed）；清单见 `docs/pi-integration-notes.md` §10.1 |
| `extensions/subagent/config.json` | pi-subagents 自己读的 extension config（`<PI_CODING_AGENT_DIR>/extensions/subagent/config.json`）：`asyncByDefault`/`forceTopLevelAsync` false、`waitTool` 关、定时关、intercom 关、`maxSubagentDepth:1`、并发 4、单 run 派发 8、`artifactDir:"session"`（产物落在会话目录的 `subagent-artifacts/`，随宿主回收与脱敏，不写工作区隐藏目录） |
| `README.md` | 本文件 |

### 工作约定、能力提示与蓝图的关系

- 工作约定（`methodology/work-agreement.md`）与方法论蓝图（`methodology/blueprints/*.md`）是 **harness 无关**的宿主级内容，每次 run 由宿主拼进系统提示（本机/Docker 面 `runs/runner.py::_extra_argv`，E2B 面 `kernel/e2b_env.py`）。它们是产品契约，不是研究流程（2026-09-24 用户决定，参照 dsh 的最小提示词）：计划、检索策略、是否委派、如何修订交给模型自主决定。
- `capabilities.md` 是 pi 包的能力提示（GLINK-ARCH-6 起从工作约定移入）：`subagent` / `researcher` 是 pi 包的工具面，不属于 harness 无关的产品契约。系统提示的固定顺序是「工作约定 → 能力提示 → 蓝图渲染」。
- `/<蓝图名> <题目>` 由宿主的准入层解释为 `blueprint=<蓝图名>` + `prompt=<题目>`（蓝图名从 `methodology/blueprints/` 推导；其余 `/` 开头输入一律拒绝）。GLINK-ARCH-6 删除了 pi 的 `prompts/deepresearch.md` 模板，方法论只剩蓝图这一份真源，`/deepresearch` 的名字保持不变。
- pi 的模板条目（`TEMPLATE_ENTRIES`：`settings.json`/`AGENTS.md`/`agents`/`extensions`/`skills`）渲染进每个 run 的 `runs/<runId>/pi-agent/`；工作约定、能力提示与蓝图渲染不入运行期目录。

### 子代理派发（pi-subagents）

- **定义位置**：pi-subagents 从 `<PI_CODING_AGENT_DIR>/agents` 读 user 作用域定义（`discoverAgents`），
  所以 `TEMPLATE_ENTRIES` 里必须有 `agents`；本仓的 `researcher` 会覆盖包内建的同名定义。
- **前台强制**：包本身没有禁用 async 的开关（GLINK-SUB-01 结论），强制前台由
  `extensions/subagent-foreground-guard.ts` 经官方 `tool_call` 钩子完成，并配
  `extensions/subagent/config.json` 的最保守取值；两者的入口清单与逐项理由见
  `docs/pi-integration-notes.md` §10。新增或调整派发入口时，两边要一起改，并补
  `tests/extensions/subagent-foreground-guard.test.mjs` 的正反例。
- **不得再派**：`researcher.md` 的 `tools` 白名单不含委派工具（子进程拿到 `--tools`），
  `maxSubagentDepth: 1` 再兵一层。
- **命令面**：pi-subagents 注册的扩展命令（`/run --bg`、`/subagents-detach` 等）由用户输入触发，
  `tool_call` 钩子不可达；宿主在准入层用 `/` 命令门禁挡住（只接受 `/<蓝图名> <题目>`，
  其余 `400 unsupported_command`，见 architecture.md §4 与 pi-integration-notes.md §10.4）。

`models.json`、`auth.json` 与 `web-search.json` 不在本目录：它们含运行期值/凭据，由宿主渲染到
每个 run 自己的 `runs/<runId>/pi-agent/`（见 `host/pi_glink/kernel/agent_env.py`）。本目录里的
文件都是常量，渲染时原样复制（先删再拷）。
