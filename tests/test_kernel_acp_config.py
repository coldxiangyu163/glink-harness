"""The host-level harness switch (``PI_GLINK_HARNESS``).

M1 selects the harness family at configuration time only: no API parameter, no
per-run choice, no front-end surface, and the default stays ``pi`` so every
existing deployment is unchanged. An ACP configuration that cannot run is
refused at startup rather than falling back to pi (which would mislabel runs).
"""

from __future__ import annotations

import dataclasses
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
                "LOCAL_WEB_EXECUTOR": "docker",
            },
            "只支持 LOCAL_WEB_EXECUTOR=inproc",
        ),
        (
            {
                "PI_GLINK_HARNESS": "acp",
                "PI_GLINK_ACP_COMMAND": "dsh acp",
                "LOCAL_WEB_EXECUTOR": "e2b",
            },
            "只支持 LOCAL_WEB_EXECUTOR=inproc",
        ),
    ],
)
def test_an_unusable_harness_configuration_is_refused_at_startup(
    values: Mapping[str, str], fragment: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        config(values)
    assert fragment in str(excinfo.value)


def test_the_harness_invariants_are_rechecked_after_construction() -> None:
    """A directly built/overridden config still passes the same gate."""
    base = config({})
    with pytest.raises(ConfigError):
        validate_host_config(dataclasses.replace(base, harness="acp", acp_command=""))
    with pytest.raises(ConfigError):
        validate_host_config(
            dataclasses.replace(base, harness="acp", acp_command="dsh acp", executor="docker")
        )
    with pytest.raises(ConfigError):
        validate_host_config(dataclasses.replace(base, harness="nope"))
    # The valid pi default is accepted unchanged.
    validate_host_config(base)
