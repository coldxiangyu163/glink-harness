# pi-glink

以 [pi](https://www.npmjs.com/package/@earendil-works/pi-coding-agent) 为执行内核的本地研究工作台。前端沿用 Glink 工作台的布局与视觉，运行概览、执行流程事件与思考展示按 pi 的原生事件语义重写。

**当前状态：骨架阶段（task-1）。** 宿主可启动、可服务前端静态资源、可提供事件流端点；pi 内核接入、真实 run、Docker 与 E2B 执行面按任务列表推进。除 `frontier_agent` 之外的任何上游模块都不参与本仓库运行。

## 边界（先读这条）

- **不 fork、不 patch、不 vendor pi 内核。** 只使用 pi 的官方 headless 协议与官方扩展点（extension / skill / package / custom provider / MCP）。
- **不复制 FrontierAgent 的 runtime。** 本仓库自有宿主只做编排、事件投影与呈现；研究方法论通过 pi 的提示词与扩展表达。
- 前端不解析终端文本，只消费结构化事件。
- 详见 [docs/pi-boundary.md](docs/pi-boundary.md) 与 [docs/architecture.md](docs/architecture.md)。

## 三种执行面

| 执行面 | 取值 | 语义 |
|---|---|---|
| 本机进程 | `inproc` | pi 以子进程运行在宿主机器上，工作区在 `<数据根>/workspaces/<runId>` |
| 容器 | `docker` | pi 与工作区都在一次性容器内，宿主只做编排与事件投影 |
| 云沙箱 | `e2b` | pi 与工作区都在 E2B 沙箱内，模板与凭据语义与 FrontierAgent 一致 |

执行面由 `LOCAL_WEB_EXECUTOR` 选择；三者消费同一份输入契约，事件流对外表现一致。

## 快速启动（本机）

```bash
cp .env.example .env         # 填写 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL
scripts/run-local-web.sh     # 默认 http://127.0.0.1:7860
```

脚本把所有依赖都铺好了：优先用仓库内的 `.venv`，否则回退 `uv run` 或系统 Python，并始终把 `host/` 放进导入路径。等价的手动方式：

```bash
uv venv --python 3.12 .venv       # 首次
uv pip install -e . . --dev       # 首次：让 pi_glink 可导入并装上 pytest
.venv/bin/python -m pi_glink.app
```

常用参数：

```bash
scripts/run-local-web.sh --port 7861     # 覆盖端口
scripts/run-local-web.sh --print-config  # 打印生效配置（不含密钥）
```

## 目录

| 路径 | 职责 |
|---|---|
| `host/pi_glink/` | 宿主：配置解析、事件总线、HTTP/SSE 服务、静态资源 |
| `kernel/` | pi 接入层：headless 协议客户端与事件归一化（task-2） |
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
