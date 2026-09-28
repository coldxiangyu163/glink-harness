# 证据：重构线合并后浏览器 UI 对齐验收（2026-09-28）

本目录对应任务 `GLINK-ARCH-UIALIGN-20260928`，被测树为 `afa9b26`。验收覆盖 token、三种身份面 acceptance、组件逐值对齐和真实宿主交互四层。结论是**未全绿**；本任务只归档证据和更新文档，没有修改产品或测试源码。

## 结果

| 层 | 命令 | 结果 | 退出码 |
|---|---|---:|---:|
| token | `python scripts/check-ui-tokens.py` | 173 / 173 逐值一致 | 0 |
| accounts | `acceptance.mjs --mode accounts` | 99 通过 / 3 失败（102） | 1 |
| local | `acceptance.mjs --mode local` | 76 通过 / 2 失败 / 3 跳过（81） | 1 |
| gateway | `acceptance.mjs --mode gateway` | 7 / 7 通过 | 0 |
| ui-parity | `ui-parity.mjs` | 30 个角色：28 OK / 1 mismatch / 1 SKIPPED；2 个逐值差异 | 1 |
| ui-features | `ui-features.mjs` | 52 / 54 通过 | 1 |

交付检查 `bash scripts/check.sh` 首次因 worktree/PATH 没有 `pytest` 在前置检查退出 2；改用任务指定主 checkout 的 `.venv` 作为依赖环境后退出 0（Python 1252 通过 / 1 deselected，Node 前端与扩展 151 通过，dsh bundle 42 通过）。两次原始结果均保留在 `default-check.txt`。

三种身份面合计 190 项：182 通过、5 失败、3 跳过。5 个失败实例归为 3 种机制：

1. accounts 邀请框按 `Esc` 后焦点落到 `BODY`（2026-09-27 已知 F-1）。
2. accounts/local 的“父会话文件不可用”检查得到 201；套件仍删除旧路径 `state/data/sessions/<runId>/session.jsonl`，当前会话路径已是 `state/data/runs/<runId>/sessions/session.jsonl`，因此本次操作没有制造预期前置条件，属于套件与新存储布局未同步，不能据此认定产品门禁回退。
3. accounts/local 的进度面板缺“用量”，其余宽度、开关、步骤、状态、用时与模型字段存在。

组件层的 `homeCard` 高度为 124px，参考页为 380px（`height` 与 `box.height` 两项差异）；`conversation/#progress` 的 `userBubble` 因参考屏没有对应角色而 SKIPPED。交互层失败为：真实完成 run 的回答区没有文件卡（报告入口、右槽与下载仍可用）；注入历史事件流 404 后没有出现“过程记录暂时读不到”恢复卡。SSE 断流重连、终帧收口、列表选择、报告槽、折叠、焦点、键盘与断点的其余检查通过。

## acceptance 输出路径兼容性

直接把 `--out` 指到仓库内时，`acceptance.mjs` 同时把 fixture 的 `state/` 放在该目录下；重构后的配置会拒绝仓库内 `PI_GLINK_ACCOUNTS_DB`，所以首次 accounts 启动在 READY 前失败。证据见 `accounts/in-repo-out-startup-failure.txt`。

三面正式运行改用仓库外的独占临时 `--out`，运行结束后只把验收产物复制回本目录，明确排除 `state/`。每面的实际命令、临时位置、退出码和控制台记录在 `<mode>/acceptance-run.txt`。`host-exit.json` 均为 `code: 0`：accounts PID 91152、local PID 93952、gateway PID 96158。

fixture 的 `fixture.log` READY 元数据包含合成 accounts 密码，因此未归档。`credential-scan.txt` 同时以真实 `.env` 凭据值、fixture 合成凭据值和通用模式扫描所有保留文件（含 PNG 字节），结果为 PASS。

## 真实宿主与参考站

- 参考站：`/Users/coldxiangyu/work/BR/glink-ui-library/dist`，最终有效 PID 6386，随机端口 62909。
- 真实宿主：主 checkout `afa9b26`，`inproc` + `local`，最终有效 PID 6390，随机端口 62910；模型面板显示 `gpt-6-sol`。
- 主 `.env` 的 data root 当时由另一宿主持有，本任务未停止或复用该服务；最终有效宿主改用仓库外临时 data root。
- 为让空 data root 上的 ui-parity 有可测的思考、工具与报告角色，先提交一条已完成的真实模型 run，结果见 `seed-run.txt`；ui-features 随后按脚本提交并约 6 秒后停止另一条真实研究。
- 启动尝试、每次 PID、端口占用检查和按 PID 清理均在 `service-lifecycle.txt`。最终 9 个已记录 PID 均停止，8 个曾用端口均无监听；临时 data root 已删除。全程未使用 `pkill` 或宽匹配清理。

## 文件索引

- `ui-tokens.txt`：token 命令、时间和退出码。
- `<mode>/acceptance.{json,txt}`：逐项结构化/可读结果；`*.png` 为关键阶段截图。
- `<mode>/host-exit.json`：隔离 fixture PID 与退出状态。
- `ui-parity.{json,txt}`、`ui-parity-run.txt`：30 角色逐值结果与原始退出码。
- `ui-features.{json,txt}`、`ui-features-run.txt`、`ui-features-*.png`：54 项真实宿主交互结果。
- `service-lifecycle.txt`、`seed-run.txt`：服务与真实模型运行记录。
- `credential-scan.txt`：提交前凭据扫描记录。
- `default-check.txt`：默认交付检查的缺依赖尝试与成功重试。

## 未覆盖

未运行 Docker/E2B、真实网关已认证路径、Safari/Firefox/移动真机、HTTPS/反代、多宿主并发或生产数据迁移。本次真实模型只证明本地 `inproc` 浏览器链路，不评价模型答案质量或云端隔离。
