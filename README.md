# pi-glink

以 [pi](https://www.npmjs.com/package/@earendil-works/pi-coding-agent) 为执行内核的研究工作台：本机 / Docker / E2B 三种执行面共享同一套 run 生命周期。默认 `local` 模式仍是单用户、全局单 run；`PI_GLINK_TENANCY=multitenant` 时启用**单宿主多租户**—— 身份入口二选一：本机认证网关注入短时效签名断言（`PI_GLINK_AUTH_MODE=gateway`，默认），或站内邀请注册 + 登录 + 服务端会话（`PI_GLINK_AUTH_MODE=accounts`）；两者都只在可信服务端生成租户上下文，Docker/E2B 可并发（默认全局 4 / 单租户 2）。模型凭据默认仍是每租户自备（`PI_GLINK_MODEL_CREDENTIALS=tenant`）；SaaS 部署可显式选择 `shared`，由宿主冻结一套模型凭据统一提供，共享模式不读取任何租户配置（配置 `PI_GLINK_TENANT_CONFIG_FILE` 即拒绝启动）。多租户目前只经过**合成验证**（临时密钥文件、自签断言、fake 执行面、临时数据根），没有真实网关、Docker/E2B live 或生产数据验收，不能把“合成矩阵通过”当作多租户已经安全上线。方向、当前实现与尚未覆盖的边界见 [架构](docs/architecture.md) 与 [多租户方案](docs/multitenancy-plan.md)；前端沿用 Glink 工作台的布局与视觉，运行概览、执行流程事件与思考展示按 pi 的原生事件语义重写。

**当前状态：本机 / Docker / E2B 三种执行面均已跑通真实 run**，验收记录见 [docs/acceptance.md](docs/acceptance.md)，证据在 `docs/evidence/`。除前端静态资源源自 Glink 工作台外，本仓库不引用 FrontierAgent 的任何运行模块。

## 边界（先读这条）

- **不 fork、不 patch、不 vendor pi 内核。** 只使用 pi 的官方 headless 协议与官方扩展点（extension / skill / package / custom provider / MCP）。
- **不复制 FrontierAgent 的 runtime。** 本仓库自有宿主只做编排、事件投影与呈现；研究方法论通过 pi 的提示词与扩展表达。
- 前端不解析终端文本，只消费结构化事件。
- 详见 [docs/pi-boundary.md](docs/pi-boundary.md) 与 [docs/architecture.md](docs/architecture.md)。开发本仓库先读 [AGENTS.md](AGENTS.md)，验证范围与交付要求见 [工程约束](docs/engineering.md)。

## 依赖与版本

| 组件 | 版本 | 安装 / 说明 |
|---|---|---|
| Python | ≥ 3.12 | 宿主与内核接入层只用标准库，无运行时依赖 |
| Node.js | 宿主开发 **v24.16.0**（nvm 固定，本机实测）；Docker / E2B 镜像 **v24.21.0**（`node:24.21.0-bookworm-slim` 精确 tag，实测） | 前端与 pi 的运行时；宿主用 nvm：`nvm install 24.16.0 && nvm use 24.16.0`；镜像/模板的 Node 版本在各自 Dockerfile 里 pin 到精确 tag（不用浮动 `node:24-bookworm-slim`） |
| pi | 0.86.1（已实测） | `npm install -g @earendil-works/pi-coding-agent@0.86.1`；Docker / E2B 面的版本固定在各自 Dockerfile 的 `ARG PI_VERSION` |
| pi 包 | `pi-web-access@0.31.0`（检索，三面）；`pi-subagents@0.40.0`（委派，三面；前台强制，见「委派与子代理」） | `harnesses/pi/settings.json` 是包声明真源（两个精确 pin）；Docker / E2B 从镜像内 `/opt/pi-packages` 本地加载，不在 run 里 `npm install`；一致性由 `tests/test_repository_boundaries.py` 卡住 |
| pytest | ≥ 8（仅开发） | `uv pip install -e ".[dev]"` |
| e2b SDK | ≥ 2（仅 E2B 执行面） | `uv pip install -e ".[e2b]"`（含代理网络需要的 `httpx[socks]`） |

本机执行面用宿主安装的 pi；换版本做演练时用 `PI_GLINK_PI_BIN` 指向另一个二进制，无需改 PATH（见 [docs/pi-boundary.md](docs/pi-boundary.md) §3）。

## 三种执行面

| 执行面 | 取值 | 语义 |
|---|---|---|
| 本机进程 | `inproc` | pi 以子进程运行在宿主机器上，工作区在 `<数据根>/workspaces/<runId>` |
| 容器 | `docker` | pi 与工作区都在一次性容器内，宿主只做编排与事件投影 |
| 云沙箱 | `e2b` | pi 与工作区都在 E2B 沙箱内（沙箱后台命令 `stdin=True` 的 RPC 双向 stdio，与另两面同一套 `--mode rpc`）；模型凭据随运行期 `auth.json` 上传到沙箱（不进沙箱进程环境） |

三面共享同一套输入/事件/收尾契约（`kernel/execution.py` 的 `ExecutionAdapter`）：每个 run 拥有
自己的运行期配置（`runs/<id>/pi-agent/`）、session 目录（会话根基目录下的 `<id>/`，默认 `<数据根>/sessions/<id>/`）与工作区；
完成后在同一对话发送追问会提交 `POST /api/runs {"prompt":"…","parentRunId":"<上一轮 runId>"}`，
返回新 run 的 `parentRunId`。它复制上一轮经安全校验的会话副本，通过 pi 官方 `--session` 续接，
不会另起会话或改写旧 run；侧栏按研究汇总，旧报告仍可打开。会话记录不可续接时返回
`409 session_unavailable`，从旧轮分叉返回 `409 stale_run`。多租户模式下 `parentRunId` 只允许属于调用者
自己租户的父 run：未知/异租户/无 owner 的父 run 统一返回 `404 unknown_run`，且在确认所有者之前不读取
它的会话、报告、状态或子 run 计数。
收尾顺序固定为「产物回收 → 销毁执行面 → 安全校验并落盘报告 → 凭据清扫 → 终态与释放」。

### E2B 执行面

```bash
# 首次/升级：先构建候选 → 在真实沙箱自检 → 再把 default（宿主引用的 tag）移过去
E2B_API_KEY=… .venv/bin/python scripts/build-e2b-template.py --tag candidate --logs
E2B_API_KEY=… .venv/bin/python scripts/build-e2b-template.py --promote-from candidate
# 回滚：候选构建前的生产 build 留了 previous tag
E2B_API_KEY=… .venv/bin/python scripts/build-e2b-template.py --promote-from previous

LOCAL_WEB_EXECUTOR=e2b LOCAL_WEB_PORT=7863 scripts/run-local-web.sh   # 用云沙箱跑 pi
```

本机直连 E2B 不可达时，构建与宿主进程都需要代理（`HTTPS_PROXY=socks5h://127.0.0.1:1080`；
CLI 的 Node fetch 不认 socks5，构建走 Python SDK + `httpx[socks]`）。

发布顺序是**镜像/模板先于宿主**：新宿主会要求容器/沙箱内有预装包守卫，旧模板不满足时直接失败（不静默降级）。模板自带 `/opt/pi-packages`（pi-web-access + pi-subagents）与守卫脚本；宿主把运行期 `settings.json` 的 `npm:` 声明渲染成该目录的本地包，run 内不再“先装包再干活”。证据：[docs/evidence/prebaked-packages.txt](docs/evidence/prebaked-packages.txt)。

模型凭据写在运行期 `auth.json`（pi 官方凭据存储，0600，仓库外），随本 run 的运行期配置上传到沙箱
`/home/user/config/`；不进沙箱进程环境（沙箱内 `env | grep -c OPENAI_API_KEY` 实测 0），随沙箱销毁
而消失。pi-web-access 凭据随运行期 `web-search.json` 一并上传。交付内容不只几个 JSON：整个运行期目录
（含 `AGENTS.md`、`extensions/`、`skills/` 等模板条目）按同一路径语义上传，并记录
`(相对路径, sha256)` 清单供核验；沙箱内 JSONL 按 LF 分帧（U+2028 不得拆帧）。沙箱命令以
`background=True, stdin=True` 启动，宿主用共享 `PiRpcClient` 走同一条 stdio 通道收发帧（stdout 块
不保证帧对齐，仍按 LF 拆帧）；流断开即 `execution_error`，已交付帧保留、不补发、不伪造完整性
（断流取舍、取消与追问的身份验证时点见 [架构](docs/architecture.md) §5.2）。报告会先拉回宿主工作区再校验/脱敏。
证据：[docs/evidence/e2b-run.txt](docs/evidence/e2b-run.txt)。

### Docker 执行面

```bash
# 首次/升级：先建候选并自检，再移动宿主引用的 latest（旧镜像先留 previous tag）
docker build -t pi-glink-worker:candidate -f docker/Dockerfile.worker .
docker tag pi-glink-worker:latest pi-glink-worker:previous
docker tag pi-glink-worker:candidate pi-glink-worker:latest
LOCAL_WEB_EXECUTOR=docker scripts/run-local-web.sh                     # 用容器跑 pi
```

镜像自包含（`node:24.21.0-bookworm-slim` + 固定版本 pi + `/opt/pi-packages` 预装 pi 包，`node -v`
实测 v24.21.0）；宿主只挂载工作区（`/workspace`）、本 run 的运行期配置（`/config`）与 session 目录
（`/sessions`）；模型凭据随 `/config/auth.json` 进容器（不经环境变量或 argv）。宿主把 `settings.json`
的 `npm:` 声明渲染成 `/opt/pi-packages` 本地包，并以镜像内守卫脚本作为容器 `--entrypoint`：镜像早于
宿主或包缺失时容器明确失败（exit 78 / 127），不会静默丢掉 `web_search`。镜像名在宿主启动时冻结，
运行中的环境变量变化不会让已接收的 run 换镜像。容器内 pi 报告的会话路径（`/sessions/…`）由宿主映射
回本 run 的 `<会话根>/<id>/…` 后再做收尾脱敏。
网络受限环境可用 `--build-arg DEBIAN_MIRROR=mirrors.aliyun.com --build-arg NPM_REGISTRY=https://registry.npmmirror.com`。
证据：[docs/evidence/docker-run.txt](docs/evidence/docker-run.txt)、
[docs/evidence/prebaked-packages.txt](docs/evidence/prebaked-packages.txt)。

执行面由 `LOCAL_WEB_EXECUTOR` 选择；三者消费同一份输入契约，事件流对外表现一致。

## 快速启动（本机）

```bash
cp .env.example .env         # 填写 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL
scripts/run-local-web.sh     # 默认 http://127.0.0.1:7860
```

脚本把所有依赖都铺好了：优先用仓库内的 `.venv`，否则回退 `uv run` 或系统 Python，并始终把 `host/` 放进导入路径。部署单位是**整个仓库目录**：宿主按仓库相对路径定位 `web/`（前端资源）、`harnesses/<pack>/`（harness 包）与 `scripts/`；`pip install -e .` 只用于让 `pi_glink` 可导入（包声明含 `pi_glink.kernel` 与 `pi_glink.runs`），独立安装 wheel 不是受支持的形态。等价的手动方式：

```bash
uv venv --python 3.12 .venv       # 首次
uv pip install -e ".[dev]"        # 首次：让 pi_glink 可导入并装上 pytest
.venv/bin/python -m pi_glink.app
```

常用参数：

```bash
scripts/run-local-web.sh --port 7861     # 覆盖端口（0 = 内核分配临时端口；覆盖后仍走同一套边界校验）
scripts/run-local-web.sh --print-config  # 打印生效配置（不含密钥）
scripts/run-local-web.sh --unreleased    # 列出执行资源未确认释放的 run（需宿主已停止）
scripts/run-local-web.sh --mark-released <runId> --note "已核对容器/沙箱不存在"   # 记录人工裁决
```

`--host` / `--port` 覆盖与构造走同一套校验（`validate_host_config`）：非 loopback 主机、或超出
0–65535 的端口直接拒绝启动（退出码 2）。数据目录（默认 `~/.pi-glink`）由宿主以 0700 创建
（`runs/`、`sessions/`、`workspaces/` 与各 run 子目录同样）；已存在的数据目录若是符号链接、不是目录，
或带组/其他人写权限，宿主拒绝启动并提示操作员自行 `chmod 700`，不会自动改动已有目录权限。

一个数据目录同一时间只能被一个宿主进程持有（`<数据根>/host.lock`）；第二个宿主无论用哪个端口都会
直接拒绝启动，不会改动正在运行的任务。执行面清理未确认成功（或宿主异常退出后资源状态未知）的 run
会持续占用运行额度：本机（`local`）模式任一持有即让新研究返回 `409 resources_unreleased`（附 run id）；
多租户模式把持有计入其 owner 租户与全局槽位，额度用尽时返回 `429 quota_exceeded`。Docker/E2B 宿主会按
记录里持久化的容器名/沙箱 id 周期性自动对账（`PI_GLINK_RESOURCE_RECONCILE_SECONDS`，默认 60 秒，0 关闭；
只探测、不猜名字），确认资源不存在后归还额度；核对真实资源后也可用 `--mark-released` 记录人工裁决。
`--unreleased` 列出待核对项，`meta.json` 损坏的记录标注 `corrupt`。

## 运行一次真实研究

```bash
scripts/run-local-web.sh &                                  # 启动宿主（默认 7860，可在 .env 覆盖）
.venv/bin/python scripts/smoke-local-run.py --base-url http://127.0.0.1:7861
```

冒烟脚本走与浏览器完全相同的 HTTP 接口，产出：run 终态、事件数、sessionId、报告大小与帧类型清单；退出码非零即失败。真实证据样例见 [docs/evidence/local-m1-smoke.txt](docs/evidence/local-m1-smoke.txt)。

## HTTP 接口（V1）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 宿主与 pi 可用性、当前活跃 run |
| GET | `/api/config` | 公开配置（无密钥、无宿主路径） |
| GET | `/api/blueprints` | 可用会话蓝图目录（`name`/`description`/阶段元数据；与准入同源推导，只读） |
| POST | `/api/runs` | 创建并启动一次 run，body `{"prompt": "…"}` |
| GET | `/api/runs` | run 列表（含状态与报告标记） |
| GET | `/api/runs/{id}` | run 详情 |
| GET | `/api/runs/{id}/events` | 事件流（SSE，支持 `Last-Event-ID` 重放；重放事实源是持久事件日志） |
| POST | `/api/runs/{id}/stop` | 请求取消：受理 `200 {"requested": true}`（终态必为 `cancelled`）；已结束 `409 run_not_cancellable`（带已提交 `status`）；未知 `404` |
| GET | `/api/runs/{id}/report` | 下载 `report.md`（宿主产物副本；旧版本的 `workspaces/<id>/report.md` 只读下载） |
| GET | `/api/me` | 当前身份：local 报 `{"mode":"local","authenticated":false}`；gateway/accounts 回展示字段与角色（accounts 另回 `csrfToken`），未认证 `401` 且带 `mode` |
| POST | `/api/auth/login` | accounts 模式登录：邮箱+密码，成功设置 `HttpOnly` 会话 cookie 并回身份 |
| POST | `/api/auth/register` | accounts 模式邀请注册：只接受绑定到邀请的邮箱，创建账号后回到登录（不静默授予会话） |
| POST | `/api/auth/logout` | accounts 模式注销：服务端废止会话并清 cookie |
| POST | `/api/invitations/check` | 校验邀请可用性，body `{"token": "…"}`（限速；无效与已用同形返回 `{"valid":false}`；邀请码只走 JSON body，不进 URL/查询/日志） |
| POST | `/api/admin/invitations` | 管理员为**本租户**签发一次性邀请（需要 CSRF；响应只在这一次返回邀请码原文） |

本机（`local`）执行面同一时间只允许一个 run；并发请求返回 `409 run_busy` 并附当前 run id；已有 run 的执行资源未确认释放时返回 `409 resources_unreleased` 并附 run id。以 `/` 开头的 prompt 只接受宿主解释的 `/<蓝图名> <题目>`（蓝图名从 `methodology/blueprints/` 推导，当前仅 `deepresearch`），其余返回 `400 unsupported_command`（错误体只给推导出的蓝图名列表，不回显被拒输入）——pi 对这类消息会先执行扩展命令，而 pi-subagents 注册了 `/run --bg`、`/subagents-detach` 等后台入口；拒绝在任何 run 目录/执行面创建之前，追问同样适用。终态以事件日志里的终帧为唯一提交点：终帧在 `fsync` 成功前对任何读取入口都不可见，失败则从未出现；收到终帧后立即读取详情、列表或报告都是同一事实。

多租户模式（`PI_GLINK_TENANCY=multitenant`）下所有 `/api/*`（健康的存活性探测除外）都要求可信身份——网关断言或站内会话：未认证 `401`，权限不足 `403`，未知/异租户/legacy 记录统一 `404`，超过并发上限 `429 quota_exceeded`。下一节说明完整语义。`/api/blueprints` 是宿主级资源的只读目录：认证后各租户看到同一份列表（与 `/api/config` 同形）；空目录/缺失目录都返回 `200 {"blueprints": []}`，格式不合契约的文件不进列表（原因只进日志）。

## 多租户（单宿主，`PI_GLINK_TENANCY=multitenant`）

身份入口由 `PI_GLINK_AUTH_MODE` 显式选择，默认 `gateway` 保持原语义；`accounts` 增加站内邀请注册、登录与服务端会话。模型凭据来源由 `PI_GLINK_MODEL_CREDENTIALS` 显式选择（默认 `tenant`，SaaS 用 `shared`，见下）。两种入口都只在服务端生成 `TenantContext`，run/list/详情/报告/SSE/取消/追问走同一套 owner 与配额校验；`local` 完全保持旧行为，不显示任何登录或组织界面。

### 身份入口 A：网关断言（默认，`PI_GLINK_AUTH_MODE=gateway`）

本模式只支持一个宿主进程部署在本机认证网关之后：网关完成登录，并在每个请求注入
`X-Pi-Glink-Identity: v1.<base64url(payload)>.<base64url(hmac-sha256)>`。宿主只信任这枚断言，
不做登录、OIDC 或用户体系；客户端无法用请求体、查询参数或自造请求头指定租户，重复断言头直接拒绝。
断言至少携带 `tenantId`、`userId`、`role`、`audience`、`iat`、`exp`，宿主以 constant-time HMAC 校验
签名并检查 audience、过期与寿命上限；过期即 401（无宽限窗口），签名正确但字段类型畸形的断言同样
统一 401，不会变成 500。

- **配置**：本模式要求 `PI_GLINK_GATEWAY_SECRET_FILE`、`PI_GLINK_TENANT_CONFIG_FILE`、
  `PI_GLINK_TENANCY_AUDIENCE` 三项齐备；两个文件必须在仓库外且权限 0600（或更严）。任一缺失、
  权限过宽或位于仓库内都拒绝启动，不会回落到无认证的本地模式。本模式只允许绑定 loopback，且
  执行面必须是 `docker`/`e2b`（`inproc` 共享宿主文件系统，只保留给本地单用户调试）。
- **归属**：run 在受理时原子写入 `tenant_id` 与 `creator_user_id`。同租户成员可看列表、详情、SSE 与
  报告；只有创建者或同租户 `admin` 能取消。其他租户、未知 run 与没有 owner 的旧记录（legacy
  `meta.json`）在列表/详情/SSE/报告/取消上都得到同一个 404，且不产生副作用；旧数据不迁移、不修改。
  追问（`parentRunId`）同样先过租户门禁：异租户/未知/legacy 父 run 统一 `404 unknown_run`，在确认
  所有者之前不读取其 session、报告、状态或子 run 计数，也不会把它的会话或报告复制给新 run。
- **凭据**：默认每 run 的模型 key（与可选搜索 key）来自该租户在 `tenants.json` 中的条目，缺
  `modelApiKey` 直接拒绝准入；绝不回退宿主全局 `OPENAI_API_KEY`、操作员 `~/.pi/web-search.json`
  或别的租户。显式 `PI_GLINK_MODEL_CREDENTIALS=shared` 时改用宿主冻结的共享凭据（见下文“模型
  凭据策略”）。E2B 平台 key 属于宿主资源管理，不是租户模型 key。每 run 的脱敏边界只包含本 run
  凭据，不会把不同租户的秘密互相写入事件、日志或产物。
- **并发**：仅本模式的 Docker/E2B 可并发，默认全局上限 4、单租户 2，超额立即返回
  `429 quota_exceeded`（带 `scope` 与 `limit`，不含任何 run id），没有排队。准入检查与槽位预留
  原子完成；终态 run 只有确认资源释放才归还容量，未确认的持有计入其 owner 租户与全局槽位
  （一个租户的泄漏不会挡住别的租户，但会持续吃掉额度；达到全局上限时拒绝所有新准入，
  fail-closed）。宿主在线按持久化身份自动对账释放；离线仍可用 `--unreleased` / `--mark-released`
  人工裁决（`--unreleased` 对损坏记录标注 `corrupt`）。
- **SSE 连接额度**：事件流同时打开数有上限——全局 `PI_GLINK_MAX_GLOBAL_STREAMS`（默认 64），
  本模式每租户 `PI_GLINK_MAX_TENANT_STREAMS`（默认 16）；额度在写出任何流响应头之前检查，
  超额返回 `429 {"error":"stream_limit", ...}`（不含 run id），流结束时归还。
- **SSE 与 CSRF**：事件流在写出任何响应头之前验权；断言过期在重放与实时两个阶段都按帧检查，
  中途过期立即结束该流（没有宽限窗口）。POST 会检查
  `Origin`/`Referer`/`Sec-Fetch-Site`，跨站请求在启动任何执行面之前被拒绝。健康端点无需身份，但
  不返回任何 run 标识；配额与非配额准入错误也不泄露其他租户的 run id。
- **前端**：不需要登录页，也不需要把令牌放进页面。浏览器以同源凭据访问宿主（`fetch` 使用
  `credentials: 'same-origin'`，`EventSource` 同源自动带 cookie），网关在反代时把已有会话转换成
  断言；会话过期时前端收到 `401`，重新认证由网关完成。
- **行为迁移与回退**：任何模式都不再允许绑定非 loopback——无认证公网模式被移除，历史的
  `LOCAL_WEB_ALLOW_REMOTE` 不再生效；远端访问必须由本机认证网关反代（本模式）。回退方式是关闭
  `PI_GLINK_TENANCY`（恢复单用户单 run），新旧 run 记录都保留，不删除任何历史。

### 身份入口 B：站内账号（`PI_GLINK_AUTH_MODE=accounts`）

邀请制注册 + 站内登录，保留网关模式的全部 owner/配额语义，但身份来自服务端会话而不是断言：

- **配置**：`PI_GLINK_TENANCY=multitenant`、`PI_GLINK_AUTH_MODE=accounts`，以及默认 `tenant` 策略下
  必需的 `PI_GLINK_TENANT_CONFIG_FILE`（`shared` 策略下不得配置）；不再需要网关签名密钥。账号库默认
  `<LOCAL_WEB_DATA_ROOT>/accounts/accounts.db`（仓库外，
  SQLite WAL），与 run/events 文件存储分离，互不迁移。宿主首次创建时建立 0700 专用目录与 0600
  库文件（WAL/SHM 同样属主独占），已有权限过宽或符号链接一律拒绝启动且不自动 chmod——旧库需操作员
  停止宿主后迁移并 `chmod 600`。宿主仍只监听 loopback，生产必须由 TLS 终止代理提供 HTTPS。
- **引导**：首位管理员由操作员在**宿主停止**时离线签发（取得 data-root ownership 后）：
  `python -m pi_glink.app --create-invite --tenant tenant-alpha --tenant-name 组织甲 --email admin@example.com --role admin`。
  默认（`tenant`）策略下该租户必须已在 `tenants.json` 里配置模型密钥（缺 key 拒绝）；
  `shared` 策略下只校验宿主冻结的共享模型配置（不配置租户凭据文件），首个邀请直接创建组织记录。
  邀请码只打印一次、数据库只存摘要。注册**只创建账号，不创建租户、不授予模型凭据**；后续管理员
  登录后在账号菜单里给本租户邮箱发一次性邀请。
- **会话**：`hashlib.scrypt`（随机盐、逐行记录参数、恒时比较）保存口令；登录成功后服务端签发高熵令牌，
  数据库只存摘要，浏览器只拿 `HttpOnly` + `SameSite=Lax`（HTTPS 下 `Secure`）cookie。会话有绝对有效期
  与空闲有效期，停用用户/租户、注销立即失效；SSE 长连接在每次轮询都重新校验，失效即断开。
- **请求防护**：登录/注册/邀请校验都有固定窗口限速。登录先按 socket peer（绝不读
  `X-Forwarded-For`）计全局预算、再按邮箱计账号预算：轮换邮箱无法无限触发口令散列，限额后的请求
  在执行 `scrypt` 之前就被拒绝；反代部署下所有用户共享代理这一个 peer，该预算即全部署登录上限
  （默认 120 次/60 秒，可配置），是保护单机 CPU 的可用性取舍。登录失败对“账号不存在”与“密码错误”
  返回同一个 401。所有状态变更 POST 都要求 `application/json`、同源 `Origin`/`Referer`/`Sec-Fetch-Site`，已登录
  会话还必须携带与 cookie 绑定的 `X-Pi-Glink-CSRF`；客户端伪造的断言头、query/body `tenantId` 一律
  忽略，登录失败也不会回退到 gateway/local。
- **回退**：切回 `PI_GLINK_AUTH_MODE=gateway`（或关闭多租户）即可；账号库保留只读，不自动改写 run
  历史，也不降低 owner 校验。

> **合并窗口兼容**：静态前端由宿主逐次读盘，先更新代码、后重启宿主时，运行中的旧 `local` 宿主
> 没有 `GET /api/me`。前端仅在 `/api/me` 明确 404 且 `/api/config` 成功返回严格 `multiTenant:false`
> 时按旧 local 流程初始化（旧历史照常显示）；401、网络错误、多租户或含糊配置一律 fail-closed，
> 不会因此绕过账号身份。宿主重启后该兼容分支即失效。

### 模型凭据策略：`PI_GLINK_MODEL_CREDENTIALS`（默认 `tenant`）

多租户模型的“谁提供模型 key”是显式配置，不是隐式兜底：

- **`tenant`（默认，现行为）**：每个租户在 `tenants.json` 里配置自己的 `modelApiKey`（可选
  `webSearchKey`）；缺模型 key 的租户拒绝准入，绝不回退宿主 `OPENAI_API_KEY`、操作员
  `~/.pi/web-search.json` 或别的租户。既有部署不需要改任何东西。
- **`shared`（显式 SaaS 策略）**：宿主在启动时冻结一套 `OPENAI_API_KEY` / `OPENAI_BASE_URL` /
  `OPENAI_MODEL` 供所有租户使用；可选搜索 key 只来自宿主级 `PI_GLINK_WEB_SEARCH_KEY`，不会
  读取操作员 `~/.pi/web-search.json` 顶替。共享模式不读取任何租户配置：不得配置
  `PI_GLINK_TENANT_CONFIG_FILE`（不设置或留空即为无该入口），设置即拒绝启动，也不会构造或
  读取租户凭据存储。三件套缺任一、或把 `shared` 用在非多租户模式，宿主一律拒绝启动，绝不
  静默回退 `tenant` 或 `local`。
- **执行隔离**：SaaS 部署首选 `LOCAL_WEB_EXECUTOR=e2b`（Docker 同样允许；`inproc` 在两种策略
  下都被拒绝）。共享 key 仍只经每个 run 的运行期 `auth.json`（0600）交付：不进沙箱进程环境、
  命令行、页面或事件/报告，E2B 面随沙箱销毁；宿主运行期目录内的 0600 副本与工具回显仍是既有
  残留面。每 run 脱敏边界包含共享 key 与宿主已知秘密，`redactor_for_record` 在 `shared` 下走
  宿主/共享边界，不会因共享模式没有租户凭据条目而漏脱敏。这仍是回显保护 + 沙箱执行隔离，不承诺 key
  不可能被宿主可读、工具回显或网络外传。
- **部署最小步骤（accounts + shared + e2b）**：配置 `E2B_API_KEY`、共享模型三件套与（可选）
  `PI_GLINK_WEB_SEARCH_KEY`；开启 `PI_GLINK_TENANCY=multitenant`、`PI_GLINK_AUTH_MODE=accounts`、
  `PI_GLINK_MODEL_CREDENTIALS=shared`、`LOCAL_WEB_EXECUTOR=e2b`，并指定仓库外的 0700 账号库目录；
  生产由 TLS 终止代提供 HTTPS 并设置 `PI_GLINK_PUBLIC_ORIGIN`；宿主停止时用 `--create-invite`
  签发首位管理员邀请。真实 HTTPS/邮件/E2B live 验收不在本期范围。
- **回退**：把 `PI_GLINK_MODEL_CREDENTIALS` 改回 `tenant` 并为每个租户补回 `modelApiKey`，或
  关闭 `PI_GLINK_TENANCY` 回到单用户；账号库与 run 历史保留只读，不自动改写、不降低 owner 校验。

> 隔离范围：宿主 API 授权、run 归属、凭据交付与 redaction 在宿主侧完成；容器/沙箱提供执行环境
> 隔离，但不构成对宿主 API 或存储的替代边界。合成验收覆盖账号存储/会话/CSRF/跨租户与 fake 执行面；
> 真实网关、真实 HTTPS/TLS 代理、邮件交付、Docker/E2B live 与生产数据仍未验证，上线前仍需按
> [多租户方案](docs/multitenancy-plan.md) §9 逐项验收。

## 搜索

检索能力全部来自 [pi-web-access](https://github.com/nicobailon/pi-web-access) 扩展，声明在 `harnesses/pi/settings.json` 的 `packages` 里（`npm:pi-web-access@0.31.0`，精确 pin）。本机 inproc 面 pi 启动时把它安装到本 run 的运行期配置目录；Docker / E2B 面宿主把该声明改写成镜像内本地包 `/opt/pi-packages/node_modules/pi-web-access`（pi 官方 local package，"Loaded from the resolved path without copying"），run 内不再访问 registry、不再每 run 生成 `npm/`；镜像守卫在包缺失时明确失败（exit 78），不让 `web_search` 静默消失。实测对照与发布/回滚见 [docs/evidence/prebaked-packages.txt](docs/evidence/prebaked-packages.txt)。

凭据由宿主渲染到**本 run 的**运行期配置目录的 `web-search.json`（权限 600）：

1. 有 `PI_GLINK_WEB_SEARCH_KEY` 时渲染为 `{"exaApiKey": …}`；
2. 否则复制操作员本机的 `~/.pi/agent/web-search.json`（或 `~/.pi/web-search.json`）；
3. 都没有也能工作：pi-web-access 会回退到无密钥的 Exa MCP 检索。

多租户下不会读取操作员本机文件：默认 `tenant` 策略只渲染该租户条目的 `webSearchKey`（缺省则无
key 检索）；显式 `shared` 策略只渲染宿主级 `PI_GLINK_WEB_SEARCH_KEY`。

本仓库不包含 Serper / Jina 链路。检索证据见 [docs/evidence/local-web-search.txt](docs/evidence/local-web-search.txt)。

## 委派与子代理

`pi-subagents@0.40.0` 已随模板加载，并配一个在本仓的取证子代理定义
（[harnesses/pi/agents/researcher.md](harnesses/pi/agents/researcher.md)：自包含任务、一手来源优先、打开原文核对、
产物落工作区、只回报要点与来源）。默认工作约定只写一句委派原则：相互独立、可并行取证的子问题
可以交给 `researcher`，主 agent 亲自核实回报的关键事实并自己写 `report.md`；何时派、派几个由
模型按题目决定。

**强制前台不靠包开关。** pi-subagents 0.40.0 没有禁用 async/detached 的配置或环境开关
（[验证记录](docs/pi-integration-notes.md#8-pi-subagents-0400-验证spike-glink-sub-01)），
产品改用本仓自有的 [前台守卫扩展](harnesses/pi/extensions/subagent-foreground-guard.ts)：官方 `tool_call`
钩子把 `async` 一律改写为 `false`，无法安全改写的后台、定时、恢复、控制与定义管理入口直接阻止，
`share`/`worktree`/工作区外路径也一并挡住；扩展配置（`harnesses/pi/extensions/subagent/config.json`）
同时固定为最保守取值（`asyncByDefault:false`、定时关、watchdog 关、intercom 关、嵌套深度 1、
并发 4、产物落工作区）。清单与逐项理由见
[集成笔记 §10](docs/pi-integration-notes.md#10-pi-subagents-0400-的前台强制接入glink-sub-02离线验证)。

未覆盖项如实记录：真实模型下的前台并行效果与子代理会话文件的收尾脱敏尚未验证；客户端输入面的
`/` 命令（`/run --bg`、`/subagents-detach` 等）不在 `tool_call` 钩子可达范围，由宿主在准入层挡住
（只接受 `/<蓝图名> <题目>`，其余 `400 unsupported_command`，见 architecture.md §4）。

## 安全边界

- **凭据不进产品面**：事件流、`events.jsonl`、报告文件与下载、pi 会话文件都经过
  `host/pi_glink/redaction.py` 的脱敏（值 + 形状双重检测）；流式回答只释放不可能再构成凭据的前缀、
  从不切开完整命中，释放结果等于整段脱敏，同一凭据被拆到多帧（含前后缀重叠的凭据）也无法拼回；
  暂存最多 8192 字符，超界时本段改输出 `[REDACTED]` 而不释放未判定原文；
  报告下载响应带 `X-Redacted` 头标明是否发生脱敏。
- **产物路径不被链接利用**：内核可达的文件只经 run 根目录的目录句柄逐级打开（任何一级是链接都拒绝，
  打开后再复核，写入在已核对的目录内原子替换），因此祖先目录在校验与打开之间被替换也读不到、写不到
  外部文件；`report.md` 脱敏后复制到宿主拥有的 `runs/<id>/artifacts/report.md` 再服务，会话文件只允许
  写在本次 run 的会话子目录内，工作区清扫也绝不跟随链接。
- **key 不进进程环境**：模型凭据写在 run 自己的运行期目录 `auth.json`（pi 官方凭据存储，权限 0600，
  仓库外），环境变量按白名单过滤——内核的 shell 工具跑 `env` 读不到它（三面真实 run 实测为 0）。
- **残留风险**：工具仍可能用 `read` 打开 `auth.json` 并回显；出站脱敏会把它替换为 `[REDACTED]`，
  但脱敏只是回显保护，不是编码级/网络级隔离。需要更强隔离时用 Docker / E2B 执行面，并给内核配
  专用、限额的凭据（完整多租户方案见 [docs/multitenancy-plan.md](docs/multitenancy-plan.md)）。
- **自检**：`scripts/check-no-secrets.py` 扫描运行态产物（真实命中即失败；文档占位符单独计数）。
- **多租户信任边界**：多租户模式的身份只来自服务端：网关模式下是验签后的 HMAC 断言，账号模式下是
  数据库里的会话记录（cookie 只存摘要，原文仅单次下发）；签名密钥、账号库与租户凭据都在仓库外。
  客户端自报 owner（请求体/query/头）在两种模式下都不被接受，账号登录失败也不会回退到网关或本地。
  这里的“隔离”
  指宿主 API 的租户授权与每 run 凭据/脱敏边界；容器/沙箱的实际隔离强度（镜像、内核、网络）不在
  本仓库的合成验证范围内，不能由此推断为生产级隔离。

## 目录

| 路径 | 职责 |
|---|---|
| `host/pi_glink/` | 宿主：配置解析、事件总线、HTTP/SSE 服务、静态资源 |
| `host/pi_glink/runs/` | 编排层：run 准入、执行骨架与收尾顺序、终态提交、外部资源台账 |
| `host/pi_glink/kernel/` | pi 接入层：headless 协议客户端（RPC / JSON）、事件投影、运行期配置渲染、三执行面适配；不得反向导入编排层或宿主编排模块 |
| `harnesses/<pack>/` | Harness 包：`harness.toml`（`family`；ACP 的 `command`/`session_config`/`permission_allow`/上游 `package`+`version`）、可选 `capabilities.md`（拼入系统提示）、ACP 的 `home/` 覆盖层；pi 包另含 `settings.json` 与 `extensions/`、`agents/`（前台守卫与 `researcher.md`），每次 run 先删再拷到 `runs/<id>/pi-agent/` |
| `web/` | 前端：按百鉴 UI 标准库逐值对齐的静态 ESM 资源（token 层 + 组件层 + 外壳） |
| `docs/` | `architecture.md`（进程拓扑、事件契约、执行器契约）、`pi-boundary.md`（扩展点与禁止项）、`multitenancy-plan.md`（下一阶段设计与决策点） |
| `tests/` | 宿主与内核的回归 |
| `<数据根>`（默认 `~/.pi-glink`） | 运行态：`host.lock`、`runs/<id>/{meta.json,events.jsonl,pi-agent/,artifacts/}`、`sessions/<id>/`、`workspaces/<id>/` |

## 前端与 UI 标准库对齐

前端没有打包器，所以「和标准库长得一样」不是靠引用同一份组件，而是靠**逐值抄写 + 逐值测量**：

- token 数值抄自标准库 `src/styles/globals.css`（`:root` + `@theme inline`），
  组件形态抄自 `src/components/*.tsx` 与完整页面 Demo（`src/pages/agent-assistant-demo/`）；
- 外壳照搬「8px 外围沟槽 + 左侧导航卡 + 右侧主工作区」与 52px 栏头、36px 导航行、
  120px 输入器（含禁用态 `opacity-45` 口径）；首页输入器按参考页顺序落在正文流里，
  品牌标识直接取标准库 `public/logo-icon.svg`（逐字节相同）；
- 断点沿用标准库登记的档位（≤1250 / ≤1050 / ≤980 / ≤900 / ≤721 / ≤600）。

```bash
scripts/check-ui-tokens.py                                 # 173 项 token 逐值比对
node tests/browser/ui-parity.mjs --base-url http://127.0.0.1:7861   # 26 个角色计算样式比对
```

口径、逐层对照表、刻意偏离与复现步骤见 [docs/ui-alignment.md](docs/ui-alignment.md)；
证据在 `docs/evidence/ui-parity.txt` 与 `docs/evidence/ui-parity.json`。
对齐校验需要标准库在场（`--library` 可指别处），不在场时 token 校验跳过而不是假装通过。

## 环境变量

与 FrontierAgent 保持同一套命名：模型 `OPENAI_*`，云沙箱 `E2B_*`，服务与执行面 `LOCAL_WEB_*`；本项目独有概念使用 `PI_GLINK_*`。完整列表（含注释）见 [.env.example](.env.example)。

宿主读一次配置后冻结，运行中的 host 不会因为改环境变量而改变行为。
配置来源优先级固定为：进程环境同名变量 > `.env` 文件 > 内置默认值；
`OPENAI_CONTEXT_WINDOW` / `OPENAI_MAX_TOKENS` 与表内其它键遵循同一顺序。

操作员常改、且此前未进文档的键（每条写明默认值与何时需要改）：

| 键 | 默认 | 何时需要改 |
|---|---|---|
| `PI_GLINK_HARNESS` | `pi` | 选择 harness 包（`pi` / `dsh` / `codex` / `opencode`）；一次部署只跑一个，启动冻结 |
| `PI_GLINK_HARNESSES_DIR` | `<repo>/harnesses` | 包根目录放在别处时；选中包缺失或清单不合法即拒绝启动 |
| `PI_GLINK_ACP_CREDENTIAL_FILES` | 空 | ACP agent 需要宿主只读凭据时，按 `source=dest`（`;` 分隔）注入每 run `HOME`；非 ACP 包下设置即拒绝启动 |
| `PI_GLINK_ACP_ENV` | 空 | 给 ACP agent 进程传环境变量时；`HOME` 不可覆盖（每 run 状态根是隔离边界） |
| `LOCAL_WEB_ACP_WORKER_IMAGE` | `pi-glink-acp-worker:latest` | ACP + `docker` 面换镜像时（与 pi 面镜像分开，二者装的 agent 不同） |
| `LOCAL_WEB_E2B_ACP_TEMPLATE` | `glink-acp-worker` | ACP + `e2b` 面换模板时（与 pi 面模板分开，同上） |
| `PI_GLINK_METHODOLOGY_DIR` | `<repo>/methodology` | 方法论根（`work-agreement.md` 与 `blueprints/`）放在别处时 |
| `PI_GLINK_BLUEPRINTS_DIR` | `<methodology>/blueprints` | 蓝图目录单独覆盖时；目录缺失不是启动错误，只表示可用蓝图集合为空 |
| `LOCAL_WEB_TASK_TIMEOUT_SECONDS` | `1800` | 单次 run 的整体超时（秒）；题型明显更长/更短时。E2B 沙箱寿命按它 + 300s 取值 |
| `OPENAI_CONTEXT_WINDOW` | `400000` | pi 面模型元数据的上下文窗口；provider 窗口更小时 |
| `OPENAI_MAX_TOKENS` | `65536` | pi 面模型元数据的输出上限；provider 上限更短时 |

ACP 族的送达通道、凭据注入与体积边界见 [ACP 集成笔记](docs/acp-integration-notes.md)；
蓝图格式、准入与列举接口见 [会话蓝图](docs/blueprint-templates.md)。

## 验证

默认无模型交付检查（预先准备 Python ≥3.12、pytest 和 Node ≥24，不自动安装依赖）：

```bash
bash scripts/check.sh
```

该入口在临时 HOME 和清空继承环境的子进程中运行 Python（含仓库边界检查）和 Node 回归，原退出码决定成败；外部标准库逐值对齐明确记为 `SKIP`。不启动真实 pi、浏览器、Docker 或 E2B，也不读取真实 `.env` 或个人 pi 配置。它不是 OS 安全沙箱。定向命令和覆盖边界见 [验证矩阵](docs/engineering.md#验证矩阵)；`scripts/run-tests.sh` 保留为仅 Python 的低层入口，不代表全部交付检查。

以下为**单独授权的浏览器/live 验收**：需要明确归属的目标宿主；`ui-features` 和 `run-smoke` 会提交真实研究、读取服务历史，以下脚本都会更新仓库证据文件。它们不接入默认检查，也不证明多租户已完成。

```bash
node tests/browser/ui-parity.mjs --base-url http://127.0.0.1:7861   # 像素级对齐（需要标准库参考页）
node tests/browser/ui-features.mjs --base-url http://127.0.0.1:7861 # 32 项全功能自检
node tests/browser/run-smoke.mjs --base-url http://127.0.0.1:7861   # 真实 run 端到端 + 截图
```

**自包含的浏览器验收（不需要既有服务、不调模型）**：

```bash
cd tests/browser && npm install --no-audit --no-fund   # 一次性；lockfile 锁定 playwright
node tests/browser/acceptance.mjs --mode accounts      # 102 项：多租户 + 站内账号
node tests/browser/acceptance.mjs --mode local         # 81 项：单用户本地面
node tests/browser/acceptance.mjs --mode gateway       # 7 项：网关未认证面
```

它自己起隔离宿主（真实 Runner/存储/事件总线/HTTP/SSE + 生产前端，执行面是合成替身，
临时数据根与合成凭据），结果与截图写 `--out`（默认系统临时目录）。最近一次全量结果见
[docs/browser-acceptance-report.md](docs/browser-acceptance-report.md)，用法见
[tests/browser/README.md](tests/browser/README.md)。它不接入 `scripts/check.sh`。
