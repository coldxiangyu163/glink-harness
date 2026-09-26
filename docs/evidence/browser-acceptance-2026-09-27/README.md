# 证据：浏览器全量验收（2026-09-27）

本目录是 [`docs/browser-acceptance-report.md`](../../browser-acceptance-report.md) 的证据。
数据全部来自**合成**场景与合成凭据；已扫描确认不含 `sk-*` 合成 key、口令或真实配置。

## 运行

| | accounts | local | gateway |
|---|---|---|---|
| 命令 | `node tests/browser/acceptance.mjs --mode accounts` | `--mode local` | `--mode gateway` |
| 结果 | 101 / 1 失败（102） | 78 通过（81，3 跳过） | 7 / 7 |
| 隔离宿主 PID / 退出 | 64646 / code 0 | 73905 / code 0 | 80914 / code 0 |
| 宿主 | `tests/fixtures/accounts_server.py`（真实 Runner/RunStore/EventBus/HTTP/SSE + 生产前端，执行面为进程内脚本化替身） | 同左，单用户面 | 同左，网关身份面 |
| 浏览器 | Chromium 145（Playwright 1.58.2，project-local `tests/browser/package.json` 锁定） | 同左 | 同左 |

数据根、账号库与 `HOME`/`XDG` 都是各次运行的临时目录，运行结束随临时目录丢弃；
未使用、未修改、未停止任何既有服务，未发起真实模型调用。

## 文件

| 文件 | 内容 |
|---|---|
| `<mode>/acceptance.json` | 逐项检查：`dimension` / `name` / `ok` / `detail`（报告中所有数字与引用的来源） |
| `<mode>/acceptance.txt` | 同一批检查的可读文本（含时间戳与命令） |
| `<mode>/host-exit.json` | 隔离宿主的 PID 与退出码（按 PID 终止，无宽匹配 pkill） |
| `accounts/requests.jsonl` | accounts 运行的 470 条原样请求行与 Referer；`invite=` 命中 0 次（token 只在浏览器 URL hash） |
| `accounts/*.png` | 运行中 / 已停止 / 工具卡 / 失败卡 / 追问 / 子代理运行中与收口 / 报告右槽 / 最终态 |
| `local/*.png` | 本地模式的运行中与子代理收口 |
| `gateway/gateway-face.png` | 网关模式的未认证入口 |

## 唯一失败项

`accounts/acceptance.json` 中 `dimension=identity`、`name=邀请对话框：Esc 关闭并把焦点还给入口按钮`
的 `ok=false`，`detail` 里是 `focus: ""`、`focusTag: "BODY"`——即 F-1（见报告 §5）。
