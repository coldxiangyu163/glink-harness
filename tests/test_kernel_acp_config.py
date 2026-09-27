"""The host-level harness switch (``PI_GLINK_HARNESS``) and the pack selection.

The deployment selects one harness **pack** at configuration time: no API
parameter, no per-run choice, no front-end surface; the default stays ``pi`` so
every existing deployment is unchanged. A pack that cannot run is refused at
startup rather than falling back to pi (which would mislabel runs), and the
retired ``acp`` value plus the retired ACP knobs are refused outright instead of
being silently mapped.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Mapping

import pytest

from pi_glink.config import ConfigError, build_config, validate_host_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def config(values: Mapping[str, str]):
    base = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
    }
    base.update(values)
    return build_config(base, REPO_ROOT)


def test_the_default_harness_is_the_pi_pack() -> None:
    default = config({})
    assert default.harness == "pi" and default.harness_pack == "pi"
    assert default.acp_command == ()
    assert default.agent_config_dir == REPO_ROOT / "harnesses" / "pi"


def test_an_acp_pack_derives_the_family_command_and_options() -> None:
    dsh = config({"PI_GLINK_HARNESS": "dsh"})
    assert dsh.harness == "acp" and dsh.harness_pack == "dsh"
    assert dsh.acp_command == ("dsh", "--profile", "acp")
    assert dsh.acp_session_config == () and dsh.acp_permission_allow == ()

    codex = config({"PI_GLINK_HARNESS": "codex"})
    assert codex.harness == "acp" and codex.acp_command == ("codex-acp",)
    # The pinned 1.13.1 mode vocabulary has no "auto" (integration notes §14.5):
    # the pack deliberately declares no session_config instead.
    assert codex.acp_session_config == ()

    opencode = config({"PI_GLINK_HARNESS": "opencode"})
    assert opencode.harness == "acp" and opencode.acp_command == ("opencode", "acp")


def test_the_pi_template_dir_defaults_to_the_selected_pack() -> None:
    assert config({}).agent_config_dir == REPO_ROOT / "harnesses" / "pi"
    dsh = build_config({"PI_GLINK_HARNESS": "dsh"}, REPO_ROOT)
    assert dsh.agent_config_dir == REPO_ROOT / "harnesses" / "dsh"


def test_the_harness_is_not_part_of_the_browser_visible_configuration() -> None:
    view = config({"PI_GLINK_HARNESS": "dsh"}).public_view()
    assert "harness" not in view and "harnessPack" not in view


@pytest.mark.parametrize(
    "values, fragment",
    [
        (
            {"PI_GLINK_HARNESS": "acp"},
            "PI_GLINK_HARNESS=acp 已由 harness 包清单取代",
        ),
        (
            {"PI_GLINK_HARNESS": "gemini"},
            "PI_GLINK_HARNESS='gemini' 不可用",
        ),
        (
            {"PI_GLINK_HARNESS": "dsh", "PI_GLINK_ACP_COMMAND": "dsh --profile acp"},
            "PI_GLINK_ACP_COMMAND 已由 harnesses/<包名>/harness.toml 的 command 取代",
        ),
        (
            {"PI_GLINK_ACP_SESSION_CONFIG": "mode=auto"},
            "PI_GLINK_ACP_SESSION_CONFIG 已由 harnesses/<包名>/harness.toml 的 "
            "session_config 取代",
        ),
        (
            {"PI_GLINK_ACP_PERMISSION_ALLOW": "allow_once"},
            "PI_GLINK_ACP_PERMISSION_ALLOW 已由 harnesses/<包名>/harness.toml 的 "
            "permission_allow 取代",
        ),
    ],
)
def test_retired_harness_configuration_is_refused_at_startup(
    values: Mapping[str, str], fragment: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        config(values)
    assert fragment in str(excinfo.value)


def test_the_acp_docker_combination_is_accepted_since_m3a() -> None:
    """原义务（M1）：ACP 只允许 inproc，docker/e2b 在启动即拒绝。

    新义务（M3a/M3b）：docker 与 e2b 面都已实现，配置不再拒绝其中任何一个
    （e2b 的阳性用例见 :func:`test_the_acp_e2b_combination_is_accepted_since_m3b`）。
    这是行为决定变更，实现/测试/文档同步修改。
    """
    acp = config({"PI_GLINK_HARNESS": "dsh", "LOCAL_WEB_EXECUTOR": "docker"})
    assert acp.harness == "acp" and acp.executor == "docker"
    assert acp.acp_worker_image == "pi-glink-acp-worker:latest"
    # A dedicated image knob: the ACP image is not the pi worker image.
    assert acp.worker_image != acp.acp_worker_image
    assert "acpWorkerImage" not in acp.public_view()


def test_the_acp_e2b_combination_is_accepted_since_m3b() -> None:
    """原义务（M1–M3a）：acp + e2b 在启动即拒绝（E2B 帧/模板未实测）。

    新义务（M3b）：帧完整性先实测通过（docs/evidence/acp-m3-e2b-framing.txt），
    独立 dev 模板 glink-acp-worker 构建并自检，配置接受该组合；ACP 的 E2B 模板
    与 pi 模板是两个冻结旋钮，浏览器可见配置仍不暴露 harness/模板。
    """
    acp = config({"PI_GLINK_HARNESS": "dsh", "LOCAL_WEB_EXECUTOR": "e2b"})
    assert acp.harness == "acp" and acp.executor == "e2b"
    assert acp.acp_e2b_template == "glink-acp-worker"
    # 与 pi 面模板分开：pi 模板里没有 ACP agent。
    assert acp.e2b_template == "pi-glink-worker"
    assert acp.e2b_template != acp.acp_e2b_template
    view = acp.public_view()
    assert "acpE2bTemplate" not in view and "harness" not in view

    override = config(
        {
            "PI_GLINK_HARNESS": "dsh",
            "LOCAL_WEB_EXECUTOR": "e2b",
            "LOCAL_WEB_E2B_ACP_TEMPLATE": "glink-acp-worker:candidate",
        }
    )
    assert override.acp_e2b_template == "glink-acp-worker:candidate"


def test_the_harness_invariants_are_rechecked_after_construction() -> None:
    """A directly built/overridden config still passes the same gate."""
    base = config({})
    with pytest.raises(ConfigError):
        validate_host_config(dataclasses.replace(base, harness="acp", acp_command=()))
    with pytest.raises(ConfigError):
        validate_host_config(dataclasses.replace(base, harness="nope"))
    # ACP + docker/e2b 都是合法组合；直接构造的配置同样被接受。
    for executor in ("docker", "e2b"):
        validate_host_config(
            dataclasses.replace(
                base,
                harness="acp",
                acp_command=("dsh", "--profile", "acp"),
                executor=executor,
            )
        )
    # The valid pi default is accepted unchanged.
    validate_host_config(base)


# -- deployment secrets: credential files and env -----------------------------


def test_the_credential_and_env_knobs_are_parsed_into_typed_entries() -> None:
    acp = config(
        {
            "PI_GLINK_HARNESS": "dsh",
            "PI_GLINK_ACP_CREDENTIAL_FILES": "~/a.json=.codex/auth.json; /b.yaml=.dsh/.credentials.yaml",
            "PI_GLINK_ACP_ENV": "ACME_MODE=workspace;ACME_EMPTY=",
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
        ("PI_GLINK_ACP_CREDENTIAL_FILES", "/a.json=../escape.json", "相对路径"),
        ("PI_GLINK_ACP_CREDENTIAL_FILES", "/a.json=/abs.json", "相对路径"),
        ("PI_GLINK_ACP_CREDENTIAL_FILES", "/a.json=.codex/auth.json;/b.json=.codex/auth.json", "重复"),
        ("PI_GLINK_ACP_CREDENTIAL_FILES", "just-a-path", "source=dest"),
        ("PI_GLINK_ACP_ENV", "HOME=/tmp/other", "HOME"),
        ("PI_GLINK_ACP_ENV", "9BAD=x", "NAME=value"),
    ],
)
def test_a_malformed_credential_or_env_knob_is_refused_at_startup(
    key: str, value: str, fragment: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        config({"PI_GLINK_HARNESS": "dsh", key: value})
    assert fragment in str(excinfo.value)


def test_the_credential_and_env_knobs_are_refused_when_the_pack_drives_pi() -> None:
    """A security-relevant ACP knob must not be a silent no-op under pi."""
    for key, value in (
        ("PI_GLINK_ACP_CREDENTIAL_FILES", "/a.json=.codex/auth.json"),
        ("PI_GLINK_ACP_ENV", "ACME_MODE=workspace"),
    ):
        with pytest.raises(ConfigError) as excinfo:
            config({key: value})
        assert "只在 ACP 族" in str(excinfo.value)
