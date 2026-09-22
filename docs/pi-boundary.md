# pi 边界契约

本仓库把 pi 当**外部内核**使用：内核包、内核源码、内核内部接口一律不动；一切定制都落在
pi 官方文档化的扩展点、环境变量与 headless 协议上。本文是这份契约的清单，每条都给出
本仓库里的实际例子，并给出可执行的交叉核验命令（见 §4）。

> 实测事实与踩坑记录集中在 [pi-integration-notes.md](pi-integration-notes.md)；
> 进程与事件结构见 [architecture.md](architecture.md)。

## 1. 允许的扩展点

| 扩展点 | 文档 | 本仓库的用法（实际文件） |
|---|---|---|
| `settings.json` | docs/settings.md | `agent/settings.json`：`defaultThinkingLevel: "medium"`、`packages: ["npm:pi-web-access"]` |
| `models.json`（自定义 provider） | docs/models.md | 宿主渲染到运行期目录（`kernel/agent_env.py::_models_document`）：`baseUrl` 字面量、`apiKey: "$OPENAI_API_KEY"`、`compat.thinkingFormat: "openrouter"` |
| packages | docs/packages.md | `npm:pi-web-access`（检索能力全部来自它） |
| extensions | docs/extensions.md | 目录预留（`agent/extensions/`），当前无扩展；有需求时只落在这里 |
| skills | docs/skills.md | 目录预留（`agent/skills/`） |
| prompt templates | docs/prompt-templates.md | 目录预留（`agent/prompts/`） |
| `AGENTS.md` | docs/quickstart.md 等 | 目录预留（`agent/AGENTS.md`），当前约定走 §下一条 |
| `--append-system-prompt` | `pi --help` | `agent/research-prompt.md` → `kernel/runner.py::_extra_argv`，内核系统提示词本身不改 |
| headless 协议 | docs/rpc.md、docs/json.md | `--mode rpc`（本机 / Docker 面，`kernel/rpc.py`）；`--mode json`（E2B 面，`kernel/e2b_env.py`） |
| 环境变量 | docs/environment-variables.md | `PI_CODING_AGENT_DIR`、`PI_CODING_AGENT_SESSION_DIR`、`PI_SKIP_VERSION_CHECK`、`PI_TELEMETRY`、`OPENAI_API_KEY` |
| CLI 参数 | docs/usage.md | `--provider`、`--model`、`--name`（`kernel/agent_env.py::build_pi_argv`） |
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
| 凭据不经脱敏边界进入产品面 | 工具回显/env dump 会把 key 带进事件流、报告与会话文件 | 六层出口均过 `redaction.py`；`scripts/check-no-secrets.py` 扫描运行态产物；`tests/test_redaction.py` |
| 把宿主整份环境变量交给 pi | 会把操作员 shell 里的其他凭据一并带入 | `kernel/agent_env.py` 的白名单 `_PASSTHROUGH_ENV` |

## 3. 升级路径

pi 的版本在三处被固定，升级只改这三处 + 回归：

| 位置 | 事实 |
|---|---|
| `docker/Dockerfile.worker` | `ARG PI_VERSION=0.86.1`（镜像内 pi 版本） |
| `docker/e2b/Dockerfile.e2b` | 同一 `ARG PI_VERSION`（E2B 模板内 pi 版本） |
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

# ③ 宿主不 import pi 的内部模块
grep -rn "earendil-works" host/ web/ --include="*.py" --include="*.mjs" | grep -v "^Binary" | sed 's/^/  /'

# ④ 密钥不入库（应当是空输出）
git grep -nE "(sk-|exa-)[A-Za-z0-9_-]{16,}" -- . ':!*.example' ':!*.txt'

# ⑤ 运行期凭据文件权限为 600 且不在仓库内
ls -l ~/.pi-glink/pi-agent/web-search.json

# ⑥ 前端无打包依赖树
ls web/node_modules 2>/dev/null || echo "无前端依赖树（原生 ESM）"
```

核验结果（2026-09-22 实测）：

* ① 空输出 —— 仓库内没有 pi（或任何）TypeScript 源码副本；
* ② 只出现 `--mode rpc`（`agent_env.py`、`docker_env.py`）与 `--mode json`（`e2b_env.py`）；
* ③ 空输出 —— 宿主与前端连包名都不引用（注释里也不用它作为标识）；
* ④ 空输出 —— 无密钥入库（`docs/evidence/` 已排除，因为日志里可能出现用户级别的占位字符串）；
* ⑤ `-rw-------`（600）—— 运行期凭据文件权限正确；（⑥ 无前端依赖树。（①③④ 的空输出是预期结果。

## 5. 已知的内核侧限制（不改内核，只记录）

| 限制 | 影响 | 我们怎么做 |
|---|---|---|
| `models.json` 的 `baseUrl` / 模型 id 不支持 `$VAR` 插值 | 无法把 endpoint 交给 pi 自己解析 | 宿主渲染运行期 `models.json`（`agent_env.py`） |
| pi 会往配置目录写运行时状态（`auth.json` / `models-store.json`） | 仓库目录会被污染 | 仓库 `agent/` 只作模板，运行期目录在 `<data_root>/pi-agent/`；`.gitignore` 兜底 |
| E2B SDK 的 stdout 回调不触发 | E2B 面事件非实时 | 沙箱内写 JSONL、结束后回放（`e2b_env.py`）；差异记在 architecture.md |
| 沙箱内会话文件随沙箱销毁 | E2B 面 `session_id` 为空 | 报告与 run 元数据仍持久；如实记录 |
| pi 的工具能读到内核自用的模型 key | inproc 面的固有属性（内核与工具同进程环境） | 输出侧六层脱敏 + 子进程环境白名单；更强隔离用 Docker / E2B 面；详见 architecture.md §6 |
