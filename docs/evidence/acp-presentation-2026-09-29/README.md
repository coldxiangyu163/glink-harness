# GLINK-ARCH-UIACP-20260929 验收证据

- 基线 `b92f661`；分支 `fix/acp-presentation-20260929`。最终代码见该分支提交及同目录图片。原用户 dsh run `27f0b4df7c1f` 的 144 帧仅只读参考，未修改其数据；脱敏合成语料见 `tests/fixtures/events/acp-presentation.jsonl`。
- 对外契约变化：旧 ACP 展示提示由 `kind` 决定（dsh 所有工具 `other` → `generic`）；现由已知 **ACP toolName 优先**、未知工具由协议 `kind` 兜底，`other` 仍为 `generic`。`tests/test_kernel_harness_acp.py` 的旧类别测试在 Lead 批准的范围扩展内改为未知工具回退负例。产品事件版本、必填字段及历史帧读取不变。
- 前端查询与 URL 从既有 `args`、标题从已脱敏的 `tool_finished.result` 中 `Fetched <url> (HTTP 2xx)` 的页内标题或已脱敏搜索结果的 URL 对应标题推导；抓取 401 不冒充有标题的来源。`run_stage.entries` 和 `todo_write.args.todos` 保留为可读计划；思考流沿既有 `thinking_delta` 链路，子代理沿既有类型事件。无 dsh `block.meta`，无新框架/组件。

## 隔离回归

| 输入/命令 | 结果 |
|---|---|
| 修改前 `PYTHONPATH=host .../.venv/bin/python -m pytest -q tests/test_kernel_acp_projection.py -k 'dsh_tool_names or a_tool_call_arriving_terminal'` | 预期红：2 失败（已知工具落 `generic`） |
| 修改前 `node --test tests/web/acp-presentation.test.mjs` | 预期红：缺少 `planEntries` |
| 引入搜索结果标题回退后，相关 Node 测试 | 预期红：来源标题缺失，修复后通过；401 负例保留 |
| `PATH=/tmp/glink-uiacp-check-bin:$PATH bash scripts/check.sh` | 最终 exit 0；`python3` 临时 shell shim `exec` 主 checkout 的 `.venv/bin/python`（Python 3.12.10），不修改主 checkout；Node 24.16.0。ruff / mypy / schema 生成物通过；Python 1324 passed / 1 deselected；覆盖率 92.1%=基线 92.1%；Node 前端+扩展 191 passed / 0 failed；dsh bundle 42 passed / 0 failed。外部 UI 标准库逐值检查明确 SKIP。原始日志在 `/tmp/glink-uiacp-check-final.log`（未归档，不含密钥） |

## 真实浏览器 + 真实 ACP E2B 模型

- 使用**当前 worktree** 源码、主 checkout `.venv` 解释器、主 checkout `.env` 只读配置源（不归档任何凭据），覆盖为 `PI_GLINK_HARNESS=dsh`、`PI_GLINK_TENANCY=local`、`PI_GLINK_MODEL_CREDENTIALS=tenant`、`LOCAL_WEB_EXECUTOR=e2b`（沿既有配置）、随机 loopback 端口及仓库外临时数据根。未构建/更换 E2B 模板，未碰用户 7861/7871/7872 服务。浏览器用 ego-browser TaskSpace 19 真 Chromium，向隔离宿主提交研究题目并通过 SSE 实际运行/回放。
- 最终宿主 `127.0.0.1:51701`，PID `86848`；run `6a42cdc4111f` → `run_finished` / `finished` / `reportStatus=ok`。`cleanup.released=true`、`failures=[]`、`artifactFailures=[]`；E2B sandbox 身份仅在临时终帧内，资源已释放。浏览器断言：3 个计划工具行、3 个搜索卡、4 个抓取行、12 个思考块、1 条带任务的派发行、1 份 `report.md`；来源面板 `8 次检索 · 4 个网页`，Nature Medicine 标题与 URL 同行，401 网页不冒充成功标题。截图 `process.png`（过程展开）与 `sources.png`（标题、检索语句和计数）；二图均为本次最终源码在真实 run 的浏览器视口。
- PID 台账：初始配置探查启动因 `.env` 不在 worktree、缺凭据值而 exit 2（无运行服务）；`63900`（试探 accounts 模式，启动后按 PID 停止，无 run）；`65144`（第一条实测 run，停止后才更改最终映射并重启）；`86848`（最终 run 与截图，按 PID 停止）。所有宿主进程均已退出；浏览器 TaskSpace 19 已 `finish({keep:[]})`。临时运行数据与日志保留在 `/tmp/glink-uiacp-live-*`、`/tmp/glink-uiacp-final-*` 供 Lead 核对，未入 Git。
- 对齐参照为仓库 pi 族的 `presentation` 词表、前端同一视图和 brief 中的同题截图描述；本次**未新提交 pi 族同题真实模型 run**，不把 pi 侧同题 live 对比或模型事实核验写成已完成。当前用户服务未部署新代码，历史 dsh run/旧报告不会被重算。Ego Lite 提示有可用更新，未获升级授权，未升级。
