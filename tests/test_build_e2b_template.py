"""Template publishing contract, with a fake SDK only (never builds or calls E2B)."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from e2b.exceptions import TemplateException


class FakeTemplate:
    def __init__(self, *, missing=False, lookup_error=None, fail_lookup_after_build=False,
                 restore=False):
        self.tags = {} if missing else {"default": "production"}
        self.missing = missing
        self.lookup_error = lookup_error
        self.fail_lookup_after_build = fail_lookup_after_build
        self.restore = restore
        self.builds = 0
        self.assignments = []
        self.queries = 0

    def __call__(self):
        return self

    def from_dockerfile(self, path):
        return self

    def get_tags(self, alias):
        self.queries += 1
        if self.lookup_error:
            raise self.lookup_error
        if self.fail_lookup_after_build and self.builds:
            raise ConnectionError("synthetic post-build query failure")
        if self.missing and not self.builds:
            error = TemplateException("synthetic not-found")
            error.status_code = 404
            raise error
        return [SimpleNamespace(tag=tag, build_id=build) for tag, build in self.tags.items()]

    def assign_tags(self, source, tag):
        self.assignments.append((source, tag))
        build = source.rsplit(":", 1)[-1]  # SDK accepts a build id or alias:tag
        build = self.tags.get(build, build)
        if tag != "default" or self.restore:
            self.tags[tag] = build
        return SimpleNamespace(build_id=build, tags=[tag])

    def build(self, builder, *, alias, tags, **kwargs):
        self.builds += 1
        self.tags["default"] = "new-build"
        for tag in tags or ():
            self.tags[tag] = "new-build"
        return SimpleNamespace(name=alias, build_id="new-build", tags=tags or ["default"])


@pytest.fixture
def script(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/build-e2b-template.py"
    spec = importlib.util.spec_from_file_location("build_e2b_template", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("E2B_API_KEY", "synthetic-e2b-key")
    # 既有用例全部覆盖 SDK 构建面：固定选中 pi-worker，不跟随默认包（dsh）。
    monkeypatch.setenv("GLINK_E2B_TEMPLATE_KEY", "pi-worker")
    return module


@pytest.mark.parametrize("error", [ConnectionError("network"), PermissionError("auth")])
def test_lookup_failure_never_starts_a_build(script, monkeypatch, error):
    fake = FakeTemplate(lookup_error=error)
    monkeypatch.setattr(script, "Template", fake)
    assert script.main(["--tag", "candidate"]) != 0
    assert fake.builds == 0 and fake.assignments == []


def test_post_build_query_failure_cannot_report_success(script, monkeypatch, capsys):
    fake = FakeTemplate(fail_lookup_after_build=True)
    monkeypatch.setattr(script, "Template", fake)
    assert script.main(["--tag", "candidate"]) != 0
    assert fake.builds == 1
    assert "人工" in capsys.readouterr().err


def test_default_must_be_confirmed_after_restore(script, monkeypatch):
    fake = FakeTemplate(restore=False)
    monkeypatch.setattr(script, "Template", fake)
    assert script.main(["--tag", "candidate"]) != 0
    assert fake.tags["default"] == "new-build"


def test_explicit_not_found_allows_first_build(script, monkeypatch):
    fake = FakeTemplate(missing=True)
    monkeypatch.setattr(script, "Template", fake)
    assert script.main(["--tag", "candidate"]) == 0
    assert fake.builds == 1 and fake.tags["candidate"] == "new-build"


def test_candidate_build_preserves_production_default(script, monkeypatch):
    fake = FakeTemplate(restore=True)
    monkeypatch.setattr(script, "Template", fake)
    assert script.main(["--tag", "candidate"]) == 0
    assert fake.tags == {"default": "production", "previous": "production", "candidate": "new-build"}


@pytest.mark.parametrize("tag", ["default", "previous"])
def test_candidate_cannot_overwrite_production_or_rollback_tags(script, monkeypatch, tag):
    fake = FakeTemplate(restore=True)
    monkeypatch.setattr(script, "Template", fake)
    assert script.main(["--tag", tag]) != 0
    assert fake.builds == 0 and fake.assignments == []


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_sdk_errors_other_than_not_found_are_not_first_builds(script, monkeypatch, status):
    error = TemplateException("synthetic SDK response")
    error.status_code = status
    fake = FakeTemplate(lookup_error=error)
    monkeypatch.setattr(script, "Template", fake)
    assert script.main(["--tag", "candidate"]) != 0
    assert fake.builds == 0


def test_post_build_not_found_is_also_a_publication_failure(script, monkeypatch):
    fake = FakeTemplate(restore=True)
    original = fake.get_tags

    def tags(alias):
        if fake.builds:
            error = TemplateException("synthetic post-build missing")
            error.status_code = 404
            raise error
        return original(alias)

    fake.get_tags = tags
    monkeypatch.setattr(script, "Template", fake)
    assert script.main(["--tag", "candidate"]) != 0
    assert fake.builds == 1


def test_promotion_confirms_the_result_instead_of_trusting_assign_return(script, monkeypatch):
    fake = FakeTemplate(restore=False)
    fake.tags["candidate"] = "ready-build"
    monkeypatch.setattr(script, "Template", fake)
    assert script.main(["--promote-from", "candidate"]) != 0
    assert fake.builds == 0
    fake.restore = True
    assert script.main(["--promote-from", "candidate"]) == 0
    assert fake.tags["default"] == "ready-build"


# -- 模板选择（GLINK-HARNESS-DEFAULT-20261001：默认包 dsh 起，构建选择跟随包清单）-----


def test_default_template_follows_the_default_pack_and_refuses_cli_builder(
    script, monkeypatch, capsys
):
    """缺省包 dsh 的镜像目标是 acp-worker（builder=cli）：SDK 脚本拒绝并指向 CLI 命令。"""
    monkeypatch.delenv("GLINK_E2B_TEMPLATE_KEY", raising=False)
    monkeypatch.delenv("GLINK_HARNESS", raising=False)
    fake = FakeTemplate()
    monkeypatch.setattr(script, "Template", fake)
    assert script.main(["--tag", "candidate"]) == 2
    assert fake.builds == 0
    err = capsys.readouterr().err
    assert "builder=cli" in err and "template create" in err


def test_default_template_follows_an_explicitly_selected_pack(script, monkeypatch):
    """GLINK_HARNESS=pi 时缺省模板键解析为 pi-worker（sdk 面），构建照常。"""
    monkeypatch.delenv("GLINK_E2B_TEMPLATE_KEY", raising=False)
    monkeypatch.setenv("GLINK_HARNESS", "pi")
    fake = FakeTemplate(restore=True)
    monkeypatch.setattr(script, "Template", fake)
    assert script.main(["--tag", "candidate"]) == 0
    assert fake.builds == 1


def test_explicit_template_flag_selects_the_entry(script, monkeypatch):
    monkeypatch.delenv("GLINK_E2B_TEMPLATE_KEY", raising=False)
    fake = FakeTemplate(restore=True)
    monkeypatch.setattr(script, "Template", fake)
    assert script.main(["--template", "pi-worker", "--tag", "candidate"]) == 0
    assert fake.builds == 1


def test_unknown_template_key_is_refused(script, monkeypatch):
    monkeypatch.delenv("GLINK_E2B_TEMPLATE_KEY", raising=False)
    with pytest.raises(SystemExit):
        script.main(["--template", "nope", "--tag", "candidate"])


# -- B2：默认模板选择 fail-closed（无效包绝不回退 pi-worker）----------------------


def test_invalid_pack_never_falls_back_or_touches_the_sdk(script, monkeypatch):
    """GLINK_HARNESS=dsh-typo：build 与 promote 都在任何 SDK 查询/构建/tag 修改前拒绝。

    回归 B2：旧实现把无效包静默回退成 pi-worker，fake SDK 会真实执行 build
    与 previous/default tag 修改且 exit 0。
    """
    monkeypatch.delenv("GLINK_E2B_TEMPLATE_KEY", raising=False)
    monkeypatch.setenv("GLINK_HARNESS", "dsh-typo")
    fake = FakeTemplate()
    monkeypatch.setattr(script, "Template", fake)
    with pytest.raises(SystemExit) as build_exit:
        script.main(["--tag", "candidate"])
    with pytest.raises(SystemExit) as promote_exit:
        script.main(["--promote-from", "candidate"])
    assert fake.queries == 0 and fake.builds == 0 and fake.assignments == []
    # SystemExit 的文本即拒绝理由（脚本入口 raise SystemExit(main()) 时打印）
    assert "dsh-typo" in str(build_exit.value)
    assert "dsh-typo" in str(promote_exit.value)


def test_a_broken_manifest_is_refused_before_the_sdk(script, monkeypatch, tmp_path):
    """缺字段的包清单（无 [image].target）同样在 SDK 之前拒绝。"""
    monkeypatch.delenv("GLINK_E2B_TEMPLATE_KEY", raising=False)
    packs = tmp_path / "packs"
    pack = packs / "broken"
    pack.mkdir(parents=True)
    (pack / "harness.toml").write_text(
        'format = "glink-harness/v2"\nname = "broken"\nfamily = "acp"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("GLINK_HARNESS", "broken")
    monkeypatch.setenv("GLINK_HARNESSES_DIR", str(packs))
    fake = FakeTemplate()
    monkeypatch.setattr(script, "Template", fake)
    with pytest.raises(SystemExit):
        script.main(["--tag", "candidate"])
    assert fake.queries == 0 and fake.builds == 0 and fake.assignments == []


def test_an_explicit_template_never_resolves_the_pack_default(script, monkeypatch):
    """显式合法 --template 不解析包清单：无效 GLINK_HARNESS 不影响，SDK 候选照常。"""
    monkeypatch.delenv("GLINK_E2B_TEMPLATE_KEY", raising=False)
    monkeypatch.setenv("GLINK_HARNESS", "dsh-typo")
    fake = FakeTemplate(restore=True)
    monkeypatch.setattr(script, "Template", fake)
    assert script.main(["--template", "pi-worker", "--tag", "candidate"]) == 0
    assert fake.builds == 1
    assert fake.tags["candidate"] == "new-build"


def test_blank_harness_keeps_the_dsh_default_cli_refusal(script, monkeypatch, capsys):
    """GLINK_HARNESS 空白视为未设置：默认包 dsh → acp-worker 的 builder=cli 拒绝。"""
    monkeypatch.delenv("GLINK_E2B_TEMPLATE_KEY", raising=False)
    monkeypatch.setenv("GLINK_HARNESS", "   ")
    fake = FakeTemplate()
    monkeypatch.setattr(script, "Template", fake)
    assert script.main(["--tag", "candidate"]) == 2
    assert fake.queries == 0 and fake.builds == 0
    assert "builder=cli" in capsys.readouterr().err
