# 前端与百鉴 UI 标准库的对齐

本仓库的前端是**无构建链的静态 ESM**（`web/` 由宿主 `host/pi_glink/static_files.py` 直接吐盘上的文件）。
它没有也不打算引入打包器，所以「和标准库长得一样」不能靠「引同一份组件」来保证，
只能靠**逐值抄写 + 逐值测量**来保证。本文记录这份对齐的口径、事实源、验证方式和刻意偏离。

- 事实源：`/Users/coldxiangyu/work/BR/glink-ui-library`
  - token 数值：`src/styles/globals.css`
  - 组件形态：`src/components/*.tsx`
  - 组合与页面几何：`src/pages/agent-assistant-demo/*`（规范站唯一的完整页面 Demo）
  - 使用规则：`百鉴 UI Design Specification.md`
- 落地：`web/styles.css`（token 层 + 组件层）、`web/index.html`（外壳骨架）、`web/modules/*.mjs`（DOM 结构）

## 1. 对齐口径

三层各自的可验证判据：

| 层 | 判据 | 验证方式 |
|---|---|---|
| token 层 | 与标准库逐值相同 | `scripts/check-ui-tokens.py`（173 项，逐值比对，退出码即结论） |
| 组件层 | 同一角色在同一视口下的计算样式逐值相同 | `node tests/browser/ui-parity.mjs`（26 个角色，几何 ≤1px，颜色/圆角/排版/描边/海拔要求逐值相等） |
| 页面层 | 外壳几何与断点行为一致 | 同上（shell / sidebar / pane header / 卡片 / 输入器几何），断点另见 §4 |

「像素级」在这里被拆成可证伪的两件事：**值相同**（token、颜色、圆角、字号、行高、描边、阴影）
与**盒相同**（高度、内边距、间距、断点下的宽度）。文本宽度天然随内容变，不参与比对。

## 2. token 层

`web/styles.css` 的 `:root` 复制标准库 `:root` 与 `@theme inline` 的取值，两层结构照搬：

- L0.1 调色板 `--n-*` / `--p-*` / `--su-*` / `--wa-*` / `--de-*`：换肤只改这一段；
- L0.2 语义层：组件只消费语义层，不直接引用调色板。

```bash
scripts/check-ui-tokens.py            # 173 项逐值比对；不一致即非零退出
scripts/check-ui-tokens.py --print-diff
```

脚本按块解析（`:root` / `@theme inline`），**不整文件扫描**：标准库在 `.dark` 里整体重映射了语义层，
整文件扫描会把本仓库的亮色主题和标准库的暗色档放在一起比，结论必错。

本仓库**未启用暗色**：工作台是亮色单主题，所以不复制 `.dark` 段，也不复制暗色专用档
（`--su-d-*` / `--de-d-*` / `--p-d-*` / `--nd-*`）。这是范围裁减，不是取值不同。

## 3. 组件层（逐值抄写的锚点）

对照的取值来自运行中的参考页（1512×900 视口实测，命令见 §6），不是文档里的描述：

| 角色 | 参考页实测 | 本仓库 |
|---|---|---|
| 外壳 | `p-8 gap-8 bg=--background`，两块 `--card` 独立面，圆角 `--corner-xl` | 同 |
| 侧栏 | 240 / 212（≤1050）/ 64（≤721）；`py-16` | 同 |
| NavItem | h-36 / 圆角 8 / `px-12 gap-12` / 13-20 / 选中 `bg=--background` + `font-medium` + `--foreground` | 同 |
| 「新建」胶囊 | h-40 / 全圆角 / 1px `--border` / `px-8 gap-8` / 13-20 | 同 |
| 栏头 | h-52 / `px-24` / 底部 1px `--border` / 13-20 | 同 |
| 路径段 | h-20 / 圆角 6 / `px-4` / `--muted-foreground` | 同 |
| 按钮 default | h-40 / 全圆角 / `--primary` + 白字 / `px-16` / 14-22 / 500 | 同 |
| 按钮 sm | h-32 / `px-12` / 12-20 | 同 |
| outline | 1px `--input` + `--card` 底 + hover `--muted` | 同 |
| Badge sm | h-20 / 全圆角 / `px-8 py-2` / 11-16 / 400 | 同 |
| 输入器 | min-h-120 / 圆角 20 / 1px `--input` / `--elev-surface` / 编辑区 `20px 20px 8px` 14-22 / 工具行 `0 12px 12px` | 同 |
| 发送键 | 36 圆形 / `--primary` / 禁用 `opacity .45` + `--ink-3` | 同 |
| 思考折叠 | 行 h-28 / 圆角 8 / `px-6 py-4` / `-ml-6`；标签 13-20 / 500 / `--muted-foreground` | 同 |
| 工具调用行 | 行盒 432（420 内容 + 两侧 -6 外边距）/ h-28 / 圆角 6 / `px-6 py-2`；名称 12-20 / 500 / `--foreground` | 同 |
| 工具对象 chip | `px-4` / 11-16 / 圆角 4 / `--muted` 底 / `--muted-foreground` 字 / 1px `--border` 环 | 同 |
| 用户气泡 | 右对齐 / 圆角 20 / `--muted` 底 / `12px 16px` / 14-22 / `max-w-90%` | 同 |

## 4. 断点

标准库登记的档位（`≤1250 / ≤1050 / ≤980 / ≤900 / ≤721 / ≤600`）在本仓库的落地：

| 断点 | 行为 |
|---|---|
| ≤1250 | 首页双列卡片间距收紧 |
| ≤1050 | 侧栏 240 → 212；首页双列 → 单列；起始页顶部留白 104 → 64 |
| ≤980 | 运行状态条贴边 |
| ≤900 | 报告右槽 → 覆盖浮层（宽 `min(620px, 100%)`），主区回到单栏 |
| ≤721 | 侧栏 → 64 图标条；起始页留白 → 48；路径与输入器贴边 |
| ≤600 | 输入器工具行可换行 |

实测（1512 / 1250 / 1050 / 900 / 721 / 600 六档）：`documentElement.scrollWidth == clientWidth`，
无横向溢出；报告栏打开时同样不溢出。

## 5. 刻意偏离（登记在案）

| 项 | 偏离 | 理由 |
|---|---|---|
| 品牌标识 | **已按裁决换成标准库资产**：`web/logo-icon.svg` 与 `public/logo-icon.svg` 逐字节相同（SHA-256 `b5f3409b…`），favicon 与侧栏 24×24 同一份文件 | 对齐范围包含品牌标识；侧栏只画 24px mark，与参考页同尺寸同资产 |
| 字体族 | 取标准库令牌 `--font-sans-base`（PingFang SC 优先） | 参考页在 `.baijian-demo` 作用域里把字体压成 `"Noto Sans SC", sans-serif`；没装 Noto 的机器上它会静默落到浏览器默认 sans（实测该页正文宽 872 / PingFang 877，即实际不是 Noto）。令牌才是数值事实源，故按令牌走，并把自托管的 Noto Sans SC 放在末位兜底非 Apple 平台 |
| 暗色主题 | 未启用 | 仓库前端是亮色单主题；启用需同时补 `.dark` 段与切换入口，属新功能 |
| 轨迹展开区宽度 | 行几何与标准库逐值一致；**展开后的机器原文**放宽到 640px | 标准库的 420/380 来自它自己的 420 宽助理列；本仓库的展开区是终端原文（长行、等宽），420 会碎行 |
| 右槽内容 | 报告阅读器（标准库 Demo 的右槽装文件预览/进度） | 同一套「右栏＝槽位」规则，装的东西随产品功能走 |
| 首页信息架构 | 「最近运行 / 工作方式」两卡；**输入器按参考页顺序落在正文流里**（标题 → 输入器 → 双列卡片），对话屏仍在贴底 dock | 结构照参考页；条目按本项目的功能重排 |
| 输入器工具行 | 左侧是键盘提示（`⌘/Ctrl` + `↵` + 「提交」，Kbd 形态抄自 `src/components/kbd.tsx`），没有参考资料/创建/网络三颗药丸 | 本项目没有那三项能力，不画没有落点的控件；Kbd 是标准库组件，不是自造 |
| 输入器能力 | 只有发送键；无 @ 引用 / 附件 / 网络 / 创建 | 本项目没有这些能力，不画没有落点的控件 |
| 工具行尾状态字 | 多一枚 `text-mini` 状态（执行中/完成/失败…） | 运行态是本页的主要信息；字号与颜色取自标准库的 `-text` 档与 `--ink-3` |
| 首页历史行右簇 | 写相对时间（刚刚 / N 分钟前 / 昨天…），状态与耗时在第二行 | 与参考页历史行「左两行 + 右时间」同版式；状态色只走 `-text` 档 |

## 6. 复现

```bash
# 0) 两侧都要起服务
python3 -m http.server 5300 --bind 127.0.0.1 &        # 在标准库 dist/ 里（pnpm build:file 产物）
scripts/run-local-web.sh --port 7861 &

# 1) token 逐值
scripts/check-ui-tokens.py

# 2) 组件与页面几何逐值（26 个角色，三块屏）
node tests/browser/ui-parity.mjs --base-url http://127.0.0.1:7861
#   证据：docs/evidence/ui-parity.txt / docs/evidence/ui-parity.json

# 3) 功能自检（32 项：外壳 / 首页 / 输入器 / 运行与停止 / 历史重放 / 折叠与工具行 /
#    报告右槽可滚 + 输入器不跨栏 / 下载 / 侧栏折叠持久化 / 六档断点 / 跳转链接 /
#    空态 / 模块加载失败恢复面 / 控制台）
node tests/browser/ui-features.mjs --base-url http://127.0.0.1:7861
#   证据：docs/evidence/ui-features.txt / ui-features.json

# 4) 真实 run 端到端（思考折叠 / 工具行 / 报告右槽 + 截图）
node tests/browser/run-smoke.mjs --base-url http://127.0.0.1:7861
```

对齐校验需要标准库在场（`--library` 可指别处）。标准库不在时 token 校验**跳过**而不是假装通过：
`tests/test_ui_tokens.py` 用 `pytest.skip` 表达这一点。

## 6.1 修过的两个布局缺陷（本轮实测抓到）

| 缺陷 | 根因 | 修复与判据 |
|---|---|---|
| 打开报告后正文无法滚动 | `.shell.preview-open .surface` 只声明了列、没声明行：隐式 `auto` 行被内容撑高，超出容器的部分被 `.surface{overflow:hidden}` 裁掉，两个滚动区的 `clientHeight` 等于 `scrollHeight` | 行钉成 `grid-template-rows: minmax(0, 1fr)` + 子项 `min-height: 0`；判据：报告正文 `scrollHeight 11061 / clientHeight 832`，两条滚动都能驱动（`ui-features` 的「报告右槽：正文真的能滚动」与「两条滚动都可驱动」） |
| 报告栏打开时输入器横跨两栏、压在报告上 | `.composer-dock` 是 `position: absolute`，而 `.pane` 没有定位，包含块一路落到 `.surface` | `.pane { position: relative }`；判据：`dockRight == paneRight`（`ui-features` 的「输入器只占左栏」） |

## 7. 未纳入比对的部分

- **提示条（toast）**：标准库 Demo 的 toast 由 sonner 运行期渲染，参考页没有稳定可测的等效节点，
  因此按 token（`--popover` / `--elev-surface` / `--corner-lg` / `--text-body`）落地，不做逐值比对。
- **自绘 tooltip**：同上，标准库走 Radix Portal，本仓库是 CSS 自绘。
- **滚动条**：标准库的「滚动时显色」是页面态（`data-scroll-active`），本仓库照抄该行为，宽度 8px 一致，
  但 Chromium 的滚动条不参与 `getComputedStyle` 比对。
