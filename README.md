# pi-glink

以 [pi](https://www.npmjs.com/package/@earendil-works/pi-coding-agent) 为执行内核的本地研究工作台。前端沿用 Glink 工作台的布局与视觉，运行概览、执行流程事件与思考展示按 pi 的原生事件语义重写。

**当前状态：本机 / Docker / E2B 三种执行面均已跑通真实 run**，验收记录见 [docs/acceptance.md](docs/acceptance.md)，证据在 `docs/evidence/`。除前端静态资源源自 Glink 工作台外，本仓库不引用 FrontierAgent 的任何运行模块。

## 边界（先读这条）

- **不 fork、不 patch、不 vendor pi 内核。** 只使用 pi 的官方 headless 协议与官方扩展点（extension / skill / package / custom provider / MCP）。
- **不复制 FrontierAgent 的 runtime。** 本仓库自有宿主只做编排、事件投影与呈现；研究方法论通过 pi 的提示词与扩展表达。
- 前端不解析终端文本，只消费结构化事件。
- 详见 [docs/pi-boundary.md](docs/pi-boundary.md) 与 [docs/architecture.md](docs/architecture.md)。

## 依赖与版本

| 组件 | 版本 | 安装 / 说明 |
|---|---|---|
| Python | ≥ 3.12 | 宿主与内核接入层只用标准库，无运行时依赖 |
| Node.js | **v24.16.0**（本机实测） | 前端与 pi 的运行时；用 nvm 固定：`nvm install 24.16.0 && nvm use 24.16.0`（或 Homebrew `node@24`） |
| pi | 0.86.1（已实测） | `npm install -g @earendil-works/pi-coding-agent@0.86.1`；Docker / E2B 面的版本固定在各自 Dockerfile 的 `ARG PI_VERSION` |
| pytest | ≥ 8（仅开发） | `uv pip install -e ".[dev]"` |
| e2b SDK | ≥ 2（仅 E2B 执行面） | `uv pip install -e ".[e2b]"`（含代理网络需要的 `httpx[socks]`） |

本机执行面用宿主安装的 pi；换版本做演练时用 `PI_GLINK_PI_BIN` 指向另一个二进制，无需改 PATH（见 [docs/pi-boundary.md](docs/pi-boundary.md) §3）。

## 三种执行面

| 执行面 | 取值 | 语义 |
|---|---|---|
| 本机进程 | `inproc` | pi 以子进程运行在宿主机器上，工作区在 `<数据根>/workspaces/<runId>` |
| 容器 | `docker` | pi 与工作区都在一次性容器内，宿主只做编排与事件投影 |
| 云沙箱 | `e2b` | pi 与工作区都在 E2B 沙箱内（`--mode json` 单向事件流），凭据经 `envs` 注入 |

### E2B 执行面

```bash
# 首次：构建模板（本机网络需要代理时带上 HTTPS_PROXY）
E2B_API_KEY=… HTTPS_PROXY=socks5h://127.0.0.1:1080 \
  .venv/bin/python scripts/build-e2b-template.py --logs

LOCAL_WEB_EXECUTOR=e2b LOCAL_WEB_PORT=7863 scripts/run-local-web.sh   # 用云沙箱跑 pi
```

模型凭据经 `Sandbox.create(envs=…)` 注入（不落盘）；pi-web-access 凭据随运行期
`web-search.json` 上传到沙箱，随沙箱销毁而消失。事件在 run 结束后一次性回放
（E2B SDK 的 stdout 回调实测不触发），报告会拉回宿主工作区。
证据：[docs/evidence/e2b-run.txt](docs/evidence/e2b-run.txt)。

### Docker 执行面

```bash
docker build -t pi-glink-worker:latest -f docker/Dockerfile.worker .   # 首次
LOCAL_WEB_EXECUTOR=docker scripts/run-local-web.sh                     # 用容器跑 pi
```

镜像自包含（node:24-slim + 固定版本 pi）；宿主只挂载工作区（`/workspace`）、
运行期配置（`/config`）与会话目录（`/sessions`），凭据按变量名转发。
网络受限环境可用 `--build-arg DEBIAN_MIRROR=mirrors.aliyun.com --build-arg NPM_REGISTRY=https://registry.npmmirror.com`。
证据：[docs/evidence/docker-run.txt](docs/evidence/docker-run.txt)。

执行面由 `LOCAL_WEB_EXECUTOR` 选择；三者消费同一份输入契约，事件流对外表现一致。

## 快速启动（本机）

```bash
cp .env.example .env         # 填写 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL
scripts/run-local-web.sh     # 默认 http://127.0.0.1:7860
```

脚本把所有依赖都铺好了：优先用仓库内的 `.venv`，否则回退 `uv run` 或系统 Python，并始终把 `host/` 放进导入路径。等价的手动方式：

```bash
uv venv --python 3.12 .venv       # 首次
uv pip install -e ".[dev]"        # 首次：让 pi_glink 可导入并装上 pytest
.venv/bin/python -m pi_glink.app
```

常用参数：

```bash
scripts/run-local-web.sh --port 7861     # 覆盖端口
scripts/run-local-web.sh --print-config  # 打印生效配置（不含密钥）
```

## 运行一次真实研究

```bash
scripts/run-local-web.sh &                                  # 启动宿主（默认 7860，可在 .env 覆盖）
.venv/bin/python scripts/smoke-local-run.py --base-url http://127.0.0.1:7861
```

冒烟脚本走与浏览器完全相同的 HTTP 接口，产出：run 终态、事件数、sessionId、报告大小与帧类型清单；退出码非零即失败。真实证据样例见 [docs/evidence/local-m1-smoke.txt](docs/evidence/local-m1-smoke.txt)。

## HTTP 接口（V1）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 宿主与 pi 可用性、当前活跃 run |
| GET | `/api/config` | 公开配置（无密钥、无宿主路径） |
| POST | `/api/runs` | 创建并启动一次 run，body `{"prompt": "…"}` |
| GET | `/api/runs` | run 列表（含状态与报告标记） |
| GET | `/api/runs/{id}` | run 详情 |
| GET | `/api/runs/{id}/events` | 事件流（SSE，支持 `Last-Event-ID` 重放） |
| POST | `/api/runs/{id}/stop` | 请求取消 |
| GET | `/api/runs/{id}/report` | 下载 `report.md` |

本机执行面同一时间只允许一个 run；并发请求返回 `409 run_busy` 并附当前 run id。

## 搜索

检索能力全部来自 [pi-web-access](https://github.com/nicobailon/pi-web-access) 扩展，声明在 `agent/settings.json` 的 `packages` 里（`npm:pi-web-access`），pi 启动时会把它安装到运行期配置目录。

凭据由宿主渲染到运行期配置目录的 `web-search.json`（权限 600）：

1. 有 `PI_GLINK_WEB_SEARCH_KEY` 时渲染为 `{"exaApiKey": …}`；
2. 否则复制操作员本机的 `~/.pi/agent/web-search.json`（或 `~/.pi/web-search.json`）；
3. 都没有也能工作：pi-web-access 会回退到无密钥的 Exa MCP 检索。

本仓库不包含 Serper / Jina 链路。检索证据见 [docs/evidence/local-web-search.txt](docs/evidence/local-web-search.txt)。

## 目录

| 路径 | 职责 |
|---|---|
| `host/pi_glink/` | 宿主：配置解析、事件总线、HTTP/SSE 服务、静态资源 |
| `host/pi_glink/kernel/` | pi 接入层：headless 协议客户端（RPC / JSON）、事件投影、运行期配置渲染、三执行面适配 |
| `agent/` | pi 侧配置与扩展：`PI_CODING_AGENT_DIR` 指向这里，内核之外的定制只落在这里 |
| `web/` | 前端：沿用 Glink 布局的静态 ESM 资源 |
| `docs/` | `architecture.md`（进程拓扑、事件契约、执行面适配）、`pi-boundary.md`（扩展点与禁止项） |
| `tests/` | 宿主与内核的回归 |
| `data/`（运行时创建） | 默认 `~/.pi-glink`：会话、工作区、事件日志 |

## 环境变量

与 FrontierAgent 保持同一套命名：模型 `OPENAI_*`，云沙箱 `E2B_*`，服务与执行面 `LOCAL_WEB_*`；本项目独有概念使用 `PI_GLINK_*`。完整列表见 [.env.example](.env.example)。

宿主读一次配置后冻结，运行中的 host 不会因为改环境变量而改变行为。

## 验证

```bash
.venv/bin/python -m pytest        # 或者：scripts/run-tests.sh
```
