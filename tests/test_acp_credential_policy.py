"""GLINK-ACP-CRED：ACP 凭据通道服从模型凭据政策（ADR 0015）。

``PI_GLINK_ACP_ENV`` / ``PI_GLINK_ACP_CREDENTIAL_FILES`` 是运营方注入每个 ACP
run 的共享凭据通道，自 ADR 0015 起纳入 ``PI_GLINK_MODEL_CREDENTIALS`` 政策管辖：

* 单用户（``local``）：通道照常——运营方自己的凭据给自己的 run，本地入口不变；
* multitenant + ``tenant``：配置任一通道即拒绝启动——那是未经声明的共享凭据，
  而租户级注入点未实现（M2）；**整通道**按凭据对待，不按变量名挑拣（安全边界
  不能依赖命名约定）；
* multitenant + ``shared``：通道成为 ACP 宿主的具名共享凭据源（镜像 pi 族的
  ``OPENAI_*`` 三件套 fail-closed），两通道全空即拒绝启动；
* shared 的 ``OPENAI_*`` 三件套要求按族分流：pi 宿主维持原语义，ACP 宿主不再
  被迫配置用不上的 OPENAI 键；
* 族准入（``AcpHarnessFamily.validate_credentials``）按同一政策分流：单用户与
  shared 维持 no-op，multitenant + tenant 在准入拒绝并给出显式出路（启动期
  ``_acp_credentials_follow_policy`` 与准入两层互为纵深）。

全部合成验证：临时目录、临时 pack、临时密钥/租户文件与假命令；不包含真实
agent、真实网关、Docker/E2B live 或生产数据。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from acp_pack import write_acp_pack
from pi_glink.config import ConfigError, HostConfig, build_config
from pi_glink.kernel.agent_env import RunCredentials
from pi_glink.kernel.harness import select_family
from pi_glink.store import RunStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def _gateway_secret(tmp_path: Path) -> str:
    path = tmp_path / "gateway-secret"
    path.write_text("test-secret-material\n", encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def _tenants_file(tmp_path: Path) -> str:
    path = tmp_path / "tenants.json"
    path.write_text(
        json.dumps(
            {
                "tenants": {
                    "tenant-alpha": {"modelApiKey": "sk-alpha-0000000000000000"},
                }
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return str(path)


def _acp_source_file(tmp_path: Path) -> str:
    path = tmp_path / "acp-key-source"
    path.write_text("sk-acp-shared-000000000000\n", encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def _acp_values(
    tmp_path: Path,
    *,
    tenancy: str = "local",
    policy: str = "tenant",
    openai_trio: bool = True,
) -> dict[str, str]:
    """Values for an ACP-host config; multitenant needs the gateway material."""
    values: dict[str, str] = {
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "PI_GLINK_HARNESS": "dsh",
        "PI_GLINK_HARNESSES_DIR": str(
            write_acp_pack(tmp_path, [sys.executable, "-c", "pass"])
        ),
    }
    if openai_trio:
        values.update(
            {
                "OPENAI_API_KEY": "sk-host-global-00000000000000",
                "OPENAI_BASE_URL": "https://example.invalid/v1",
                "OPENAI_MODEL": "test/model",
            }
        )
    if tenancy == "multitenant":
        values.update(
            {
                "PI_GLINK_TENANCY": "multitenant",
                "LOCAL_WEB_EXECUTOR": "docker",
                "PI_GLINK_GATEWAY_SECRET_FILE": _gateway_secret(tmp_path),
                "PI_GLINK_TENANCY_AUDIENCE": "https://example.invalid/id",
            }
        )
        if policy == "tenant":
            values["PI_GLINK_TENANT_CONFIG_FILE"] = _tenants_file(tmp_path)
        else:
            values["PI_GLINK_MODEL_CREDENTIALS"] = "shared"
    return values


def _family(
    tmp_path: Path, values: dict[str, str]
) -> tuple[HostConfig, Any]:
    config = build_config(values, REPO_ROOT)
    store = RunStore(config.data_root / "runs")
    return config, select_family(config.harness, config=config, store=store)


# -- 单用户：通道照常 ----------------------------------------------------------


def test_single_user_keeps_acp_env_and_credential_files(tmp_path: Path) -> None:
    values = _acp_values(tmp_path)
    values["PI_GLINK_ACP_ENV"] = "ANTHROPIC_API_KEY=sk-acp-local-000000"
    values["PI_GLINK_ACP_CREDENTIAL_FILES"] = f"{_acp_source_file(tmp_path)}=a/key"
    config = build_config(values, REPO_ROOT)
    assert config.acp_env == (("ANTHROPIC_API_KEY", "sk-acp-local-000000"),)
    assert config.acp_credential_files == (
        (str(tmp_path / "acp-key-source"), "a/key"),
    )


# -- multitenant + tenant：整通道按凭据拒绝 --------------------------------------


def test_multitenant_tenant_refuses_acp_env(tmp_path: Path) -> None:
    values = _acp_values(tmp_path, tenancy="multitenant", policy="tenant")
    values["PI_GLINK_ACP_ENV"] = "SOME_PROXY_VAR="
    with pytest.raises(ConfigError) as excinfo:
        build_config(values, REPO_ROOT)
    message = str(excinfo.value)
    assert "tenant 政策下不允许" in message
    assert "PI_GLINK_MODEL_CREDENTIALS=shared" in message


def test_multitenant_tenant_refuses_credential_files(tmp_path: Path) -> None:
    values = _acp_values(tmp_path, tenancy="multitenant", policy="tenant")
    values["PI_GLINK_ACP_CREDENTIAL_FILES"] = f"{_acp_source_file(tmp_path)}=a/key"
    with pytest.raises(ConfigError) as excinfo:
        build_config(values, REPO_ROOT)
    assert "tenant 政策下不允许" in str(excinfo.value)


def test_multitenant_tenant_accepts_acp_host_without_channels(tmp_path: Path) -> None:
    """通道未配置的宿主可以启动；run 级拒绝由族准入给出可读错误。"""
    values = _acp_values(tmp_path, tenancy="multitenant", policy="tenant")
    config = build_config(values, REPO_ROOT)
    assert config.acp_env == () and config.acp_credential_files == ()


# -- multitenant + shared：通道是具名共享凭据源 ----------------------------------


def test_multitenant_shared_requires_a_channel(tmp_path: Path) -> None:
    values = _acp_values(tmp_path, tenancy="multitenant", policy="shared")
    with pytest.raises(ConfigError) as excinfo:
        build_config(values, REPO_ROOT)
    message = str(excinfo.value)
    assert "PI_GLINK_MODEL_CREDENTIALS=shared" in message
    assert "缺源不启动" in message


def test_multitenant_shared_accepts_acp_env_without_openai_trio(tmp_path: Path) -> None:
    """shared 按族分流：ACP 宿主的凭据源是 ACP 通道，不再被迫配 OPENAI 三件套。"""
    values = _acp_values(
        tmp_path, tenancy="multitenant", policy="shared", openai_trio=False
    )
    values["PI_GLINK_ACP_ENV"] = "ANTHROPIC_API_KEY=sk-acp-shared-000000"
    config = build_config(values, REPO_ROOT)
    assert config.multi_tenant and config.shared_model_credentials
    assert config.acp_env == (("ANTHROPIC_API_KEY", "sk-acp-shared-000000"),)


def test_multitenant_shared_accepts_credential_files(tmp_path: Path) -> None:
    values = _acp_values(tmp_path, tenancy="multitenant", policy="shared")
    source = _acp_source_file(tmp_path)
    values["PI_GLINK_ACP_CREDENTIAL_FILES"] = f"{source}=credentials/api_key"
    config = build_config(values, REPO_ROOT)
    assert config.acp_credential_files == ((source, "credentials/api_key"),)


def test_shared_pi_family_still_requires_openai_trio(tmp_path: Path) -> None:
    """pi 宿主维持原 fail-closed：shared 缺三件套仍拒绝启动。"""
    values = _acp_values(
        tmp_path, tenancy="multitenant", policy="shared", openai_trio=False
    )
    values["PI_GLINK_HARNESS"] = "pi"
    values["PI_GLINK_HARNESSES_DIR"] = str(REPO_ROOT / "harnesses")
    values.pop("PI_GLINK_ACP_ENV", None)
    with pytest.raises(ConfigError) as excinfo:
        build_config(values, REPO_ROOT)
    assert "PI_GLINK_MODEL_CREDENTIALS=shared 需要宿主冻结" in str(excinfo.value)


# -- 族准入：同一政策分流 --------------------------------------------------------


def test_acp_family_validate_credentials_noop_for_single_user(tmp_path: Path) -> None:
    _config, family = _family(tmp_path, _acp_values(tmp_path))
    family.validate_credentials(RunCredentials(model_api_key=""))


def test_acp_family_validate_credentials_noop_for_shared(tmp_path: Path) -> None:
    values = _acp_values(tmp_path, tenancy="multitenant", policy="shared")
    values["PI_GLINK_ACP_ENV"] = "ANTHROPIC_API_KEY=sk-acp-shared-000000"
    _config, family = _family(tmp_path, values)
    family.validate_credentials(RunCredentials(model_api_key=""))


def test_acp_family_validate_credentials_refused_under_tenant(tmp_path: Path) -> None:
    values = _acp_values(tmp_path, tenancy="multitenant", policy="tenant")
    _config, family = _family(tmp_path, values)
    with pytest.raises(ConfigError) as excinfo:
        family.validate_credentials(RunCredentials(model_api_key=""))
    message = str(excinfo.value)
    assert "tenant 政策下 ACP 族不可用" in message
    assert "PI_GLINK_MODEL_CREDENTIALS=shared" in message


def test_multitenant_tenant_refuses_credential_env(tmp_path: Path) -> None:
    """PI_GLINK_ACP_CREDENTIAL_ENV 同样是运营方凭据通道，tenant 政策下整通道拒绝（ADR 0015/0016）。"""
    values = _acp_values(tmp_path, tenancy="multitenant", policy="tenant")
    values["PI_GLINK_ACP_CREDENTIAL_ENV"] = "OPENROUTER_API_KEY"
    values["OPENROUTER_API_KEY"] = "sk-or-shared-0001"
    with pytest.raises(ConfigError) as excinfo:
        build_config(values, REPO_ROOT)
    assert "tenant 政策下不允许" in str(excinfo.value)
    assert "PI_GLINK_ACP_CREDENTIAL_ENV" in str(excinfo.value)


def test_multitenant_shared_accepts_credential_env_as_named_source(
    tmp_path: Path,
) -> None:
    """shared 部署可把 env 渲染通道作为具名共享凭据源（镜像 FILES 分支）。"""
    values = _acp_values(
        tmp_path, tenancy="multitenant", policy="shared", openai_trio=False
    )
    values["PI_GLINK_ACP_CREDENTIAL_ENV"] = "OPENROUTER_API_KEY"
    values["OPENROUTER_API_KEY"] = "sk-or-shared-0002"
    config = build_config(values, REPO_ROOT)
    assert config.acp_credential_env == ("OPENROUTER_API_KEY",)
