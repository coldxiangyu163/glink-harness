# 证据：浏览器全量验收（2026-10-05）

本目录是 [`docs/browser-acceptance-report.md`](../../browser-acceptance-report.md) 的 2026-10-05 小节的证据。
数据全部来自**合成**场景与合成凭据；已扫描确认不含 `sk-*` 合成 key、口令或真实配置
（`credential-scan.txt`）。

本轮在 R3 创建幂等提交 `6bb3e2d` 之后的 `fix/arch-review-p1` 工作区执行，验证 R1–R3
累积改动（R2 执行核心服务化、R3 幂等）后浏览器面零回归。

## 运行

| | accounts | local | gateway |
|---|---|---|---|
| 命令 | `node tests/browser/acceptance.mjs --mode accounts` | `--mode local` | `--mode gateway` |
| 结果 | **102 / 102** | **78 通过 / 0 失败 / 3 跳过**（81，账号/租户维度按设计跳过） | **7 / 7** |
| 隔离宿主 PID / 退出 | 37176 / code 0 | 39464 / code 0 | 41362 / code 0 |
| 宿主 | `tests/fixtures/accounts_server.py`（真实 Runner/RunStore/EventBus/HTTP/SSE + 生产前端，执行面为进程内脚本化替身） | 同左，单用户面 | 同左，网关身份面 |
| 浏览器 | Chromium（Playwright 1.58.2，project-local `tests/browser/package.json` 锁定） | 同左 | 同左 |

与 2026-09-28 复验（accounts 99/102、local 76 过 + 2 失败 + 3 跳过）相比，当轮的
accounts 3 项失败与 local 2 项失败在本轮全部通过。

本轮唯一的设施改动：fixture 显式设置 `GLINK_HARNESS=pi`。2026-10-01 起仓库默认包改为
dsh，其清单 `session_config` 校验要求 `OPENAI_REASONING_EFFORT` 环境值，fixture（其执行面
替身是 pi 族的 `harness_pi.PiExecutionAdapter`）在默认包下启动即 fail-closed；钉回 pi 包
恢复其设计支撑的面，不是放宽任何断言。

数据根、账号库与 `HOME`/`XDG` 都是各次运行的临时目录，运行结束随临时目录丢弃；
未使用、未修改、未停止任何既有服务，未发起真实模型调用。

## 文件

| 文件 | 内容 |
|---|---|
| `<mode>/acceptance.json` | 逐项检查：`dimension` / `name` / `ok` / `detail`（报告中所有数字与引用的来源） |
| `<mode>/acceptance.txt` | 同一批检查的可读文本（含时间戳、命令与 baseUrl） |
| `<mode>/host-exit.json` | 隔离宿主的 PID 与退出码（按 PID 终止，无宽匹配 pkill） |
| `<mode>/requests.jsonl` | 原样请求行与 Referer（accounts 549 条 / local 355 / gateway 52；`invite=` 命中 0，token 只在浏览器 URL hash） |
| `<mode>/*.png` | 运行中 / 工具卡 / 失败卡 / 追问 / 子代理运行中与收口 / 报告 / 最终态 / 网关入口与资产 |
| `credential-scan.txt` | 归档前凭据扫描记录 |
