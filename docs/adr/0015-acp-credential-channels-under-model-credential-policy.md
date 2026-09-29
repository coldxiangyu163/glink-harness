# 0015 ACP 凭据通道纳入模型凭据政策

- 状态：已接受（2026-09-29，凭据边界收口）
- 范围：`host/pi_glink/config.py`（`_acp_credentials_follow_policy`、shared 三件套按族分流）、
  `host/pi_glink/kernel/harness_acp.py`（`validate_credentials` 政策分流）、
  `tests/test_acp_credential_policy.py`、`tests/test_kernel_harness_pi.py`、
  `docs/multitenancy-plan.md`、`docs/architecture.md`、`docs/engineering.md`、`README.md`

## 背景

`PI_GLINK_ACP_ENV` / `PI_GLINK_ACP_CREDENTIAL_FILES` 把运营方配置的凭据（环境变量与凭据文件
副本）注入**每个** ACP run，与租户身份无关。它们此前游离在 `PI_GLINK_MODEL_CREDENTIALS` 政策
之外：

- multitenant + `tenant`（默认政策）：任何租户的 ACP run 都静默使用运营方凭据——绕过
  「模型凭据默认每租户自备」的产品边界，且不需要 shared 政策所要求的那次显式声明；
- multitenant + `shared`：用法合法，但通道不在 shared 的「宿主冻结一套凭据」清单里，
  忘配通道的宿主照常启动，run 进到 agent 无凭据才失败（fail-open）。

族准入 `AcpHarnessFamily.validate_credentials` 是无条件 no-op（自述「注入点是 M2 工作」），
租户没有任何 key 也照样受理。这不是已声明的债务：README 的多租户边界声明没有记录这个缺口。

## 决定

1. **通道即模型凭据**：两个 ACP 通道整体视为模型凭据的一种形态，服从既有政策原则
   「共享必须显式声明，声明后不读任何租户配置」。整通道按凭据对待、不按变量名挑拣
   （如区分「代理变量」），因为依赖命名约定的边界可以被改名绕过。
2. **multitenant + `tenant` 拒绝通道**：配置任一通道即拒绝启动（`_acp_credentials_follow_policy`，
   fail-closed）。租户级注入点未实现（M2）前，tenant 政策下不存在合法的 ACP 共享凭据用法。
   通道未配置的宿主可以启动，但每个 ACP run 在准入被族级拒绝并给出显式出路——启动期与
   准入期两层互为纵深。
3. **multitenant + `shared` 通道是具名凭据源**：两通道全空即拒绝启动，镜像 pi 族
   `OPENAI_*` 三件套的「shared 缺凭据源不 boot」。shared 的三件套要求**按族分流**：
   pi 宿主维持原语义；ACP 宿主的凭据源是 ACP 通道，不再被迫配置用不上的 OPENAI 键。
4. **单用户（`local`）不变**：运营方自己的凭据给自己的 run，是文档化的本地入口。
5. `AcpHarnessFamily.validate_credentials` 按 `self._config` 的政策分流：单用户与 shared
   维持 no-op；multitenant + tenant 抛 `ConfigError`（web 面转 400 `not_configured`）。

## 不兼容与边界

- multitenant + tenant + 配置了 ACP 通道的宿主从此不能启动；multitenant + tenant 的 ACP run
  从「静默用运营方凭据」变为准入拒绝。受影响部署的出路：显式 `shared`，或等 M2 租户注入点。
- 本决定只管辖宿主的两个显式注入通道。ACP agent 状态根继承（追问链路）与镜像内预置凭据不在
  管辖内——前者属续接种子链路，后者属镜像审计，均为 M2 范围。
- ACP 族接入出口代理（真实 key 不进执行环境，ADR 0008 的 ACP 侧）仍是未决项；本决定不改变
  「ACP agent 在执行环境内持有注入凭据副本、收尾删除」的现状（ADR 0003 / 集成笔记 §18）。
- M2 租户注入点落地后，规则 2 放宽为「租户级注入」，本 ADR 的政策原则不变。
