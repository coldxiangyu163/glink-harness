"""The host-level harness switch (``PI_GLINK_HARNESS``).

M1 selects the harness family at configuration time only: no API parameter, no
per-run choice, no front-end surface, and the default stays ``pi`` so every
existing deployment is unchanged. An ACP configuration that cannot run is
refused at startup rather than falling back to pi (which would mislabel runs).
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
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
    }
    base.update(values)
    return build_config(base, REPO_ROOT)


def test_the_default_harness_is_pi_and_acp_is_an_explicit_opt_in() -> None:
    assert config({}).harness == "pi"
    assert config({}).acp_command == ""
    acp = config({"PI_GLINK_HARNESS": "acp", "PI_GLINK_ACP_COMMAND": "dsh acp"})
    assert acp.harness == "acp"
    assert acp.acp_command == "dsh acp"


def test_the_harness_is_not_part_of_the_browser_visible_configuration() -> None:
    view = config({"PI_GLINK_HARNESS": "acp", "PI_GLINK_ACP_COMMAND": "dsh acp"}).public_view()
    assert "harness" not in view


@pytest.mark.parametrize(
    "values, fragment",
    [
        ({"PI_GLINK_HARNESS": "claude"}, "PI_GLINK_HARNESS must be one of"),
        ({"PI_GLINK_HARNESS": "acp"}, "PI_GLINK_ACP_COMMAND"),
        (
            {
                "PI_GLINK_HARNESS": "acp",
                "PI_GLINK_ACP_COMMAND": "dsh acp",
                "LOCAL_WEB_EXECUTOR": "e2b",
            },
            "M3b",
        ),
    ],
)
def test_an_unusable_harness_configuration_is_refused_at_startup(
    values: Mapping[str, str], fragment: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        config(values)
    assert fragment in str(excinfo.value)


def test_the_acp_docker_combination_is_accepted_since_m3a() -> None:
    """原义务（M1）：ACP 只允许 inproc，docker/e2b 在启动即拒绝。

    新义务（M3a）：docker 面已实现，配置不再拒绝；e2b 仍由 M3b 拒绝（见上一
    个参数化用例）。这是行为决定变更，实现/测试/文档同步修改。
    """
    acp = config(
        {
            "PI_GLINK_HARNESS": "acp",
            "PI_GLINK_ACP_COMMAND": "dsh --profile acp",
            "LOCAL_WEB_EXECUTOR": "docker",
        }
    )
    assert acp.harness == "acp" and acp.executor == "docker"
    assert acp.acp_worker_image == "pi-glink-acp-worker:latest"
    # A dedicated image knob: the ACP image is not the pi worker image.
    assert acp.worker_image != acp.acp_worker_image
    assert "acpWorkerImage" not in acp.public_view()


def test_the_harness_invariants_are_rechecked_after_construction() -> None:
    """A directly built/overridden config still passes the same gate."""
    base = config({})
    with pytest.raises(ConfigError):
        validate_host_config(dataclasses.replace(base, harness="acp", acp_command=""))
    with pytest.raises(ConfigError):
        validate_host_config(
            dataclasses.replace(base, harness="acp", acp_command="dsh acp", executor="e2b")
        )
    with pytest.raises(ConfigError):
        validate_host_config(dataclasses.replace(base, harness="nope"))
    # M3a：ACP + docker 现在是合法组合，直接构造的配置同样被接受。
    validate_host_config(
        dataclasses.replace(base, harness="acp", acp_command="dsh --profile acp", executor="docker")
    )
    # The valid pi default is accepted unchanged.
    validate_host_config(base)


# -- M2 ACP knobs -------------------------------------------------------------


def test_m2_knobs_are_parsed_into_typed_entries() -> None:
    acp = config(
        {
            "PI_GLINK_HARNESS": "acp",
            "PI_GLINK_ACP_COMMAND": "fake --acp",
            "PI_GLINK_ACP_CREDENTIAL_FILES": "~/a.json=.codex/auth.json; /b.yaml=.dsh/.credentials.yaml",
            "PI_GLINK_ACP_ENV": "ACME_MODE=workspace;ACME_EMPTY=",
            "PI_GLINK_ACP_PERMISSION_ALLOW": "allow_once, execute, allow_once",
            "PI_GLINK_ACP_SESSION_CONFIG": "mode=auto;reasoning_effort=high",
        }
    )
    assert acp.acp_credential_files == (
        ("~/a.json", ".codex/auth.json"),
        ("/b.yaml", ".dsh/.credentials.yaml"),
    )
    assert acp.acp_env == (("ACME_MODE", "workspace"), ("ACME_EMPTY", ""))
    assert acp.acp_permission_allow == ("allow_once", "execute")
    assert acp.acp_session_config == (("mode", "auto"), ("reasoning_effort", "high"))
    # The browser-visible view never carries any of it.
    view = json.dumps(acp.public_view())
    assert "allow_once" not in view and "mode=auto" not in view


@pytest.mark.parametrize(
    "key, value, fragment",
    [
        ("PI_GLINK_ACP_CREDENTIAL_FILES", "/a.json=../escape.json", "相对路径"),
        ("PI_GLINK_ACP_CREDENTIAL_FILES", "/a.json=/abs.json", "相对路径"),
        ("PI_GLINK_ACP_CREDENTIAL_FILES", "/a.json=.codex/auth.json;/b.json=.codex/auth.json", "重复"),
        ("PI_GLINK_ACP_CREDENTIAL_FILES", "just-a-path", "source=dest"),
        ("PI_GLINK_ACP_ENV", "HOME=/tmp/other", "HOME"),
        ("PI_GLINK_ACP_ENV", "9BAD=x", "NAME=value"),
        ("PI_GLINK_ACP_PERMISSION_ALLOW", "reject_once", "allow 选项类型"),
        ("PI_GLINK_ACP_PERMISSION_ALLOW", "everything", "allow 选项类型"),
        ("PI_GLINK_ACP_SESSION_CONFIG", "mode", "id=value"),
    ],
)
def test_a_malformed_m2_knob_is_refused_at_startup(
    key: str, value: str, fragment: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        config({"PI_GLINK_HARNESS": "acp", "PI_GLINK_ACP_COMMAND": "fake --acp", key: value})
    assert fragment in str(excinfo.value)


def test_m2_knobs_are_refused_when_the_host_drives_pi() -> None:
    """A security-relevant ACP knob must not be a silent no-op under pi."""
    for key, value in (
        ("PI_GLINK_ACP_CREDENTIAL_FILES", "/a.json=.codex/auth.json"),
        ("PI_GLINK_ACP_ENV", "ACME_MODE=workspace"),
        ("PI_GLINK_ACP_PERMISSION_ALLOW", "allow_once"),
        ("PI_GLINK_ACP_SESSION_CONFIG", "mode=auto"),
    ):
        with pytest.raises(ConfigError) as excinfo:
            config({key: value})
        assert "只在 PI_GLINK_HARNESS=acp 下生效" in str(excinfo.value)
