# 0012 harness 包清单 v2 为唯一真源，产物由清单生成

- 状态：已接受（2026-09-28，架构重设计 P3）
- 范围：`harnesses/<pack>/harness.toml`、`host/pi_glink/harnesses.py`、`config.py`、
  `scripts/generate-pack-artifacts.py`、`docker/**/Dockerfile*`、`docker/e2b/templates.json`、
  `harnesses/pi/settings.json`

## 背景

包的入口命令、能力与上游版本同时出现在清单、`HostConfig` 字段（`acp_command` 等）、环境变量
（`PI_GLINK_ACP_*`）、Dockerfile `ARG`、E2B 模板参数与族代码的包名特判里，一致性靠逐字比对测试维持。

## 决定

1. 清单格式 `pi-glink-harness/v2`：`[entry]`、`[capabilities]`（`resume` / `subagent_dispatch` /
   `child_progress` / `session_config` / `permission_allow`）、`[image]` 与 `[[image.packages]]` 精确版本；
   pi 包同样声明。
2. Dockerfile `ARG *_VERSION`、`docker/e2b/templates.json` 与 pi `settings.json` 的 `packages` 由
   `scripts/generate-pack-artifacts.py` 从清单写出并入库；`--check` 与仓库边界测试校验一致，不手改产物。
3. `HostConfig` 持有 `HarnessPack` 对象（`config.pack`），`harness` / `harness_pack` 为派生属性；族代码
   只读能力声明，不按包名特判；`resume=false` 的包在准入层一律 `session_unavailable`。

## 不兼容

v1 清单启动即拒绝；`PI_GLINK_HARNESS=acp` 与 `PI_GLINK_ACP_COMMAND` / `_SESSION_CONFIG` /
`_PERMISSION_ALLOW` 不再读取（删除专用拒绝分支）；`CONTAINER_LOCAL_PACKAGES` 删除。无迁移。
