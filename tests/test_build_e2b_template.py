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

    def __call__(self):
        return self

    def from_dockerfile(self, path):
        return self

    def get_tags(self, alias):
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
