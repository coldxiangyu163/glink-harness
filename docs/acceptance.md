# 验收记录

日期：2026-09-22 · 仓库：`~/AI/git/pi-glink` · 基线 pi：0.86.1（升级演练另用 0.87.0）

本记录汇总本次目标（task-1 ~ task-9）的代码范围、实际命令、真实退出状态、证据文件与
未覆盖项。所有结论都可在本机重跑；证据文件在 `docs/evidence/`。

## 1. 代码范围

| 提交 | 范围 |
|---|---|
| `7d0411a` | task-1 仓库骨架、宿主服务、事件总线、前端基线 |
| `7d0ae2e` | task-2 pi 接入层（RPC 客户端、事件投影、运行期配置渲染） |
| `a3628ec` | task-3 本机执行面端到端（run 编排、run API、冒烟脚本） |
| `4ff3c9e` | task-4 前端事件层重写（run-stream / run-view / workspace） |
| `530bc53` | task-5 搜索接入 pi-web-access |
| `f29c3b3` `8a1bd03` | task-6 Docker 执行面与验收证据 |
| `603c1c4` | task-7 E2B 执行面与验收证据 |
| `e093976` | task-8 架构文档与 pi 边界契约 |

目录边界与各目录职责见 `docs/architecture.md` §7。

## 2. 实际命令与退出状态

| 命令 | 结果 |
|---|---|
| `scripts/run-tests.sh`（Python 全套） | 73 项通过，exit 0 |
| `node --test tests/web/*.test.mjs` | 11 项通过，exit 0 |
| `node tests/browser/run-smoke.mjs --base-url http://127.0.0.1:7861` | 通过（真实 run 56.4s；5 思考折叠 / 14 工具卡片 / 6 张含流式输出 / 报告预览），exit 0 |
| `scripts/smoke-local-run.py`（inproc, pi 0.86.1） | finished，45.1s，165 事件，报告 9648B，exit 0 |
| `scripts/smoke-local-run.py`（inproc, pi 0.87.0，升级演练） | finished，45.1s，327 事件，报告 400B，exit 0 |
| `docker build -t pi-glink-worker:latest -f docker/Dockerfile.worker .` | 成功，镜像 1.09GB |
| `scripts/smoke-local-run.py`（docker 面） | finished，11.5s，136 事件，报告 645B（容器内 `/workspace/` 路径），exit 0 |
| `scripts/build-e2b-template.py`（E2B 模板） | 成功，47s，`pi-glink-worker` buildId `2bec2f1d…` |
| `scripts/smoke-local-run.py`（e2b 面） | finished，68.4s，203 事件（13 种帧），报告 7115B / 13 URL，exit 0 |
| pi-boundary §4 交叉核验（6 条） | 全部通过（无源码副本 / 只用官方协议 / 无内部引用 / 无密钥入库 / 凭据 600 / 前端无依赖树） |

## 3. 证据文件

| 文件 | 内容 |
|---|---|
| `docs/evidence/local-m1-smoke.txt` | 本机执行面真实 run：45.1s、165 事件、80 段 thinking、session/workspace 对应关系 |
| `docs/evidence/browser-run.png` `browser-run.json` | 浏览器真实 run 的截图与 DOM 统计 |
| `docs/evidence/local-web-search.txt` | pi-web-access 检索 run：web_search ×1 + fetch_content ×3，报告 10 URL |
| `docs/evidence/docker-run.txt` | 镜像体积、构建命令、容器内 run、`/workspace/` 路径证据 |
| `docs/evidence/e2b-run.txt` | 沙箱内 node/pi 版本、凭据注入方式、203 事件 run、三处差异 |
| `docs/evidence/upgrade-drill.txt` | pi 0.87.0 升级演练与隔离安装做法 |
| `docs/pi-integration-notes.md` | 每条实测事实（插值范围、thinkingFormat、pi-web-access 配置目录、RPC 帧语义） |

## 4. 对照成功标准

| 标准 | 状态 | 证据 |
|---|---|---|
| 1) 本机一条命令启动 → 实时 thinking/工具/文本 → 报告落盘 | ✅ | local-m1-smoke.txt、browser-run.png |
| 2) 模型与 E2B 配置与 FrontierAgent 同源，密钥不进前端/不落仓库 | ✅ | `OPENAI_*`/`E2B_*` 同名语义；`models.json` 用 `$OPENAI_API_KEY` 占位；交叉核验 ④ |
| 3) 搜索由 pi-web-access 完成，报告含可核验 URL，无 Serper/Jina | ✅ | local-web-search.txt（web_search + 10 URL；仓库无 Serper/Jina 依赖） |
| 4) Docker 与 E2B 各跑通一次真实 run（含 Node + pi 与凭据注入） | ✅ | docker-run.txt（11.5s/136 事件）、e2b-run.txt（68.4s/203 事件） |
| 5) 前端无 Glink 特有依赖、不解析终端文本、断线/迟到事件不破坏状态 | ✅ | run-stream.test.mjs（seq 去重/缺口/终态）、源码无终端解析、终态后主动关闭流 |
| 6) architecture.md + pi-boundary.md + 可重复 smoke 命令 | ✅ | 两份文档 + `scripts/smoke-local-run.py`、`tests/browser/run-smoke.mjs` |
| 7) 验收记录含范围/命令/退出状态/证据/未覆盖项 | ✅ | 本文档 |

## 5. 未覆盖项与已知限制

1. **E2B 面事件非实时**：E2B SDK 2.x 的 stdout 回调实测不触发，事件在 run 结束后一次性
   回放（顺序与词汇正确）。见 `e2b-run.txt`。
2. **E2B 面会话不持久**：会话文件留在沙箱内，销毁即失，`session_id` 为空。
3. **版本 pin 差异**：inproc 面已在 pi 0.87.0 验证；Docker 镜像与 E2B 模板仍 pin 0.86.1
   （已实测），按 `docs/pi-boundary.md` §3 重建即可升级，本次未重建。
4. **串行执行**：三种执行面都只允许同时一个 run（`409 run_busy`）。
5. **报告无强制校验**：是否产出 `report.md` 取决于 pi 遵循研究约定；缺失记为 `no_report`，
   宿主不伪造产物。
6. **思考内容依赖上游**：thinking 事件仅在模型返回 reasoning 时出现（同一任务在不同
   prompt 下可能没有思考块），前端能正常接受"这一轮没有思考"。
7. **未做**：多用户/权限、成本与配额治理、移动端适配验证、E2B 模板生产硬化、并发调度。
8. **代理前提**：本机访问 E2B 需要代理（`socks5h://127.0.0.1:1080`），已记入 `e2b-run.txt`。

## 6. 边界与环境的如实说明

1. **FrontierAgent 仓库未被本任务改动**：本任务对它只有读操作（读 `.env` 取 `OPENAI_*` 值、
   复制 `deploy/local_web/static/` 作为前端基线）。检查时它工作区里存在未提交改动
   （`config/tiers/*`、`deploy/local_web/*` 等），那是使用者自己的在制品，与本任务无关；
   本仓库也没有引用它的任何模块。
2. **全局 pi 的时间线**：task-2 的实测基线是 `pi 0.86.1`；任务进行中（当日 10:03）
   全局安装被外部升级到 `0.87.0`（非本任务所为）。这反而让 task-9 的升级演练成为
   真实场景：宿主、接入层、投影与前端在更高版本下无需改动即通过
   （证据 `docs/evidence/upgrade-drill.txt`）。
3. **未触发需要暂停的条件**：本次创建的是**新** E2B 模板（`pi-glink-worker`），
   没有重建或覆盖 FrontierAgent 已有的 `glink-frontier-e2b` 模板；沙箱内的
   `web-search.json` 是临时的（随沙箱销毁），不存在凭据长期驻留。
