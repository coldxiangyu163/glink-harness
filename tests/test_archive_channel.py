"""归档通道（docs/adr/0018）的容器契约与 HTTP 面测试。

单元层覆盖：

* ``publish_archive`` 的全量/增量发布、sha256 与 base 长度校验、发布原子性
  （失败不动已发布状态）；
* tar 容器的安全边界：成员名白名单、tarfile data filter（绝对路径与
  ``..``）、manifest 里的凭据目标拒绝；
* ``serve`` 的回灌打包：内容完整性、manifest 与凭据目标不进包；
* ``EgressProxy`` 上 ``/archive/*`` 路由的鉴权隔离（``pga_`` 与 ``pge_``
  互不通用）与体量上限。

全链路（fake 沙箱真跑 node/tar/curl）见 ``tests/test_kernel_acp_archive.py``。
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from pi_glink.egress import EgressProxy
from pi_glink.kernel.archive_channel import (
    ARCHIVE_TOKEN_PREFIX,
    ArchiveChannel,
    ArchiveError,
    incremental_base,
    publish_archive,
    read_state_manifest,
)


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_archive(
    path: Path,
    *,
    full: dict[str, bytes] | None = None,
    tails: dict[str, tuple[int, bytes]] | None = None,
    entries: dict[str, dict] | None = None,
    members: dict[str, bytes] | None = None,
    with_manifest: bool = True,
) -> Path:
    """Write one archive container the way the sandbox packer would.

    ``full`` members carry whole files; ``tails`` carry ``(base, whole new
    bytes)`` — the tail member is the ``[base, …)`` slice and the manifest
    entry hashes the whole file. ``entries``/``members`` let a test forge
    shapes the honest packer would never produce.
    """
    payload: dict[str, bytes] = dict(members or {})
    manifest_files: dict[str, dict] = {}
    for relative, data in (full or {}).items():
        payload[f"files/{relative}"] = data
        manifest_files[relative] = {"size": len(data), "sha256": sha_of(data)}
    for relative, (base, whole) in (tails or {}).items():
        payload[f"delta/{relative}"] = whole[base:]
        manifest_files[relative] = {
            "size": len(whole),
            "sha256": sha_of(whole),
            "base": base,
        }
    # Forged entries win over the honest ones (a test shapes what the honest
    # packer would never produce).
    manifest_files.update(entries or {})
    if with_manifest:
        payload["manifest.json"] = json.dumps(
            {"version": 1, "files": manifest_files}
        ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w") as tar:
        for name in sorted(payload):
            data = payload[name]
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def published_files(state: Path) -> dict[str, bytes]:
    return {
        p.relative_to(state).as_posix(): p.read_bytes()
        for p in sorted(state.rglob("*"))
        if p.is_file() and p.name != "manifest.json"
    }


# -- publish: full and incremental ----------------------------------------------


def test_full_archive_publishes_the_state_and_manifest(tmp_path: Path) -> None:
    conversation = tmp_path / "conversations" / "r1"
    archive = write_archive(
        tmp_path / "a.tar",
        full={".dsh/sessions/session.v4.jsonl.zstd": b"frame1-frame2"},
    )
    facts = publish_archive(archive, conversation, source_run_id="r1")
    state = conversation / "state"
    assert published_files(state) == {
        ".dsh/sessions/session.v4.jsonl.zstd": b"frame1-frame2"
    }
    manifest = read_state_manifest(conversation)
    assert manifest is not None
    assert manifest["sourceRunId"] == "r1"
    assert manifest["mode"] == "full"
    assert manifest["files"][".dsh/sessions/session.v4.jsonl.zstd"] == {
        "size": 13,
        "sha256": sha_of(b"frame1-frame2"),
    }
    assert facts["mode"] == "full"
    assert facts["bytes"] == 13
    # The incremental base for the next round is exactly what was published.
    assert incremental_base(conversation) == {
        ".dsh/sessions/session.v4.jsonl.zstd": {
            "size": 13,
            "sha256": sha_of(b"frame1-frame2"),
        }
    }


def test_incremental_archive_appends_only_the_tail(tmp_path: Path) -> None:
    conversation = tmp_path / "conversations" / "r1"
    write_archive(
        tmp_path / "a1.tar",
        full={"s/session.zst": b"0123456789", "cache/blob": b"xx"},
    )
    publish_archive(tmp_path / "a1.tar", conversation, source_run_id="r1")
    # The append-only session grows; the untouched cache is not re-sent and a
    # brand-new file arrives whole.
    write_archive(
        tmp_path / "a2.tar",
        tails={"s/session.zst": (10, b"0123456789-append1")},
        full={"new.txt": b"fresh"},
        entries={"cache/blob": {"size": 2, "sha256": sha_of(b"xx"), "base": 2}},
    )
    facts = publish_archive(tmp_path / "a2.tar", conversation, source_run_id="r2")
    assert facts["mode"] == "incremental"
    assert published_files(conversation / "state") == {
        "s/session.zst": b"0123456789-append1",
        "cache/blob": b"xx",
        "new.txt": b"fresh",
    }
    manifest = read_state_manifest(conversation)
    assert manifest is not None and manifest["sourceRunId"] == "r2"


def test_deleted_files_leave_the_published_state(tmp_path: Path) -> None:
    conversation = tmp_path / "conversations" / "r1"
    write_archive(tmp_path / "a1.tar", full={"gone.txt": b"x", "kept": b"y"})
    publish_archive(tmp_path / "a1.tar", conversation, source_run_id="r1")
    write_archive(tmp_path / "a2.tar", full={"kept": b"y"})
    publish_archive(tmp_path / "a2.tar", conversation, source_run_id="r2")
    assert published_files(conversation / "state") == {"kept": b"y"}


def test_delta_with_wrong_base_length_is_refused_and_keeps_the_old_state(
    tmp_path: Path,
) -> None:
    conversation = tmp_path / "conversations" / "r1"
    write_archive(tmp_path / "a1.tar", full={"s": b"0123456789"})
    publish_archive(tmp_path / "a1.tar", conversation, source_run_id="r1")
    before = published_files(conversation / "state")
    # The manifest claims base=4, the host's file is 10 bytes long.
    write_archive(
        tmp_path / "a2.tar",
        tails={"s": (4, b"0123456789-wrongtail")},
    )
    with pytest.raises(ArchiveError, match="增量基线长度不符"):
        publish_archive(tmp_path / "a2.tar", conversation, source_run_id="r2")
    assert published_files(conversation / "state") == before
    assert read_state_manifest(conversation)["sourceRunId"] == "r1"


def test_member_sha_mismatch_is_refused(tmp_path: Path) -> None:
    conversation = tmp_path / "conversations" / "r1"
    write_archive(
        tmp_path / "a.tar",
        full={"f": b"actual-bytes"},
        entries={"f": {"size": 12, "sha256": sha_of(b"other-bytes")}},
    )
    with pytest.raises(ArchiveError, match="sha256 校验失败"):
        publish_archive(tmp_path / "a.tar", conversation, source_run_id="r1")
    assert not (conversation / "state").exists()


# -- container security ---------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["../escape.txt", "/abs.txt", "files/../escape", "delta/../../out", "other/x"],
)
def test_traversing_or_unknown_members_are_refused(tmp_path: Path, name: str) -> None:
    conversation = tmp_path / "conversations" / "r1"
    write_archive(
        tmp_path / "a.tar", full={"ok.txt": b"fine"}, members={name: b"evil"}
    )
    with pytest.raises(ArchiveError):
        publish_archive(tmp_path / "a.tar", conversation, source_run_id="r1")
    assert not (conversation / "state").exists()


def test_credential_destination_in_the_manifest_refuses_the_archive(
    tmp_path: Path,
) -> None:
    conversation = tmp_path / "conversations" / "r1"
    write_archive(
        tmp_path / "a.tar", full={".dsh/.credentials.yaml": b"version: 1"}
    )
    with pytest.raises(ArchiveError, match="凭据目标"):
        publish_archive(
            tmp_path / "a.tar",
            conversation,
            source_run_id="r1",
            credential_dests=(".dsh/.credentials.yaml",),
        )
    assert not (conversation / "state").exists()


def test_empty_state_archive_publishes_an_empty_root(tmp_path: Path) -> None:
    conversation = tmp_path / "conversations" / "r1"
    write_archive(tmp_path / "a.tar", full={"gone": b"x"})
    publish_archive(tmp_path / "a.tar", conversation, source_run_id="r1")
    write_archive(tmp_path / "a2.tar")
    publish_archive(tmp_path / "a2.tar", conversation, source_run_id="r2")
    assert published_files(conversation / "state") == {}
    assert read_state_manifest(conversation)["sourceRunId"] == "r2"


# -- serve (host → sandbox) ------------------------------------------------------


def test_serve_streams_the_state_without_manifest_or_credentials(
    tmp_path: Path,
) -> None:
    conversation = tmp_path / "conversations" / "r1"
    write_archive(tmp_path / "a.tar", full={"s/a": b"one", "s/b": b"two"})
    publish_archive(tmp_path / "a.tar", conversation, source_run_id="r1")
    grant = ArchiveChannel().issue(
        run_id="r1",
        conversation_dir=conversation,
        credential_dests=("s/b",),
    )
    out = io.BytesIO()
    facts = ArchiveChannel().serve(grant, out)
    with tarfile.open(fileobj=io.BytesIO(out.getvalue())) as tar:
        names = tar.getnames()
        assert names == ["s/a"]
        member = tar.extractfile("s/a")
        assert member is not None and member.read() == b"one"
    assert facts["files"] == 1
    assert facts["bytes"] == 3


# -- the HTTP face on the egress proxy -------------------------------------------


class _Probe:
    def __init__(self, proxy: EgressProxy) -> None:
        self.proxy = proxy

    #: Loopback must never travel through the operator's HTTP(S)_PROXY —
    #: the same rule the egress forwarder applies to its own upstream calls.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, url: str, *, token: str, data: bytes | None = None):
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {token}"},
        )
        return self.opener.open(request, timeout=30)


@pytest.fixture()
def proxy(tmp_path: Path):
    proxy = EgressProxy(model_upstream="https://m.example", search_upstream="https://s.example")
    yield proxy
    proxy.close()


def test_archive_routes_require_an_archive_token(proxy: EgressProxy, tmp_path: Path) -> None:
    conversation = tmp_path / "conversations" / "r1"
    grant = proxy.issue_archive(run_id="r1", conversation_dir=conversation)
    egress_grant = proxy.issue(
        run_id="r1", tenant=None, model_key="mk", model_id="m", search_key="sk"
    )
    assert grant.token.startswith(ARCHIVE_TOKEN_PREFIX)
    url = f"http://127.0.0.1:{proxy.port}/archive/state"
    # An egress token must not move state; an archive token must not forward.
    with pytest.raises(urllib.error.HTTPError) as exc:
        _Probe(proxy).request("POST", url, token=egress_grant.token, data=b"x")
    assert exc.value.code == 401
    with pytest.raises(urllib.error.HTTPError) as exc:
        _Probe(proxy).request(
            "POST", f"http://127.0.0.1:{proxy.port}/model/chat/completions", token=grant.token,
            data=b'{"model":"m"}',
        )
    assert exc.value.code == 401


def test_post_publishes_and_get_serves_over_http(proxy: EgressProxy, tmp_path: Path) -> None:
    conversation = tmp_path / "conversations" / "r1"
    grant = proxy.issue_archive(run_id="r1", conversation_dir=conversation)
    archive = write_archive(tmp_path / "a.tar", full={"s/state": b"hello"})
    url = f"http://127.0.0.1:{proxy.port}/archive/state"
    with _Probe(proxy).request(
        "POST", url, token=grant.token, data=archive.read_bytes()
    ) as response:
        assert response.status == 200
        assert json.loads(response.read())["status"] == "published"
    assert published_files(conversation / "state") == {"s/state": b"hello"}
    with _Probe(proxy).request("GET", url, token=grant.token) as response:
        assert response.headers["Content-Type"] == "application/x-tar"
        with tarfile.open(fileobj=io.BytesIO(response.read())) as tar:
            member = tar.extractfile("s/state")
            assert member is not None and member.read() == b"hello"
    # Revoking the run's only grant stops the shared listener with it: there
    # is no port to answer on afterwards at all (an idle host has no open
    # egress port — ADR 0008's rule, unchanged by the archive capability).
    proxy.revoke_archive(grant.token)
    assert proxy.archive.grant_for(grant.token) is None
    assert proxy.port is None


def test_post_over_the_ceiling_aborts_without_publishing(
    proxy: EgressProxy, tmp_path: Path
) -> None:
    conversation = tmp_path / "conversations" / "r1"
    grant = proxy.issue_archive(run_id="r1", conversation_dir=conversation)
    proxy.archive.max_bytes = 64
    url = f"http://127.0.0.1:{proxy.port}/archive/state"
    with pytest.raises(urllib.error.HTTPError) as exc:
        _Probe(proxy).request("POST", url, token=grant.token, data=b"z" * 65)
    assert exc.value.code == 413
    assert not (conversation / "state").exists()
    # No staging leftovers keep the conversation dirty.
    assert not (conversation / ".archive-staging" / "r1").exists()


def test_archive_grant_keeps_the_listener_alive_without_egress_grants(
    proxy: EgressProxy, tmp_path: Path
) -> None:
    conversation = tmp_path / "conversations" / "r1"
    grant = proxy.issue_archive(run_id="r1", conversation_dir=conversation)
    port = proxy.port
    assert port is not None
    proxy.revoke_archive(grant.token)
    assert proxy.port is None
    grant = proxy.issue_archive(run_id="r2", conversation_dir=conversation)
    # Rebind succeeds (the socket was released synchronously on revoke); the
    # OS need not hand back the same ephemeral port.
    assert proxy.port is not None


# -- malformed manifests, members and transports ----------------------------------


def test_read_state_manifest_treats_garbage_as_absent(tmp_path: Path) -> None:
    conversation = tmp_path / "conversations" / "r1"
    assert read_state_manifest(conversation) is None
    state = conversation / "state"
    state.mkdir(parents=True)
    (state / "manifest.json").write_text("{not json")
    assert read_state_manifest(conversation) is None
    (state / "manifest.json").write_text(json.dumps({"version": 2, "files": {}}))
    assert read_state_manifest(conversation) is None
    (state / "manifest.json").write_text(json.dumps({"version": 1, "files": []}))
    assert read_state_manifest(conversation) is None
    (state / "manifest.json").write_text(
        json.dumps({"version": 1, "files": {"a": {"size": 1, "sha256": "x"}}, "junk": 1})
    )
    manifest = read_state_manifest(conversation)
    assert manifest is not None and manifest["junk"] == 1


@pytest.mark.parametrize(
    "entries",
    [
        {"a/../b": {"size": 1, "sha256": sha_of(b"x")}},
        {"..": {"size": 1, "sha256": sha_of(b"x")}},
        {"a": "not-a-dict"},
        {"a": {"sha256": sha_of(b"x")}},
        {"a": {"size": -1, "sha256": sha_of(b"x")}},
        {"a": {"size": 1, "sha256": "not-a-hash"}},
        {"a": {"size": 4, "sha256": sha_of(b"xxxx"), "base": 5}},
        {"a": {"size": 4, "sha256": sha_of(b"xxxx"), "base": -1}},
        {"a/b\u0000c": {"size": 1, "sha256": sha_of(b"x")}},
    ],
)
def test_malformed_manifest_entries_are_refused(
    tmp_path: Path, entries: dict[str, dict]
) -> None:
    conversation = tmp_path / "conversations" / "r1"
    write_archive(tmp_path / "a.tar", entries=entries, members={"files/a": b"x"})
    with pytest.raises(ArchiveError):
        publish_archive(tmp_path / "a.tar", conversation, source_run_id="r1")
    assert not (conversation / "state").exists()


def test_archive_without_a_manifest_is_refused(tmp_path: Path) -> None:
    conversation = tmp_path / "conversations" / "r1"
    write_archive(tmp_path / "a.tar", members={"files/a": b"x"}, with_manifest=False)
    with pytest.raises(ArchiveError, match="缺少 manifest"):
        publish_archive(tmp_path / "a.tar", conversation, source_run_id="r1")


def test_non_tar_body_is_refused(tmp_path: Path) -> None:
    conversation = tmp_path / "conversations" / "r1"
    path = tmp_path / "a.tar"
    path.write_bytes(b"definitely not a tar" * 10)
    with pytest.raises(ArchiveError, match="合法 tar"):
        publish_archive(path, conversation, source_run_id="r1")


def test_missing_delta_member_is_refused(tmp_path: Path) -> None:
    conversation = tmp_path / "conversations" / "r1"
    write_archive(tmp_path / "a1.tar", full={"s": b"0123456789"})
    publish_archive(tmp_path / "a1.tar", conversation, source_run_id="r1")
    # The manifest claims an appended tail that has no member in the tar.
    write_archive(
        tmp_path / "a2.tar",
        entries={"s": {"size": 15, "sha256": sha_of(b"0123456789-appen"), "base": 10}},
    )
    with pytest.raises(ArchiveError, match="增量成员缺失"):
        publish_archive(tmp_path / "a2.tar", conversation, source_run_id="r2")


def test_missing_base_file_for_a_delta_is_refused(tmp_path: Path) -> None:
    conversation = tmp_path / "conversations" / "r1"
    write_archive(tmp_path / "a.tar", full={"other": b"x"})
    publish_archive(tmp_path / "a.tar", conversation, source_run_id="r1")
    write_archive(
        tmp_path / "a2.tar",
        tails={"missing": (3, b"abc-tail")},
    )
    with pytest.raises(ArchiveError, match="增量基线文件缺失"):
        publish_archive(tmp_path / "a2.tar", conversation, source_run_id="r2")


def test_post_without_a_body_length_is_refused(proxy: EgressProxy, tmp_path: Path) -> None:
    conversation = tmp_path / "conversations" / "r1"
    grant = proxy.issue_archive(run_id="r1", conversation_dir=conversation)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    # POST with an empty body: Content-Length 0 answers bad_length.
    request = urllib.request.Request(
        f"http://127.0.0.1:{proxy.port}/archive/state",
        data=b"",
        method="POST",
        headers={"Authorization": f"Bearer {grant.token}"},
    )
    try:
        with opener.open(request, timeout=30) as response:  # pragma: no cover
            assert response.status == 400
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
    # An unknown archive kind answers 404.
    for path in ("/archive/other", "/archive/", "/archive/state/extra"):
        try:
            with opener.open(
                urllib.request.Request(
                    f"http://127.0.0.1:{proxy.port}{path}",
                    headers={"Authorization": f"Bearer {grant.token}"},
                ),
                timeout=30,
            ):  # pragma: no cover
                pytest.fail(f"{path} should not exist")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404


def test_a_truncated_stream_publishes_nothing(tmp_path: Path) -> None:
    """The receive side aborts on a short body and leaves no state behind."""
    conversation = tmp_path / "conversations" / "r1"
    grant = ArchiveChannel().issue(run_id="r1", conversation_dir=conversation)
    write_archive(tmp_path / "a.tar", full={"s": b"would-be-state"})
    body = io.BytesIO((tmp_path / "a.tar").read_bytes()[:-4])
    with pytest.raises(ArchiveError, match="传输中断"):
        ArchiveChannel().receive(grant, body, length=(tmp_path / "a.tar").stat().st_size)
    assert not (conversation / "state").exists()


def test_member_count_ceiling_is_enforced(tmp_path: Path) -> None:
    conversation = tmp_path / "conversations" / "r1"
    write_archive(tmp_path / "a.tar", full={"s/a": b"x"})
    with pytest.raises(ArchiveError, match="成员数超过上限"):
        publish_archive(
            tmp_path / "a.tar", conversation, source_run_id="r1", max_members=0
        )


def test_missing_state_is_not_a_successful_empty_archive(proxy, tmp_path):
    grant = proxy.issue_archive(run_id="r1", conversation_dir=tmp_path / "conversation")
    out = io.BytesIO()
    with pytest.raises(ArchiveError, match="状态"):
        proxy.archive.serve(grant, out)
    assert out.getvalue() == b""
    with pytest.raises(urllib.error.HTTPError) as exc:
        _Probe(proxy).request("GET", f"http://127.0.0.1:{proxy.port}/archive/state", token=grant.token)
    assert exc.value.code == 404


def test_incremental_publishes_cannot_grow_state_past_the_total_budget(tmp_path: Path) -> None:
    """The reconstructed tree, not each transfer, is held to the state budget.

    原义务：只约束单次归档体（``ARCHIVE_MAX_BYTES``）与成员数；连续小增量可以把
    发布状态重建到超过继承/逐文件回收路径的总量与单文件上限。
    新义务：manifest 描述的完整状态（文件数、单文件、总量）在重建前按同一预算
    校验，超限整次拒绝且旧发布状态保持不变。
    """
    conversation = tmp_path / "conversations" / "r1"
    first = b"a" * 60
    write_archive(tmp_path / "1.tar", full={"s/log": first})
    publish_archive(tmp_path / "1.tar", conversation, source_run_id="r1",
                    max_total_bytes=100, max_file_bytes=100)
    grown = first + b"b" * 60  # the transfer carries 60 bytes, the state is 120
    write_archive(tmp_path / "2.tar", tails={"s/log": (len(first), grown)})
    with pytest.raises(ArchiveError, match="单文件上限|总量上限"):
        publish_archive(tmp_path / "2.tar", conversation, source_run_id="r2",
                        max_total_bytes=100, max_file_bytes=100)
    assert (conversation / "state" / "s" / "log").read_bytes() == first


def test_state_total_budget_counts_every_listed_file(tmp_path: Path) -> None:
    conversation = tmp_path / "conversations" / "r1"
    write_archive(tmp_path / "a.tar", full={"s/a": b"x" * 40, "s/b": b"y" * 40})
    with pytest.raises(ArchiveError, match="总量上限"):
        publish_archive(tmp_path / "a.tar", conversation, source_run_id="r1",
                        max_total_bytes=64, max_file_bytes=64)
    assert not (conversation / "state").exists()


def test_state_file_count_budget_is_enforced(tmp_path: Path) -> None:
    conversation = tmp_path / "conversations" / "r1"
    write_archive(tmp_path / "a.tar", full={"s/a": b"x", "s/b": b"y"})
    with pytest.raises(ArchiveError, match="文件数"):
        publish_archive(tmp_path / "a.tar", conversation, source_run_id="r1", max_state_files=1)


def test_the_default_state_budget_matches_the_inheritance_bounds() -> None:
    from pi_glink.kernel import acp_env, archive_channel

    assert archive_channel.STATE_MAX_FILES == acp_env.INHERIT_MAX_FILES
    assert archive_channel.STATE_MAX_FILE_BYTES == acp_env.INHERIT_MAX_FILE_BYTES
    assert archive_channel.STATE_MAX_TOTAL_BYTES == acp_env.INHERIT_MAX_TOTAL_BYTES
