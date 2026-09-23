"""Multi-tenant identity material: assertion verification and config loading.

These are synthetic-only tests: every secret is generated inside a temporary
directory, no gateway or network is involved, and the host's own configuration
is never read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pi_glink.config import ConfigError, build_config
from pi_glink.tenancy import (
    ASSERTION_HEADER,
    GatewayAuthenticator,
    TenantAuthError,
    TenantCredentialStore,
    TenantNotConfigured,
    build_tenancy,
    issue_assertion,
    read_restricted_file,
    verify_assertion,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SECRET = b"0123456789abcdef0123456789abcdef"
AUDIENCE = "pi-glink-test"


def write_file(tmp_path: Path, name: str, content: bytes, mode: int = 0o600) -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    path.chmod(mode)
    return path


def tenant_document() -> dict:
    return {
        "tenants": {
            "tenant-alpha": {
                "modelApiKey": "sk-alpha-0000000000000000",
                "webSearchKey": "search-alpha-000000000000",
            },
            "tenant-beta": {"modelApiKey": "sk-beta-0000000000000000"},
        }
    }


# -- assertion verification ---------------------------------------------------


def test_assertion_round_trip_yields_the_signed_identity() -> None:
    token = issue_assertion(
        SECRET,
        tenant_id="tenant-alpha",
        user_id="user-1",
        audience=AUDIENCE,
        role="admin",
        ttl_seconds=120,
        request_id="req-1",
    )
    context = verify_assertion(SECRET, token, audience=AUDIENCE)
    assert (context.tenant_id, context.user_id, context.role) == (
        "tenant-alpha",
        "user-1",
        "admin",
    )
    assert context.audience == AUDIENCE
    assert context.request_id == "req-1"
    assert context.is_admin is True
    assert context.expired() is False


def test_tampered_role_or_tenant_fails_the_signature() -> None:
    token = issue_assertion(
        SECRET, tenant_id="tenant-alpha", user_id="user-1", audience=AUDIENCE
    )
    version, payload, signature = token.split(".")
    forged = issue_assertion(
        SECRET, tenant_id="tenant-beta", user_id="user-1", audience=AUDIENCE
    ).split(".")[1]
    with pytest.raises(TenantAuthError, match="signature"):
        verify_assertion(SECRET, f"{version}.{forged}.{signature}", audience=AUDIENCE)


def test_wrong_secret_audience_and_version_are_refused() -> None:
    token = issue_assertion(
        SECRET, tenant_id="tenant-alpha", user_id="user-1", audience=AUDIENCE
    )
    with pytest.raises(TenantAuthError, match="signature"):
        verify_assertion(b"another-secret-another-secret-32b", token, audience=AUDIENCE)
    with pytest.raises(TenantAuthError, match="audience"):
        verify_assertion(SECRET, token, audience="another-audience")
    with pytest.raises(TenantAuthError, match="version"):
        verify_assertion(SECRET, "v2." + token.split(".", 1)[1], audience=AUDIENCE)


def test_expired_and_long_lived_assertions_are_refused() -> None:
    expired = issue_assertion(
        SECRET, tenant_id="tenant-alpha", user_id="user-1", audience=AUDIENCE, ttl_seconds=1
    )
    with pytest.raises(TenantAuthError, match="expired"):
        verify_assertion(SECRET, expired, audience=AUDIENCE, now=10**12)
    # A gateway that hands out a long-lived token violates the short-lived
    # contract, even though the signature is valid.
    long_lived = issue_assertion(
        SECRET,
        tenant_id="tenant-alpha",
        user_id="user-1",
        audience=AUDIENCE,
        ttl_seconds=3600,
    )
    with pytest.raises(TenantAuthError, match="lifetime"):
        verify_assertion(SECRET, long_lived, audience=AUDIENCE)


def test_authenticator_requires_exactly_one_header_value() -> None:
    authenticator = GatewayAuthenticator(SECRET, audience=AUDIENCE)
    token = issue_assertion(
        SECRET, tenant_id="tenant-alpha", user_id="user-1", audience=AUDIENCE
    )
    assert authenticator.authenticate([token]).tenant_id == "tenant-alpha"
    with pytest.raises(TenantAuthError, match="no identity"):
        authenticator.authenticate(None)
    with pytest.raises(TenantAuthError, match="more than one"):
        authenticator.authenticate([token, token])


def test_issue_assertion_rejects_malformed_identities() -> None:
    with pytest.raises(ValueError):
        issue_assertion(SECRET, tenant_id="bad tenant", user_id="user-1", audience=AUDIENCE)
    with pytest.raises(ValueError):
        issue_assertion(SECRET, tenant_id="tenant-alpha", user_id="", audience=AUDIENCE)
    with pytest.raises(ValueError):
        issue_assertion(
            SECRET, tenant_id="tenant-alpha", user_id="user-1", audience=AUDIENCE, role="root"
        )


# -- restricted files ---------------------------------------------------------


def test_secret_file_must_be_owner_only(tmp_path: Path) -> None:
    path = write_file(tmp_path, "gateway.key", SECRET, mode=0o644)
    with pytest.raises(ConfigError, match="权限过宽"):
        read_restricted_file(path, repo_root=REPO_ROOT, what="密钥")
    path.chmod(0o600)
    assert read_restricted_file(path, repo_root=REPO_ROOT, what="密钥") == SECRET


def test_secret_file_must_live_outside_the_repository(tmp_path: Path) -> None:
    inside = REPO_ROOT / "docs" / "does-not-exist.key"
    assert not inside.exists()
    with pytest.raises(ConfigError, match="仓库之外"):
        read_restricted_file(REPO_ROOT / "README.md", repo_root=REPO_ROOT, what="密钥")


def test_secret_file_must_be_a_regular_non_symlink_and_long_enough(tmp_path: Path) -> None:
    target = write_file(tmp_path, "real.key", SECRET)
    link = tmp_path / "link.key"
    link.symlink_to(target)
    with pytest.raises(ConfigError, match="普通文件"):
        read_restricted_file(link, repo_root=REPO_ROOT, what="密钥")
    short = write_file(tmp_path, "short.key", b"tiny")
    with pytest.raises(ConfigError, match="内容过短"):
        read_restricted_file(short, repo_root=REPO_ROOT, what="密钥")


def test_tenant_config_requires_a_model_key_per_tenant(tmp_path: Path) -> None:
    document = tenant_document()
    document["tenants"]["tenant-beta"] = {"webSearchKey": "only-search"}
    path = write_file(tmp_path, "tenants.json", json.dumps(document).encode())
    with pytest.raises(ConfigError, match="modelApiKey"):
        TenantCredentialStore.from_file(path, repo_root=REPO_ROOT)

    path = write_file(
        tmp_path,
        "tenants.json",
        json.dumps(tenant_document()).encode(),
    )
    store = TenantCredentialStore.from_file(path, repo_root=REPO_ROOT)
    alpha = store.require("tenant-alpha")
    assert alpha.model_api_key.startswith("sk-alpha-")
    assert alpha.web_search_key.startswith("search-alpha-")
    beta = store.require("tenant-beta")
    assert beta.web_search_key == ""
    assert store.get("unknown") is None
    with pytest.raises(TenantNotConfigured):
        store.require("unknown")


# -- configuration gating -----------------------------------------------------


def make_values(tmp_path: Path, **overrides: str) -> dict[str, str]:
    secret = write_file(tmp_path, "gateway.key", SECRET)
    tenants = write_file(
        tmp_path, "tenants.json", json.dumps(tenant_document()).encode()
    )
    values = {
        "OPENAI_BASE_URL": "https://example.invalid/v1",
        "OPENAI_MODEL": "test/model",
        "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
        "PI_GLINK_TENANCY": "multitenant",
        "LOCAL_WEB_EXECUTOR": "docker",
        "PI_GLINK_GATEWAY_SECRET_FILE": str(secret),
        "PI_GLINK_TENANT_CONFIG_FILE": str(tenants),
        "PI_GLINK_TENANCY_AUDIENCE": AUDIENCE,
        "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent-web-search.json"),
    }
    values.update(overrides)
    return values


def test_multitenant_requires_loopback_container_face_and_material(tmp_path: Path) -> None:
    config = build_config(make_values(tmp_path), REPO_ROOT)
    assert config.multi_tenant is True
    assert config.binds_loopback is True
    tenancy = build_tenancy(config)
    assert tenancy is not None
    assert tenancy.authenticator.audience == AUDIENCE

    with pytest.raises(ConfigError, match="loopback"):
        build_config(
            make_values(tmp_path, LOCAL_WEB_HOST="0.0.0.0", LOCAL_WEB_ALLOW_REMOTE="1"),
            REPO_ROOT,
        )
    with pytest.raises(ConfigError, match="Docker/E2B"):
        build_config(make_values(tmp_path, LOCAL_WEB_EXECUTOR="inproc"), REPO_ROOT)
    with pytest.raises(ConfigError, match="PI_GLINK_GATEWAY_SECRET_FILE"):
        values = make_values(tmp_path)
        del values["PI_GLINK_GATEWAY_SECRET_FILE"]
        build_config(values, REPO_ROOT)
    with pytest.raises(ConfigError, match="PI_GLINK_TENANCY_AUDIENCE"):
        values = make_values(tmp_path)
        del values["PI_GLINK_TENANCY_AUDIENCE"]
        build_config(values, REPO_ROOT)
    with pytest.raises(ConfigError, match="PI_GLINK_TENANCY"):
        build_config(make_values(tmp_path, PI_GLINK_TENANCY="open"), REPO_ROOT)


def test_multitenant_startup_refuses_a_world_readable_key_file(tmp_path: Path) -> None:
    config = build_config(make_values(tmp_path), REPO_ROOT)
    Path(config.gateway_secret_file).chmod(0o644)
    with pytest.raises(ConfigError, match="权限过宽"):
        build_tenancy(config)


def test_local_mode_ignores_the_multitenant_material(tmp_path: Path) -> None:
    config = build_config(
        {
            "OPENAI_MODEL": "test/model",
            "LOCAL_WEB_DATA_ROOT": str(tmp_path / "data"),
            "PI_GLINK_WEB_SEARCH_CONFIG": str(tmp_path / "absent-web-search.json"),
        },
        REPO_ROOT,
    )
    assert config.multi_tenant is False
    assert build_tenancy(config) is None
    assert config.public_view()["multiTenant"] is False


def test_assertion_header_name_is_part_of_the_contract() -> None:
    assert ASSERTION_HEADER == "X-Pi-Glink-Identity"
