"""The host-level harness switch (``GLINK_HARNESS``) and the pack selection.

The deployment selects one harness **pack** at configuration time: no API
parameter, no per-run choice, no front-end surface. The default is ``dsh``
since 2026-10-01 (GLINK-HARNESS-DEFAULT-20261001：dsh 是主力接入方案，pi 降级为
显式选择的扩展包；此前默认为 ``pi``）。A pack that cannot run is refused at
startup rather than falling back to another pack (which would mislabel runs).
The config holds the parsed pack object itself (``config.pack``); there are no
``acp_*`` fields copied out of the manifest and no legacy knobs are read any
more.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Mapping

import pytest

from glink_harness.config import ConfigError, build_config, validate_host_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def config(values: Mapping[str, str]):
    base = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "OPENAI_REASONING_EFFORT": "high",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
    }
    base.update(values)
    return build_config(base, REPO_ROOT)


def test_the_default_harness_is_the_dsh_pack() -> None:
    """默认包为 dsh（GLINK-HARNESS-DEFAULT-20261001）；pi 包显式选择后行为不变。"""
    default = config({})
    assert default.harness == "acp" and default.harness_pack == "dsh"
    assert default.pack.command == ("dsh", "--profile", "acp")
    assert default.pi_bin == "dsh"  # pi_bin 缺省跟随包 entry argv[0]

    pi = config({"GLINK_HARNESS": "pi"})
    assert pi.harness == "pi" and pi.harness_pack == "pi"
    assert pi.pack.command == ("pi",)
    assert pi.pi_bin == "pi"
    assert pi.agent_config_dir == REPO_ROOT / "harnesses" / "pi"


def test_an_acp_pack_derives_the_family_command_and_options() -> None:
    dsh = config({"GLINK_HARNESS": "dsh"})
    assert dsh.harness == "acp" and dsh.harness_pack == "dsh"
    assert dsh.pack.command == ("dsh", "--profile", "acp")
    assert dsh.session_config == (
        ("model", "test/model", "dsh-model"), ("reasoning_effort", "high", "value"),
    )
    assert dsh.pack.capabilities.permission_allow == ()

    codex = config({"GLINK_HARNESS": "codex"})
    assert codex.harness == "acp" and codex.pack.command == ("codex-acp",)
    # The pinned 1.13.1 mode vocabulary has no "auto" (integration notes §14.5):
    # the pack deliberately declares no session_config instead.
    assert codex.pack.capabilities.session_config == ()

    opencode = config({"GLINK_HARNESS": "opencode"})
    assert opencode.harness == "acp" and opencode.pack.command == ("opencode", "acp")


def test_the_pi_template_dir_defaults_to_the_selected_pack() -> None:
    assert config({}).agent_config_dir == REPO_ROOT / "harnesses" / "pi"
    dsh = build_config({
        "GLINK_HARNESS": "dsh", "OPENAI_MODEL": "test/model",
        "OPENAI_REASONING_EFFORT": "high",
    }, REPO_ROOT)
    assert dsh.agent_config_dir == REPO_ROOT / "harnesses" / "dsh"


def test_the_harness_is_not_part_of_the_browser_visible_configuration() -> None:
    view = config({"GLINK_HARNESS": "dsh"}).public_view()
    assert "harness" not in view and "harnessPack" not in view


@pytest.mark.parametrize("requested", ["acp", "gemini"])
def test_a_harness_that_names_no_pack_is_refused_at_startup(requested: str) -> None:
    """Only pack names are accepted; the retired family value ``acp`` is just
    another unknown pack (no dedicated legacy branch, no mapping)."""
    with pytest.raises(ConfigError) as excinfo:
        config({"GLINK_HARNESS": requested})
    message = str(excinfo.value)
    assert f"GLINK_HARNESS={requested!r} 不可用" in message
    assert "可用包" in message


def test_retired_acp_knobs_are_not_read() -> None:
    """The v1-era ``GLINK_ACP_COMMAND`` etc. are no longer part of the config
    surface: they neither override the pack nor get a dedicated refusal."""
    selected = config(
        {
            "GLINK_HARNESS": "dsh",
            "GLINK_ACP_COMMAND": "other-agent",
            "GLINK_ACP_SESSION_CONFIG": "mode=auto",
            "GLINK_ACP_PERMISSION_ALLOW": "allow_once",
        }
    )
    assert selected.pack.command == ("dsh", "--profile", "acp")
    assert selected.session_config == (
        ("model", "test/model", "dsh-model"), ("reasoning_effort", "high", "value"),
    )
    assert selected.pack.capabilities.permission_allow == ()


def test_the_acp_docker_combination_is_accepted_since_m3a() -> None:
    """原义务（M1）：ACP 只允许 inproc，docker/e2b 在启动即拒绝。

    新义务（M3a/M3b）：docker 与 e2b 面都已实现，配置不再拒绝其中任何一个
    （e2b 的阳性用例见 :func:`test_the_acp_e2b_combination_is_accepted_since_m3b`）。
    这是行为决定变更，实现/测试/文档同步修改。
    """
    acp = config({"GLINK_HARNESS": "dsh", "LOCAL_WEB_EXECUTOR": "docker"})
    assert acp.harness == "acp" and acp.executor == "docker"
    assert acp.acp_worker_image == "glink-harness-acp-worker:latest"
    # A dedicated image knob: the ACP image is not the pi worker image.
    assert acp.worker_image != acp.acp_worker_image
    assert "acpWorkerImage" not in acp.public_view()


def test_the_acp_e2b_combination_is_accepted_since_m3b() -> None:
    """原义务（M1–M3a）：acp + e2b 在启动即拒绝（E2B 帧/模板未实测）。

    新义务（M3b）：帧完整性先实测通过（docs/evidence/acp-m3-e2b-framing.txt），
    独立 dev 模板 glink-acp-worker 构建并自检，配置接受该组合；ACP 的 E2B 模板
    与 pi 模板是两个冻结旋钮，浏览器可见配置仍不暴露 harness/模板。
    """
    acp = config({"GLINK_HARNESS": "dsh", "LOCAL_WEB_EXECUTOR": "e2b"})
    assert acp.harness == "acp" and acp.executor == "e2b"
    assert acp.acp_e2b_template == "glink-acp-worker"
    # 与 pi 面模板分开：pi 模板里没有 ACP agent。
    assert acp.e2b_template == "glink-harness-worker"
    assert acp.e2b_template != acp.acp_e2b_template
    view = acp.public_view()
    assert "acpE2bTemplate" not in view and "harness" not in view

    override = config(
        {
            "GLINK_HARNESS": "dsh",
            "LOCAL_WEB_EXECUTOR": "e2b",
            "LOCAL_WEB_E2B_ACP_TEMPLATE": "glink-acp-worker:candidate",
        }
    )
    assert override.acp_e2b_template == "glink-acp-worker:candidate"


def test_the_harness_invariants_are_rechecked_after_construction() -> None:
    """A directly built/overridden config still passes the same gate."""
    base = config({})
    dsh = config({"GLINK_HARNESS": "dsh"})
    with pytest.raises(ConfigError, match="entry.command"):
        validate_host_config(
            dataclasses.replace(dsh, pack=dataclasses.replace(dsh.pack, command=()))
        )
    with pytest.raises(ConfigError, match="harness 族"):
        validate_host_config(
            dataclasses.replace(base, pack=dataclasses.replace(base.pack, family="nope"))
        )
    # ACP + docker/e2b 都是合法组合；直接构造的配置同样被接受。
    for executor in ("docker", "e2b"):
        validate_host_config(dataclasses.replace(dsh, executor=executor))
    # The valid pi default is accepted unchanged.
    validate_host_config(base)


# -- deployment secrets: credential files and env -----------------------------


def test_the_credential_and_env_knobs_are_parsed_into_typed_entries() -> None:
    acp = config(
        {
            "GLINK_HARNESS": "dsh",
            "GLINK_ACP_CREDENTIAL_FILES": "~/a.json=.codex/auth.json; /b.yaml=.dsh/.credentials.yaml",
            "GLINK_ACP_ENV": "ACME_MODE=workspace;ACME_EMPTY=",
        }
    )
    assert acp.acp_credential_files == (
        ("~/a.json", ".codex/auth.json"),
        ("/b.yaml", ".dsh/.credentials.yaml"),
    )
    assert acp.acp_env == (("ACME_MODE", "workspace"), ("ACME_EMPTY", ""))
    # The browser-visible view never carries any of it.
    view = json.dumps(acp.public_view())
    assert "ACME_MODE" not in view and "auth.json" not in view


@pytest.mark.parametrize(
    "key, value, fragment",
    [
        ("GLINK_ACP_CREDENTIAL_FILES", "/a.json=../escape.json", "相对路径"),
        ("GLINK_ACP_CREDENTIAL_FILES", "/a.json=/abs.json", "相对路径"),
        ("GLINK_ACP_CREDENTIAL_FILES", "/a.json=.codex/auth.json;/b.json=.codex/auth.json", "重复"),
        ("GLINK_ACP_CREDENTIAL_FILES", "just-a-path", "source=dest"),
        ("GLINK_ACP_ENV", "HOME=/tmp/other", "HOME"),
        ("GLINK_ACP_ENV", "9BAD=x", "NAME=value"),
    ],
)
def test_a_malformed_credential_or_env_knob_is_refused_at_startup(
    key: str, value: str, fragment: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        config({"GLINK_HARNESS": "dsh", key: value})
    assert fragment in str(excinfo.value)


def test_the_credential_and_env_knobs_are_refused_when_the_pack_drives_pi() -> None:
    """A security-relevant ACP knob must not be a silent no-op under pi."""
    for key, value in (
        ("GLINK_ACP_CREDENTIAL_FILES", "/a.json=.codex/auth.json"),
        ("GLINK_ACP_ENV", "ACME_MODE=workspace"),
    ):
        with pytest.raises(ConfigError) as excinfo:
            config({"GLINK_HARNESS": "pi", key: value})
        assert "只在 ACP 族" in str(excinfo.value)
