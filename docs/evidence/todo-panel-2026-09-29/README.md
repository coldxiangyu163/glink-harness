# GLINK-ARCH-TODOPANEL-20260929 验收

源码：worktree `feat/todo-panel-20260929`（提交见交付回执）；Node v24.16.0、Python 3.12（主 checkout `.venv`，仅借解释器与依赖）、ego-browser Chromium 152.0.7977.54。主 checkout 源码、模板和用户服务未修改；测试使用本 worktree 静态页面与宿主、单用户 local、`dsh`/ACP、真实 E2B 和模型。宿主只在内存中读取主 checkout `.env` 的模型/E2B 凭据，不归档密钥。

## 红绿与默认检查

- 新增 `tests/web/todo-panel.test.mjs` 和 `tests/web/fixtures/todo-updates.jsonl`：基线 `node --test tests/web/todo-panel.test.mjs` **exit 1**（首个断言缺最新面板），实现后 **exit 0**（3/3：完整事件经 reducer → 实际 DOM 最新列表、两条历史行、无源与清空、面板停靠与 dispose）。
- 初次 `bash scripts/check.sh` 使用系统 Python，缺 `pytest`，**exit 2**；按 brief 用主 checkout `.venv` 借依赖：`PATH="/Users/coldxiangyu/AI/git/pi-glink/.venv/bin:$PATH" bash scripts/check.sh` 最终 **exit 0**，Python 1343 passed / 1 deselected，Node 前端+扩展 199 passed，dsh bundle 41 passed，schema/ruff/mypy/覆盖率 ratchet 均 exit 0。外部标准库逐值对齐 SKIP；本入口不含浏览器/live。

## 隔离浏览器 + 真实 E2B

- 独占宿主 `127.0.0.1:52823`，PID **40245**；独占 egress `127.0.0.1:52824`（宿主内监听）；独占临时 Cloudflare tunnel PID **39423** 转到该 egress 端口。临时数据根 `/tmp/todo-panel-live-20260929/data`。未访问或停止 7861/7872，也未用 `pkill`。
- 真实 run `d6e586747cb4`：浏览器运行中初态实际观察 `进行中核验事实 / 待开始查证资料 / 待开始整理结论`，后更新为 `已完成核验事实 / 进行中查证资料 / 进行中整理结论`；计数 `1/3 项已完成 · 2 项进行中`，折叠后的过程展开有两行「计划更新」且不重复清单。重载历史后最新清单仍显示；面板已停靠在滚动区外（`#chat-view`），过程按钮可点击，不遮挡。事件库证实两次 `todo_write` 整表投影序号 6、10；`finished`，24 帧，报告保存。截图 [`live-history.png`](live-history.png)；结构化事实 [`acceptance.json`](acceptance.json)。初态观察为浏览器 snapshot（该瞬间未留截图）；并行最终态与历史行有截图。
- 独立真实 run `6b66759e1ae9` 不调用 todo：`finished`、12 帧、零 `todo_write`，浏览器运行结束与重载旧记录均断言面板不可见、没有历史计划行。截图 [`live-replay-no-todo.png`](live-replay-no-todo.png)；事实同上 JSON。
- 两个 run 的持久记录 `cleanup.released=true`、`failures=[]`、`resource_adjudication=null`、`report_status=ok`；真实 SDK `Sandbox.get_info(id, api_key=…)` 均 `SandboxNotFoundException`/404（SDK 脚本 exit 0），仅检查这两枚持久化 id。宿主与 tunnel 分别 `kill -TERM 40245 39423`，退出码 0，随后两 PID 不存在、52823/52824 无监听、HTTP 连接失败。临时数据根与原始日志在摘要归档后已删除。

## 限制

- 初态和最终态来自同一真实研究；浏览器先观察到初态，随后从同一页面观察到最终态。没有对两帧之间的具体渲染延迟逐帧计时。
- 真实验收是本次隔离服务，不代表用户的 7861/7872 服务已生效，也未构建/替换 E2B 模板；无多租户 live 验收。ego-browser 报告有可用更新，本次未升级。
