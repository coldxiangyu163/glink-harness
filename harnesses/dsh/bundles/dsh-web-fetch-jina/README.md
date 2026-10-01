# @glink-harness/dsh-web-fetch-jina

glink-harness 的 **out-of-tree dsh bundle**：给 DeepSeek Harness 的 `ctx.web` 注册一个
Jina Reader（`https://r.jina.ai/`）fetch provider（id `jina`），替代默认的本地 HTTP
provider（id `http`）。替代动机是 GLINK-DSH-01 的根因：`dsh-web-fetch-http` 在
「目标主机只有 AAAA + 运行环境无 IPv6 出口」时让一次 socket `'error'` 升级为未捕获异常，
整个 dsh 进程 exit 1（E2B 上表现为整轮 `kernel_exit`）。Jina 侧完成解析与抓取，本进程
不再直接连接目标，也不做地址钉定。

## 用在哪一层

* **bundle 声明**：`package.json` 的 `dsh.bundle.patch` 指向 `./cordis.patch.yml`。
* **patch 做三件事**：插入 `web-fetch-jina` 行（`name: './lib/index.js'`，patch 相对路径，
  由 dsh 在加载期转成 file URL）、禁用 `web-fetch-http`（只禁用不删除：误配时 fail closed，
  不静默回退到崩溃面）、把 `web.fetchProvider` 指向 `jina`；另把 `tool-web.fetchTimeoutMs`
  提到 60s（Jina 读 PDF/JS 页面常超过 30s 默认 fetch 预算）。
* **交付**：bundle 由两个 ACP 镜像在构建期装进 dsh 安装自身的
  `node_modules/@glink-harness/dsh-web-fetch-jina`（`resolveBundleDir` 的第一个解析锚点）；
  每 run 的 ACP profile 清单由 `harnesses/dsh/home/.dsh/profiles/acp/package.json` 提供
  （覆盖层只带清单，代码随镜像重建更新，避免追问继承旧副本）。细节与验证见
  `docs/acp-integration-notes.md` §19。

## 配置

| 字段 | 默认值 | 含义 |
|---|---|---|
| `apiKeyEnv` | `JINA_API_KEY` | dsh credentials 引用名（`credentialRef`）；每次 fetch 解析，配置面没有字面 key 字段 |
| `baseURL` | `https://r.jina.ai` | Reader 基地址；目标 URL 在 POST body 的 `url` 字段 |
| `timeoutMs` | `60000` | provider 兜底超时；模型侧 `tool-web.fetchTimeoutMs` 先到期 |
| `maxResponseBytes` | `5000000` | 响应体上限（JSON 信封必须完整，超限即 `WEB_FETCH_TOO_LARGE`） |
| `maxBodyChars` | `100000` | 解码后正文上限，超出截断并置 `truncated` |
| `userAgent` | `glink-harness-dsh-web-fetch-jina/0.1.0` | 发给 Jina 的 `User-Agent` |

## 行为契约

* **成功**：`WebFetchResult { url, statusCode, body: { kind: 'text', content }, truncated }`。
  `statusCode` 是**目标**的状态：Jina 对目标失败也回 HTTP 200，目标状态在
  `data.httpStatus`（直接对 `r.jina.ai` 实测 200/404 两例）；目标 404 因此是结果而非错误。
* **失败**：一律 `WebError`，绝不升级为进程级异常。
  - `WEB_INVALID_URL`：非 http(s)、内嵌凭据、超长、无法解析；空 `apiKeyEnv` 等配置错误。
  - `WEB_BLOCKED_URL`：IP 字面量/保留主机名属于非公共目的地，或本地解析返回任一非公共地址
    （best-effort；解析失败不拦截，仍交给 Jina）。拒绝发生在请求发出**之前**。
  - `WEB_PROVIDER_CREDENTIAL_MISSING`：`apiKeyEnv` 未解析到非空值。
  - `WEB_ABORTED` / `WEB_FETCH_TIMEOUT`：调用方取消 / 本 provider 超时。
  - `WEB_FETCH_TOO_LARGE`：声明长度或流式读取超过 `maxResponseBytes`。
  - `WEB_PROVIDER_ERROR`：Jina 401/402/403/422/429/5xx、非 JSON 响应、信封缺 `data.content`、
    传输失败、凭据解析失败、配置在运行期变为非法值。
* **PDF / Office / JS 页面**：由 Jina 转文本，本 provider 不区分内容类型。
* **不写会话日志**：provider 不向 durable session 追加任何事件。dsh 0.1.5-rc.3 在
  `session/resume` 时按冻结词表回放日志，树外事件必须带 envelope 级 `ignorable: true`，
  而 `session.append()` 无法设置它；早先的 `web/jina-fetch-request` 审计事件因此让整条会话
  无法续接（`-32603 Internal error`）。每 run 取证改用 `web_fetch` 的 tool call/result
  与本 bundle 的 provider 配置。
* **数据外发**：被抓取的 URL 与 `JINA_API_KEY` 发给 `r.jina.ai`（可配置自建 Reader）；
  目标页面内容经 Jina 返回本进程。调用方（研究 Agent）接受该外发面。

## 测试

```bash
node --import ./harnesses/dsh/bundles/dsh-web-fetch-jina/tests/support/register-stubs.mjs \
  --test harnesses/dsh/bundles/dsh-web-fetch-jina/tests/*.test.mjs
```

单测不联网：`tests/support/` 用模块解析 hook 提供 dsh seam 的替身（`WebError`、`deadline`/
`timeoutOf`、`credentialRef`、`launchEnvironmentOf`、`schemastery`），`fetch` 与 DNS resolver
全部注入。替身的 schemastery 刻意没有 `.volatile()`：镜像钉版 0.1.5-rc.3 的 schemastery
3.18.2 没有该字段种类（0.1.7 才引入），同类不兼容应在单测变红而不是镜像启动时才暴露。

## 限制与未覆盖

* 不做本地解析后的地址钉定（设计如此：本进程不连接目标）；DNS 检查是 best-effort，
  split-horizon 之外的解析差异可能漏判或误拦。
* `data.url` 是 Jina 收到的请求 URL（重定向后仍是它），不是最终落地 URL。
* 匿名模式被显式拒绝：没有 key 就是 `WEB_PROVIDER_CREDENTIAL_MISSING`。
* 速率/配额是 Jina 账户事实（实测响应头 `x-ratelimit-limit: 500, 500;w=60`）；429 映射为
  `WEB_PROVIDER_ERROR`，不做自动重试与退避。
* 真实可达性、限流与费用只在 E2B/镜像验证范围内记录（`docs/acp-integration-notes.md` §19）；
  对境外受限站点的可达性依赖 Jina 侧，未穷举。
