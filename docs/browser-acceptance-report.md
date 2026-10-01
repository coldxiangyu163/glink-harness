# 浏览器全量验收报告

> **改名说明（2026-10-01）**：本仓库由 `pi-glink` 更名为 `glink-harness`（Python 包 `pi_glink`→`glink_harness`、环境变量 `PI_GLINK_*`→`GLINK_*`、镜像/模板/数据根同步更名）。本文改名前的实测记录保留当时旧名——它们是同一工程的历史标识，与 `docs/evidence/` 原始日志一致；现行命令、路径与变量名以当前源码为准。

## 2026-09-28 晚间：B2 浏览器行为闭环与全层复验（`6c73023`）

任务 `GLINK-ARCH-BROWSERWIN-20260928`。被测源码为主 checkout `6c73023`；验证 worktree 同 commit，未改产品或测试源码。Node 24.16.0、Python 3.12.10、project-local Playwright 1.58.2 / Chromium；真实宿主为主 checkout 的 local / inproc + 真实模型，参考站为已有 `dist/` 静态产物。隔离 acceptance 使用脚本化执行面。证据与可复现脚本均在 [`docs/evidence/browser-window-2026-09-28/`](evidence/browser-window-2026-09-28/)；各命令退出码留在对应 `*-command.txt`、`tokens.txt` 与 `acceptance-lifecycle.txt`。

| 层 | 本次结果 | 退出码 / 对照 09-28 uifix |
|---|---|---|
| token | 173/173 | 0，持平 |
| acceptance accounts | **102 通过 / 0 失败**；F-1 Esc 焦点转绿 | 0，原 101/102（预期红） |
| acceptance local | 78 通过 / 0 失败 / 3 跳过 | 0，持平 |
| acceptance gateway | 7/7 | 0，持平 |
| ui-parity | 29 OK / 0 mismatch / 1 SKIPPED（30 个角色） | 0，持平 |
| 原版 ui-features | **53/54**，唯一失败为空列表桩未命中分页 URL | **1**，低于原 54/54；定向空列表实测另为 0 |

三面 acceptance 合计 **187 通过 / 0 失败 / 3 跳过（190 项）**；不是 brief 中“189 通过/3 跳过”的算术值。`ui-features` 并未声称 54/54：原版第 K 节仅拦截 `**/api/runs`，B2 后首次列表请求为 `/api/runs?limit=50`，返回真实数据而非空列表，失败 detail 是 `rows:2, homeEmpty:0`。同一真实宿主的**独立定向**检查改为对该 URL 注入合成空列表，测得 `rows:0, homeEmpty:2, role:status, variant:no-data`，退出 0（`ui-empty-targeted.json/png`）。因此这条是历史脚本测量前置过期，**脚本未修、未将定向结果冒充原版绿测**；原版余 53 项均通过。需由后续测试 owner 修正历史脚本后才可称全层脚本全绿。

### B2 四项行为

- **M8 单流回放**：`m8-real-chain.py` 在独立临时数据根创建真实模型 5 轮（初轮 + 4 次连续追问，均 `finished`），再从侧栏点最深一轮。页面注入的 `EventSource` 包装器逐次记构造和 `close`：4 段前史全部显示，5 次 open / 5 次 close、**并发峰值 2、最终活跃 0**；`m8-stream-count.json`、`m8-history.png`、运行命令退出 0。5 轮中只有首轮生成报告，后续 `no_report` 不影响回放与计数。
- **M9 >100 条分页**：停止 M8 宿主后，在**新**临时数据根开机前用 `m9-inject.py` 经 `RunStore.create/commit_terminal` 写入 SQLite 的 110 条合成已完成记录与 110 条终帧（不调用模型）；链条 4 轮横跨分页边界。真实宿主首批 50 条，点「显示更多」2 次后分页载荷 50/50/10、110 个唯一 ID 均取得；侧栏为 **107 条顶层**、3 个中间轮不重复列为顶层，最老独立记录可见，点链尾显示 3 段前史。`m9-injected.json`、`m9-pages.json`、三张截图与命令退出 0；链尾打开后还触发一次正常的首批 50 条刷新，不计作加载更多。
- **M10 双标签 CSRF**：隔离 accounts 真实宿主 + 同 cookie context 的两个独立 page JS realm。A 登录，B 注销并重新登录使 cookie / csrfToken 轮换；A 不刷新直接提交真实 UI POST，网络顺序为 `403 csrf_invalid → GET /api/me 200 → POST /api/runs 201`，运行收口为 finished，未出现失败 toast。负例通过 Playwright 转发 UI 发出的请求到同一真实宿主、仅把 `Origin` 换为异站，**服务端返回非 CSRF 的 `403 forbidden`**；A 不重试，首页出现“研究没能开始，请稍后重试。”失败通知。`m10-sequence.json`、前后截图和命令退出 0；一开始尝试浏览器 `route.continue` 改 Origin 未生效（记录为诊断失败，已改用 `route.fetch` 真实请求）。两个 page 共用 cookie 是复现轮换所必需，并非两个互不共享登录状态的 browser context。
- **M11 呈现**：独立真实 pi run 完成、3 张工具卡动词为“执行 / 写入”、文件卡可见；`m11-real-tools.json/png`、命令退出 0。ACP 真实 run 未执行（可选，成本较高）；本证据不证明 ACP kind 的浏览器真实呈现。

### 资源、限制与安全

三面 fixture PID 30161 / 32469 / 34805 均由 acceptance 按记录 PID 结束（exit 0）。真实宿主、参考站、M9/UI 临时宿主和 M10 fixture 的完整 PID、随机 loopback 端口、起止与监听复核在 `service-lifecycle.txt` / `m10-sequence.json`：**全部已停止、全部端口无监听**；M8、M9、UI 临时 data root 与 M10 state 均删除；acceptance 的仓库外临时 `--out` 删除后仅将验收 JSON/TXT/PNG/host-exit/requests 复制回来，排除 `state/`、`fixture.log`。凭据扫描覆盖 90 个保留文件（含 PNG 字节），对照主 `.env` 中 2 个真实密钥值、3 个 fixture 合成凭据值及通用模式，未命中，`credential-scan.txt` 退出 0。未碰主 checkout 文件、用户已有服务、Docker/E2B、模板或真实网关；未做默认 `scripts/check.sh`（本次仅验证浏览器窗口、未修改源码）。真实 run 的临时数据已清理，故不保留逐 run 的 cleanup/resource_adjudication 原始存储；本报告只断言主机 PID/端口和临时目录清理，不把未存档的外部资源结论写成已证实。

---

## 2026-09-28：重构线 `afa9b26` 全套复验

- 任务：`GLINK-ARCH-UIALIGN-20260928`
- 被测基线：`afa9b26`（与当时 `main` 相同）
- 对照点：任务口径中的上一轮全套浏览器验收点 `421ab15`；当前 `web/modules/` 相比该点为 7 个文件、+1923/−678，新增事件 schema、reducer 与 run-list view，`web/styles.css` / `web/index.html` 未变。
- 环境：Node 24.16.0；acceptance 使用 project-local Playwright 1.58.2；ui-parity / ui-features 使用历史脚本解析到的本机 Playwright；真实宿主为主 checkout 的 `inproc` / `local` 面。
- 证据：`docs/evidence/browser-acceptance-2026-09-28/`。

### 结论

四层均已执行，但**本次不通过全绿验收**：

| 层 | 结果 | 退出码 |
|---|---:|---:|
| token | 173 / 173 逐值一致 | 0 |
| acceptance accounts | 99 通过 / 3 失败（102） | 1 |
| acceptance local | 76 通过 / 2 失败 / 3 跳过（81） | 1 |
| acceptance gateway | 7 / 7 通过 | 0 |
| ui-parity | 30 个角色中 28 OK / 1 mismatch / 1 SKIPPED；2 个逐值差异 | 1 |
| ui-features | 52 / 54 通过 | 1 |

三种身份面仍为 190 项，合计 182 通过、5 失败、3 跳过。失败实例中，一项是 09-27 已知问题，一项是套件未跟随存储路径迁移形成的无效前置条件；其余结果暴露新的对齐/集成偏差。本任务只记录证据，没有修改 `host/`、`web/` 或 `tests/`。

### 失败与偏差

1. **已知 F-1 仍在**：accounts 邀请对话框按 `Esc` 后已关闭，但焦点落到 `BODY`，没有回到可见入口。复现仍是“管理员登录 → 账号菜单 → 邀请成员 → Esc”。
2. **acceptance 会话缺失检查与新布局未同步**（accounts/local 各一项）：检查得到 201 而非预期 409。套件删除的是旧路径 `state/data/sessions/<runId>/session.jsonl`，当前实现使用 `state/data/runs/<runId>/sessions/session.jsonl`，因此本次并未实际删除父会话文件；该项不能作为产品门禁回退的证据。
3. **acceptance 进度面板缺用量**（accounts/local 各一项）：340px 宽度、开关、步骤、状态、用时和模型均存在，但文本没有“用量”。复现为完成 fixture 的 subagent 场景后打开“进度与上下文”。
4. **ui-parity 首页卡高度偏离**：`homeCard` 参考值 380px、当前值 124px，分别在 `height` 和 `box.height` 形成 2 个差异。其余 28 个屏幕角色通过；`conversation/#progress` 没有参考 `userBubble`，按脚本规则记 SKIPPED。复现命令及逐属性值见 `ui-parity.txt/json`。
5. **真实宿主回答区不再画文件卡**：真实完成 run 有报告入口，报告右槽和下载均可用，但历史重放时 `.pi-output` 不可见，导致“待核验草稿 + 文件卡 + 动作条”检查失败。复现为完成一条写出 `report.md` 的真实研究，再从侧栏打开历史。
6. **历史流 404 恢复卡未出现**：ui-features 对已完成 run 的事件流注入 404 后重新打开该历史，未显示“过程记录暂时读不到”及“重试”动作。该检查在 09-27 通过，需要结合重构后的订阅时机判断是行为回退还是历史脚本注入点失效。

SSE 首次断流后的有界重连、finished 终帧收口、侧栏/首页列表选择、报告槽与分割线、过程折叠、焦点、键盘、断点、租户拒绝和 gateway 未认证入口的其余自动检查通过。真实模型面板显示 `gpt-6-sol`；一条预置 run 完整结束，ui-features 的测试 run 按设计约 6 秒后停止。这里不评价模型答案质量。

### 执行与资源边界

默认交付检查首次因 worktree/PATH 缺少 `pytest` 在前置检查退出 2；使用任务指定主 checkout 的 `.venv` 作为依赖环境重跑后退出 0（Python 1252 通过 / 1 deselected，Node 151 + 42 通过），见 `default-check.txt`。

acceptance 直接把 `--out` 指到仓库时会把 fixture state 一并放进仓库；新的配置边界拒绝仓库内 accounts DB，首次启动因此在 READY 前失败。正式三面运行改用仓库外独占临时输出目录，完成后只归档证据并排除 `state/`。accounts/local/gateway 的 fixture PID 分别为 91152 / 93952 / 96158，`host-exit.json` 均为 code 0。

主 `.env` 的 data root 当时被另一个宿主持有，本任务没有停止或复用它。最终参考站 PID 6386、真实宿主 PID 6390，使用随机 loopback 端口；均按记录 PID 停止。连同启动诊断阶段在内的 9 个记录 PID 最终全部停止，8 个曾用端口全部无监听，临时 data root 已删除；详见 `service-lifecycle.txt`。未使用 `pkill` 或宽匹配清理。

提交前移除了会暴露 fixture 合成密码的 `fixture.log`，并用主 `.env` 的真实凭据值、fixture 合成凭据值及通用模式扫描全部保留证据（含 PNG 字节）；`credential-scan.txt` 结果为 PASS。

### UIFIX follow-up 闭环（`GLINK-ARCH-UIFIX-20260928`）

基于 `0139fb3` 的逐项归因确认：R1/R2/R3 都是历史浏览器脚本的测量前置、选择器或等待时机问题，产品 UI 本身没有对应回归；因此没有修改 `web/` 或 `host/`。S1/S2 则是 acceptance fixture 没有同步 egress 权威用量与 SQLite run 目录，已修复。

| 层 | follow-up 结果 | 退出码 |
|---|---:|---:|
| acceptance accounts | 101 通过 / 1 失败（102）；只剩范围外已知 F-1 | 1（预期红） |
| acceptance local | 78 通过 / 0 失败 / 3 跳过（81） | 0 |
| ui-parity | 30 个角色中 29 OK / 0 mismatch / 1 SKIPPED；0 个逐值差异 | 0 |
| ui-features | 54 / 54 | 0 |
| `bash scripts/check.sh` | Python 1252 通过 / 1 deselected；Node 151 + 42 通过 | 0 |

- **R1**：首页卡随真实 run 数量增长；旧宿主数据填满时目标恰为 380px，本次一条 run 时为 124px。修正 `ui-parity` 的内容高度测量口径，卡片 chrome 与行几何仍逐值比较。
- **R2**：同一次红测截图已经显示文件卡。子代理面板复用 `.pi-output` 且排在真实文件区前，旧脚本选中了隐藏的 `.pi-output.pi-children`；改用 `[aria-label="生成的文件"]` 后转绿，并同步 run-smoke/probe 消费者。
- **R3**：产品 404 恢复卡正常。旧脚本在 2s 取样，但 EventSource 的既定有界重试预算为 1.5s + 4s + 10s；改为等待恢复卡（20s 上限）后得到“过程记录暂时读不到”和“重试 / 打开报告”。
- **S1**：FakeAdapter 现在在终帧提交前把合成模型响应计入 live egress grant；accounts/local 均显示“用量 · 1,000 token”。
- **S2**：删除路径改为 `data/runs/<runId>/sessions/session.jsonl`；accounts/local 均验证 `409 session_unavailable`。

绿证据与 PID 清理记录见 `docs/evidence/browser-acceptance-2026-09-28/uifix/`。真实参考站/宿主 PID 73857/73861 已按 PID 停止，端口释放、临时 data root 删除；acceptance 的定向/全量 fixture PID 71649/88651/91960 均 code 0。增量凭据扫描结果为 PASS。

### 未覆盖

未执行 Docker/E2B、真实网关已认证路径、HTTPS/反向代理、Safari/Firefox/移动真机、多宿主并发、生产数据迁移或模型质量评估。此次真实面只证明本地 `inproc` 浏览器链路；隔离 acceptance 仍使用脚本化执行替身。

---

以下保留 2026-09-27 的基线记录，便于逐项对照。

- 日期：2026-09-27（CST）
- 被测基线：`52e622d`（浏览器验收套件本批提交；宿主与前端代码为 `67274a7` + 本批新增文件）
- 测试对象：`web/`（生产前端，无构建链静态 ESM）+ `host/pi_glink/`（真实宿主 HTTP/SSE/存储）+ 三种身份面
- 测试工具：`tests/browser/acceptance.mjs`（本次新增，project-local Playwright 1.58.2 / Chromium 145 / Node 24.16.0 / Python 3.12.10）
- 证据：`docs/evidence/browser-acceptance-2026-09-27/`（合成数据，零真实凭据）

## 结论

在**隔离的真实宿主**上用 Chromium 跑了 **190 项**浏览器检查（accounts 102 / local 81 / gateway 7）：

| 模式 | 项数 | 通过 | 失败 | 跳过 | 结论 |
|---|---|---|---|---|---|
| accounts（多租户 + 站内账号） | 102 | 101 | 1 | 0 | 无阻塞缺陷；1 个低严重度可访问性发现（F-1） |
| local（单用户本地面） | 81 | 78 | 0 | 3 | 全通过；账号/租户维度按设计不适用 |
| gateway（多租户 + 网关身份） | 7 | 7 | 0 | 0 | 未认证入口与静态资产全通过 |

前端的主要用户面——登录/邀请注册/注销、外壳与断点、输入器、运行生命周期（含工具卡、失败、截断、停止）、历史重放与追问、报告槽与分割线、进度与上下文浮层、子代理派发/进度/产物、跨租户越权矩阵、断流重连与恢复面——都在**真实浏览器**里验证过；`shell`/`composer`/`lifecycle`/`subagent`/`report`/`tenancy`/`resilience` 七个维度零失败。

唯一失败项是键盘焦点问题（F-1），不影响主流程，列出复现步骤与代码机制。

## 1. 测试对象与范围

| 层 | 本次覆盖 |
|---|---|
| 前端页面 | `web/index.html` + `web/modules/*.mjs` + `web/styles.css`（生产资产，由宿主原样吐出） |
| 宿主 HTTP/SSE | `/api/me`、`/api/runs`（创建/列表/详情/事件流/停止/报告/子代理文件）、`/api/invitations/check`、静态资产 |
| 身份面 | accounts（两租户 + 管理员/成员角色）、local（单用户）、gateway（未认证面） |
| 运行事实 | 合成场景：默认（思考+回答+报告）、工具调用（成功/失败）、子代理派发（含运行中进度与产物）、停止、模型错误、输出截断 |
| 租户边界 | 跨租户读/写/取消/追问、同租户成员读/取消、跨站来源拒绝 |
| 韧性 | SSE 断流重连、运行中刷新、历史记录不可读、模块图失败恢复、控制台清洁度 |

**不在本次范围**：真实 pi / Docker / E2B 执行、真实模型与检索质量、真实网关断言注入、HTTPS/TLS、生产数据与迁移、多宿主并发（这些需要单独授权与对应环境，见 §6）。

## 2. 环境与隔离方式

`tests/fixtures/accounts_server.py` 起一个**真实宿主**：真实 `RunManager`/`RunStore`/`EventBus`/`Ownership`/`HTTP`/`SSE` 与生产前端，数据根、SQLite 账号库、`HOME`/`XDG` 全部指向本次运行的临时目录；唯一替换的是执行面（`pi_glink.kernel.harness_pi.compose_adapter` → 进程内 `FakeAdapter`），因此**没有任何** pi 进程、容器、沙箱或模型调用。凭据是 fixture 现场写出的合成值（0600），运行结束随临时目录丢弃。

每个模式的独立运行都有：OS 分配的 loopback 端口、独立临时数据根、独立浏览器上下文（Cookie 不共享）、按 PID 终止（`host-exit.json` 记录 `{"code": 0}`）。`requests.jsonl` 原样记录请求行与 Referer，用于证明邀请 token 从未进入请求行（`#invite=` 只存在于浏览器 URL hash）。

**这是隔离，不是安全沙箱**：它避免意外依赖个人配置，并保证被测对象真实；它不替代真实 Docker/E2B/网关的端到端验收。

## 3. 结果总览

### accounts（102 项）

| 维度 | 项数 | 结果 |
|---|---|---|
| identity 登录/邀请/注册/注销 | 19 | 18 ✅ / 1 ❌（F-1） |
| shell 外壳/断点/键盘 | 16 | 16 ✅ |
| composer 输入器 | 8 | 8 ✅ |
| lifecycle 运行生命周期/历史/追问 | 21 | 21 ✅ |
| subagent 子代理派发/进度/产物 | 8 | 8 ✅ |
| report 报告槽 | 8 | 8 ✅ |
| progress 进度与上下文 | 7 | 7 ✅ |
| tenancy 跨租户与角色 | 6 | 6 ✅ |
| resilience 断流/恢复面/泄露边界 | 9 | 9 ✅ |

### local（81 项）

identity 3（1 跳过）、shell 16（1 跳过：账号区不存在）、composer 8、lifecycle 21、subagent 8、report 8、progress 7、tenancy 1（跳过：无租户身份）、resilience 9 —— **全部通过**。

### gateway（7 项）

未认证入口 5 项（说明面无表单、`/api/me` 401+mode=gateway、匿名读写拒绝、跨站 POST 拒绝、文字不暴露内核）+ 静态资产 2 项（15 个前端资源全 200、控制台无意外错误）。

## 4. 覆盖要点（节选）

- **身份**：未登录面不揭示工作台、也无历史闪现；错误密码与未知账号同形；会话 Cookie `HttpOnly`；邀请链接只在 URL hash、一次性、复用报“无效或已使用”；注册校验（<10 字符、两次不一致）在字段旁拒绝；注册后回登录面由用户自己登录；注销后 API 立即 401、刷新仍为登录面。
- **外壳**：8px 沟槽 / 240 侧栏 / 52 栏头；断点 240→212（<1050）→64（<721）、账号区 <721 只留 28px 头像、各档无横向溢出；空格键序焦点（skip-link 首焦点、普通控件 2px `--ring`、受管输入由容器给 1px 近黑激活态）。
- **运行生命周期**：运行中停止键原位替换发送键、输入只读、侧栏只标记当前研究、刷新后从服务端记录恢复运行中；停止是中性终态（虚线圆、明说“这不是错误”）；工具卡按动作命名并可展开、失败行有状态字；模型错误给失败卡 + 已交付内容保留 + 查看详情；`stopReason=length` 显示“交付不完整”；历史重放贴底、过程自动收起；追问把父轮折叠进“之前的研究”，列表按会话合并；父会话文件缺失时 409 拒绝续接；“重新研究”以同一问题开启新一轮。
- **子代理（GLINK-SUB-R2/R3）**：一次派发两个任务 → 两张派发卡片（不是通用工具卡）；运行中卡片写“N 步 · 最近<动作> <对象>”（transcript 进度）；子代理面板标题“派发了 2 个子代理”、行状态从“进行中”到“已完成”并带时长/工具次数；每个子代理四个产物链接可经宿主代理下载；历史重放从落盘记录重建卡片、面板与 8 个产物链接；children API 与面板一致（2 个 child、meta 增强）。
- **报告与进度**：右槽两栏、默认对半、分割线可拖/记忆/双击复位、对话列不窄于 560/报告不窄于 360、主区 <921 改覆盖浮层；下载名“研究报告-…md”、正文 Markdown 渲染；进度浮层 340、时间轴步骤、运行信息（状态/用时/用量/模型，模型名去路由前缀）、来源页签列出检索词、Esc 收起并把焦点还给开关；单一槽位（打开进度面板时报告让位）符合当前实现语义。
- **租户与安全**：异租户读面一律 404（详情/SSE/报告/子代理）、写面 404（取消/追问）、同租户成员可读不可取消（403）；邀请 token 不进请求行；页面 HTML/可见文字/`localStorage`/URL 不含任何合成凭据；界面文字不含仓库名/执行面/内核/会话 id；匿名探测产生的 401 是唯一预期控制台错误。
- **韧性**：SSE 首次断开后前端重连、研究仍以 finished 收口且正文完整；事件流不可读时给“过程记录暂时读不到”与重试；模块图失败给恢复面、重试后历史完整恢复。

## 5. 发现

### F-1（低，可访问性）：邀请对话框 Esc 关闭后焦点回落到 `body`

- **维度/检查**：identity ·「邀请对话框：Esc 关闭并把焦点还给入口按钮」
- **复现**：登录（管理员）→ 点账号菜单 → 点“邀请成员” → 按 `Esc`
- **实测**：对话框关闭（`data-open=false`、`hidden=true`），但 `document.activeElement` 是 `BODY`；观察到的 `inviteVisible: false`（入口按钮随菜单一起关闭）。
- **预期**（代码自己的意图）：焦点回到触发按钮。`web/modules/account.mjs` 的 `closeDialog({ focus: true })` 会调用 `inviteItem.focus()`，但 `openDialog()` 先执行了 `closeMenu()`，`#account-invite` 此时在 `hidden` 的菜单里，`focus()` 无效。
- **影响**：键盘/屏幕阅读器用户关闭对话框后失去位置，需要重新 Tab 定位。主流程（鼠标/触摸）不受影响。
- **建议**（未实施，属交互决定）：焦点回退到仍可见的兜底目标（如 `#account-button`），或让菜单在对话框关闭前保持可见。
- **证据**：`docs/evidence/browser-acceptance-2026-09-27/accounts/acceptance.json` 中该检查的 `detail`。

### 已核实非缺陷的行为（记录在案）

- **报告与进度面板互斥**：`web/modules/workspace.mjs` 的 `slot` 是单值状态，打开进度面板会让报告让位，`Esc` 收起后两者都关闭。这是当前实现语义（检查「单一槽位…」通过），不是误关；若产品希望两者共存，需要改槽位状态机。
- **跨租户取消返回 404、同租户非创建者返回 403**：与 `tests/test_multitenant_api.py` 的既有契约一致。
- **gateway 未认证面**：CSRF/来源校验排在身份校验之后，因此无名跨站 POST 得到 401 而不是 403，动作未执行，属安全可接受顺序。

## 6. 明确未覆盖 / 不能由此报告推断

1. **真实执行面**：本批全部用合成执行面。真实 pi（inproc/Docker/E2B）、ACP 族、真实工具与检索质量没有在浏览器里验证；真实模型下的长回答、超长报告、大量工具调用的渲染性能未测。
2. **真实网关**：gateway 面的“已认证用户”路径需要网关签发断言，本次只覆盖未认证面。
3. **生产环境**：HTTPS/TLS、真实域名与 Cookie `Secure`、反向代理、CDN、跨浏览器（只测了 Chromium 145；未测 Safari/Firefox/移动端真机）。
4. **数据与并发**：未在真实历史数据、旧布局报告、多宿主并发、SSE 连接额度打满等条件下测试。
5. **自动化门禁**：`acceptance.mjs` 目前**不在** `scripts/check.sh` 内，也不在 CI；把它变成门禁需要决定是否允许在 CI 装浏览器依赖。
6. **像素级对齐**：本报告不替代 `scripts/check-ui-tokens.py` 的 token 逐值检查与历史 `ui-parity.mjs` 的组件逐值比对（后者需要标准库参考站）。

## 7. 资源、回退与清理结论

- 每次运行创建：一个临时目录（数据根/账号库/HOME/日志/证据）+ 一个隔离宿主进程 + 一个 Chromium 进程；模式之间互不复用。
- 隔离宿主由调用方按记录的 PID 发 `SIGTERM`，三次运行退出码都是 `0`（`host-exit.json`：accounts pid 64646 / local pid 73905 / gateway pid 80914）。
- 未使用、未修改、未停止任何既有服务；未写 `~/.pi`、`~/.pi-glink`、仓库 `outputs/`；未发起真实模型调用（也就没有云端费用）。
- 回退：删除 `tests/browser/`（除历史三个脚本）与 `tests/fixtures/accounts_server.py` 的本批改动即可恢复；`docs/evidence/browser-acceptance-2026-09-27/` 只是证据，可整体删除。

## 8. 复现

```bash
cd tests/browser && npm install --no-audit --no-fund
node ../browser/acceptance.mjs --mode accounts   # 102 项，预计 4–5 分钟
node ../browser/acceptance.mjs --mode local      # 81 项，预计 3–4 分钟
node ../browser/acceptance.mjs --mode gateway    # 7 项，约 30 秒
```

（在仓库根目录执行等价：`node tests/browser/acceptance.mjs --mode accounts`。）

## 9. 证据索引

```
docs/evidence/browser-acceptance-2026-09-27/
├── accounts/{acceptance.json,acceptance.txt,requests.jsonl,host-exit.json,*.png}
├── local/{acceptance.json,acceptance.txt,host-exit.json,*.png}
└── gateway/{acceptance.json,acceptance.txt,host-exit.json,gateway-face.png}
```

- `acceptance.json`：逐项检查（dimension/name/ok/detail）——报告所有数字的来源
- `requests.jsonl`：accounts 运行的原样请求行与 Referer（470 条，无 token）
- `*.png`：运行中/已停止/工具卡/失败卡/追问/子代理运行与收口/报告/最终态
- 套件与 fixture 用法见 `tests/browser/README.md`

## 附录 A：accounts 完整检查清单

### identity

| 检查 | 结果 |
|---|---|
| 未登录入口：单卡登录面、工作台外壳不揭示、也无历史闪现 | ✅ |
| 未登录 API：/api/runs 401 且带 mode=accounts | ✅ |
| 登录失败：字段旁给出同形错误、不进入工作台 | ✅ |
| 登录成功：账号区显示组织与姓名（首字母头像） | ✅ |
| 会话 Cookie：HttpOnly 且非 Secure 的 loopback 会话（口令不进 localStorage） | ✅ |
| 浏览器存储：没有会话/凭据类键 | ✅ |
| 账号菜单：管理员可见组织/邮箱/角色 + 邀请成员 + 退出登录 | ✅ |
| 邀请对话框：标题、角色选项（成员/管理员） | ✅ |
| 邀请结果：只显示一次的链接（token 在 URL hash，不进请求行） | ✅ |
| 邀请对话框：Esc 关闭并把焦点还给入口按钮 | ❌ |
| 服务端请求日志不含邀请 token（hash 只留在浏览器） | ✅ |
| 注册校验：过短密码与两次不一致都在字段旁拒绝、不提交 | ✅ |
| 邀请链接 → 注册表单：组织与受邀邮箱只读预填 | ✅ |
| 注册成功：回到登录面并提示用新账号登录（不自动登录） | ✅ |
| 普通成员：菜单无「邀请成员」、有退出；历史隔离为空 | ✅ |
| 邀请码一次性：再次打开提示无效/已使用且不出注册表单 | ✅ |
| 注销：回到登录面、账号与历史内容不闪现 | ✅ |
| 注销后 API：会话立即失效（401） | ✅ |
| 注销后刷新：仍是登录面（服务端会话已废止） | ✅ |

### shell

| 检查 | 结果 |
|---|---|
| 外壳几何：8px 沟槽 + 240 导航 + 52 栏头，品牌取标准库资产 | ✅ |
| 字体：页面作用域与标准库 Demo 同族（Noto Sans SC 优先） | ✅ |
| 服务正常时侧栏页脚不出现状态牌 | ✅ |
| 首页：标题 + 双卡 + 无选中、栏头右簇为空 | ✅ |
| 空态：侧栏 + 两张首页卡都画 EmptyState（role=status / no-data） | ✅ |
| 侧栏收起：64 图标条 + aria 两态 + 「新建」去描边 | ✅ |
| 侧栏收起态悬停：Tooltip 圆角 8 / 12px / 近黑 | ✅ |
| 侧栏折叠状态刷新后保持 | ✅ |
| 侧栏可再展开（回到 240） | ✅ |
| 断点：侧栏 240/212/64 与标准库同为 width<N 边界 | ✅ |
| 断点：各档均无横向溢出 | ✅ |
| 断点：首页双列 → 单列（1050 边界） | ✅ |
| 断点：账号区 <721 只留 28px 头像 | ✅ |
| 跳转链接：Tab 首个焦点且滑入可见 | ✅ |
| 键盘焦点可见：普通控件画 2px --ring | ✅ |
| 键盘焦点可见：受管输入由容器给出激活态（1px 近黑描边 + focus-within） | ✅ |

### composer

| 检查 | 结果 |
|---|---|
| 输入器：空输入发送键禁用、不显示计数 | ✅ |
| 输入器：普通长度 120 高、发送键启用、计数隐藏 | ✅ |
| 输入器：多行自增到 240 上限并滚动 | ✅ |
| 输入器：接近上限才显示计数 | ✅ |
| 输入器：清空后回落并禁用发送 | ✅ |
| 输入器：激活态 1px 近黑描边 + 内层认领焦点 | ✅ |
| 输入器：输入法组字中的回车不提交 | ✅ |
| 输入器：Shift+Enter 换行不提交 | ✅ |

### lifecycle

| 检查 | 结果 |
|---|---|
| 运行中：停止键原位替换发送键、输入只读、侧栏只标记当前研究 | ✅ |
| 运行中：过程折叠展开并写动作（不写内核生命周期） | ✅ |
| 运行中：输入器实测高度写回留白变量 | ✅ |
| 运行中刷新：服务端记录让页面恢复运行中（可继续停止），运行中没有报告入口 | ✅ |
| 停止：中性终态（虚线圆、说明「这不是错误」）、输入恢复 | ✅ |
| 默认场景：问题进入气泡、回答带待核验草稿、成功不画终态卡 | ✅ |
| 回答动作条：复制 / 下载报告 / 重新研究 | ✅ |
| 工具卡：三条工具行按动作命名（执行/读取/检索），失败行写状态字 | ✅ |
| 过程折叠：完成后写「已完成 · N 个步骤」并自动收起 | ✅ |
| 工具行可展开看细节（高度张开 > 收起）、不露宿主绝对路径 | ✅ |
| 失败场景：失败卡 + 已交付内容保留 + 重试/查看详情 | ✅ |
| 失败详情：展开后能看到原始错误 | ✅ |
| 截断场景：finished + stopReason=length 显示「交付不完整」 | ✅ |
| 历史列表：一次停止 + 四个已结束研究，按会话合并列出 | ✅ |
| 历史重放：时间线完整、贴底、过程收起、文件卡给出报告 | ✅ |
| 历史重放的追问入口：输入器写「继续追问」 | ✅ |
| 追问：父轮折叠进「之前的研究」、当前轮独立、列表行数不变 | ✅ |
| 刷新后历史仍在：侧栏条目与首页最近研究都非空 | ✅ |
| 命令门禁：`/run …` 被拒（不创建研究、不进入运行态） | ✅ |
| 追问门禁：父会话文件不可用时拒绝续接（不静默新起会话） | ✅ |
| 重新研究：同一问题开启新一轮，列表新增一条 | ✅ |

### subagent

| 检查 | 结果 |
|---|---|
| 派发卡片：一次派发两个任务 → 两张卡片（不是通用工具行） | ✅ |
| 运行中进度（R3a）：卡片写「N 步 · 最近<动作> <对象>」 | ✅ |
| 子代理面板（运行中）：标题「派发了 2 个子代理」、两行写「进行中」 | ✅ |
| 子代理收口：两行转「已完成」并带时长与工具次数 | ✅ |
| 子代理产物（R2）：每个子代理列出 input/output/meta/transcript 四个可下载链接 | ✅ |
| 子代理产物可下载（宿主代理同一道路径校验，正文来自 children/<n>/） | ✅ |
| children API 与面板一致：两个 child、meta 增强、各 4 个产物 | ✅ |
| 子代理历史重放：卡片、面板、产物链接都从落盘记录重建 | ✅ |

### report

| 检查 | 结果 |
|---|---|
| 报告右槽：两栏一线、默认对半、名字档写文件名与待核验草稿 | ✅ |
| 报告正文：Markdown 渲染（H1 + 段落），无横向溢出、无重名控件 | ✅ |
| 报告开合键：双箭头收起、对话收起键让位 | ✅ |
| 分割线：可拖改宽并记住（localStorage） | ✅ |
| 分割线：对话列不窄于 560、报告不窄于 360 | ✅ |
| 分割线：双击复位 | ✅ |
| 报告下载：文件名可读（研究报告-…）、内容非空 | ✅ |
| 窄主区（1000px）：报告改覆盖浮层、竖线隐藏、不溢出 | ✅ |

### progress

| 检查 | 结果 |
|---|---|
| 单一槽位：打开进度面板时报告让位（不叠两个槽位） | ✅ |
| 进度与上下文：340 浮层、开关两态、时间轴有步骤、运行信息齐全 | ✅ |
| 进度面板：模型名去掉路由前缀（browser-model） | ✅ |
| 进度面板：来源页签可切换、列出检索词 | ✅ |
| 进度面板：Esc 收起并把焦点还给开关 | ✅ |
| 单一槽位：Esc 收起后两个槽位都关闭（报告不自动恢复） | ✅ |
| 报告收起：开关回到左栏栏头并接住焦点 | ✅ |

### tenancy

| 检查 | 结果 |
|---|---|
| 异租户列表：看不到组织甲的任何研究（UI 空态 + API 也不含） | ✅ |
| 异租户读面：详情 / SSE / 报告 / 子代理一律 404（同一 unknown_run 口径） | ✅ |
| 异租户写面：取消与追问都不泄露该 run 存在 | ✅ |
| 跨站来源的 POST：被拒绝（Origin 校验先于身份） | ✅ |
| 同租户成员：可读他人研究但不能取消（403 forbidden） | ✅ |
| 同租户管理员：自己的研究仍可读（拒绝是租户/角色范围而非全面封锁） | ✅ |

### resilience

| 检查 | 结果 |
|---|---|
| 流断开后前端有界重连：研究仍以 finished 收口且正文完整 | ✅ |
| 过程记录读不到：给出说明与下一步（重试 / 打开报告），不无限「重连中」 | ✅ |
| 模块图失败：恢复面（role=alert）+ 近黑胶囊重试键 | ✅ |
| 重试后工作台完整恢复（历史仍在） | ✅ |
| 页面（HTML/可见文字/存储/URL）不含任何合成凭据 | ✅ |
| 界面文字不暴露仓库名 / 执行面 / 内核 / 会话 id / 模型全名 | ✅ |
| 研究详情 API：不吐宿主绝对路径（会话 id 属产品字段，界面不显示） | ✅ |
| 界面（HTML/可见文字）不显示会话 id | ✅ |
| 浏览器控制台：除套件注入的失败（401/403/404/409、断流、模块图）外没有其他错误 | ✅ |
