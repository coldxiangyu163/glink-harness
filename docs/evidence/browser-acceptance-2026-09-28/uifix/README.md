# GLINK-ARCH-UIFIX-20260928：红绿闭环证据

基线为 `0139fb3`。本 follow-up 对上一轮四个失败机制逐项归因并重跑受影响层。结论：R1/R2/R3 都是历史浏览器脚本的测量前置或选择器问题，产品 UI 本身没有对应回归，因此未修改 `web/` 或 `host/`；S1/S2 是 acceptance fixture 与新 egress/SQLite 契约不同步，已修复测试夹具。

## 结果

| 层 | 红（`0139fb3` 既有证据） | 绿（本目录） |
|---|---|---|
| R1 ui-parity | 28 OK / 1 mismatch / 1 SKIPPED，`homeCard` 124px vs 380px，exit 1 | 29 OK / 0 mismatch / 1 SKIPPED，0 处差异，exit 0 |
| R2/R3 ui-features | 52/54，文件卡与 404 恢复卡失败，exit 1 | 54/54，exit 0 |
| S1/S2 accounts | 99/102：F-1 + 用量 + 会话路径三项失败，exit 1 | 101/102；仅保留范围外 F-1，exit 1 |
| S1/S2 local | 76 通过 / 2 失败 / 3 跳过，exit 1 | 78 通过 / 0 失败 / 3 跳过，exit 0 |

红证据位于父目录的 `ui-parity.*`、`ui-features.*`、`accounts/acceptance.*`、`local/acceptance.*`；绿证据分别为本目录 `ui-parity-green.*`、`ui-features-green.*` 及 `<mode>/acceptance.*`。

## 逐项归因

### R1：首页卡片高度是假偏差

`web/styles.css` 在对照点前后未变化，卡片本来就随真实 run 条目数增长、列表上限为 320px。旧的已填充宿主恰好把目标卡撑到 380px，历史 `docs/evidence/ui-parity.json` 记录参考/目标均为 `584x380`；本次隔离宿主只有一条 run，卡片自然是 124px。高度是数据前置，不是组件几何。

修复：`ui-parity.mjs` 不再把 `homeCard` 的内容高度作为 box 几何门禁；卡片颜色/圆角/描边/海拔与 `homeRow` 行几何仍逐值比较。绿测在同样只有少量 run 的宿主上得到 0 个 mismatch。

### R2：文件卡从未消失，脚本选中了子代理面板

同一次红测的 `ui-features-trace.png` 已清楚显示 `report.md` 文件卡。重构后子代理面板为了复用 chrome 使用 `.pi-output.pi-children`，且在 DOM 中先于真正的文件区；旧断言 `document.querySelector('.pi-output')` 因而读到隐藏的子代理面板，错误报告 `output:false`。

修复：ui-features、run-smoke 与两个诊断 probe 统一按语义角色 `[aria-label="生成的文件"]` 选择文件区，并等待该可见合同。产品渲染无需修改。

### R3：产品 404 恢复路径正常，脚本在重试预算结束前取样

产品对被拒的 EventSource 按 1.5s、4s、10s 有界重试，约 15.5s 后才进入 `unavailable`。旧脚本固定等待 2s，只能看到 `reconnecting`。修复后等待“过程记录暂时读不到”恢复卡（20s 上限），绿测得到 `shown:true`，动作包含“重试 / 打开报告”。

### S1：fixture 同步 egress 终帧 usage

真实 pi 通过 egress proxy，终帧的权威 `usage` 来自 per-run grant；FakeAdapter 只发 message/turn usage 而从不走代理，终帧因此是全零 egress usage。fixture 现在在 `wait()` 收口前把每个合成场景的一次模型响应计入该 run 的 live grant，同时保留原 `kernelUsage`。accounts/local 的进度面板均重新显示“用量 · 1,000 token”。

### S2：fixture 同步 SQLite run 目录

会话删除路径从旧的 `data/sessions/<runId>/session.jsonl` 改为 `data/runs/<runId>/sessions/session.jsonl`。accounts/local 复验均得到 `409 {"error":"session_unavailable"}`。

## 运行与资源

- 真实参考站 PID 73857、真实 inproc 宿主 PID 73861，随机 loopback 端口 62482/62483；均按 PID 发 SIGTERM，端口已释放，临时 data root 已删除。
- acceptance fixture：定向 accounts PID 71649、全量 accounts PID 88651、全量 local PID 91960；均由脚本按记录 PID 停止并以 code 0 退出。
- 真实模型预置 run 完成，记录见 `seed-run.txt`；ui-features 按既有合同另提交并停止真实研究。未运行 Docker/E2B。
- 完整 PID/端口记录见 `service-lifecycle.txt`。

## 文件

- `ui-parity-green.{json,txt}`、`ui-parity-green-run.txt`
- `ui-features-green.{json,txt}`、`ui-features-green-run.txt`、三张截图
- `accounts/`、`local/`：全量 acceptance JSON/TXT、截图与 `host-exit.json`
- `default-check.txt`：默认检查
- `credential-scan.txt`：增量证据凭据扫描
