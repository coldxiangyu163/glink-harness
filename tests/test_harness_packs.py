"""Harness packs: manifest parsing, selection, the HOME overlay and the record.

A pack (``harnesses/<pack>/harness.toml``, format ``pi-glink-harness/v2``) is
the one place a harness's differences live: family, ``[entry]`` argv, the
``[capabilities]`` matrix family code reads, and the ``[image]`` requirement the
Dockerfile ARGs / E2B template parameters are generated from, plus an optional
capability prompt and (ACP only) the ``home/`` overlay. Parsing is
all-or-nothing: a typo must refuse startup instead of silently falling back to
another harness.
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
from store_helpers import finish, stored_record

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_PACKS = REPO_ROOT / "harnesses"


PI_IMAGE = (
    '\n[image]\ntarget = "pi-worker"\n'
    '\n[[image.packages]]\narg = "PI_VERSION"\n'
    'package = "@earendil-works/pi-coding-agent"\nversion = "1.0.0"\nrole = "kernel"\n'
)
ACP_IMAGE = (
    '\n[image]\ntarget = "acp-worker"\n'
    '\n[[image.packages]]\narg = "P_VERSION"\npackage = "p"\nversion = "1.0.0"\n'
    'role = "agent"\n'
)


def pi_manifest(name: str = "demo", *, capabilities: str = "", image: str = PI_IMAGE) -> str:
    return (
        f'format = "pi-glink-harness/v2"\nname = "{name}"\nfamily = "pi"\n'
        '\n[entry]\ncommand = ["pi"]\n'
        f'\n[capabilities]\nresume = true\n{capabilities}'
        f"{image}"
    )


def acp_manifest(
    name: str = "demo",
    *,
    command: str = '["fake"]',
    capabilities: str = "resume = true\n",
    image: str = ACP_IMAGE,
) -> str:
    return (
        f'format = "pi-glink-harness/v2"\nname = "{name}"\nfamily = "acp"\n'
        f"\n[entry]\ncommand = {command}\n"
        f"\n[capabilities]\n{capabilities}"
        f"{image}"
    )


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
    }
    values.update(overrides)
    return values


# -- the shipped packs --------------------------------------------------------


def test_the_repository_packs_declare_the_expected_families_and_commands() -> None:
    assert pack_names(REPO_PACKS) == ("codex", "dsh", "opencode", "pi")
    pi = load_pack(REPO_PACKS, "pi")
    assert pi.family == "pi" and pi.command == ("pi",) and pi.home is None
    assert pi.prompt and pi.prompt_path == REPO_PACKS / "pi" / "capabilities.md"
    assert pi.capabilities.resume is True
    assert [item.role for item in pi.image.packages] == ["kernel", "pi-package", "pi-package"]
    assert pi.image.target == "pi-worker"
    assert pi.prebaked_pi_packages() == ("pi-web-access", "pi-subagents")

    dsh = load_pack(REPO_PACKS, "dsh")
    assert dsh.family == "acp" and dsh.command == ("dsh", "--profile", "acp")
    assert [(i.package, i.version, i.role) for i in dsh.image.packages] == [
        ("@deepseek-ai/dsh", "0.1.5-rc.3", "agent")
    ]
    assert dsh.image.target == "acp-worker"
    caps = dsh.capabilities
    assert caps.session_config == () and caps.permission_allow == ()
    # dsh 的 subagent 工具异步派发（GLINK-OBS-01 实测），由包声明而非宿主识别回复文本。
    assert caps.subagent_dispatch == "async"
    # 运行中子代理进度源由能力声明，族代码不再按包名 / 状态根目录特判 dsh。
    assert caps.child_progress == "dsh-session-zstd" and caps.resume is True

    codex = load_pack(REPO_PACKS, "codex")
    assert codex.command == ("codex-acp",)
    assert [(i.package, i.version) for i in codex.image.packages] == [
        ("@agentclientprotocol/codex-acp", "1.13.1")
    ]
    # 1.13.1 的 mode 词表没有 auto（集成笔记 §14.5）：包故意不声明 session_config。
    assert codex.capabilities.session_config == ()

    opencode = load_pack(REPO_PACKS, "opencode")
    assert opencode.command == ("opencode", "acp")
    assert [(i.package, i.version) for i in opencode.image.packages] == [
        ("opencode-ai", "1.18.32")
    ]
    # 未声明即同步、无运行中进度源：子代理工具调用的终态就是子代理的结果。
    for pack in (codex, opencode):
        assert pack.capabilities.subagent_dispatch == "sync"
        assert pack.capabilities.child_progress is None


def test_a_missing_harnesses_directory_offers_no_names(tmp_path: Path) -> None:
    assert pack_names(tmp_path / "absent") == ()
    with pytest.raises(HarnessPackError, match="不存在"):
        load_pack(tmp_path / "absent", "pi")


def test_capability_prompt_is_read_verbatim(tmp_path: Path) -> None:
    pack_dir = write_manifest(tmp_path, "demo", pi_manifest())
    (pack_dir / "capabilities.md").write_text("## 能力\n\n- 一条事实。\n", encoding="utf-8")
    pack = load_pack(tmp_path / "harnesses", "demo")
    assert pack.prompt == "## 能力\n\n- 一条事实。"
    assert pack.prompt_path == pack_dir / "capabilities.md"


def test_a_pack_without_a_prompt_file_has_an_empty_prompt(tmp_path: Path) -> None:
    write_manifest(tmp_path, "demo", pi_manifest())
    pack = load_pack(tmp_path / "harnesses", "demo")
    assert pack.prompt == "" and pack.prompt_path is None


# -- manifest validation ------------------------------------------------------

_V1 = 'format = "pi-glink-harness/v1"\nname = "demo"\nfamily = "pi"\n'


@pytest.mark.parametrize(
    "body, fragment",
    [
        # 退役的 v1 清单：不迁移，直接拒绝。
        (_V1, "旧格式不再可读"),
        (pi_manifest().replace("pi-glink-harness/v2", "v2"), "format"),
        (pi_manifest().replace('name = "demo"', 'name = "other"'), "目录名"),
        (pi_manifest().replace('family = "pi"', 'family = "nope"'), "family"),
        (pi_manifest() + "nope = 1\n", "未知键"),
        (pi_manifest(capabilities="nope = 1\n"), "未知键"),
        # 缺 [entry] / [capabilities] / [image] 表。
        (pi_manifest().replace('\n[entry]\ncommand = ["pi"]\n', ""), "[entry]"),
        (pi_manifest(image=""), "[image]"),
        (
            'format = "pi-glink-harness/v2"\nname = "demo"\nfamily = "pi"\n'
            '\n[entry]\ncommand = ["pi"]\n' + PI_IMAGE,
            "[capabilities]",
        ),
        # resume 必须显式声明为 bool。
        (pi_manifest().replace("resume = true\n", ""), "capabilities.resume"),
        (pi_manifest().replace("resume = true", 'resume = "yes"'), "capabilities.resume"),
        # pi 的 entry 只命名内核可执行名。
        (pi_manifest().replace('["pi"]', '["pi", "--mode", "rpc"]'), "一项"),
        (acp_manifest(command="[]"), "entry.command"),
        (acp_manifest(command='[""]'), "entry.command"),
        # ACP 专属能力写进 pi 包即拒绝（不再按族名静默忽略）。
        (pi_manifest(capabilities='subagent_dispatch = "async"\n'), 'family="acp"'),
        (pi_manifest(capabilities='child_progress = "dsh-session-zstd"\n'), 'family="acp"'),
        (pi_manifest(capabilities='permission_allow = ["read"]\n'), 'family="acp"'),
        (
            acp_manifest(capabilities='resume = true\npermission_allow = ["everything"]\n'),
            "permission_allow",
        ),
        (
            acp_manifest(capabilities='resume = true\nsubagent_dispatch = "background"\n'),
            "subagent_dispatch",
        ),
        (
            acp_manifest(capabilities='resume = true\nchild_progress = "tail-the-log"\n'),
            "child_progress",
        ),
        # 镜像需求：target 必须属于本族、版本必须精确、ARG 名规范、角色合族。
        (acp_manifest(image=PI_IMAGE), "image.target"),
        (acp_manifest(image=ACP_IMAGE.replace('"1.0.0"', '"latest"')), "精确版本"),
        (acp_manifest(image=ACP_IMAGE.replace('"1.0.0"', '"^1.0.0"')), "精确版本"),
        (acp_manifest(image=ACP_IMAGE.replace('"P_VERSION"', '"P_TAG"')), "*_VERSION"),
        (acp_manifest(image=ACP_IMAGE.replace('"agent"', '"kernel"')), "role"),
        (acp_manifest(image=ACP_IMAGE.replace('role = "agent"\n', 'role = "agent"\nsha = "x"\n')), "未知键"),
        (acp_manifest(image='\n[image]\ntarget = "acp-worker"\n'), "[[image.packages]]"),
        (
            acp_manifest(
                image=ACP_IMAGE
                + '\n[[image.packages]]\narg = "P_VERSION"\npackage = "q"\n'
                'version = "1.0.0"\nrole = "agent"\n'
            ),
            "重复",
        ),
        (acp_manifest(image=ACP_IMAGE.replace('target = "acp-worker"', 'target = "acp-worker"\ncpu_count = 0')), "image.cpu_count"),
        (pi_manifest(image=PI_IMAGE.replace('"kernel"', '"pi-package"')), "kernel"),
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
        assert document["format"] == "pi-glink-harness/v2"
        assert document["name"] == name
        # resume 在每个随仓库发布的包里都是显式事实，不是默认值。
        assert isinstance(document["capabilities"]["resume"], bool)
        assert load_pack(REPO_PACKS, name).document == document


def test_an_oversized_manifest_is_refused(tmp_path: Path) -> None:
    padding = "# " + "x" * (17 * 1024) + "\n"
    write_manifest(tmp_path, "demo", pi_manifest() + padding)
    with pytest.raises(HarnessPackError, match="字节"):
        load_pack(tmp_path / "harnesses", "demo")


def test_symlinked_pack_entries_are_not_repository_content(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "harness.toml").write_text(pi_manifest("linked"), encoding="utf-8")
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
        acp_manifest(
            command='["fake", "--acp"]',
            capabilities=(
                'resume = true\npermission_allow = ["allow_once", "read"]\n'
                '\n[capabilities.session_config]\nmode = "agent"\n'
            ),
        ),
    )
    pack = load_pack(tmp_path / "harnesses", "demo")
    assert pack.capabilities.session_config == (("mode", "agent"),)
    assert pack.capabilities.permission_allow == ("allow_once", "read")
    assert pack.command_line() == "fake --acp"


def test_an_acp_pack_declares_its_subagent_dispatch_mode(tmp_path: Path) -> None:
    write_manifest(
        tmp_path,
        "demo",
        acp_manifest(capabilities='resume = true\nsubagent_dispatch = "async"\n'),
    )
    pack = load_pack(tmp_path / "harnesses", "demo")
    assert pack.capabilities.subagent_dispatch == "async"
    config = build_config(
        build_values(tmp_path, tmp_path / "harnesses", PI_GLINK_HARNESS="demo"), REPO_ROOT
    )
    # 运行期直接引用包对象，配置里不再有抄来的 acp_* 字段。
    assert config.pack == pack
    assert config.pack.capabilities.subagent_dispatch == "async"
    assert not hasattr(config, "acp_subagent_dispatch")


def test_an_empty_home_overlay_directory_is_a_directory_not_a_failure(tmp_path: Path) -> None:
    pack_dir = write_manifest(tmp_path, "demo", acp_manifest())
    (pack_dir / "home").mkdir()
    pack = load_pack(tmp_path / "harnesses", "demo")
    assert pack.home == pack_dir / "home"


def test_a_home_overlay_on_a_pi_pack_is_refused(tmp_path: Path) -> None:
    pack_dir = write_manifest(tmp_path, "demo", pi_manifest())
    (pack_dir / "home").mkdir()
    with pytest.raises(HarnessPackError, match="home/"):
        load_pack(tmp_path / "harnesses", "demo")


# -- selection through the host configuration ---------------------------------


def test_the_selected_pack_supplies_family_capabilities_and_overlay(tmp_path: Path) -> None:
    pack_dir = write_manifest(
        tmp_path,
        "demo",
        acp_manifest(
            command='["fake", "--acp"]',
            capabilities=(
                'resume = true\npermission_allow = ["allow_once"]\n'
                '\n[capabilities.session_config]\nmode = "agent"\n'
            ),
        ),
    )
    (pack_dir / "capabilities.md").write_text("- 一条能力。\n", encoding="utf-8")
    (pack_dir / "home").mkdir()
    (pack_dir / "home" / "state.txt").write_text("overlay\n", encoding="utf-8")
    config = build_config(
        build_values(tmp_path, tmp_path / "harnesses", PI_GLINK_HARNESS="demo"),
        REPO_ROOT,
    )
    assert config.harness == "acp" and config.harness_pack == "demo"
    assert config.pack.command == ("fake", "--acp")
    assert config.pack.capabilities.session_config == (("mode", "agent"),)
    assert config.pack.capabilities.permission_allow == ("allow_once",)
    assert config.pack.prompt == "- 一条能力。"
    assert config.pack.home == pack_dir / "home"
    assert config.agent_config_dir == pack_dir
    for retired in (
        "acp_command",
        "acp_session_config",
        "acp_permission_allow",
        "acp_subagent_dispatch",
        "pack_capabilities",
        "pack_home",
    ):
        assert not hasattr(config, retired), retired


# -- the ACP HOME overlay -----------------------------------------------------


def make_overlay_config(
    tmp_path: Path, *, credential_source: Path | None = None
) -> tuple[object, Path]:
    """A temporary ACP pack with a ``home/`` overlay; returns (config, pack home)."""
    pack_dir = write_manifest(tmp_path, "demo", acp_manifest())
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
    write_manifest(tmp_path, "demo", acp_manifest())
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
    written = stored_record(store, record.run_id)
    assert written["spec"]["family"] == "acp" and written["spec"]["pack"] == "demo"
    assert not {"harness", "harness_pack", "executor"} & written.keys()

    # 同包但父 run 未结束：按状态的既有拒绝（run_busy），不是包名拒绝。
    with pytest.raises(FollowupUnavailable) as excinfo:
        manager._admission.resumable_parent(None, "parent-run", manager._admission.spec(None))  # noqa: SLF001
    assert excinfo.value.code == "run_busy"

    # 异包（同族）：fail-closed 为 session_unavailable，不再读父会话。
    store.create(
        "题目",
        run_id="foreign-run",
        spec=RunSpec(family="acp", pack="opencode", executor="inproc"),
    )
    finish(store, "foreign-run", FINISHED)
    with pytest.raises(FollowupUnavailable) as excinfo:
        manager._admission.resumable_parent(None, "foreign-run", manager._admission.spec(None))  # noqa: SLF001
    assert excinfo.value.code == "session_unavailable"


def test_a_pack_that_declares_no_resume_refuses_every_followup(tmp_path: Path) -> None:
    """``capabilities.resume = false``: admission refuses before reading the parent.

    The same pack and a finished parent would otherwise be continued (the
    adjacent legal input below); the refusal comes from the declared capability,
    not from the pack's name or the parent's state.
    """
    write_manifest(
        tmp_path, "demo", acp_manifest(capabilities="resume = false\n")
    )
    config = build_config(
        build_values(tmp_path, tmp_path / "harnesses", PI_GLINK_HARNESS="demo"),
        REPO_ROOT,
    )
    assert config.pack.capabilities.resume is False
    store = RunStore(config.data_root / "runs")
    manager = RunManager(config, EventBus(), store)
    store.create(
        "题目",
        run_id="parent-run",
        spec=RunSpec(family="acp", pack="demo", executor="inproc"),
    )
    finish(store, "parent-run")
    admission = manager._admission  # noqa: SLF001
    with pytest.raises(FollowupUnavailable) as excinfo:
        admission.resumable_parent(None, "parent-run", admission.spec(None))
    assert excinfo.value.code == "session_unavailable"

    # 相邻合法输入：同一父 run、声明 resume = true 的同名包可以续接。
    write_manifest(tmp_path, "demo", acp_manifest(capabilities="resume = true\n"))
    resumable = build_config(
        build_values(tmp_path, tmp_path / "harnesses", PI_GLINK_HARNESS="demo"),
        REPO_ROOT,
    )
    admission = RunManager(resumable, EventBus(), store)._admission  # noqa: SLF001
    parent = admission.resumable_parent(None, "parent-run", admission.spec(None))
    assert parent is not None
