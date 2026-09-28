# pi 边界契约

本仓库把 pi 当**外部内核**使用：内核包、内核源码、内核内部接口一律不动；一切定制都落在
pi 官方文档化的扩展点、环境变量与 headless 协议上。本文是这份契约的清单，每条都给出
本仓库里的实际例子，并给出可执行的交叉核验命令（见 §4）。

> 实测事实与踩坑记录集中在 [pi-integration-notes.md](pi-integration-notes.md)；
> 进程与事件结构见 [architecture.md](architecture.md)。

## 1. 允许的扩展点

| 扩展点 | 文档 | 本仓库的用法（实际文件） |
|---|---|---|
| `settings.json` | docs/settings.md | `harnesses/pi/settings.json`：`defaultThinkingLevel: "medium"`、`packages: ["npm:pi-web-access@0.31.0", "npm:pi-subagents@0.40.0"]`（两个精确 pin；docker/e2b 面渲染时改写为镜像内本地包路径，见 §3）、`subagents.watchdog` 关闭（pi-subagents 从这个键读 watchdog，见 pi-integration-notes.md §10.2） |
| `models.json`（自定义 provider） | docs/models.md | 宿主渲染到每个 run 自己的运行期目录（`kernel/agent_env.py::_models_document`）：`baseUrl` 与模型 id 字面量、**不含 `apiKey`**；`baseUrl` 是宿主凭据出口代理的 `/model` 地址，运行期 `auth.json` 只含本 run 的代理 token（ADR 0008）、`compat.thinkingFormat: "openrouter"`；**不写 `cost` 价格表**，因为宿主不知道操作员费率，伪造价格会让 pi 报出假计费 |
| packages | docs/packages.md | `npm:pi-web-access@0.31.0`（检索能力全部来自它；inproc 运行期安装，docker/e2b 从 `/opt/pi-packages` 本地加载）；`npm:pi-subagents@0.40.0`（自 GLINK-SUB-02 加载：前台强制靠本仓自有 `tool_call` 扩展，不靠包开关，见 pi-integration-notes.md §10） |
| extensions | docs/extensions.md | `harnesses/pi/extensions/subagent-foreground-guard.ts`：官方 `tool_call` 钩子把 pi-subagents 的派发改写为前台同步、其余后台/定时/控制入口阻止（含 fail-closed）；`harnesses/pi/extensions/subagent/config.json` 是同包读取的最保守 extension config |
| skills | docs/skills.md | 目录预留（`harnesses/pi/skills/`） |
| prompt templates | docs/prompt-templates.md | **本仓已不使用**（GLINK-ARCH-6）：方法论在 `methodology/`（`work-agreement.md` + `blueprints/*.md`），`/<蓝图名> <题目>` 由宿主解释为蓝图请求，不再依赖 pi 的模板展开 |
| `AGENTS.md` | docs/quickstart.md 等 | 目录预留（`harnesses/pi/AGENTS.md`），当前约定走 §下一条 |
| `--append-system-prompt` | `pi --help` | `methodology/work-agreement.md`（最小工作约定：产品契约）+ 选中包的 `capabilities.md`（如 pi 的 `subagent`/`researcher` 委派）+ 蓝图渲染 → `runs/stages.py::PrepareStage.extra_argv`，内核系统提示词本身不改 |
| headless 协议 | docs/rpc.md | 三面均 `--mode rpc`（共享 `kernel/rpc.py::PiRpcClient`；本机直接 spawn，Docker 跨容器 stdio，E2B 经沙箱后台命令 `stdin=True` + `SandboxCommandProcess` 以 `popen=` 注入，GLINK-ARCH-3 收敛）；三面都只按 LF 分帧（`kernel/framing.py`，共享语料 `tests/fixtures/lf-framing.json`） |
| 环境变量 | docs/environment-variables.md | `PI_CODING_AGENT_DIR`、`PI_CODING_AGENT_SESSION_DIR`、`PI_SKIP_VERSION_CHECK`、`PI_TELEMETRY`（模型凭据不经环境变量；运行期 `auth.json` 只含出口代理 token；两个目录都是每 run 独立） |
| CLI 参数 | docs/cli.md、docs/sessions.md | `--provider`、`--model`、`--name`；追问用 `--session <path>` 打开上一轮会话的独立副本（`kernel/agent_env.py::build_pi_argv`、`runs/runner.py`、`kernel/e2b_env.py`） |
| MCP（可选） | docs/mcp 相关 | 尚未使用；接入方式与 extensions 同级，属允许范围 |

**判断新需求是否越界的顺序**：先看有没有官方扩展点能表达 → 没有则在 `extensions/` 写扩展
→ 仍不行则把需求作为设计决定提出讨论，**而不是改内核**。

## 2. 禁止项

| 禁止 | 为什么 | 核验方式（§4 有命令） |
|---|---|---|
| fork / patch pi 源码 | 升级会失去可比性，且违背 objective | 仓库内无 pi 源码或补丁文件 |
| vendor pi 包（复制 dist/src 进仓库） | 同上，并掩盖版本事实 | 仓库内不存在 pi 的构建产物 |
| import pi 的内部模块/私有路径 | 未文档化接口随版本变化 | 宿主与前端不引用 node_modules 内的 pi 路径 |
| 依赖未文档化行为 | 同上；实测结论必须来自官方文档或可复现实验（读私有落盘格式的有界例外见下「私有格式观察者例外」） | `docs/pi-integration-notes.md` 每条都标注来源 |
| 修改用户全局 `~/.pi` | 会污染用户既有环境 | 宿主总是重定向 `PI_CODING_AGENT_DIR`；不再读取用户的 `web-search.json`（ADR 0008） |
| 把密钥写进仓库 | 安全事故 | `.gitignore` + 密钥扫描 |
| 容器/沙箱依赖宿主运行时 | 隔离面会失去意义 | 镜像与模板自包含（`docker/Dockerfile.worker`、`docker/e2b/Dockerfile.e2b`） |
| 前端解析终端输出 | 事件契约的基础前提 | `tests/web/run-stream.test.mjs` 的断言 |
| 凭据不经脱敏边界进入产品面 | 工具回显/env dump 会把 key 带进事件流、报告与会话文件 | 所有出站出口均过 `redaction.py`（含跨 delta 暂存）；`scripts/check-no-secrets.py` 扫描运行态产物（run 数据库按列经 SQLite 读取）；`tests/test_redaction.py`、`tests/test_check_no_secrets.py` |
| 把宿主整份环境变量交给 pi | 会把操作员 shell 里的其他凭据一并带入 | `kernel/agent_env.py` 的白名单 `_PASSTHROUGH_ENV` |

### 私有格式观察者例外

§1 与 [architecture.md](architecture.md) §1 的「不依赖未文档化接口」约束的是内核**协议与
控制面**：启动、协议帧、事件、终态与报告仍只用文档化接口。以下两处读**私有落盘格式**做
可观测性，是本节钉死的有界例外：

* GLINK-SUB-R2 读 pi-subagents 包写在会话目录下的 `subagent-artifacts/` 私有布局
  （`_meta.json`、transcript 与产物命名；见 architecture.md §3.5「R2 增强与关联」）；
* GLINK-SUB-R3a/R3b 读 dsh 私有 zstd 会话格式（关联链与进度帧；见 architecture.md §3.5
  「运行中进度」与 `docs/evidence/acp-subagent-zstd-spike.txt`）。

例外成立的纪律（三条缺一不可）：

1. **只读观察**：宿主从不写这些文件，也从不在内核控制路径（启动/协议/事件/终态/报告）
   依赖它们；run 生命周期与权威状态不以其为事实来源，观察者失败只损失子代理进度呈现，
   不回填 `child.json`。
2. **精确 pin + pin 测试**：包/格式由 `harnesses/pi/settings.json` 与 Dockerfile 精确 pin，pin
   测试钉住所读布局/格式；上游升级导致 pin 变红时观察者**诚实降级**（child 显示普通
   「进行中」，不伪造进度），修通后才允许滚动升级。
3. **不承重正确性**：有 pin、有降级、权威路径不依赖；这条例外不放宽 §2 的禁止项本身，
   新增任何私有格式读取都必须按同三条纪律先立条款再实现。

## 3. 升级路径

pi 与两个预装包的版本**只在一处声明**：`harnesses/pi/harness.toml` 的 `[[image.packages]]`
（`PI_VERSION` = `@earendil-works/pi-coding-agent` 0.86.1，`role = "kernel"`；
`PI_WEB_ACCESS_VERSION` = `pi-web-access` 0.31.0 与 `PI_SUBAGENTS_VERSION` = `pi-subagents` 0.40.0，
`role = "pi-package"`）。其余位置是 `scripts/generate-pack-artifacts.py` 的生成产物（入库，
`tests/test_repository_boundaries.py` 比对生成结果与仓库现状）：

| 位置 | 事实 |
|---|---|
| `docker/Dockerfile.worker` | 生成的 `ARG PI_VERSION` / `ARG PI_WEB_ACCESS_VERSION` / `ARG PI_SUBAGENTS_VERSION`（包装到 `/opt/pi-packages`）；手写的 `FROM node:24.21.0-bookworm-slim`（Node 精确 tag，不是包属性） |
| `docker/e2b/Dockerfile.e2b` | 同一组生成的 ARG；同一 Node 精确 tag；同一 `/opt/pi-packages` 布局 |
| `docker/e2b/templates.json` | 生成的 E2B 模板参数（`pi-worker`：别名、Dockerfile、CPU/内存、构建参数、构建命令）；`scripts/build-e2b-template.py` 的缺省值从这里读 |
| `harnesses/pi/settings.json` | 生成的 `packages`：`npm:pi-web-access@0.31.0` 与 `npm:pi-subagents@0.40.0`。容器面按清单的 `pi-package` 名单把 `npm:` 改写为 `/opt/pi-packages/node_modules/<name>` 本地包 |
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
3. 若通过 → 改 `harnesses/pi/harness.toml` 里 kernel 条目的 `version`，运行
   `python3 scripts/generate-pack-artifacts.py` 并提交产物；重建镜像与 E2B 模板、重跑三面 smoke
   （后两步需单独授权）；
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
grep -rn "earendil-works" host/ web/ harnesses/ --include="*.py" --include="*.mjs" --include="*.ts" | grep -v "^Binary" | sed 's/^/  /'

# ④ 密钥不入库（应当是空输出）
git grep -nE "(sk-|exa-)[A-Za-z0-9_-]{16,}" -- . ':!*.example' ':!*.txt'

# ⑤ 运行期 token 文件权限为 600 且不在仓库内（每次 run 一个目录；只含已吊销的出口代理 token）
find ~/.pi-glink/runs -path '*/pi-agent/web-search.json' -maxdepth 4 -exec ls -l {} \;

# ⑥ 前端无打包依赖树
ls web/node_modules 2>/dev/null || echo "无前端依赖树（原生 ESM）"
```

核验结果（2026-09-22 实测）：

* ① 空输出 —— 仓库内没有 pi（或任何）TypeScript 源码副本；
* ② 只出现 `--mode rpc`（`agent_env.py`、`harness_pi.py`、`docker_env.py`）——三面同一套 RPC 双向通道，argv 不再有任何单面分支；
* ③ 空输出 —— 宿主与前端连包名都不引用（注释里也不用它作为标识）；
* ④ 空输出 —— 无密钥入库（`docs/evidence/` 已排除，因为日志里可能出现用户级别的占位字符串）；
* ⑤ `-rw-------`（600）—— 每个 run 的运行期凭据文件权限正确；（⑥ 无前端依赖树。（①③④ 的空输出是预期结果。

本次整改的核验口径变化：原 ⑤ 命令指向共享目录 `~/.pi-glink/pi-agent/`，该目录已不再是运行期配置
（每个 run 使用 `runs/<id>/pi-agent/`），命令已同步更新。

GLINK-SUB-02（2026-09-24）复跑：

* ① 现在会输出本仓自有的两个 `.ts`（`harnesses/pi/extensions/subagent-foreground-guard.ts`、
  `tests/extensions/probe-tool-inventory.ts`）——它们是本仓的 pi 扩展与测试探针（pi 官方加载形态
  就是 TypeScript），不是 pi 源码副本；命令要排除 pi 自带产物时仍以仓库内没有 `dist/`/`src/`
  副本为准。
* ③ 把扫描范围扩到 `harnesses/` 与 `.ts` 后只命中一条：`harnesses/pi/extensions/subagent-foreground-guard.ts:34`
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
| pi 会往配置目录写运行时状态（`auth.json` / `models-store.json`） | 仓库目录会被污染 | 仓库 `harnesses/pi/` 只作模板；每次 run 渲染到 `runs/<id>/pi-agent/`（先删再拷，凭据缺失则删除旧文件）；`.gitignore` 兜底 |
| E2B 后台命令的流式接口是 `CommandHandle`，不是 `commands.run()` 上的回调 | 沿用旧结论会让该面退化为结束后回放 | `E2BSandbox.spawn` 以 `background=True, stdin=True` 启动 `pi --mode rpc`，handle 经 `SandboxCommandProcess` 注入共享 `PiRpcClient` 实时投递；断流即 `execution_error`、不补发（`sandbox.py` + `e2b_env.py`，见 architecture.md §5.2） |
| E2B 会话文件不会自动留存 | 未回收则不能追问 | 销毁前回收单个有界 `.jsonl` 到本 run 专属目录并脱敏；失败时明确拒绝追问 |
| 本 run 的出口代理 token 位于运行期 `auth.json` / `web-search.json`（真实 key 不在执行环境） | 工具可在 run 期间读到 token 并在配额内调用代理或回显 | token 只通向宿主冻结的两个上游、受每 run 配额约束、终帧吊销；回显被出站脱敏覆盖（architecture.md §6/§6.1、ADR 0008） |
| pi-subagents 的扩展命令（`/run --bg`、`/chain --bg`、`/subagents-detach` 等）由用户输入直接执行，且先于 pi 的 `input` 事件派发 | `tool_call` 钩子不可达；载入 pi-subagents 后，客户端可控的 prompt 一旦以 `/` 开头就会先执行这些命令，发起不随主 run 取消/超时收口的后台进程 | 模型工具面用 `harnesses/pi/extensions/subagent-foreground-guard.ts` 强制前台（pi-integration-notes.md §10）；客户端输入面由宿主在**准入层**加 `/` 命令门禁：只接受 `/<蓝图名> <题目>`（蓝图名从 `methodology/blueprints/` 推导），其余 `400 unsupported_command`（architecture.md §4，回归 `tests/test_runs_api.py` / `tests/test_runner.py`） |
