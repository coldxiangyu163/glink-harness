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
import stat
import tempfile
from pathlib import Path
from typing import Mapping

import pytest

from acp_pack import apply_acp_pack
from glink_harness.config import ConfigError, build_config
from harness_helpers import compose_launch, execution_spec
from glink_harness.kernel.acp_env import (
    AcpStateInheritError,
    learn_conversation_state_secrets,
    prepare_acp_runtime,
)
from glink_harness.redaction import credential_secrets_from_text

REPO_ROOT = Path(__file__).resolve().parents[1]


def config(values: Mapping[str, str]):
    base = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "PI_CODING_AGENT_DIR": str(REPO_ROOT / "harnesses" / "pi"),
        "GLINK_HARNESS": "acp",
        "TEST_ACP_COMMAND": "fake-agent --acp",
    }
    base.update(values)
    apply_acp_pack(base, Path(tempfile.mkdtemp(prefix="glink-acp-pack-")))
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
        {"GLINK_ACP_CREDENTIAL_FILES": f"{source}=.codex/auth.json"},
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
        {"GLINK_ACP_CREDENTIAL_FILES": "~/.dsh/.credentials.yaml=.dsh/.credentials.yaml"},
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
            {"GLINK_ACP_CREDENTIAL_FILES": f"{tmp_path / 'missing.json'}=.codex/auth.json"},
        )


def test_a_rendered_destination_is_replaced_by_the_credential(tmp_path: Path) -> None:
    """原义务（M1）：凭据目标已存在就拒绝写入（防重定向）。

    新义务（GLINK-ARCH-6）：包 `home/` 覆盖层先渲染，凭据目标必须优先——同一路径的
    覆盖层普通文件被凭据内容替换（每个 run 的 HOME 都是新渲染的，不存在宿主/操作员
    的旧文件）。仍是 fail-closed 的另一半：链接与非普通文件在下面两个用例里继续被拒绝。
    """
    source = tmp_path / "source.json"
    source.write_text('{"apiKey": "opaque-value-1234567890"}', encoding="utf-8")
    run_dir = tmp_path / "acp-agent"
    target = run_dir / "home" / ".codex" / "auth.json"
    target.parent.mkdir(parents=True)
    target.write_text("preexisting", encoding="utf-8")

    runtime = prepare_acp_runtime(
        config({"GLINK_ACP_CREDENTIAL_FILES": f"{source}=.codex/auth.json"}),
        runtime_dir=run_dir,
    )
    assert target.read_text(encoding="utf-8") == '{"apiKey": "opaque-value-1234567890"}'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert runtime.credential_dests == (".codex/auth.json",)


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
            config({"GLINK_ACP_CREDENTIAL_FILES": f"{source}=.codex/auth.json"}),
            runtime_dir=run_dir,
        )
    assert outside.read_text(encoding="utf-8") == "untouched"


def test_env_entries_are_applied_and_home_stays_the_run_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config({"GLINK_ACP_ENV": "ACME_MODE=workspace;ACME_EMPTY="})
    result = prepare_acp_runtime(cfg, runtime_dir=tmp_path / "acp-agent")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/operator")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-operator-shell-value")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = execution_spec(cfg, result, workspace=workspace)
    launch, sandbox = compose_launch("acp", "inproc", spec, store_root=tmp_path / "store")
    env = sandbox.spawn(launch).env
    assert env["HOME"] == str(result.home)
    assert env["HOME"] != "/operator"
    assert env["ACME_MODE"] == "workspace"
    assert env["ACME_EMPTY"] == ""
    # Only the passthrough names and the configured entries cross the boundary.
    assert "OPENAI_API_KEY" not in env


def test_env_credential_values_join_the_redaction_boundary(tmp_path: Path) -> None:
    """A credential-shaped ``GLINK_ACP_ENV`` value is a secret of the run.

    The env channel has no file to scan, so the variable name is the hint: a
    credential-looking name adds its value to the run's redaction boundary, an
    ordinary knob does not (adding it would mask unrelated output). Values the
    redactor would skip anyway (too short, documentation placeholders) stay out.
    """
    cfg = config(
        {
            "GLINK_ACP_ENV": (
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


def test_credential_secrets_from_text_reads_opencode_oauth_fields() -> None:
    """opencode's auth.json names OAuth fields ``access``/``refresh``.

    The generic rule only knows ``*token``-style names, so the M5 regression
    pins the exact-field rule that makes those values join the run's redactor.
    Ordinary neighbours (``type``, ``expires``, ``accountId``) stay out: an
    account id is not a credential, and masking it would corrupt output.
    """
    document = json.dumps(
        {
            "openai": {
                "type": "oauth",
                "access": "opencode-access-value-0001",
                "refresh": "opencode-refresh-value-0002",
                "expires": 4_102_444_800_000,
                "accountId": "acct-1234567890",
            },
            "codex": {
                "access": "opencode-codex-access-0003",
                "refresh": "opencode-codex-refresh-0004",
            },
        }
    )
    found = credential_secrets_from_text(document)
    assert "opencode-access-value-0001" in found
    assert "opencode-refresh-value-0002" in found
    assert "opencode-codex-access-0003" in found
    assert "opencode-codex-refresh-0004" in found
    assert "acct-1234567890" not in found
    assert "oauth" not in found
    assert "4102444800000" not in found


def test_a_field_that_merely_starts_with_access_is_not_a_credential() -> None:
    document = json.dumps({"provider": {"accessMode": "workspace-wide-mode"}})
    assert credential_secrets_from_text(document) == ()


def test_oauth_fields_in_scalar_lines_join_the_redaction_boundary() -> None:
    material = "access: opaque-oauth-access-0005\nrefresh: opaque-oauth-refresh-0006\n"
    found = credential_secrets_from_text(material)
    assert "opaque-oauth-access-0005" in found
    assert "opaque-oauth-refresh-0006" in found


def test_credential_env_renders_a_private_dsh_credentials_file(
    tmp_path: Path,
) -> None:
    """GLINK_ACP_CREDENTIAL_ENV：.env 单源渲染沙箱凭据（M 系列收尾）。

    凭据值只存 ``.env``（0600、git-ignored）；run 准备时宿主渲染一份 dsh 格式的
    ``.dsh/.credentials.yaml`` 进 run HOME：0600、refs 平面映射、值进该 run 的
    脱敏边界，渲染副本作为 credential destination 在收尾删除（决定 C）——与注入
    文件同一条生命周期。
    """
    result = runtime(
        tmp_path,
        {
            "GLINK_ACP_CREDENTIAL_ENV": "OPENROUTER_API_KEY,JINA_API_KEY",
            "OPENROUTER_API_KEY": "sk-or-secret-value-0001",
            "JINA_API_KEY": "jina's-secret-0002",
        },
    )
    rendered = result.home / ".dsh" / ".credentials.yaml"
    assert rendered.is_file(), "env 凭据必须渲染为沙箱 HOME 内的 dsh credentials 文件"
    assert stat.S_IMODE(rendered.stat().st_mode) == 0o600
    text = rendered.read_text(encoding="utf-8")
    assert text.startswith("version: 1\nrefs:\n")
    # 单引号标量：内部单引号以双写转义，dsh 的 YAML 解析还原原值。
    assert "  OPENROUTER_API_KEY: 'sk-or-secret-value-0001'" in text
    assert "  JINA_API_KEY: 'jina''s-secret-0002'" in text
    assert ".dsh/.credentials.yaml" in result.credential_dests
    for sweep in result.session_sweeps():
        assert ".dsh/.credentials.yaml" in sweep.credential_copies
    assert "sk-or-secret-value-0001" in result.secrets
    assert "jina's-secret-0002" in result.secrets


def test_credential_env_and_credential_files_are_mutually_exclusive() -> None:
    """两个来源都往 run HOME 投递凭据，一次部署必须二选一，启动即拒绝。"""
    source = Path(tempfile.mkdtemp(prefix="glink-mutex-")) / "creds.yaml"
    source.write_text("version: 1\nrefs: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="互斥"):
        config(
            {
                "GLINK_ACP_CREDENTIAL_ENV": "OPENROUTER_API_KEY",
                "OPENROUTER_API_KEY": "sk-or-value",
                "GLINK_ACP_CREDENTIAL_FILES": f"{source}=.dsh/creds.yaml",
            }
        )


def test_credential_env_requires_the_declared_values_to_exist() -> None:
    """声明了键但没有值 → 启动拒绝，绝不渲染空凭据。"""
    with pytest.raises(ConfigError, match="MISSING_PROVIDER_KEY"):
        config({"GLINK_ACP_CREDENTIAL_ENV": "MISSING_PROVIDER_KEY"})
    with pytest.raises(ConfigError, match="环境变量名"):
        config(
            {
                "GLINK_ACP_CREDENTIAL_ENV": "bad-name",
                "bad-name": "v",
            }
        )


def test_credential_env_values_never_reach_the_config_repr() -> None:
    cfg = config(
        {
            "GLINK_ACP_CREDENTIAL_ENV": "OPENROUTER_API_KEY",
            "OPENROUTER_API_KEY": "sk-or-repr-leak-0003",
        }
    )
    assert cfg.acp_credential_env == ("OPENROUTER_API_KEY",)
    assert "sk-or-repr-leak-0003" not in repr(cfg)


# -- 失败准备不留明文副本（ADR 0016 的删除义务覆盖 prepare 失败路径） ------------


def test_failed_inherit_deletes_the_rendered_env_credentials(tmp_path: Path) -> None:
    """继承失败发生在渲染之后：明文 .credentials.yaml 就地删除，不留给没有收尾管线的 run 目录。"""
    cfg = config(
        {
            "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
            "GLINK_ACP_CREDENTIAL_ENV": "OPENROUTER_API_KEY",
            "OPENROUTER_API_KEY": "sk-or-failed-prepare-0000000",
        }
    )
    runtime_dir = tmp_path / "acp-agent"
    with pytest.raises(AcpStateInheritError):
        prepare_acp_runtime(
            cfg, runtime_dir=runtime_dir, inherit_from=tmp_path / "missing-parent"
        )
    assert not (runtime_dir / "home" / ".dsh" / ".credentials.yaml").exists()


def test_failed_inherit_deletes_the_injected_file_copies(tmp_path: Path) -> None:
    """FILES 通道同义务：注入后准备失败，副本就地删除。"""
    source = tmp_path / "operator" / "auth.json"
    source.parent.mkdir()
    source.write_text(
        '{"tokens": {"access_token": "opaque-access-token-value-1"}}', encoding="utf-8"
    )
    os.chmod(source, 0o600)
    cfg = config(
        {
            "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
            "GLINK_ACP_CREDENTIAL_FILES": f"{source}=.codex/auth.json",
        }
    )
    runtime_dir = tmp_path / "acp-agent"
    with pytest.raises(AcpStateInheritError):
        prepare_acp_runtime(
            cfg, runtime_dir=runtime_dir, inherit_from=tmp_path / "missing-parent"
        )
    assert not (runtime_dir / "home" / ".codex" / "auth.json").exists()


def test_pi_session_seed_branch_deletes_rendered_copies(tmp_path: Path) -> None:
    """族级 session_seed 拒绝同样发生在渲染之后：副本删除后再抛 FollowupUnavailable。"""
    from glink_harness.kernel.harness import select_family
    from glink_harness.contracts import FollowupUnavailable
    from glink_harness.store import RunStore

    cfg = config(
        {
            "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
            "GLINK_ACP_CREDENTIAL_ENV": "OPENROUTER_API_KEY",
            "OPENROUTER_API_KEY": "sk-or-seed-branch-00000000000",
        }
    )
    store = RunStore(cfg.data_root / "runs")
    family = select_family(cfg.harness, config=cfg, store=store)
    run_dir = tmp_path / "runs" / "r-seed"
    with pytest.raises(FollowupUnavailable):
        family.prepare_runtime(
            "r-seed",
            run_dir,
            parent=None,
            session_seed=b"pi-seed-has-no-acp-meaning",
            egress=None,
            sandbox=None,
        )
    assert not (run_dir / "acp-agent" / "home" / ".dsh" / ".credentials.yaml").exists()


# -- 对话权威状态的学习扫描（cc 终审 B3：跳过继承的路径仍要认识历史凭据形值） ----


def test_learn_conversation_state_secrets_reads_text_and_skips_binary(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "session.jsonl").write_text(
        'the agent echoed sk-live-history-credential-1\n', encoding="utf-8"
    )
    (state / "binary.bin").write_bytes(b"sk-bin\x00ary" + b"\x00" * 10)
    secrets = learn_conversation_state_secrets(state)
    assert "sk-live-history-credential-1" in secrets
    assert all("\x00" not in value for value in secrets), "二进制文件被跳过"


def test_learn_conversation_state_secrets_is_empty_without_the_directory(tmp_path: Path) -> None:
    assert learn_conversation_state_secrets(tmp_path / "absent") == ()
