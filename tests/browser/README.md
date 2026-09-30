# 浏览器验收套件（`tests/browser/acceptance.mjs`）

各维度全量浏览器测试：用 Chromium 驱动**生产前端**，跑在**隔离的真实宿主**上，
每一项检查都落成 JSON 证据。它不属于 `scripts/check.sh`，也不会写 `docs/evidence/`
之外的地方（默认输出到系统临时目录）。

## 与历史脚本的区别

`run-smoke.mjs` / `ui-features.mjs` / `ui-parity.mjs` 是历史脚本：它们需要一台已在
运行的服务、会提交真实研究、从 `~/.npm/_npx` 猜 Playwright 版本，并按约定覆盖
`docs/evidence/`。本目录的 `acceptance.mjs` 是它们的替代路径：

| | 历史脚本 | acceptance.mjs |
|---|---|---|
| 依赖 | npx 缓存里的任意版本 | `tests/browser/package.json` 钉死的 `playwright@1.58.2` + `package-lock.json` |
| 目标服务 | 已运行的宿主（真实模型） | 自己启动的隔离 fixture，随机 loopback 端口 |
| 执行面 | 真实 pi / Docker / E2B | 进程内脚本化替身（无模型、无容器、无沙箱） |
| 身份面 | 假定 local | accounts / local / gateway 三种 |
| 副作用 | 写 `docs/evidence/`、可能停别人的 run | 只写 `--out` 目录；只按 PID 终止自己起的 fixture |

历史脚本保留原样，作为“对已有服务做人工核对”的工具。

## 用法

```bash
# 依赖（一次性；仓库内 node_modules 已被 .gitignore 忽略）
cd tests/browser && npm install --no-audit --no-fund

# 三种身份面
node tests/browser/acceptance.mjs --mode accounts
node tests/browser/acceptance.mjs --mode local
node tests/browser/acceptance.mjs --mode gateway

# 只跑部分维度 / 指定输出目录
node tests/browser/acceptance.mjs --mode accounts --only shell,lifecycle --out /tmp/my-run
```

退出码：有失败为 1，否则 0。结果落在 `--out`（缺省是系统临时目录）：

- `acceptance.json` — 每项检查的 dimension / name / ok / detail（报告引用它）
- `acceptance.txt` — 同一批检查的可读文本
- `fixture.log` — 隔离宿主的日志
- `requests.jsonl` — fixture 记录的原样请求行与 Referer（凭证不进请求行的证据）
- `host-exit.json` — 隔离宿主 PID 与退出码
- `*.png` — 每个阶段的关键截图

## 隔离边界

`tests/fixtures/accounts_server.py` 起一个**真实宿主**（真实 Runner / RunStore /
EventBus / HTTP / SSE / 生产前端），只把执行面换成进程内替身；数据根、账号库、
HOME / XDG 都是本次运行专属的临时目录，合成凭据只写进临时文件。不会读操作员的
`.env`、`~/.pi`、`~/.pi-glink`，不会调用真实模型、Docker 或 E2B，也不会碰任何
已在运行的服务。fixture 由调用方按**记录的 PID** 终止，不用宽匹配 `pkill`。

题目里的 `[scenario:…]` 标记选择合成场景（`tools` / `subagent` / `slow` / `fail` /
`length`，无标记是默认的思考+回答+报告；任务专用场景为 `todo` / `todoempty` / `todolong`）。同一批场景在 HTTP 层也有 Python 回归
（`tests/test_runner.py` 等）覆盖；浏览器层验证的是**前端在这些事实下的呈现**。

## 覆盖维度

`identity`（登录/邀请/注册/注销）、`shell`、`composer`、`lifecycle`、`subagent`、
`report`、`progress`、`tenancy`、`resilience`、`gateway-identity`、`assets`。
缺省 `accounts` 模式跑 102 项，`local` 81 项（账号/租户维度按设计跳过），
`gateway` 7 项（没有站内身份，可测面就是未认证入口与静态资产）。

## 当前任务组件对齐

```bash
node tests/browser/todo-panel.mjs --reference /Users/coldxiangyu/work/BR/glink-ui-library
```

通过真实生产交互创建/停止合成研究：仅执行面是 fake，投影、存储、SSE、reducer、
workspace 与 DOM 均为生产链，不手填最终状态。`todo` / `todoempty` / `todolong` 场景以
既有 fake pi wire 产生与 ACP `todo_write` 同形的整表产品事件，不代表真实 ACP 或模型验收。
数据根、HOME 与宿主端口均隔离，不触碰已有服务或真实凭据。

参考面用标准库自身已安装的 Vite 渲染**工作台 `ProgressPanel` 真源码 + 场景 CSS**，不是
单独组件展区，不修改标准库源码或重建 `dist/`。`--reference` 必填，不缺库静默通过；
参考服务也使用 OS 分配的 loopback 端口，Vite 缓存与证据写本次 `--out`（默认系统临时目录）。
宿主按记录 PID 关闭，参考服务由 `finally` 关闭。

覆盖默认对话不挂任务卡、右侧任务时间轴三态/行距/字号/关闭键逐值、13 个视口含断点两侧与
640px 最小高度、输入器不遮挡、键盘折叠与更新保持、×/Esc 焦点归还、报告互斥、上下文页签、
停止不伪造完成、历史回放与追问不串计划、长清单单层滚动、减弱动效、空表替换。
完成/并行计数是研究事实，不套单个当前步骤；无源不画序号/百分比；标题24px命中区与
真实运行信息是已登记的消费方差异，见 `docs/ui-alignment.md`。本脚本不接入默认 `scripts/check.sh`。

## 诊断工具

`tools/probe*.mjs` 是写断言之前用来量 DOM 的一次性探测脚本，不进验收结果；
保留它们是为了下一次改动前端时可以快速重新取样。
