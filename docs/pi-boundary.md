# pi 边界契约

本仓库把 pi 当**外部内核**使用：内核包、内核源码、内核内部接口一律不动；一切定制都落在
pi 官方文档化的扩展点、环境变量与 headless 协议上。本文是这份契约的清单，每条都给出
本仓库里的实际例子，并给出可执行的交叉核验命令（见 §4）。

> 实测事实与踩坑记录集中在 [pi-integration-notes.md](pi-integration-notes.md)；
> 进程与事件结构见 [architecture.md](architecture.md)。

## 1. 允许的扩展点

| 扩展点 | 文档 | 本仓库的用法（实际文件） |
|---|---|---|
| `settings.json` | docs/settings.md | `agent/settings.json`：`defaultThinkingLevel: "medium"`、`packages: ["npm:pi-web-access@0.31.0", "npm:pi-subagents@0.40.0"]`（两个精确 pin；docker/e2b 面渲染时改写为镜像内本地包路径，见 §3）、`subagents.watchdog` 关闭（pi-subagents 从这个键读 watchdog，见 pi-integration-notes.md §10.2） |
| `models.json`（自定义 provider） | docs/models.md | 宿主渲染到每个 run 自己的运行期目录（`kernel/agent_env.py::_models_document`）：`baseUrl` 与模型 id 字面量、**不含 `apiKey`**（凭据写运行期 `auth.json`）、`compat.thinkingFormat: "openrouter"`；**不写 `cost` 价格表**，因为宿主不知道操作员费率，伪造价格会让 pi 报出假计费 |
| packages | docs/packages.md | `npm:pi-web-access@0.31.0`（检索能力全部来自它；inproc 运行期安装，docker/e2b 从 `/opt/pi-packages` 本地加载）；`npm:pi-subagents@0.40.0`（自 GLINK-SUB-02 加载：前台强制靠本仓自有 `tool_call` 扩展，不靠包开关，见 pi-integration-notes.md §10） |
| extensions | docs/extensions.md | `agent/extensions/subagent-foreground-guard.ts`：官方 `tool_call` 钩子把 pi-subagents 的派发改写为前台同步、其余后台/定时/控制入口阻止（含 fail-closed）；`agent/extensions/subagent/config.json` 是同包读取的最保守 extension config |
| skills | docs/skills.md | 目录预留（`agent/skills/`） |
| prompt templates | docs/prompt-templates.md | `agent/prompts/deepresearch.md`：`/deepresearch <题目>` 显式入口，强制深入流程并叠加更严格的覆盖声明、核查留痕与二次来源收紧；通用工作约定仍走 `--append-system-prompt` |
| `AGENTS.md` | docs/quickstart.md 等 | 目录预留（`agent/AGENTS.md`），当前约定走 §下一条 |
| `--append-system-prompt` | `pi --help` | `agent/research-prompt.md`（最小工作约定：产品契约 + 可选委派；方法论在 `/deepresearch`） → `kernel/runner.py::_extra_argv`，内核系统提示词本身不改 |
| headless 协议 | docs/rpc.md、docs/json.md | `--mode rpc`（本机 / Docker 面，`kernel/rpc.py`）；`--mode json`（E2B 面，`kernel/e2b_env.py`）；两面都只按 LF 分帧（`kernel/framing.py`，共享语料 `tests/fixtures/lf-framing.json`） |
| 环境变量 | docs/environment-variables.md | `PI_CODING_AGENT_DIR`、`PI_CODING_AGENT_SESSION_DIR`、`PI_SKIP_VERSION_CHECK`、`PI_TELEMETRY`（模型凭据不经环境变量，写运行期 `auth.json`；两个目录都是每 run 独立） |
| CLI 参数 | docs/cli.md、docs/sessions.md | `--provider`、`--model`、`--name`；追问用 `--session <path>` 打开上一轮会话的独立副本（`kernel/agent_env.py::build_pi_argv`、`kernel/runner.py`、`kernel/e2b_env.py`） |
| MCP（可选） | docs/mcp 相关 | 尚未使用；接入方式与 extensions 同级，属允许范围 |

**判断新需求是否越界的顺序**：先看有没有官方扩展点能表达 → 没有则在 `extensions/` 写扩展
→ 仍不行则把需求作为设计决定提出讨论，**而不是改内核**。

## 2. 禁止项

| 禁止 | 为什么 | 核验方式（§4 有命令） |
|---|---|---|
| fork / patch pi 源码 | 升级会失去可比性，且违背 objective | 仓库内无 pi 源码或补丁文件 |
| vendor pi 包（复制 dist/src 进仓库） | 同上，并掩盖版本事实 | 仓库内不存在 pi 的构建产物 |
| import pi 的内部模块/私有路径 | 未文档化接口随版本变化 | 宿主与前端不引用 node_modules 内的 pi 路径 |
| 依赖未文档化行为 | 同上；实测结论必须来自官方文档或可复现实验 | `docs/pi-integration-notes.md` 每条都标注来源 |
| 修改用户全局 `~/.pi` | 会污染用户既有环境 | 宿主总是重定向 `PI_CODING_AGENT_DIR`；只**读**用户的 `web-search.json` |
| 把密钥写进仓库 | 安全事故 | `.gitignore` + 密钥扫描 |
| 容器/沙箱依赖宿主运行时 | 隔离面会失去意义 | 镜像与模板自包含（`docker/Dockerfile.worker`、`docker/e2b/Dockerfile.e2b`） |
| 前端解析终端输出 | 事件契约的基础前提 | `tests/web/run-stream.test.mjs` 的断言 |
| 凭据不经脱敏边界进入产品面 | 工具回显/env dump 会把 key 带进事件流、报告与会话文件 | 所有出站出口均过 `redaction.py`（含跨 delta 暂存）；`scripts/check-no-secrets.py` 扫描运行态产物；`tests/test_redaction.py` |
| 把宿主整份环境变量交给 pi | 会把操作员 shell 里的其他凭据一并带入 | `kernel/agent_env.py` 的白名单 `_PASSTHROUGH_ENV` |

## 3. 升级路径

pi 的版本在三处被固定，升级只改这三处 + 回归：

| 位置 | 事实 |
|---|---|
| `docker/Dockerfile.worker` | `ARG PI_VERSION=0.86.1`（镜像内 pi 版本）；`FROM node:24.21.0-bookworm-slim`（Node 精确 tag）；`ARG PI_WEB_ACCESS_VERSION` / `ARG PI_SUBAGENTS_VERSION` 把包装到 `/opt/pi-packages` |
| `docker/e2b/Dockerfile.e2b` | 同一 `ARG PI_VERSION`；同一 Node 精确 tag（E2B 模板内 pi / Node 版本）；同一包 ARG 与 `/opt/pi-packages` 布局 |
| `agent/settings.json` | 包声明的真源：`npm:pi-web-access@0.31.0` 与 `npm:pi-subagents@0.40.0`。容器面的 `npm:` 改写为 `/opt/pi-packages/node_modules/<name>` 本地包；两者一致性与 Dockerfile ARG 由 `tests/test_repository_boundaries.py` 卡住 |
| 本机 inproc 面 | 使用宿主安装的 pi；`PI_GLINK_PI_BIN` 可指向任意版本做隔离演练 |

步骤：

1. 在**隔离目录**装目标版本（不动用户全局 pi）：
   ```bash
   npm install --prefix /tmp/pi-upgrade @earendil-works/pi-coding-agent@<version>
   ```
2. **用目标版本的 pi 启动宿主**（`PI_GLINK_PI_BIN` 是宿主启动配置；smoke 脚本只发 HTTP 请求，不读它），然后对新宿主跑冒烟：
   ```bash
   PI_GLINK_PI_BIN=/tmp/pi-upgrade/node_modules/.bin/pi \
     LOCAL_WEB_PORT=7864 .venv/bin/python -m pi_glink.app &

   .venv/bin/python scripts/smoke-local-run.py --base-url http://127.0.0.1:7864 --prompt "…"
   ```
   验证证据：宿主启动横幅会打印实际使用的 pi 路径；也可用 `ps` 确认子进程来自隔离目录。
3. 若通过 → 更新 Dockerfile 的 `ARG PI_VERSION`、重建镜像与 E2B 模板、重跑三面 smoke；
4. 若失败 → 把差异记进 `docs/pi-integration-notes.md`（哪条假设变了），再决定适配或暂缓；
   适配也只允许落在 §1 的扩展点里。

兼容性判据（回归已覆盖）：

* 协议分帧（仅 LF、`id` 关联、错误帧形式）—— `tests/test_kernel_rpc.py`；
* 事件类型与投影 —— `tests/test_kernel_rpc.py`、`tests/web/run-stream.test.mjs`；
* 配置解析（`PI_CODING_AGENT_DIR` 重定向、`models.json` 生成）—— `tests/test_kernel_agent_env.py`；
* 三面运行命令与凭据流 —— 各面的 smoke 脚本与 `docs/evidence/`。

## 4. 交叉核验命令

```bash
# ① 仓库内没有 pi 源码 / TS 源码副本
find . -path ./.venv -prune -o -path ./node_modules -prune -o -name "*.ts" -print | head

# ② 只通过官方 headless 协议启动 pi
grep -rn '"--mode"' host/pi_glink/kernel/ | sed 's/^/  /'

# ③ 宿主/前端/扩展不 import pi 的内部模块（普通扩展和 .ts 也扫）
grep -rn "earendil-works" host/ web/ agent/ --include="*.py" --include="*.mjs" --include="*.ts" | grep -v "^Binary" | sed 's/^/  /'

# ④ 密钥不入库（应当是空输出）
git grep -nE "(sk-|exa-)[A-Za-z0-9_-]{16,}" -- . ':!*.example' ':!*.txt'

# ⑤ 运行期凭据文件权限为 600 且不在仓库内（每次 run 一个目录）
find ~/.pi-glink/runs -path '*/pi-agent/web-search.json' -maxdepth 4 -exec ls -l {} \;

# ⑥ 前端无打包依赖树
ls web/node_modules 2>/dev/null || echo "无前端依赖树（原生 ESM）"
```

核验结果（2026-09-22 实测）：

* ① 空输出 —— 仓库内没有 pi（或任何）TypeScript 源码副本；
* ② 只出现 `--mode rpc`（`agent_env.py`、`docker_env.py`）与 `--mode json`（`e2b_env.py`）；
* ③ 空输出 —— 宿主与前端连包名都不引用（注释里也不用它作为标识）；
* ④ 空输出 —— 无密钥入库（`docs/evidence/` 已排除，因为日志里可能出现用户级别的占位字符串）；
* ⑤ `-rw-------`（600）—— 每个 run 的运行期凭据文件权限正确；（⑥ 无前端依赖树。（①③④ 的空输出是预期结果。

本次整改的核验口径变化：原 ⑤ 命令指向共享目录 `~/.pi-glink/pi-agent/`，该目录已不再是运行期配置
（每个 run 使用 `runs/<id>/pi-agent/`），命令已同步更新。

GLINK-SUB-02（2026-09-24）复跑：

* ① 现在会输出本仓自有的两个 `.ts`（`agent/extensions/subagent-foreground-guard.ts`、
  `tests/extensions/probe-tool-inventory.ts`）——它们是本仓的 pi 扩展与测试探针（pi 官方加载形态
  就是 TypeScript），不是 pi 源码副本；命令要排除 pi 自带产物时仍以仓库内没有 `dist/`/`src/`
  副本为准。
* ③ 把扫描范围扩到 `agent/` 与 `.ts` 后只命中一条：`agent/extensions/subagent-foreground-guard.ts:34`
  的 `import type { ExtensionAPI } from "@earendil-works/pi-coding-agent"`——官方文档要求的公共包
  类型导入，没有 `src/`/`dist/` 私有路径（`tests/test_repository_boundaries.py` 另用反例卡住私有路径）。
* ④ 的历史记录已过期：合成 fixture（`tests/fixtures/accounts_server.py`、若干 `tests/test_*.py`）
  里的 `sk-alpha-…` 占位值会被这条 grep 命中；仓库边界测试按“测试 fixture 中的合成样例可保留”
  处理（`tests/test_repository_boundaries.py::test_examples_and_synthetic_json_fixtures_are_allowed`），
  本任务未改动该口径。

## 5. 已知的内核侧限制（不改内核，只记录）

| 限制 | 影响 | 我们怎么做 |
|---|---|---|
| `models.json` 的 `baseUrl` / 模型 id 不支持 `$VAR` 插值 | 无法把 endpoint 交给 pi 自己解析 | 宿主渲染本 run 的运行期 `models.json`（`agent_env.py`） |
| pi 会往配置目录写运行时状态（`auth.json` / `models-store.json`） | 仓库目录会被污染 | 仓库 `agent/` 只作模板；每次 run 渲染到 `runs/<id>/pi-agent/`（先删再拷，凭据缺失则删除旧文件）；`.gitignore` 兜底 |
| E2B 后台命令的流式接口是 `CommandHandle`，不是 `commands.run()` 上的回调 | 沿用旧结论会让该面退化为结束后回放 | 命令 stdout `tee` 到 JSONL，宿主迭代 handle 实时投递；断流按文件补齐（`e2b_env.py`，见 architecture.md §5.2） |
| E2B 会话文件不会自动留存 | 未回收则不能追问 | 销毁前回收单个有界 `.jsonl` 到本 run 专属目录并脱敏；失败时明确拒绝追问 |
| 模型 key 以文件形式位于本 run 的运行期 `auth.json`（不在进程环境） | 工具可用 `read` 打开并回显 | `env` 类读取已不可行（三面实测 0）；回显被出站脱敏覆盖（含跨 delta 分割，见 architecture.md §3.3/§6.1）；更强隔离用 Docker / E2B 面 |
| pi-subagents 的扩展命令（`/run --bg`、`/chain --bg`、`/subagents-detach` 等）由用户输入直接执行，且先于 pi 的 `input` 事件派发 | `tool_call` 钩子不可达；载入 pi-subagents 后，客户端可控的 prompt 一旦以 `/` 开头就会先执行这些命令，发起不随主 run 取消/超时收口的后台进程 | 模型工具面用 `agent/extensions/subagent-foreground-guard.ts` 强制前台（pi-integration-notes.md §10）；客户端输入面由宿主在**准入层**加 `/` 命令门禁：只接受 `agent/prompts/` 模板名，其余 `400 unsupported_command`（architecture.md §4，回归 `tests/test_runs_api.py` / `tests/test_runner.py`） |
