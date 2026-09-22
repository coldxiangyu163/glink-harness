# agent —— pi 侧配置模板（渲染到 `PI_CODING_AGENT_DIR`）

本目录是**模板**，不是 pi 的运行期目录：每次启动 run 时，宿主把这里的文件复制/渲染到
`<data_root>/pi-agent/`（默认 `~/.pi-glink/pi-agent/`，仓库外），再把 `PI_CODING_AGENT_DIR`
指向运行期目录，于是 pi 读取的是这份配置，而不是用户 `~/.pi/agent` 的个人配置。
副本写在仓库外的原因：pi 会把运行期状态（`auth.json`、模型缓存等）写进配置目录，
那些内容既不能进版本库，也不该被一次 run 覆盖掉用户的本机配置。目的有两个：

1. **可复现**：本机、容器、E2B 三种执行面拿到同一份内核配置。
2. **可升级**：对 pi 的一切定制都落在这个目录（以及 `kernel/` 的协议消费层），内核包保持原样，升级 pi 只需要换版本号。

## 允许放什么

| 位置 | 用途 |
|---|---|
| `settings.json` | pi 的官方设置（模型、思考档位、工具开关等） |
| `AGENTS.md` | 研究 agent 的运行约定：工作区路径、报告产出位置、检索纪律 |
| `extensions/` | pi 扩展：自定义工具、provider、命令（官方 extension API） |
| `skills/` | 按需加载的能力包（官方 skill 机制） |
| `prompts/` | 提示词模板 |

## 不允许放什么

- **不放置 pi 的源码或补丁。** 任何需要改内核才能实现的需求，先在 `docs/pi-boundary.md` 记录并作为设计决定提出，不得直接改内核。
- **不放置密钥。** 凭据走 pi 官方凭据机制：`auth.json` 由宿主渲染到运行期目录
  `<data_root>/pi-agent/`（0600，仓库外），不进进程环境变量；本目录下的文件会进版本库。
  检索凭据同理，渲染为运行期目录下的 `web-search.json`。
- **不放置宿主实现。** 宿主逻辑在 `host/pi_glink/`，这里只描述内核侧行为。

本目录当前包含：

| 文件 | 内容 |
|---|---|
| `settings.json` | `defaultThinkingLevel` 与 `packages: ["npm:pi-web-access"]` |
| `research-prompt.md` | 研究执行约定（由宿主以 `--append-system-prompt` 追加） |
| `extensions/` | 扩展位（当前为空；新需求优先落在这里） |
| `README.md` | 本文件 |

`models.json`、`auth.json` 与 `web-search.json` 不在本目录：它们含运行期值/凭据，由宿主渲染到
`<data_root>/pi-agent/`（见 `host/pi_glink/kernel/agent_env.py`）。本目录里的文件都是常量，
渲染时原样复制。
