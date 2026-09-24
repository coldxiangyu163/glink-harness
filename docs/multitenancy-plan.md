# 多租户：单宿主实现与后续阶段

[架构](architecture.md) · [工程约束](engineering.md) · [pi 边界](pi-boundary.md)

本文记录多租户的实现状态、已经落地的决定与仍然需要产品/运维裁决的部分。**单宿主多租户已按
§2–§6 实现并通过合成验证（临时密钥、自签断言、fake 执行面、临时数据根）；它不是上线验收**：
真实网关注入、真实 Docker/E2B live、生产数据与多宿主都不在已交付范围内。

## 1. 当前实现（单宿主）

| 能力 | 现状 | 实现位置 |
|---|---|---|
| 租户身份（gateway） | 本机认证网关注入 `X-Pi-Glink-Identity` 短时效 HMAC-SHA256 断言；宿主验签（constant-time）、校验 audience/过期/寿命 | `host/pi_glink/tenancy.py`、`app.py` |
| 租户身份（accounts） | 邀请注册 + 站内登录 + SQLite 会话（只存摘要），cookie `HttpOnly`/`SameSite`，CSRF 头，停用/注销/过期即时失效；`/api/me` 与账号界面 | `host/pi_glink/accounts.py`、`auth.py`、`app.py`、`web/modules/{auth,account}.mjs` |
| 请求授权 | 多租户下除健康探测外所有 `/api/*` 验签；未认证 401、权限不足 403、未知/异租户/legacy 同形 404；SSE 在响应头之前验权并在断言过期时结束 | `app.py::HostRequestHandler` |
| run 归属 | 受理时原子写 `tenant_id`/`creator_user_id`；`get_for`/`list_for` 按租户过滤；同租户 creator/admin 才能取消 | `store.py`、`kernel/runner.py` |
| 并发准入 | 仅多租户 Docker/E2B 并发，默认全局 4 / 单租户 2，原子检查与预留，超额 429，无队列；资源台账全局 fail-closed | `kernel/runner.py::RunManager._check_admission` |
| 租户凭据 | 默认每租户条目提供 `modelApiKey`（必需）/`webSearchKey`（可选）；显式 `PI_GLINK_MODEL_CREDENTIALS=shared` 时改为宿主冻结的一套模型凭据，且不得配置/读取租户凭据文件 | `tenancy.py`、`kernel/agent_env.py` |
| 脱敏边界 | 每 run 一个 redactor（宿主已知秘密 + 本 run 凭据），事件/日志/报告/工作区/会话/下载都经过它 | `kernel/runner.py`、`redaction.py` |
| 执行资源上限 | Docker 容器实例限 cpu/memory/pids 并降权；E2B 只限生命周期（SDK 不暴露 cpu/内存） | `kernel/docker_env.py`、`kernel/e2b_env.py` |
| 历史数据 | 无 owner 的旧记录在多租户读面完全不可见、不迁移、不改写；本地模式按旧口径读取 | `store.py`、`app.py` |

**仍未实现**：多宿主租约与共享存储（SQLite/Postgres/outbox）、真实网关与密钥轮换、BYOK 与短期
凭据代理、计费与上线运维；容器/沙箱的实际隔离强度不在本仓库的验证范围内。

## 2. 身份与授权（已实现的关键决定）

- **形式**：D1 选择 (a) —— 网关完成登录，宿主只信任注入断言；D1 的扩展（用户批准
  `docs/account-experience-proposal.md`）允许显式切换 `PI_GLINK_AUTH_MODE=accounts`，由宿主内的
  邀请注册 + 登录 + 服务端会话承担同一职责。两者互斥、只在服务端生成 `TenantContext`，客户端自报
  owner 在两种模式下都不被接受；账号登录失败不会回退到断言或本地模式。OIDC 仍不在范围内。
- **断言**：`v1.<base64url(payload)>.<base64url(hmac)>`，payload 至少 `tenantId`、`userId`、`role`、
  `audience`、`iat`、`exp`，role ∈ {member, admin}；密钥只来自仓库外 0600 文件（≥32 字节），
  寿命上限默认 300 秒、容忍时钟偏移默认 30 秒（可配置）。`exp` 一到即拒绝（不存在宽限窗口），
  因此通过验证的上下文绝不会已过期；时钟偏移只用于签发时间与寿命边界。签名正确但类型畸形的
  payload（如 role 为 JSON 数组/对象）一律按未认证拒绝（401），不得表现为宿主内部错误。
- **不可伪造**：请求体、查询参数和其它请求头都不参与身份推导；重复断言头直接拒绝；宿主只监听
  loopback，由本机网关反代。网关必须清除客户端同名头（宿主以“重复即拒绝”兜底）。
- **错误口径**：未认证 `401`；权限不足（同租户非 creator 的 member、租户未配置凭据）`403`；
  未知 run、异租户 run、无 owner 的 legacy 记录统一 `404`；配额超额 `429`。健康端点无需身份，
  但既不返回 run 标识，也不披露其他租户状态。
- **SSE**：输出任何响应头之前完成 owner 校验；重放与实时两个阶段都按帧检查断言过期并结束该流。
- **多轮追问**：`parentRunId` 的父 run 先过租户门禁；未知/异租户/无 owner 的父 run 在读取其状态、
  会话、报告或子 run 计数之前就返回 `404 unknown_run`，不会向新 run 复制跨租户会话或报告；同租户
  父 run 的具体拒绝（`run_busy`/`stale_run`/`session_unavailable`）保持可解释。
- **CSRF**：POST 校验 `Origin`/`Referer`/`Sec-Fetch-Site`；跨站请求在启动执行面之前被拒绝。

## 3. 存储（保持单机文件存储）

### 3.1 单机事务存储（当前）

本期明确**不**把 run/events 存储切到 SQLite/Postgres/outbox，也不引入多宿主租约：继续使用
`meta.json` 原子替换、`events.jsonl` 追加与终帧提交协议；owner 过滤与原子准入都落在现有模块内部。
旧记录保持原样，不写真实历史。

`accounts` 身份入口另有一个**独立**的 SQLite 库（`<data_root>/accounts/accounts.db`，stdlib + WAL）：
它只存租户/用户/邀请/会话，与 run 存储互不迁移、互不共享，也不改变上述 run 事务协议；停用账号
不会改写或删除任何 run 记录。该库必须位于同一个 data_root 内（`realpath` 判定），并且只在宿主
取得 `host.lock` **之后**才创建/迁移/打开：库文件与 WAL 侧车都是 data-root 写入，第二宿主必须
在写任何字节之前就被拒绝；`--print-config` 不打开账号库。库目录为 0700、库文件与 WAL/SHM 侧车为
0600，首次创建时以 `O_CREAT|O_EXCL|O_NOFOLLOW` 受限建立；已有的宽权限文件/目录或符号链接一律
fail-closed 且不自动 chmod，操作员需停止宿主后自行迁移与收紧（见 §7 回退说明）。多宿主演进时，这个库与 run 存储一样需要
单独的选型与迁移窗口。

### 3.2 多宿主演进（未实现）

单机事务库不能跨机器共享状态，多宿主要求：共享数据库（推荐 Postgres）+ 对象存储；事件发布改为
outbox 模式，事务内写 `events` 后投递到共享流供多宿主 SSE 与对账；调度改为「租约 + 心跳」
（`owner_host`/`lease_expires_at`/`fencing_token`），租约过期的接管方按策略终止/回收执行面并标记
`interrupted`；产物路径改为对象存储 key。选型与迁移窗口需要单独裁决，与
[架构 §3.1/§3.2](architecture.md#31-提交先于发布) 的现有单机承诺不冲突。

## 4. 并发与资源（已实现的边界）

- 默认全局 4、单租户 2；`PI_GLINK_MAX_GLOBAL_RUNS` / `PI_GLINK_MAX_TENANT_RUNS` 可调。
- 准入检查与槽位预留原子完成；超额立即拒绝（`429 quota_exceeded`，带 scope/limit，不含 run id），
  不设无限队列。
- 终态 run 只有在清理报告确认 `released: true`（或运维裁决）后才归还容量；资源状态未知继续
  挡住新准入（全局 fail-closed），**不自动**销毁任何外部资源。
- `inproc` 在两种模式下都维持全局单 run；多租户配置直接拒绝 `inproc`（共享宿主文件系统）。
- Docker：每个容器实例带 `--cpus`/`--memory`/`--pids-limit`（默认 2 / 2g / 512，可配置）与
  `--cap-drop ALL` / `--security-opt no-new-privileges`；镜像与模板不动。
- E2B：只限制沙箱生命周期（`LOCAL_WEB_E2B_TIMEOUT_SECONDS`，默认 task timeout + 300 秒）；
  SDK 不暴露 CPU/内存控制，宿主只声明实际传给云端的限制。以上都没有 live 验证。

## 5. 凭据策略（已实现的最小版本）

默认 `PI_GLINK_MODEL_CREDENTIALS=tenant`：

- 每租户从仓库外 0600 文件读取 `modelApiKey`（必需）与 `webSearchKey`（可选），渲染到本 run 的运行期
  `auth.json` / `web-search.json`；缺模型 key 拒绝准入，缺搜索 key 走无 key 检索。
- 无任何回退路径：宿主全局 `.env`、操作员 `~/.pi/web-search.json`、其他租户条目都不会被使用。

显式 `PI_GLINK_MODEL_CREDENTIALS=shared`（SaaS 统一供模型，已实现）：

- 宿主启动时冻结 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`，任一缺失即拒绝启动；
- 每个 run 使用同一套宿主共享凭据（`RunCredentials(origin="shared")`）；共享模式不读取任何租户
  配置：不得配置 `PI_GLINK_TENANT_CONFIG_FILE`（不设置或留空即为无该入口），设置即拒绝启动，
  也不会构造或读取 `TenantCredentialStore`；
- 可选搜索 key 只来自宿主级 `PI_GLINK_WEB_SEARCH_KEY`，不会用操作员本机文件顶替；
- 每 run 脱敏边界包含共享 key；报告下载的 `redactor_for_record` 在 shared 下走宿主/共享边界，
  不因租户条目无 key 而漏脱敏；owner、配额与审计语义与 tenant 模式完全一致；
- 这是 SaaS 部署的显式选择，绝不做 tenant/local 的隐式兜底；回退时改回 tenant 并为各租户补回 key。

两种策略共同保留的边界：E2B 平台 key 属于宿主资源管理，不是租户模型 key；每 run 的 redactor 只含
本 run 实际使用的凭据；共享 key 仍是宿主可读、可被工具回显或经网络外传的资源秘密，只承诺现有脱敏
回显保护与沙箱执行隔离，不宣称 key 不可能泄露。短期凭据代理/STS、轮换、额度与审计仍未实现。

## 6. 历史数据归属

D5 选择 **C（只读保留）**：历史记录不带 owner，在多租户读面始终不可见、不可下载、不可 SSE、
不可取消，也不写回、不自动导入；本地模式仍可读旧历史。后续若需要默认租户回填或显式导入，
必须单独立项并遵守“不删除用户历史”的约束。

## 7. 行为迁移与回退

- **迁移**：任何模式都不再允许绑定非 loopback：无认证公网模式被移除，历史的 `LOCAL_WEB_ALLOW_REMOTE`
  不再生效；远端访问必须经过本机认证网关注入身份（`PI_GLINK_TENANCY=multitenant`）。
- **回退**：关闭 `PI_GLINK_TENANCY` 回到单用户模式，或把 `PI_GLINK_AUTH_MODE` 从 `accounts`
  切回 `gateway`；如使用 `shared`，也可把 `PI_GLINK_MODEL_CREDENTIALS` 改回 `tenant` 并为每个租户
  补回 `modelApiKey`。新旧 run 记录都保留，账号库保留只读、不自动改写历史、不降低 owner 校验；
  多租户密钥文件/账号库在切回后不再被读取。
- **配置范例**：见 `.env.example`，全部使用虚构占位符（含 `accounts` 模式的开关、会话/限速参数与
  离线签发首位管理员邀请的命令）。

## 8. 仍需裁决的问题

- **D3 存储**：何时引入 SQLite/Postgres 与对象存储；是否允许历史导入写回。
- **D4 凭据增强**：是否建设短期凭据代理/STS；轮换、吊销、额度与告警；是否支持 BYOK。SaaS 统一共享
  模型凭据（`PI_GLINK_MODEL_CREDENTIALS=shared`）已按 GLINK-SAAS-01 落地，它不是短期代理也不是 BYOK。
- **D6 开通流程**：首位管理员的离线签发与后续管理员邀请已经落地（`--create-invite` /
  `POST /api/admin/invitations`）；**仍未裁决**：租户本身的创建/停用运维入口（当前停用通过账号库
  状态位生效，没有 HTTP 管理接口）、停用后的事件流与运行中 run 处置口径、邮件交付与密码重置。
- **D7 审计与合规**：审计事件保留期与导出格式；数据驻留与删除请求支持。
- **多宿主**：租约、心跳、接管语义与共享存储选型（见 §3）。
- **真实环境验收**：网关实现与密钥轮换演练、Docker/E2B live 资源上限与隔离强度。

## 9. 验收状态

已由合成矩阵覆盖（`tests/test_tenancy.py`、`tests/test_multitenant_api.py`，见
[验证矩阵](engineering.md#验证矩阵)）：

1. **跨租户拒绝矩阵**：列表、详情、事件流、报告下载、取消、启动、凭据——owner 正例与另一租户反例，
   同一 404 口径且无副作用；legacy 记录不可见。
2. **并发与配额**：租户/全局上限、超额 429、九请求竞争填满且不超过上限、资源未确认释放继续挡准入、
   已受理取消优先。
3. **凭据隔离**：每租户只渲染自己的 key；缺模型 key 拒绝准入；缺搜索 key 回退无 key；每 run
   redactor 只含本 run 凭据。
4. **会话与 SSE**：异租户在响应头之前 404；断言过期在重放与实时两个阶段都终止流（含大量历史、
   无实时订阅与慢读取）；重启后重放仍按租户过滤。
5. **认证边界**：伪造/篡改/过期/超长寿命/错误 audience/重复头 401；跨站 POST 拒绝；健康端点不
   泄露 run id。
6. **多轮追问的租户隔离**：异租户父 run 在运行中/已有后续轮次/无会话等形态下都返回同一
   `unknown_run`，不建立新 run、不创建会话目录、不改写父会话；legacy/未知父 run 同理；同租户
   追问保持 owner、私有会话副本与报告种子，旧轮分叉返回 `stale_run`。

**账号模式（`PI_GLINK_AUTH_MODE=accounts`）另有合成矩阵**（`tests/test_accounts.py`、
`tests/test_accounts_api.py`，以及隔离 fixture 上的手动真实浏览器验收）：

1. **存储与会话**：口令用 scrypt 参数逐行记录、恒时校验；邀请码与会话令牌只存摘要、一次性、过期
   拒绝、并发竞争只成功一次；邮箱全局唯一、角色/输入边界、停用用户/租户立即失效、空闲与绝对过期。
2. **HTTP 边界**：未登录统一 401（带 mode）；未知账号与错误密码同形；按账号与按 socket peer
   两层登录限速（peer 拒绝发生在 scrypt 之前，轮换邮箱不能放大 CPU）；限速 429；跨站 Origin/
   Sec-Fetch-Site 与错误 Content-Type 拒绝；会话 POST 无 CSRF 头 403；`/api/me` 不回任何密钥。
3. **跨租户与回退**：两租户登录后的列表/详情/报告/SSE/停止/追问矩阵与 legacy 不可见；accounts 模式
   忽略断言头、gateway 模式忽略会话 cookie；登录失败不回退；缺本租户模型 key 仍拒绝准入。
4. **实时失效**：注销/停用/绝对过期发生在 SSE 流动期间时，流在轮询间隔内结束，而 run 本身不被取消。
5. **浏览器**：隔离 fixture（临时账号库 + fake 执行面）上人工用 ego-browser 走过登录/邀请注册/账号
   菜单/运行与报告/跨租户 404/窄栏与注销，证据为用户临时目录中的截图（见 engineering.md 的浏览器段）。

**SaaS 共享模型凭据（`PI_GLINK_MODEL_CREDENTIALS=shared`）的合成矩阵**
（`tests/test_saas_shared_model.py`）：

1. **配置门禁**：缺 `OPENAI_API_KEY`/`OPENAI_BASE_URL`/`OPENAI_MODEL`、非多租户模式、未知取值一律
   拒绝启动；shared 与 `PI_GLINK_TENANT_CONFIG_FILE` 同时存在即拒绝，shared 无该文件可用，
   且不构造/不读取 `TenantCredentialStore`；tenant 模式的逐租户 key 语义与文件入口不变。
2. **凭据交付**：共享 key 只进每 run `auth.json`（0600）与（可选）宿主级 `web-search.json`；Docker
   argv/环境、E2B `Sandbox.create` 环境与命令、沙箱命令环境均不含 key；沙箱 kill 后凭据随沙箱销毁；
   `sandbox_id` 对账与报告回收入 run 记录。
3. **脱敏与回显**：事件日志、报告副本、下载内容均不含共享 key；`redactor_for_record` 在 shared 下
   包含共享 key；客户端提交 `modelApiKey`/`model`/`baseUrl`/`apiKey`/`webSearchKey` 字段返回 400，
   query/header 不能改模型。
4. **多租户不变**：两租户共享同一宿主模型配置，但列表/详情/报告/SSE/取消/追问跨租户 404，配额仍按
   租户计数；`--create-invite` 在 shared 下校验宿主配置而非租户条目。

**未覆盖的真实环境验收**：真实网关注入与密钥轮换、真实 HTTPS/TLS 代理与外部 Origin、邮件交付、
Docker/E2B live 资源上限执行、N 租户 × M 并发负载、生产数据扫描与上线演练。合成矩阵通过不等于
多租户已经安全上线。
