# 工程约束与验证

[开发入口](../AGENTS.md) · [架构与多租户方向](architecture.md) · [pi 接入边界](pi-boundary.md)

本页规定如何修改、验证和交付，不另定义产品运行时。当前契约放在所属架构或模块文档，历史实验与交付记录不作为当前代码已经通过的证明。

## 需要先取得决定的变更

局部实现和不改变契约的修复可在任务范围内推进。以下变更先说明原义务、新义务、生产方/消费方、兼容或迁移、验证与回退，再取得用户或维护者的明确决定：

- pi 版本或官方接入方式、依赖方向、研究方法论在宿主与内核间的归属；pi 内核本身保持外部依赖。
- 产品事件的累计/增量/终态语义，SSE 重放与历史记录格式。
- 租户身份来源、授权、资源与凭据归属、存储隔离、执行并发和取消权限。单宿主多租户已按
  [架构 §10](architecture.md#10-单宿主多租户已实现合成验证) 落地（网关注入签名断言、owner 过滤、
  每租户凭据、Docker/E2B 并发上限与全局 fail-closed 资源台账）；**多宿主租约、SQLite/Postgres、
  短期凭据代理、BYOK、历史迁移与真实环境验收仍需先取得决定**。
- 真实数据迁移、历史报告改写、发布原子性、Docker 镜像或 E2B 模板切换。
- UI 标准或已批准交互的改变，而非仅对齐其现有要求。

决定更新所属文档；只有存在需要长期保留的替代方案和取舍时才新增独立设计记录。小修复不要求新增 ADR。规则、测试或基线的放宽必须说明被替代的义务，不能仅以检查失败为理由。

## 验证入口与安全范围

### 默认检查

```bash
bash scripts/check.sh
```

需要预先安装 Python ≥3.12、pytest 和 Node ≥24；脚本优先使用仓库 `.venv/bin/python`，否则使用 PATH 中的 `python3`。依赖缺失明确失败，不自动安装、同步环境或下载包。

入口在清空继承环境的子进程中依次执行 Python 和 Node 回归，使用独立临时 HOME、XDG 目录及 pytest 临时目录，禁用 pytest 外部插件自动加载与仓库缓存写入。默认不启动浏览器、真实 pi、Docker 或 E2B，不读取真实 `.env` 或个人 pi 配置，不扫描或改写真实运行数据。现有 API/执行测试使用 fake pi/SDK 替身和临时 HTTP 服务。

依赖外部标准库的 `test_ui_tokens_match_standard_library` 不在默认检查范围，脚本显式报告 `SKIP`；其“缺库返回 skip”的回归仍运行。默认检查成功只代表列出的必需检查通过，不代表 UI 对齐已经通过。

子命令的非零退出状态原样使入口失败；Python 失败后不继续 Node。无参数是唯一支持的调用方式，未知参数明确拒绝，避免误以为已经执行 browser/live 验收。定向迭代可以运行验证矩阵中的命令，准备交付时再完成受影响的最终覆盖。

**这不是 OS 安全沙箱。** 环境隔离与测试替身用于避免意外依赖个人配置，不阻止任意测试代码访问绝对路径或网络。新增测试必须继续遵守临时资源与外部输入替身约定，不能据此执行不可信代码。

### 仓库边界检查

`tests/test_repository_boundaries.py` 是默认 pytest 的一部分，检测：

- Git 已跟踪文件中出现真实配置/运行态文件的禁止路径；不读取 `.env` 内容。测试 fixture 中明确合成的 JSON 凭据样例可以保留。
- `host/` 的 Python 导入重新依赖 `frontier_agent` 或 `apodex`，以及源码中的字面 JS 导入直接引用 pi 的 `src/` 或 `dist/` 私有路径。
- Docker 与 E2B Dockerfile 的 Node/pi 声明丢失精确版本或发生分叉，以及两个预装包
  （`pi-web-access` / `pi-subagents`）的模板 pin 与镜像 ARG 漂移。

检查器自身有临时仓库和非法输入反例。合法的 TypeScript extension 和 pi 公共包导入不被禁止。路径/import 检查不能识别所有源码复制、动态加载或间接泄漏；没有命中不代表经过完整供应链或安全审计。Dockerfile 一致也不证明当前镜像/模板已重建。

`scripts/check-no-secrets.py` 另属运行态产物扫描：它读取操作员凭据来源并检查指定数据目录，不是提交前仓库密钥扫描，也不接入默认入口。其“目录不存在”或“没有扫描到文件”不能作为无泄漏证据；真实数据扫描须有对应授权。

## 验证矩阵

| 修改范围 | 最窄可信检查 | 最终覆盖与限制 |
|---|---|---|
| 仓库边界、Dockerfile 版本声明 | `python -m pytest tests/test_repository_boundaries.py` | 非法输入反例和当前工作区；不证明部署产物版本 |
| 默认检查入口 | `python -m pytest tests/test_check_entrypoint.py` | 环境隔离、无自动安装、原退出码、失败不继续、资源清理 |
| 配置与凭据交付（每 run 独立） | `python -m pytest tests/test_kernel_agent_env.py tests/test_redaction.py` | 合成凭据穿过真实投影/存储/下载/会话路径；流式释放等于整段脱敏（前后缀重叠、重复片段、全部分割位置、多帧、逐字符与合法相邻文本），并走 fake pi → runner → 日志/总线；`HOLD_CAP` 边界（8192/8193/8194/20000 字符已知值、重叠长值、任意多帧）不释放未判定文本、暂存不超界、越界后本块失败即闭合；同目录重渲染不会残留旧凭据/扩展；用临时 HOME，不读个人 `web-search.json` |
| 子代理派发与委派纪律（GLINK-SUB-02） | `node --test tests/extensions/*.test.mjs` + `python -m pytest tests/test_agent_prompts.py tests/test_kernel_agent_env.py tests/test_kernel_docker_env.py tests/test_kernel_e2b_env.py tests/test_repository_boundaries.py` | 前台守卫扩展的每个 detach 入口正反例（`async` 改写、只读 action 放行、后台/定时/控制/管理 action 阻止、`share`/`worktree` 阻止、工作区外路径阻止、`chainDir` 改写、`subagent_wait` 阻止、handler 异常 fail-closed）；`subagent` 工具卡片投影与脱敏；模板 pin 与两个镜像 ARG 一致、容器面两个包都映射到镜像内路径、`agents/` 进模板清单；`researcher` 定义的工具白名单与取证纪律。提示词：`tests/test_agent_prompts.py` 断言默认约定是最小产品契约（含可选委派与亲自核实、无方法论、无预算措辞、无确认门）、`/deepresearch` 承载完整方法论。不包含真实模型/检索 run、取消传播、Docker/E2B live 与子代理会话文件脱敏（见 pi-integration-notes.md §10.4）；无模型加载冒烟 `bash tests/extensions/smoke-subagents-load.sh` 需本机 pi 与本地 pi-subagents 副本，不在默认检查内 |
| 产物接收与路径安全 | `python -m pytest tests/test_artifacts.py` | 符号链接/目录链接/非普通文件（含 FIFO）/超限/越界一律拒绝；在遍历、打开、发布写入前注入祖先目录替换，外部文件不被读写；宿主拥有 `artifacts/report.md` 副本；HTTP 下载与 `hasReport` 走同一道校验 |
| RPC、投影、run 生命周期 | `python -m pytest tests/test_kernel_rpc.py tests/test_kernel_projection.py tests/test_kernel_acp_projection.py tests/test_runner.py tests/test_runs_api.py` | 正常、缺报告、被拒报告、模型 error/aborted/length、重试成功/失败、进程死亡、超时、取消；回收/关闭/报告阶段受理的取消必为 `cancelled`、与终帧提交竞争的取消返回已提交状态；资源持有的准入口径（local `409 resources_unreleased`、multitenant 计入租户与全局槽位）、`shutdown` 如实上报、自动对账与人工裁决放行；旧布局报告只读下载；pi/ACP 两族投影的事件映射、累计快照、截断标记、参数投影、usage 求和去重与脱敏边界（直接单元测试，不经 RunManager）；产品事件或 HTTP 口径变化同时验证 Node 消费方 |
| run 存储索引 | `python -m pytest tests/test_store_index.py` | 索引与全量扫描（`scan_all`/`scan_unreadable`）在创建、普通帧、终帧提交未落定、`fsync` 失败、settle 失败、重启重建、对账 interrupted、人工裁决与并发创建/提交下一致；`_load_meta` 计数桩证明热索引下列表/租户过滤/资源查询/准入不读 `meta.json`；不包含真实多进程竞争 |
| 资源台账与在线对账 | `python -m pytest tests/test_resource_ledger.py tests/test_kernel_e2b_env.py` | 两租户槽位计入与全局 429、legacy/损坏记录只计全局、local 409、重启后台账一致、损坏 meta 读面隐藏+全局持有+侧车裁决+不改写原文件、自动对账确认释放并与人工裁决区分（`source`）、探测失败/异常/超时保持持有、按持久化身份选面与调用参数断言（Docker 只 inspect 指定名字、E2B 只按 id）、E2B 探测使用冻结 `api_key` 且只认 SDK 的 not-found 契约、身份创建即持久化（上传+kill 双失败仍保留 `sandbox_id`）、探测线程启停与在途 run 不探测；不包含真实 Docker/E2B 调用 |
| SSE 连接额度与宿主 HTTP 结构 | `python -m pytest tests/test_stream_limit.py tests/test_multitenant_api.py tests/test_host_app.py tests/test_runs_api.py tests/test_accounts_api.py tests/test_saas_shared_model.py` + `node --test tests/web/*.test.mjs` | 全局上限在任何流响应头之前返回 `429 stream_limit`、断开/正常结束归还额度、租户与全局 scope 分离、配置必须 >= 1、前端有界重连（三次后失败、live 重置、close 取消、CONNECTING 不重复调度）；`app.py` 拆分后路由、状态码、载荷与入口（`pi-glink`、`python -m pi_glink.app`、`pi_glink.app.main/load_config/_resource_command` 再导出）不变，既有断言不改即通过；不证明真实浏览器的重连时序 |
| 结论不得比事实更完整（GLINK-INTEG-01） | `python -m pytest tests/test_kernel_e2b_env.py tests/test_kernel_rpc.py` + `node --test tests/web/run-stream.test.mjs tests/web/run-outcome.test.mjs tests/web/workspace-session.test.mjs` | E2B 补读 fail-closed（断流 + 事件文件缺失/超限 → `execution_error`，健康流不要求文件，超限在读取前就被拒绝）、沙箱文件读取前限幅（整份载入被拒/SDK 无大小也无流式时拒绝）、stderr 尾部读失败空串；权威回答帧截断带 `truncated: true`、前端保留更长的流式正文且不截断时仍以权威副本替换、重放历史同理；`finished + stopReason=length` 显示“交付不完整”（复用既有 outcome/ConnectionState 组件）；前端身份代次：过期会话的在途 `listRuns`/`startRun` 响应不渲染、新登录正常渲染；实例条目按 SDK 的 `EntryInfo.type` 区分（目录/符号链接跳过不算失败，字符串与真实 `FileType` 枚举均覆盖）；不包含真实 E2B live 与真实浏览器时序 |
| 宿主边界与打包（GLINK-HARDEN-01） | `python -m pytest tests/test_host_app.py tests/test_ownership.py tests/test_store.py tests/test_runner.py tests/test_repository_boundaries.py` | `--host`/`--port` 覆盖后经过同一套 `validate_host_config`（非 loopback 或越界 → exit 2）；整数配置范围（端口 0–65535，并发/超时/限速/资源上限必须为正，对账间隔与时钟偏移可为 0）拒绝 0/负数并报出键名；run 目录排他创建（生成 id 碰撞重新生成、显式 id 碰撞拒绝、既有 meta 字节不变、有限重试次数）；`data_root`/`runs`/`sessions`/`workspaces` 与 run 子目录 0700，已存在的符号链接、非目录或组/其他人可写目录拒绝启动且不自动 chmod；`pyproject.toml` 的包声明覆盖 `pi_glink.kernel`；不证明独立 wheel 安装、生产部署或真实多用户竞争 |
| 宿主所有权 | `python -m pytest tests/test_ownership.py` | 同端口/不同端口/跨进程的第二宿主在对账前被拒绝，活跃 owner 的记录字节不变；无所有权的对账被拒绝；`flock` 只证明单机本地文件系统 |
| 事件提交与重放 | `python -m pytest tests/test_event_replay.py` | 先落盘再发布、写盘失败不产生幻影帧、超环容量重放、活跃→结束接续、`Last-Event-ID`、重启对账与慢消费者断开；终帧发布瞬间详情/列表/报告已可读；在终帧已写入、`fsync` 未返回处阻塞时 HTTP 与 Store 均看不到终态，`fsync` 成功后单调可见、失败后从未可见（含重启对账与半行修复）；落定副本写失败、终帧写失败、报告副本写失败及重启后各读面一致 |
| 三面执行接口 | `python -m pytest tests/test_kernel_execution.py tests/test_kernel_acp_execution.py tests/test_kernel_docker_env.py tests/test_kernel_e2b_env.py` | 适配器协议调用顺序、三面事件与终态一致、LF 分帧共享语料、配置上传清单、shell 输入安全；ACP 适配器的 outcome 封存一次性、cancel/timeout/deadline 语义与 kernel_exit 归因（直接单元测试）；Docker 资源上限/降权参数与 E2B 生命周期上限、以及 E2B 内核命令的显式运行上限（不得沿用 SDK 的 60 秒默认，也不得超过沙箱寿命）只在命令契约层验证；mock SDK/进程行为不是真实隔离或云端质量证明 |
| 多租户身份、归属与并发（gateway） | `python -m pytest tests/test_tenancy.py tests/test_multitenant_api.py` | 合成密钥文件（可含非法权限/仓库内路径）、自签断言（伪造/过期/超长寿命/错误 audience/重复头/类型畸形 payload）、跨租户列表/详情/SSE/报告/取消矩阵、legacy 不可见、每租户凭据渲染与每 run 脱敏边界、全局/租户配额与九请求竞争、未释放持有计入租户与全局槽位、SSE 连接额度按租户分离、断言过期终止 SSE（重放+实时）、CSRF 来源拒绝、追问父 run 的租户门禁（异租户/未知/legacy 不读取不复制，同租户正例与 stale 相邻输入）、POST 忽略请求体后的 keep-alive 边界；不包含真实网关注入、Docker/E2B live 或多宿主 |
| 账号库权限 | `python -m pytest tests/test_accounts_permissions.py` | 首次创建即 0600 文件 + 0700 专用目录（`O_CREAT\|O_EXCL\|O_NOFOLLOW`，SQLite 打开前完成），WAL/SHM/journal 侧车属主独占（宽侧车被收紧）；已有 0644 文件、0777 父目录、符号链接、非属主文件一律在读取/迁移前拒绝且原样保留（不改权限、不写字节）；合法首次建库、写入用户与 WAL 重启（重开验证）正例；不包含跨宿主并发权限竞争 |
| 账号库所有权与放置 | `python -m pytest tests/test_accounts_ownership.py` | 同进程与**跨进程**第二宿主在同一 data_root 被 `DataRootBusy` 拒绝，`accounts.db` 及 `-wal`/`-shm` 字节逐文件哈希不变；首次合法初始化仍可建库/建表/写账号；绑定端口失败时账号连接被关闭且锁被释放（下一次启动可写）；`--print-config` 不创建也不改写账号库（含已有库字节不变）；自定义 `PI_GLINK_ACCOUNTS_DB` 必须位于 data_root 内，越界路径与符号链接逃逸被拒；不包含真实并发宿主压力测试 |
| 站内账号（`accounts`）存储与 HTTP 边界 | `python -m pytest tests/test_accounts.py tests/test_accounts_api.py` | 临时账号库：scrypt 逐行参数与恒时校验、未知账号与错误密码同量工作、邀请码/会话令牌只存摘要、一次性与并发竞争、过期、邮箱全局唯一、角色/输入边界、空闲/绝对过期、注销与停用即时失效、限速窗口；HTTP：统一 401（带 mode）、同形登录失败、429（含同 peer 轮换邮箱的登录预算：限额后不再执行 scrypt、窗口后恢复）、邀请校验只接受 POST JSON（GET/查询一律 405，日志请求行剥掉查询串，原始请求行与日志双负例证明 token 不出现）、跨站与 Content-Type 拒绝、会话 POST 的 CSRF、`/api/me` 无密钥、accounts 忽略断言头 / gateway 忽略 cookie、两租户列表/详情/报告/SSE/停止/追问矩阵、legacy 不可见、缺模型 key 仍拒绝、注销/停用/过期在 SSE 流动中结束该流、离线 `--create-invite`（只打印一次、未配置租户/宿主运行中/gateway 模式拒绝）；不包含真实 HTTPS 代理、邮件交付或生产数据 |
| SaaS 共享模型凭据（显式 `PI_GLINK_MODEL_CREDENTIALS=shared`） | `python -m pytest tests/test_saas_shared_model.py` | 策略门禁：宿主必须冻结 `OPENAI_API_KEY`/`OPENAI_BASE_URL`/`OPENAI_MODEL`，非多租户、未知取值一律拒绝；shared 与 `PI_GLINK_TENANT_CONFIG_FILE` 同时存在即拒绝启动，shared 无该文件可用且不构造/不读取 `TenantCredentialStore`（含直接构造 HostConfig 的防御性拒绝），tenant 模式的逐租户 key 文件语义不变；共享 key 仅进每 run 运行期 `auth.json`（0600）与宿主级 `web-search.json`，Docker argv/环境、E2B `Sandbox.create` 环境与命令、沙箱命令环境均不含 key，sandbox kill 后随沙箱销毁，`sandbox_id` 对账与报告回收仍走现有 E2B 适配器；事件/报告/下载脱敏包含共享 key，`redactor_for_record` 走宿主/共享边界，客户端上传 model/apiKey/baseUrl 等字段 400，query/header 不能改模型；两租户共享同一宿主模型但 owner/配额不变，`--create-invite` 校验宿主配置；不包含真实 E2B live、HTTPS 代理、云端资源上限或生产上线验收 |
| 存储与 HTTP | `python -m pytest tests/test_store.py tests/test_host_app.py tests/test_runs_api.py` | 真实临时 HTTP/文件路径，拒绝与错误状态；历史格式变化需单独兼容决定 |
| 前端合并窗口兼容 | `node --test tests/web/workspace-session.test.mjs` | 旧 local 宿主（`/api/me` 404 + `/api/config` 200 `multiTenant:false`）按旧流程进入并显示旧历史；旧 local 宿主 + 当前 `web/` 资产的真实浏览器验收（baseline 代码临时实例、临时数据根、随机 loopback）验证列表/详情/旧报告；负例矩阵：401（accounts/gateway）、`/api/me` 网络错误、multitenant、缺失/字符串 `multiTenant`、config 401/网络错误一律 fail-closed 且不回退；不包含真实 7861 服务重启 |
| 前端状态与呈现逻辑 | `node --test tests/web/*.test.mjs` | 累计快照、权威终帧、取消/失败、重复帧、缺口；导航竞态/dispose（stub DOM）；工具参数数组跨层语料；登录/邀请注册/恢复面、账号菜单与邀请对话框、会话生命周期（`/api/me` 前不揭示外壳、401 清屏回登录、退出只在服务端废止后生效）、HTTP 客户端请求形状（JSON/CSRF/same-origin）；不证明 DOM 布局 |
| UI 布局与交互 | Node 回归及 [UI 对齐文档](ui-alignment.md)的对应浏览器步骤 | 现有浏览器脚本有下述副作用，默认检查不自动执行 |
| 文档 | 对照实现检查命令、链接、作用域与验收口径 | 纯文档修改不默认跑真实研究或三面 smoke |

| run HTTP 准入与命令门禁 | `python -m pytest tests/test_runs_api.py tests/test_runner.py tests/test_kernel_agent_env.py` | 以 `/` 开头的 prompt 只接受 `agent/prompts/` 模板命令（当前仅 `/deepresearch`），其他（`/run --bg`、`/subagents-detach`、前导空白 + `/chain`、裸 `/`、大小写或名字相近的变体）返回 `400 unsupported_command`、错误体只给允许列表且不回显输入、拒绝早于 run 目录/执行面创建；普通题目（含正文中间出现 `/run`）与前导空白的普通问题照常运行；追问（`parentRunId`）走同一门禁；命令名单从模板目录推导（新增/删除模板文件立即生效，符号链接不计入，目录缺失时 fail-closed）。不证明真实模型下 pi 的扩展命令派发行为，也不覆盖内核启动后由其他扩展注入命令的路径 |

表中 `python` 应指向项目依赖环境。直接执行定向测试时也应使用临时 HOME、合成配置和自建资源；统一入口会完成环境隔离。优先复用可核验的同范围通过证据；代码、输入、环境或集成行为变化时，只补失效部分。

### 浏览器与 live 验收

当前 `tests/browser/run-smoke.mjs`、`ui-features.mjs` 和 `ui-parity.mjs` 仍是**历史脚本**：它们会向目标服务提交真实研究、读取历史或扫描个人 npx 缓存，并写入 `docs/evidence/`。它们不在默认检查内，运行前须明确服务归属、模型/云端授权和证据覆盖范围。不得借用固定端口上的未知服务或停止不属于本任务的 run。

**隔离浏览器入口（账号模式，人工验收）**：`tests/fixtures/accounts_server.py` 启动一个真实宿主
（真实 Runner/存储/事件总线/HTTP/SSE/生产前端）在 OS 分配的 loopback 端口上，使用临时 data root 与
临时 SQLite 账号库，只把执行面替换为进程内 fake adapter（`pi_glink.kernel.runner.build_adapter`，
与 Python 回归同一机制）；它只读合成凭据、不发邮件、不接触真实 `.env`/`~/.pi`/既有服务，退出时由
调用者按**记录的 PID** 终止（不要用宽匹配 `pkill`）。它不接入 `scripts/check.sh`，也不声明为自动
门禁。2026-09-23 的账号浏览器验收用 ego-browser（用户已登录的 Chromium，不启动第二个浏览器、不
读取个人 npx 缓存）在该 fixture 上人工执行，覆盖：登录/邀请注册/账号菜单与邀请对话框/运行与报告/
跨租户 404/窄栏账号区/注销后无历史闪现；截图与状态保存在会话临时目录，未写入 `docs/evidence/`。

**仍待实现（需先取得锁定依赖的决定）**：可自动重复、可进默认检查的真实浏览器门禁。它必须使用
project-local、lockfile 锁定的浏览器依赖（不得扫描 `~/.npm/_npx` 或猜版本），并复用上面的隔离
fixture；在取得该决定与依赖之前，账号 UI 的浏览器层只有人工验收 + Node DOM 回归，不能把它写成
自动化已覆盖。

真实 pi / Docker / E2B 验收单独记录版本、执行面、目标服务与资源范围。模板构建、切换及云端付费调用不因一次本地测试授权自动获准。多租户上线还需要架构中列出的跨租户拒绝与隔离证据，不能由单 run smoke 替代；合成矩阵（临时密钥、自签断言、fake 执行面）只证明宿主侧授权、归属、凭据与准入逻辑，不证明真实网关、镜像/模板隔离强度或云沙箱资源限制。

## 分工与收敛

- 开工核对当前 HEAD、未提交修改、任务文件范围和需要保护的消费者；同一文件的在途写入由任务 owner 串行负责。
- 独立评审优先用于凭据、执行器、事件与持久化变更。多 Agent 的启用和具体角色遵循当前全局策略；项目不自动启动 reviewer。
- finding 要给位置、失败场景和可复核证据，区分阻塞与建议。复核修复增量，不因进入下一轮就重新扩大全仓审查；误报说明反证。
- 同一语义族反复返工时，先检查共享表示与验收缺口，再提出有界整改，不无限增加特判。必要的契约改变重新取得决定。
- 主动减法在最终验证前完成；满足验收后新发现的非阻塞精简另记，不重启已经收口的实现。

## 交付证据与生效边界

每次交付至少报告：

1. **代码范围**：commit 与本次文件范围；含未提交修改时记录对应 diff，不能只绑定 HEAD。排除他人在途修改，指出无法验证的组合。
2. **执行证据**：实际命令、解释器/运行时版本、原退出码、通过/失败/跳过及覆盖行为。红测单列；管道或尾部摘要不能掩盖失败。
3. **生效对象**：源码、隔离服务、用户当前服务、Docker 镜像或 E2B 模板分别说明；旧报告不因代码更新自动重算。
4. **剩余范围**：未验证的模型质量、云端、UI 与租户能力；阻塞项和非阻塞风险分开。
5. **资源与回退**：本任务创建的资源、处置结果、配置/模板变更的回退方法；不清理未知资源。
6. **隔离与清理结论**：run 记录里的 `cleanup.released/failures`（`null` 表示未知）、`resource_adjudication`、被拒产物（`report_rejected`）与遗留外部身份；未释放或未知的资源不能写成已清理，人工裁决要写明核对依据。

新过程日志和截图使用仓库外临时目录；确需长期保护的脱敏输入与断言进入 `tests/fixtures/`。`docs/evidence/` 的既有记录是历史证据，现有脚本会覆盖它们，运行时必须明确这一点。正式决定更新所属文档，不复制整段会话或堆叠过期“最终通过”记录。

本地检查不是不可绕过的合并权限。将来接入远端时，CI 应调用同一入口，保护主分支并审查规则放宽；没有远端状态就不声明 CI 已通过。实现、自测、合并、push、部署与真实数据迁移分别遵守对应授权。
