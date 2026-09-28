"""``select_family`` 的族映射与防御性拒绝。

族实例在 ``RunManager.__init__`` 由 ``select_family`` 唯一选出；本文件钉住
映射表（pi/acp 各自的实现类型与 ``name``）、未知 harness 的 fail-closed，以及
``RunManager.__init__`` 的唯一选择点（runner 重接线已落地）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from acp_pack import write_acp_pack
from pi_glink.config import ConfigError, HostConfig, build_config
from pi_glink.kernel.harness import HarnessFamily, select_family
from pi_glink.kernel.harness_acp import AcpHarnessFamily
from pi_glink.kernel.harness_pi import PiHarnessFamily
from pi_glink.store import RunStore

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_ACP = Path(__file__).resolve().parent / "fixtures" / "fake_acp.py"


def make_config(tmp_path: Path, harness: str) -> HostConfig:
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "PI_GLINK_HARNESS": harness,
    }
    if harness == "acp":
        packs = write_acp_pack(
            tmp_path, f"{sys.executable} {FAKE_ACP} --config settings.json"
        )
        values["PI_GLINK_HARNESS"] = "dsh"
        values["PI_GLINK_HARNESSES_DIR"] = str(packs)
    return build_config(values, REPO_ROOT)


def test_select_family_returns_the_pi_family(tmp_path: Path) -> None:
    config = make_config(tmp_path, "pi")
    family = select_family(config.harness, config=config, store=RunStore(tmp_path / "runs"))
    assert type(family) is PiHarnessFamily
    assert isinstance(family, HarnessFamily)
    assert family.name == "pi"


def test_select_family_returns_the_acp_family(tmp_path: Path) -> None:
    config = make_config(tmp_path, "acp")
    family = select_family(config.harness, config=config, store=RunStore(tmp_path / "runs"))
    assert type(family) is AcpHarnessFamily
    assert isinstance(family, HarnessFamily)
    assert family.name == "acp"


def test_select_family_refuses_an_unknown_harness(tmp_path: Path) -> None:
    """config 层已校验取值；这里是 fail-closed 的防御性兜底。"""
    config = make_config(tmp_path, "pi")
    with pytest.raises(ConfigError, match="未知 harness"):
        select_family("bogus", config=config, store=RunStore(tmp_path / "runs"))


def test_run_manager_resolves_the_configured_family_at_init(tmp_path: Path) -> None:
    """``RunManager.__init__`` 按冻结配置解析一次族（启动即 fail-closed）。

    原义务：进程级唯一族实例 ``manager._family``。新义务（S2）：族按 run 自己
    的 ``RunSpec.family`` 取（``_family_for``，按名缓存），准入 spec 来自冻结
    配置；初始化时已解析配置族并缓存同一实例。
    """
    from pi_glink.events import EventBus  # noqa: PLC0415
    from pi_glink.runs.runner import RunManager  # noqa: PLC0415

    for harness, family_type in (("pi", PiHarnessFamily), ("acp", AcpHarnessFamily)):
        config = make_config(tmp_path / harness, harness)
        manager = RunManager(config, EventBus(), RunStore(config.data_root / "runs"))
        family = manager._family_for(config.harness)  # noqa: SLF001
        assert type(family) is family_type
        assert family.name == config.harness
        assert manager._families == {config.harness: family}  # noqa: SLF001
        assert manager._family_for(config.harness) is family  # noqa: SLF001
        assert manager._admission_spec(None).family == config.harness  # noqa: SLF001
