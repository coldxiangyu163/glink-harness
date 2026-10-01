# 0008 真实 key 只留在宿主出口代理

- 状态：已接受（2026-09-28，多租户计划决定 D4；取代 [0003](0003-acp-credential-copies-removed-at-close-out.md) 的 pi 部分）
- 范围：`host/glink_harness/egress.py`、`kernel/agent_env.py`（运行期渲染）、`kernel/harness_pi.py`、
  `kernel/{sandbox,docker_env,e2b_env}.py::egress_origin`、`runs/runner.py`（grant 签发/吊销与终帧用量）、
  `config.py::_egress_is_reachable`

## 背景

此前 pi 族 run 的真实模型 key 渲染进本 run 的 `auth.json`，检索 key 渲染进 `web-search.json`
（或从操作员 `~/.pi/web-search.json` / `GLINK_WEB_SEARCH_CONFIG` 复制），随执行环境挂载或上传到
容器/沙箱，收尾时删除副本（0003）。内核工具在 run 期间可以读出真实 key；出站脱敏只能做回显保护，
在多租户下这意味着每个租户的真实 key 都进入一个不受信的执行环境。

## 决定

1. 宿主进程内运行一个凭据出口代理：独立 loopback 监听（`GLINK_EGRESS_HOST` / `GLINK_EGRESS_PORT`），
   首个 grant 签发时绑定、最后一个吊销后同步停止（socket 关闭后才释放锁，固定端口下下一次签发可立即重绑）。`/model/…` 转发到宿主冻结的 `OPENAI_BASE_URL`，
   `/search/…` 转发到宿主冻结的 `GLINK_WEB_SEARCH_UPSTREAM`，由宿主替换 `Authorization` /
   `x-api-key`。仅放行 `POST /model/chat/completions` 与有检索授权的
   `POST /search/search`、`POST /search/answer`，路径精确匹配（不接受编码别名、额外后缀或查询串）；
   grant 绑定宿主冻结的模型 id，UTF-8 JSON 请求的顶层 `model` 必须精确相等，重复键、非法 JSON
   与非 JSON 媒体类型拒绝，且不转发、不计量、不注入真实 key。依据 pi 0.86.1 的
   `openai-completions` 与 pi-web-access 0.31.0 的实际消费者；包升级须重新核对白名单。
   不跟随重定向；上游只接受 https 或 loopback http。
2. 每个 run 一个短期 token（`pge_…`），绑定 `run_id` 与 tenant，只存内存；终帧提交时吊销，宿主重启
   全部失效。执行环境只看到代理 URL 与 token：`auth.json` 的 key 与 `web-search.json` 的 `exaApiKey`
   都是 token，`models.json` / `web-search.json` 的 base URL 指向代理。
3. inproc 走 loopback；Docker 经 `host.docker.internal`（`--add-host …:host-gateway`）；E2B 必须配置
   `GLINK_EGRESS_PUBLIC_URL`，否则启动即拒绝（fail closed）。配置了 public URL 时各面都用它。
   pi-web-access 拒绝非 loopback http 检索地址，所以 Docker 面带检索 key 也要求 https public URL。
4. 代理按上游 `usage` 计量，作为 pi 族终帧/run 记录的权威 `usage`（`source: "egress"`）；内核自报值
   另记 `kernelUsage`。唯一策略钩子是按租户的每 run 配额（请求数 / token 数，429 拒绝）。
5. 删除真实 key 的渲染与 pi 的 `credential_copies` 收尾删除：运行期文件只含已吊销的 token，保留无害；
   `SessionSweep.scan` 字段随之删除。
6. ACP 族维持 0003 的注入副本方案，未接入代理。

## 不兼容

不写迁移：`GLINK_WEB_SEARCH_CONFIG` 与操作员 `~/.pi/web-search.json` 不再读取；E2B 部署未配置
`GLINK_EGRESS_PUBLIC_URL` 将无法启动；旧 run 的 `usage` 仍是内核自报形状（无 `source`），旧 run
目录里可能残留的真实 key 文件不做处理。

## 理由与残留

真实 key 不再进入任何执行环境，租户之间也不再共享同一份可读 key。token 在 run 期间对内核工具可见，
但只能访问宿主冻结的两个上游中获准的端点与模型、受配额限制、run 结束即失效。
这仍允许工具直接使用 token 请求同一模型或合法搜索端点，不是网络隔离；ACP 的注入例外不变。越过 token 配额的那一次请求仍会完成；
多宿主进程之间不共享配额；public URL 与固定端口的转发属于部署义务。
