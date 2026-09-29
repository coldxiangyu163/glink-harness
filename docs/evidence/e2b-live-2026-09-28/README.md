# E2B live 验收：GLINK-ARCH-E2BLIVE-20260928

执行时间：2026-09-28 UTC / 2026-09-29 本机时区。被测源码为主 checkout `6c730234a10cc0e3ac5777eac3998b4a01174a7f`；本 worktree 同 HEAD，只写文档。真实 E2B 模板现有 `pi-glink-worker:default`，未构建、更新或切换。宿主 macOS arm64，Python 3.12.10、e2b SDK 2.51.0、pi 模板既有版本 0.86.1；模型配置来自主 checkout `.env`（openai/gpt-6-sol，经宿主 egress 代理），没有把任何密钥值写进证据。执行面为 `e2b` / 本机单用户 `local`，**不是**多租户 live 验收。

## 复现形状与原始退出状态

- 宿主从主 checkout 起：`env -u HTTPS_PROXY -u HTTP_PROXY ALL_PROXY=socks5h://127.0.0.1:1080 LOCAL_WEB_EXECUTOR=e2b PI_GLINK_TENANCY=local PI_GLINK_AUTH_MODE=gateway PI_GLINK_MODEL_CREDENTIALS=tenant LOCAL_WEB_PORT=<随机 loopback 端口> LOCAL_WEB_DATA_ROOT=<仓库外 0700 临时目录> PI_GLINK_EGRESS_PORT=<另一个随机端口> PI_GLINK_EGRESS_PUBLIC_URL=<临时 HTTPS tunnel URL> scripts/run-local-web.sh`。使用临时独占隧道把 HTTPS URL 反代至本次的本机 egress 端口，不复用已有服务；网络出口到 E2B 使用 SOCKS。服务 PID 与两个端口见 [processes.txt](processes.txt)。主 checkout 文件未修改。
- `scripts/smoke-local-run.py --base-url http://127.0.0.1:<随机端口> --timeout 600 --prompt '<写 notes.md 与 report.md，锚点松针-17>'`：exit **0**，run `10b065830440`，终态 `finished`、55 帧、报告 172 B。
- 同脚本，prompt 要求用 bash `for i in $(seq -w 1 70); do printf ... > "z${i}.txt"; done` 并写 `report.md`：exit **0**，run `f2a66107ec40`，65 帧。工具回执显示 `70` 个文件，宿主只回收 64 个普通文件（`report.md` + `z01.txt` 至 `z63.txt`）；终帧 `artifactFailures` 精确包含 `工作区产物超过文件数上限（64）`，仍为 `finished` 且报告可下载。
- `POST /api/runs`，`{"parentRunId":"f2a66107ec40","prompt":"追问上一轮的文件数量；写本轮 report.md..."}`：HTTP **201**，run `e2323dc13294`，终态 `finished`、136 帧；会话 id 与父轮一致。前轮报告和 `continued.jsonl` 均上传 HTTP 200，宿主命令含 `--session /home/user/sessions/continued.jsonl`；首个 `session_ready`（seq 2）早于 `agent_start`（seq 3），同一身份，收尾补发带宿主会话路径的 `session_ready`（seq 135）；本轮报告明确写 70 个文件及“续接”，父轮记录 SHA-256 前后相同。HTTP 客户端验证脚本正常退出 **0**，见 [run-excerpts.json](run-excerpts.json)、[host-excerpts.txt](host-excerpts.txt)。
- 另直接使用真实 `e2b.Sandbox.create('pi-glink-worker', timeout=120)`，写 18 B 普通文件；`files.get_info(path, request_timeout=15)` 返回 `size=18`，`files.read(path, format='stream', request_timeout=15)` 返回原字节；`kill()` 用时 0.278s、返回 true，`Sandbox.get_info(id)` 随后 404。该 SDK 直测 exit **0**，见 [sdk-probe.json](sdk-probe.json)。三条成功 run 的宿主摘录中没有 `request_timeout` / `TypeError` 异常，文件回收与流式读取实际完成。

## 收尾、限制与异常

三条成功 run 都按「工作区回收 → 主会话回收 → 沙箱 DELETE HTTP 204 → 终帧提交」完成；`cleanup.released=true`、`failures=[]`、`external.sandbox` 与持久记录的 `sandbox_id` 相同。终帧前最后一条“会话已回收”事件到终帧的**整个剩余收尾窗口**分别为 1.568 / 1.511 / 1.313 秒，小于 `kill_within_budget` 的 15 秒；这是正常销毁路径的上界，**不是挂起 kill 的实测**。六个由宿主创建的沙箱（含三条失败尝试）分别用真实 SDK `Sandbox.get_info(id, api_key=...)` 得到 `SandboxNotFoundException` / HTTP 404；直测沙箱也为 404。没有留下孤儿沙箱。所起服务和隧道只按已记录 PID 停止，`lsof` 对两个端口无监听，临时数据根已删除，见 [processes.txt](processes.txt) 与 [sandbox-absence.txt](sandbox-absence.txt)。

异常如实保留：第一次公网 Cloudflare 临时隧道虽给出 URL，但外部请求返回空体 404，run `a4c2a41d8cb1` 为 `model_error`；第二次改用短时 SSH 隧道后 `HTTPS_PROXY=socks5h://...` 被宿主 `urllib` 作为不支持的代理使用，run `0c05a572e116` 收到 `upstream_unreachable`；取消 `HTTPS_PROXY` 后真实 run 成功。首次对 `10b065830440` 的追问 `c0ee196a93c2` 因短时 tunnel 到期返回 `503 no tunnel here`，虽然 session id 对齐但该轮未成功；改用有效期 60 分钟的临时隧道后对另一个已完成父轮重新追问成功。上述失败均已终态、沙箱 404；没有修改产品代码。**另外三条成功 run 均意外带有** `无法列出子代理产物目录: FileNotFoundException: ... /sessions/subagent-artifacts` 的 `artifactFailures`，即未使用子代理也记录失败；不影响 `finished` / 报告回收，但应交 Lead 评估，未在本任务内修复。

未覆盖：挂起/卡死的 SDK kill 分支和 64 MiB 总量超限（仍由 fake SDK 回归覆盖），Docker 面、真实多租户与生产网关、UI/浏览器、模板重建。此前已持久化的历史 run 不因本次验证改变。

## 文件说明

- [run-excerpts.json](run-excerpts.json)：SQLite run 记录与事件终帧的必要字段、阶段/会话帧顺序、报告摘要、文件数与收尾窗口；不含完整 prompt、模型输出或密钥。
- [host-excerpts.txt](host-excerpts.txt)：仅保留目标文件传输、目标沙箱 DELETE 与 finalized 的宿主日志摘录，不归档含运行期配置或 system prompt 的完整日志。
- [sdk-probe.json](sdk-probe.json)：真实 SDK 的限时参数、流式读、独立沙箱销毁结果。
- [sandbox-absence.txt](sandbox-absence.txt)：按持久化 id 检查不存在；不枚举或触碰其他人的沙箱。
- [processes.txt](processes.txt)：本任务服务/隧道/探测进程 PID、端口与临时数据根清理。证据为清理前摘录，原始 SQLite 和完整宿主日志已随临时根删除。
