"""Harness packs: manifest parsing, selection, the HOME overlay and the record.

A pack (``harnesses/<pack>/harness.toml``) is the one place a harness's
differences live: family, ACP argv, upstream pin, packaged session options and
permission allowlist, plus an optional capability prompt and (ACP only) the
``home/`` overlay. Parsing is all-or-nothing: a typo must refuse startup instead
of silently falling back to another harness.
"""

from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path

import pytest

from pi_glink.config import ConfigError, build_config
from pi_glink.events import EventBus
from pi_glink.harnesses import (
    HarnessPackError,
    load_pack,
    pack_names,
)
from pi_glink.kernel.acp_env import ACP_HOME_DIRNAME, prepare_acp_runtime
from pi_glink.kernel.harness import FollowupUnavailable
from pi_glink.runs.runner import RunManager
from pi_glink.store import FINISHED, RunStore, RunSpec

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_PACKS = REPO_ROOT / "harnesses"


def write_manifest(root: Path, name: str, body: str) -> Path:
    pack_dir = Path(root) / "harnesses" / name
    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "harness.toml").write_text(body, encoding="utf-8")
    return pack_dir


def build_values(tmp_path: Path, harnesses_dir: Path, **overrides: str) -> dict[str, str]:
    values = {
        "OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "PI_GLINK_HARNESSES_DIR": str(harnesses_dir),
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent-web-search.json"),
    }
    values.update(overrides)
    return values


# -- the shipped packs --------------------------------------------------------


def test_the_repository_packs_declare_the_expected_families_and_commands() -> None:
    assert pack_names(REPO_PACKS) == ("codex", "dsh", "opencode", "pi")
    pi = load_pack(REPO_PACKS, "pi")
    assert pi.family == "pi" and pi.command == () and pi.package == ""
    assert pi.capabilities is not None and pi.home is None

    dsh = load_pack(REPO_PACKS, "dsh")
    assert dsh.family == "acp" and dsh.command == ("dsh", "--profile", "acp")
    assert (dsh.package, dsh.version) == ("@deepseek-ai/dsh", "0.1.5-rc.3")
    assert dsh.session_config == () and dsh.permission_allow == ()
    # dsh 的 subagent 工具异步派发（GLINK-OBS-01 实测），由包声明而非宿主识别回复文本。
    assert dsh.subagent_dispatch == "async"

    codex = load_pack(REPO_PACKS, "codex")
    assert codex.command == ("codex-acp",)
    assert (codex.package, codex.version) == ("@agentclientprotocol/codex-acp", "1.13.1")
    # 1.13.1 的 mode 词表没有 auto（集成笔记 §14.5）：包故意不声明 session_config。
    assert codex.session_config == ()

    opencode = load_pack(REPO_PACKS, "opencode")
    assert opencode.command == ("opencode", "acp")
    assert (opencode.package, opencode.version) == ("opencode-ai", "1.18.32")
    # 未声明即同步：子代理工具调用的终态就是子代理的结果。
    assert codex.subagent_dispatch == "sync" and opencode.subagent_dispatch == "sync"


def test_a_missing_harnesses_directory_offers_no_names(tmp_path: Path) -> None:
    assert pack_names(tmp_path / "absent") == ()
    with pytest.raises(HarnessPackError, match="不存在"):
        load_pack(tmp_path / "absent", "pi")


def test_capability_prompt_is_read_verbatim(tmp_path: Path) -> None:
    pack_dir = write_manifest(
        tmp_path,
        "demo",
        'format = "pi-glink-harness/v1"\nname = "demo"\nfamily = "pi"\n',
    )
    (pack_dir / "capabilities.md").write_text("## 能力\n\n- 一条事实。\n", encoding="utf-8")
    pack = load_pack(tmp_path / "harnesses", "demo")
    assert pack.capabilities_text() == "## 能力\n\n- 一条事实。"


# -- manifest validation ------------------------------------------------------


@pytest.mark.parametrize(
    "body, fragment",
    [
        ('format = "v2"\nname = "demo"\nfamily = "pi"\n', "format"),
        ('format = "pi-glink-harness/v1"\nname = "other"\nfamily = "pi"\n', "目录名"),
        ('format = "pi-glink-harness/v1"\nname = "demo"\nfamily = "nope"\n', "family"),
        (
            'format = "pi-glink-harness/v1"\nname = "demo"\nfamily = "pi"\nnope = 1\n',
            "未知键",
        ),
        (
            'format = "pi-glink-harness/v1"\nname = "demo"\nfamily = "pi"\n'
            'command = ["pi"]\n',
            "只对 family=\"acp\"",
        ),
        (
            'format = "pi-glink-harness/v1"\nname = "demo"\nfamily = "acp"\n',
            "command",
        ),
        (
            'format = "pi-glink-harness/v1"\nname = "demo"\nfamily = "acp"\n'
            'command = ["fake"]\n',
            "package",
        ),
        (
            'format = "pi-glink-harness/v1"\nname = "demo"\nfamily = "acp"\n'
            'command = ["fake"]\npackage = "p"\nversion = "latest"\n',
            "精确版本",
        ),
        (
            'format = "pi-glink-harness/v1"\nname = "demo"\nfamily = "acp"\n'
            'command = ["fake"]\npackage = "p"\nversion = "1.0.0"\n'
            'permission_allow = ["everything"]\n',
            "permission_allow",
        ),
        (
            'format = "pi-glink-harness/v1"\nname = "demo"\nfamily = "acp"\n'
            'command = ["fake"]\npackage = "p"\nversion = "1.0.0"\n'
            'subagent_dispatch = "background"\n',
            "subagent_dispatch",
        ),
        (
            'format = "pi-glink-harness/v1"\nname = "demo"\nfamily = "pi"\n'
            'subagent_dispatch = "async"\n',
            "只对 family=\"acp\"",
        ),
    ],
)
def test_a_manifest_that_is_not_exactly_one_pack_is_refused(
    tmp_path: Path, body: str, fragment: str
) -> None:
    write_manifest(tmp_path, "demo", body)
    with pytest.raises(HarnessPackError) as excinfo:
        load_pack(tmp_path / "harnesses", "demo")
    assert fragment in str(excinfo.value)


def test_the_shipped_manifests_parse_as_toml_and_validate(tmp_path: Path) -> None:
    for name in ("pi", "dsh", "codex", "opencode"):
        document = tomllib.loads(
            (REPO_PACKS / name / "harness.toml").read_text(encoding="utf-8")
        )
        assert document["format"] == "pi-glink-harness/v1"
        assert document["name"] == name


def test_an_oversized_manifest_is_refused(tmp_path: Path) -> None:
    padding = "# " + "x" * (17 * 1024) + "\n"
    write_manifest(
        tmp_path,
        "demo",
        'format = "pi-glink-harness/v1"\nname = "demo"\nfamily = "pi"\n' + padding,
    )
    with pytest.raises(HarnessPackError, match="字节"):
        load_pack(tmp_path / "harnesses", "demo")


def test_symlinked_pack_entries_are_not_repository_content(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "harness.toml").write_text(
        'format = "pi-glink-harness/v1"\nname = "linked"\nfamily = "pi"\n',
        encoding="utf-8",
    )
    packs = tmp_path / "harnesses"
    packs.mkdir()
    os.symlink(real, packs / "linked")
    assert pack_names(packs) == ()
    with pytest.raises(HarnessPackError, match="不存在"):
        load_pack(packs, "linked")


def test_an_acp_pack_may_carry_session_config_and_permission_allow(tmp_path: Path) -> None:
    write_manifest(
        tmp_path,
        "demo",
        'format = "pi-glink-harness/v1"\nname = "demo"\nfamily = "acp"\n'
        'command = ["fake", "--acp"]\npackage = "demo-package"\nversion = "1.2.3"\n'
        '\npermission_allow = ["allow_once", "read"]\n'
        '\n[session_config]\nmode = "agent"\n',
    )
    pack = load_pack(tmp_path / "harnesses", "demo")
    assert pack.session_config == (("mode", "agent"),)
    assert pack.permission_allow == ("allow_once", "read")


def test_an_acp_pack_declares_its_subagent_dispatch_mode(tmp_path: Path) -> None:
    write_manifest(
        tmp_path,
        "demo",
        'format = "pi-glink-harness/v1"\nname = "demo"\nfamily = "acp"\n'
        'command = ["fake"]\npackage = "demo-package"\nversion = "1.2.3"\n'
        'subagent_dispatch = "async"\n',
    )
    pack = load_pack(tmp_path / "harnesses", "demo")
    assert pack.subagent_dispatch == "async"
    config = build_config(
        build_values(tmp_path, tmp_path / "harnesses", PI_GLINK_HARNESS="demo"), REPO_ROOT
    )
    assert config.acp_subagent_dispatch == "async"


def test_an_empty_home_overlay_directory_is_a_directory_not_a_failure(tmp_path: Path) -> None:
    pack_dir = write_manifest(
        tmp_path,
        "demo",
        'format = "pi-glink-harness/v1"\nname = "demo"\nfamily = "acp"\n'
        'command = ["fake"]\npackage = "p"\nversion = "1.0.0"\n',
    )
    (pack_dir / "home").mkdir()
    pack = load_pack(tmp_path / "harnesses", "demo")
    assert pack.home == pack_dir / "home"


def test_a_home_overlay_on_a_pi_pack_is_refused(tmp_path: Path) -> None:
    pack_dir = write_manifest(
        tmp_path,
        "demo",
        'format = "pi-glink-harness/v1"\nname = "demo"\nfamily = "pi"\n',
    )
    (pack_dir / "home").mkdir()
    with pytest.raises(HarnessPackError, match="home/"):
        load_pack(tmp_path / "harnesses", "demo")


# -- selection through the host configuration ---------------------------------


def test_the_selected_pack_supplies_family_capabilities_and_overlay(tmp_path: Path) -> None:
    pack_dir = write_manifest(
        tmp_path,
        "demo",
        'format = "pi-glink-harness/v1"\nname = "demo"\nfamily = "acp"\n'
        'command = ["fake", "--acp"]\npackage = "p"\nversion = "1.0.0"\n'
        '\npermission_allow = ["allow_once"]\n'
        '\n[session_config]\nmode = "agent"\n',
    )
    (pack_dir / "capabilities.md").write_text("- 一条能力。\n", encoding="utf-8")
    (pack_dir / "home").mkdir()
    (pack_dir / "home" / "state.txt").write_text("overlay\n", encoding="utf-8")
    config = build_config(
        build_values(tmp_path, tmp_path / "harnesses", PI_GLINK_HARNESS="demo"),
        REPO_ROOT,
    )
    assert config.harness == "acp" and config.harness_pack == "demo"
    assert config.acp_command == ("fake", "--acp")
    assert config.acp_session_config == (("mode", "agent"),)
    assert config.acp_permission_allow == ("allow_once",)
    assert config.pack_capabilities == "- 一条能力。"
    assert config.pack_home == pack_dir / "home"
    assert config.agent_config_dir == pack_dir


# -- the ACP HOME overlay -----------------------------------------------------


def make_overlay_config(
    tmp_path: Path, *, credential_source: Path | None = None
) -> tuple[object, Path]:
    """A temporary ACP pack with a ``home/`` overlay; returns (config, pack home)."""
    pack_dir = write_manifest(
        tmp_path,
        "demo",
        'format = "pi-glink-harness/v1"\nname = "demo"\nfamily = "acp"\n'
        'command = ["fake"]\npackage = "p"\nversion = "1.0.0"\n',
    )
    home = pack_dir / "home"
    home.mkdir()
    values = build_values(tmp_path, tmp_path / "harnesses", PI_GLINK_HARNESS="demo")
    if credential_source is not None:
        values["PI_GLINK_ACP_CREDENTIAL_FILES"] = (
            f"{credential_source}=.dsh/credentials.yaml"
        )
    return build_config(values, REPO_ROOT), home


def test_the_overlay_lands_first_and_credentials_win_the_same_path(tmp_path: Path) -> None:
    credential = tmp_path / "credential.yaml"
    credential.write_text("token: real-credential\n", encoding="utf-8")
    config, pack_home = make_overlay_config(tmp_path, credential_source=credential)
    (pack_home / ".config").mkdir()
    (pack_home / ".config" / "mode.txt").write_text("overlay-mode\n", encoding="utf-8")
    (pack_home / ".dsh").mkdir()
    (pack_home / ".dsh" / "credentials.yaml").write_text(
        "token: overlay-must-not-win\n", encoding="utf-8"
    )
    (pack_home / ".config" / "link.txt").symlink_to("/etc/hosts")

    runtime = prepare_acp_runtime(config, runtime_dir=tmp_path / "run" / "acp-agent")
    home = runtime.home
    assert home.name == ACP_HOME_DIRNAME
    assert (home / ".config" / "mode.txt").read_text(encoding="utf-8") == "overlay-mode\n"
    # 凭据目标优先：覆盖层同路径内容被凭据覆盖。
    assert (home / ".dsh" / "credentials.yaml").read_text(encoding="utf-8") == (
        "token: real-credential\n"
    )
    # 覆盖层里的链接不会被跟随（外部文件既不读也不拷）。
    assert not (home / ".config" / "link.txt").exists()
    mode = stat.S_IMODE((home / ".config" / "mode.txt").stat().st_mode)
    assert mode == 0o600
    assert runtime.command == "fake"


def test_a_run_record_carries_the_pack_and_a_foreign_pack_cannot_continue(
    tmp_path: Path,
) -> None:
    write_manifest(
        tmp_path,
        "demo",
        'format = "pi-glink-harness/v1"\nname = "demo"\nfamily = "acp"\n'
        'command = ["fake"]\npackage = "p"\nversion = "1.0.0"\n',
    )
    config = build_config(
        build_values(tmp_path, tmp_path / "harnesses", PI_GLINK_HARNESS="demo"),
        REPO_ROOT,
    )
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    record = store.create(
        "题目",
        run_id="parent-run",
        spec=RunSpec(family="acp", pack="demo", executor="inproc"),
    )
    written = json.loads(
        (store.run_dir(record.run_id) / "meta.json").read_text(encoding="utf-8")
    )
    assert written["spec"]["family"] == "acp" and written["spec"]["pack"] == "demo"
    assert not {"harness", "harness_pack", "executor"} & written.keys()

    # 同包但父 run 未结束：按状态的既有拒绝（run_busy），不是包名拒绝。
    with pytest.raises(FollowupUnavailable) as excinfo:
        manager._resumable_parent(None, "parent-run", manager._admission_spec(None))  # noqa: SLF001
    assert excinfo.value.code == "run_busy"

    # 异包（同族）：fail-closed 为 session_unavailable，不再读父会话。
    store.create(
        "题目",
        run_id="foreign-run",
        spec=RunSpec(family="acp", pack="opencode", executor="inproc"),
    )
    store.update("foreign-run", status=FINISHED)
    with pytest.raises(FollowupUnavailable) as excinfo:
        manager._resumable_parent(None, "foreign-run", manager._admission_spec(None))  # noqa: SLF001
    assert excinfo.value.code == "session_unavailable"
