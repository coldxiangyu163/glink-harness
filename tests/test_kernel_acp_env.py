"""Per-run ACP configuration regressions: HOME, env and credential injection.

The ACP family injects per-agent credentials by copying read-only source files
into each run's private HOME (mode 0600, parents 0700) and by adding explicit
environment entries; the values learned from the copied material join that run's
redaction boundary. These tests use synthetic files in ``tmp_path`` and never
touch a real agent state directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

import pytest

from pi_glink.config import ConfigError, build_config
from pi_glink.kernel.acp_env import (
    build_acp_env,
    credential_secrets_from_text,
    prepare_acp_runtime,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def config(values: Mapping[str, str]):
    base = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "agent"),
        "PI_GLINK_HARNESS": "acp",
        "PI_GLINK_ACP_COMMAND": "fake-agent --acp",
    }
    base.update(values)
    return build_config(base, REPO_ROOT)


def runtime(tmp_path: Path, values: Mapping[str, str], *, operator_home: Path | None = None):
    cfg = config(values)
    return prepare_acp_runtime(
        cfg, runtime_dir=tmp_path / "acp-agent", operator_home=operator_home
    )


def test_credential_files_are_copied_private_and_the_source_is_untouched(
    tmp_path: Path,
) -> None:
    source = tmp_path / "operator" / "auth.json"
    source.parent.mkdir()
    payload = {"tokens": {"access_token": "opaque-access-token-value-1"}}
    source.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(source, 0o600)
    before = source.stat()

    result = runtime(
        tmp_path,
        {"PI_GLINK_ACP_CREDENTIAL_FILES": f"{source}=.codex/auth.json"},
    )

    copied = result.home / ".codex" / "auth.json"
    assert copied.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert (copied.stat().st_mode & 0o777) == 0o600
    assert (copied.parent.stat().st_mode & 0o777) == 0o700
    assert (result.home.stat().st_mode & 0o777) == 0o700
    # Read-only source: content, mode and mtime are exactly as before.
    after = source.stat()
    assert (before.st_mode, before.st_ino) == (after.st_mode, after.st_ino)
    assert source.read_text(encoding="utf-8") == json.dumps(payload)
    assert result.secrets == ("opaque-access-token-value-1",)


def test_credential_source_expands_a_leading_tilde_against_the_operator_home(
    tmp_path: Path,
) -> None:
    operator_home = tmp_path / "operator-home"
    (operator_home / ".dsh").mkdir(parents=True)
    source = operator_home / ".dsh" / ".credentials.yaml"
    source.write_text("DEEPSEEK_API_KEY: sk-opaque-credential-1234567890\n", encoding="utf-8")

    result = runtime(
        tmp_path,
        {"PI_GLINK_ACP_CREDENTIAL_FILES": "~/.dsh/.credentials.yaml=.dsh/.credentials.yaml"},
        operator_home=operator_home,
    )
    assert (result.home / ".dsh" / ".credentials.yaml").is_file()
    assert "sk-opaque-credential-1234567890" in result.secrets


def test_an_unreadable_credential_source_fails_instead_of_running_without_it(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError):
        runtime(
            tmp_path,
            {"PI_GLINK_ACP_CREDENTIAL_FILES": f"{tmp_path / 'missing.json'}=.codex/auth.json"},
        )


def test_an_existing_destination_is_never_overwritten(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    source.write_text('{"apiKey": "opaque-value-1234567890"}', encoding="utf-8")
    run_dir = tmp_path / "acp-agent"
    target = run_dir / "home" / ".codex" / "auth.json"
    target.parent.mkdir(parents=True)
    target.write_text("preexisting", encoding="utf-8")

    with pytest.raises(ConfigError):
        prepare_acp_runtime(
            config({"PI_GLINK_ACP_CREDENTIAL_FILES": f"{source}=.codex/auth.json"}),
            runtime_dir=run_dir,
        )
    assert target.read_text(encoding="utf-8") == "preexisting"


def test_a_symlinked_destination_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    source.write_text('{"apiKey": "opaque-value-1234567890"}', encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text("untouched", encoding="utf-8")
    run_dir = tmp_path / "acp-agent"
    link = run_dir / "home" / ".codex" / "auth.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)

    with pytest.raises(ConfigError):
        prepare_acp_runtime(
            config({"PI_GLINK_ACP_CREDENTIAL_FILES": f"{source}=.codex/auth.json"}),
            runtime_dir=run_dir,
        )
    assert outside.read_text(encoding="utf-8") == "untouched"


def test_env_entries_are_applied_and_home_stays_the_run_root(tmp_path: Path) -> None:
    cfg = config({"PI_GLINK_ACP_ENV": "ACME_MODE=workspace;ACME_EMPTY="})
    result = prepare_acp_runtime(cfg, runtime_dir=tmp_path / "acp-agent")
    env = build_acp_env(cfg, result, base_env={"PATH": "/usr/bin", "HOME": "/operator"})
    assert env["HOME"] == str(result.home)
    assert env["HOME"] != "/operator"
    assert env["ACME_MODE"] == "workspace"
    assert env["ACME_EMPTY"] == ""
    # Only the passthrough names and the configured entries cross the boundary.
    assert "OPENAI_API_KEY" not in env


def test_env_credential_values_join_the_redaction_boundary(tmp_path: Path) -> None:
    """A credential-shaped ``PI_GLINK_ACP_ENV`` value is a secret of the run.

    The env channel has no file to scan, so the variable name is the hint: a
    credential-looking name adds its value to the run's redaction boundary, an
    ordinary knob does not (adding it would mask unrelated output). Values the
    redactor would skip anyway (too short, documentation placeholders) stay out.
    """
    cfg = config(
        {
            "PI_GLINK_ACP_ENV": (
                "ANTHROPIC_API_KEY=opaque-env-credential-value-1;"
                "ACME_MODE=workspace;"
                "EMPTY_TOKEN=;"
                "PLACEHOLDER_KEY=your-key-here-please-replace"
            )
        }
    )
    result = prepare_acp_runtime(cfg, runtime_dir=tmp_path / "acp-agent")
    assert "opaque-env-credential-value-1" in result.secrets
    assert "workspace" not in result.secrets
    assert "your-key-here-please-replace" not in result.secrets


def test_credential_secrets_from_text_finds_json_yaml_shapes_and_skips_placeholders() -> None:
    document = json.dumps(
        {
            "tokens": {"access_token": "opaque-access-token-value-1"},
            "OPENAI_API_KEY": None,
            "modelTag": "not-a-secret-value-here",
        }
    )
    found = credential_secrets_from_text(document)
    assert "opaque-access-token-value-1" in found
    assert "not-a-secret-value-here" not in found
    assert "null" not in found

    yaml_like = (
        "version: 1\n"
        "refs:\n"
        "  DEEPSEEK_API_KEY: sk-yaml-credential-1234567890\n"
        "  EFFORT: high\n"
    )
    found = credential_secrets_from_text(yaml_like)
    assert "sk-yaml-credential-1234567890" in found
    assert "high" not in found

    shaped = "some log line with exa-abcdefghijklmnopqrstuvwxyz inside"
    assert "exa-abcdefghijklmnopqrstuvwxyz" in credential_secrets_from_text(shaped)

    placeholder = credential_secrets_from_text('{"apiKey": "your-key-here-please-replace"}')
    assert placeholder == ()
