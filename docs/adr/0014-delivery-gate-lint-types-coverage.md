# 0014 交付门禁加入静态检查、类型检查与覆盖率 ratchet

- 状态：已接受（2026-09-29，交付门禁加固）
- 范围：`pyproject.toml`（`[tool.ruff]`、`[tool.mypy]`、`dev` extra）、`scripts/check.sh`、
  `scripts/check-coverage.py`、`scripts/coverage-baseline.json`、`docs/engineering.md`、`README.md`

## 背景

`scripts/check.sh` 之前只跑事件 schema 生成物校验与 Python/Node 回归：没有静态检查，也没有覆盖率
门槛。回归退化、未使用导入、类型注解漂移只能靠人工发现；覆盖率只在人工报告里出现，无法在提交时
拦住倒退。仓库已有的 `# noqa: PLC0415` 等注释说明历史上按 ruff 书写，但没有版本化配置。

## 决定

1. 交付门禁固定为「事件 schema 生成物 → ruff 静态检查 → mypy 类型检查 → Python 回归（pytest-cov
   采集 `host/pi_glink` 覆盖率）→ 覆盖率总量 ratchet → Node 回归 → dsh bundle 单测」。任一步非零
   退出即按原退出码失败，沿用现有 fail-fast 与 `env -i` 隔离口径；失败不吞成警告。
2. 开发工具（ruff、mypy、pytest-cov/coverage）只放 `[project.optional-dependencies].dev`，永不进入
   运行期 `dependencies`，「宿主零运行时依赖」不变。缺失时预检 exit 2，提示
   `uv pip install -e ".[dev]"`。
3. ruff 规则集显式收窄为 `E4,E7,E9,F`（语法/导入/未使用），不启用该版本默认全集（实测 593 条，
   多为风格面）。放宽 `select` 或新增 `ignore` 必须说明被替代的义务。
4. mypy 覆盖 `host/pi_glink`；唯一豁免是四个组合式 web mixin 模块（`web_http`/`web_security`/
   `web_sse`/`web_routes`）的 `attr-defined`：它们依赖 `PiGlinkServer` 组合后的属性，逐行
   `type: ignore` 反而会埋没真实错误，其余错误类别在这些文件里仍受检。
5. 覆盖率以 `scripts/coverage-baseline.json` 的总量基线为准，只做**总量 ratchet**（不做 diff
   覆盖率）；实测低于基线即失败。升基线须重跑门禁读取实测值（向下取整到 0.1）随代码提交；降基线
   必须说明被替代的义务（`docs/engineering.md`「需要先取得决定的变更」）。

## 不兼容

新门禁会让违反上述规则的新代码变红；本决定落地时存量问题已一并修完，门禁当时全绿。覆盖率是
`host/pi_glink` 的语句覆盖总量，不证明关键路径均已覆盖；mixin 豁免意味着这四个文件里跨 mixin 的
属性拼写错误仍需 pytest 全量回归兜底。
