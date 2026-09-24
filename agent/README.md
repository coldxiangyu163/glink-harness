# agent —— pi 侧配置模板（渲染到 `PI_CODING_AGENT_DIR`）

本目录是**模板**，不是 pi 的运行期目录：每个 run 开始时，宿主把这里的文件复制/渲染到该 run
自己的目录 `runs/<runId>/pi-agent/`（仓库外），再把 `PI_CODING_AGENT_DIR` 指向那个目录，于是
pi 读取的是这份配置，而不是用户 `~/.pi/agent` 的个人配置，也不是其他 run 的目录。副本写在
仓库外的原因：pi 会把运行期状态（`auth.json`、模型缓存等）写进配置目录，那些内容既不能进
版本库，也不该被一次 run 覆盖掉用户的本机配置或其他 run 的凭据。每个 run 的渲染都是「先删再拷」，
因此从模板里删掉的扩展/凭据不会作为旧文件残留。目的有两个：

1. **可复现**：本机、容器、E2B 三种执行面拿到同一份内核配置。
2. **可升级**：对 pi 的一切定制都落在这个目录（以及 `kernel/` 的协议消费层），内核包保持原样，升级 pi 只需要换版本号。

## 允许放什么

| 位置 | 用途 |
|---|---|
| `settings.json` | pi 的官方设置（模型、思考档位、工具开关等） |
| `AGENTS.md` | 目录级运行约定：工作区路径、报告产出位置、检索纪律 |
| `extensions/` | pi 扩展：自定义工具、provider、命令（官方 extension API） |
| `skills/` | 按需加载的能力包（官方 skill 机制） |
| `agents/` | pi-subagents 的 user 作用域子代理定义（`PI_CODING_AGENT_DIR/agents/*.md`，frontmatter + 正文）；当前只有 `researcher.md` |
| `prompts/` | 提示词模板 |

## 不允许放什么

- **不放置 pi 的源码或补丁。** 任何需要改内核才能实现的需求，先在 `docs/pi-boundary.md` 记录并作为设计决定提出，不得直接改内核。
- **不放置密钥。** 凭据走 pi 官方凭据机制：`auth.json` 由宿主渲染到本 run 的运行期目录
  `runs/<runId>/pi-agent/`（0600，仓库外），不进进程环境变量；本目录下的文件会进版本库。
  检索凭据同理，渲染为同一个运行期目录下的 `web-search.json`。
- **不放置宿主实现。** 宿主逻辑在 `host/pi_glink/`，这里只描述内核侧行为。

本目录当前包含：

| 文件 | 内容 |
|---|---|
| `settings.json` | `defaultThinkingLevel`、两个精确 pin 的包声明（`npm:pi-web-access@0.31.0`、`npm:pi-subagents@0.40.0`）与关闭的 watchdog 设置 `subagents.watchdog`。本机（inproc）面按此在 run 启动时安装到运行期配置目录；Docker/E2B 面由宿主渲染成镜像内本地包 `/opt/pi-packages/node_modules/<name>`（`docs/architecture.md` §5.3），不在 run 里联网安装 |
| `research-prompt.md` | 通用工作约定：任务分级、工具与取证纪律、深入任务纪律（由宿主以 `--append-system-prompt` 追加）。**注意**：委派纪律（任务 5）按 Lead 指令暂缓，本文件目前仍是“单 agent 执行”版约定，与已加载的 `subagent` 工具并存；待用户决定后再同步 |
| `prompts/deepresearch.md` | 显式 `/deepresearch <题目>` 入口：跳过「直接回答」分级，强制深入流程并收紧覆盖声明与核查 |
| `agents/researcher.md` | 取证子代理定义：自包含任务、一手来源优先、打开原文核对、产物落工作区约定文件名、只回报要点与来源；`tools` 白名单里没有委派工具，子代理不得再派 |
| `extensions/subagent-foreground-guard.ts` | 本仓自有的前台守卫扩展：官方 `tool_call` 钩子把 pi-subagents 的 `async` 改写为 `false`，无法安全改写的后台/定时/控制/管理入口与工作区外路径直接阻止（fail-closed）；清单见 `docs/pi-integration-notes.md` §10.1 |
| `extensions/subagent/config.json` | pi-subagents 自己读的 extension config（`<PI_CODING_AGENT_DIR>/extensions/subagent/config.json`）：`asyncByDefault`/`forceTopLevelAsync` false、`waitTool` 关、定时关、intercom 关、`maxSubagentDepth:1`、并发 4、单 run 派发 8、`artifactDir:"project"` |
| `README.md` | 本文件 |

### 工作约定与 `/deepresearch` 的关系

- `research-prompt.md` 每次 run 都由宿主追加到系统提示（本机/Docker 面 `kernel/runner.py::_extra_argv`，E2B 面 `kernel/e2b_env.py`），先要求对任务分级：闲聊与单点事实直接回答（必要时一两次检索并给出来源，不写计划与报告）；只有跨来源比较、专业分析、多子问题或用户明确要求报告时才进入深入流程，交付 `report.md`、就近引注、来源分层、覆盖声明与自我核查。
- `prompts/deepresearch.md` 是 pi 的 prompt template：**文件名就是命令名**（pi 官方 `docs/prompt-templates.md`），用户在工作台提问框以 `/deepresearch <题目>` 开头发送时，pi 在把消息交给模型之前展开模板，`$@` 替换为题目内容。它强制走深入流程，并只叠加四条更严格的要求（必须交付报告、覆盖声明逐项给原因、核查留痕、二次来源收紧），不重复通用约定，也没有"等用户确认"的确认门：展开后直接执行。
- 两者都随模板渲染进每个 run 的 `runs/<runId>/pi-agent/`：`TEMPLATE_ENTRIES`（`kernel/agent_env.py`）包含 `prompts`，pi 从 `PI_CODING_AGENT_DIR` 这个个人配置目录加载模板与追加提示。

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
- **未覆盖**：pi-subagents 注册的扩展命令（`/run --bg`、`/subagents-detach` 等）由用户输入触发，
  `tool_call` 钩子不可达，详见 §10.4 的已知未覆盖项。

`models.json`、`auth.json` 与 `web-search.json` 不在本目录：它们含运行期值/凭据，由宿主渲染到
每个 run 自己的 `runs/<runId>/pi-agent/`（见 `host/pi_glink/kernel/agent_env.py`）。本目录里的
文件都是常量，渲染时原样复制（先删再拷）。
