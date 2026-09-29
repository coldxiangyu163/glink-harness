"""Accounts store: invitations, users, password hashing, sessions, throttling.

These are synthetic-only tests over a temporary SQLite file. No host, no HTTP,
no real data root, no pi process: the domain rules (one-time invitations,
global email uniqueness, scrypt parameters, session expiry/revocation, bounded
rate limits) are proven here, and the HTTP contract is proven in
``tests/test_accounts_api.py``.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pi_glink import accounts
from pi_glink.accounts import (
    DEFAULT_SCRYPT,
    MIN_PASSWORD_CHARS,
    TENANT_ACTIVE,
    TENANT_DISABLED,
    USER_ACTIVE,
    USER_DISABLED,
    AccountsError,
    AccountsStore,
    RateLimiter,
    normalize_email,
)

PASSWORD = "correct horse battery"


class Clock:
    """A monotonic test clock the store reads instead of ``time.time``."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def store(tmp_path: Path, clock: Clock):
    # Lighter scrypt for the many synthetic hashes; the default parameter set is
    # asserted separately in ``test_default_scrypt_parameters_are_strong``.
    instance = AccountsStore(
        tmp_path / "accounts.db",
        clock=clock,
        scrypt_params={"alg": "scrypt", "n": 2**11, "r": 8, "p": 1, "dklen": 32},
    )
    try:
        yield instance
    finally:
        instance.close()


def seed_tenant(store: AccountsStore, tenant_id: str = "tenant-alpha", name: str = "组织甲"):
    return store.upsert_tenant(tenant_id, name)


def seed_user(
    store: AccountsStore,
    *,
    tenant_id: str = "tenant-alpha",
    email: str = "member@example.test",
    name: str = "成员",
    role: str = "member",
    password: str = PASSWORD,
):
    seed_tenant(store, tenant_id)
    return store.create_user(
        tenant_id=tenant_id, email=email, name=name, role=role, password=password
    )


def raw_rows(store: AccountsStore, table: str) -> list[dict]:
    """Read the underlying rows to prove what is persisted, not what is returned."""
    connection = sqlite3.connect(store.path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]
    finally:
        connection.close()


# -- schema and storage separation --------------------------------------------


def test_store_uses_wal_and_its_own_tables(tmp_path: Path, clock: Clock) -> None:
    path = tmp_path / "data" / "accounts.db"
    instance = AccountsStore(path, clock=clock)
    try:
        assert path.is_file()
        connection = sqlite3.connect(path)
        try:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()
        assert mode.lower() == "wal"
        assert {"tenants", "users", "invitations", "sessions"} <= tables
    finally:
        instance.close()


def test_default_scrypt_parameters_are_strong() -> None:
    assert DEFAULT_SCRYPT["alg"] == "scrypt"
    assert DEFAULT_SCRYPT["n"] >= 2**14
    assert DEFAULT_SCRYPT["r"] >= 8
    assert DEFAULT_SCRYPT["p"] >= 1
    assert DEFAULT_SCRYPT["dklen"] >= 32


def test_tenant_round_trip_and_status(store: AccountsStore) -> None:
    tenant = seed_tenant(store, "tenant-alpha", "组织甲")
    assert tenant.name == "组织甲"
    assert tenant.status == TENANT_ACTIVE
    assert store.get_tenant("tenant-alpha").name == "组织甲"
    assert store.set_tenant_status("tenant-alpha", TENANT_DISABLED) is True
    assert store.get_tenant("tenant-alpha").status == TENANT_DISABLED
    assert store.set_tenant_status("absent", TENANT_DISABLED) is False


# -- password hashing ---------------------------------------------------------


def test_password_is_hashed_with_recorded_scrypt_parameters(store: AccountsStore) -> None:
    user = seed_user(store, email="Member@Example.Test", password=PASSWORD)
    rows = raw_rows(store, "users")
    assert len(rows) == 1
    row = rows[0]
    assert row["email_canonical"] == "member@example.test"
    serialized = json.dumps(
        {key: (value.decode("latin1") if isinstance(value, bytes) else value) for key, value in row.items()}
    )
    assert PASSWORD not in serialized
    assert isinstance(row["password_salt"], bytes) and len(row["password_salt"]) >= 16
    assert isinstance(row["password_hash"], bytes) and len(row["password_hash"]) >= 32
    params = json.loads(row["password_params"])
    assert params["alg"] == "scrypt"
    assert params["n"] >= 2**11
    assert store.verify_credentials("member@example.test", PASSWORD).user_id == user.user_id
    assert store.verify_credentials("member@example.test", "wrong password!!") is None


def test_same_password_hashes_differently_per_user(store: AccountsStore) -> None:
    seed_user(store, email="a@example.test")
    seed_user(store, email="b@example.test")
    rows = raw_rows(store, "users")
    salts = {row["password_salt"] for row in rows}
    hashes = {row["password_hash"] for row in rows}
    assert len(salts) == 2
    assert len(hashes) == 2


def test_unknown_account_and_wrong_password_do_the_same_work(
    store: AccountsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Login must not be able to tell "no such user" from "wrong password"."""
    calls = {"count": 0}
    original = accounts._scrypt

    def counting(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(accounts, "_scrypt", counting)
    seed_user(store, email="known@example.test")
    calls["count"] = 0
    assert store.verify_credentials("absent@example.test", PASSWORD) is None
    unknown_calls = calls["count"]
    calls["count"] = 0
    assert store.verify_credentials("known@example.test", "definitely wrong!") is None
    wrong_calls = calls["count"]
    assert unknown_calls == wrong_calls == 1


def test_emails_are_canonical_and_globally_unique(store: AccountsStore) -> None:
    seed_user(store, tenant_id="tenant-alpha", email="First@Example.test")
    seed_tenant(store, "tenant-beta", "组织乙")
    with pytest.raises(AccountsError) as excinfo:
        store.create_user(
            tenant_id="tenant-beta",
            email="  first@example.test  ",
            name="另一人",
            role="member",
            password=PASSWORD,
        )
    assert excinfo.value.code == "email_in_use"


@pytest.mark.parametrize(
    "email",
    ["", "no-at-sign", "a@b", "two@@example.test", "white space@example.test", "x" * 300 + "@example.test"],
)
def test_malformed_emails_are_refused(store: AccountsStore, email: str) -> None:
    seed_tenant(store)
    with pytest.raises(AccountsError) as excinfo:
        store.create_user(
            tenant_id="tenant-alpha", email=email, name="成员", role="member", password=PASSWORD
        )
    assert excinfo.value.code == "invalid_email"


def test_short_passwords_are_refused_and_bounded(store: AccountsStore) -> None:
    seed_tenant(store)
    for password in ("", "short", "x" * (MIN_PASSWORD_CHARS - 1), "y" * 5001):
        with pytest.raises(AccountsError) as excinfo:
            store.create_user(
                tenant_id="tenant-alpha",
                email=f"user{len(password)}@example.test",
                name="成员",
                role="member",
                password=password,
            )
        assert excinfo.value.code == "invalid_password"


def test_role_must_be_member_or_admin(store: AccountsStore) -> None:
    seed_tenant(store)
    with pytest.raises(AccountsError) as excinfo:
        store.create_user(
            tenant_id="tenant-alpha",
            email="owner@example.test",
            name="成员",
            role="owner",
            password=PASSWORD,
        )
    assert excinfo.value.code == "invalid_role"


def test_user_cannot_be_created_in_an_unknown_or_disabled_tenant(store: AccountsStore) -> None:
    with pytest.raises(AccountsError) as excinfo:
        store.create_user(
            tenant_id="absent",
            email="a@example.test",
            name="成员",
            role="member",
            password=PASSWORD,
        )
    assert excinfo.value.code == "unknown_tenant"
    seed_tenant(store)
    store.set_tenant_status("tenant-alpha", TENANT_DISABLED)
    with pytest.raises(AccountsError) as excinfo:
        store.create_user(
            tenant_id="tenant-alpha",
            email="a@example.test",
            name="成员",
            role="member",
            password=PASSWORD,
        )
    assert excinfo.value.code == "tenant_disabled"


def test_normalize_email() -> None:
    assert normalize_email("  A@Example.TEST ") == "a@example.test"
    assert normalize_email("bad") is None
    assert normalize_email(None) is None


# -- invitations --------------------------------------------------------------


def test_invitation_token_is_stored_only_as_a_digest(store: AccountsStore) -> None:
    seed_tenant(store)
    record, token = store.create_invitation(
        tenant_id="tenant-alpha", email="invitee@example.test", role="member"
    )
    assert len(token) >= 32
    document = json.dumps(raw_rows(store, "invitations"))
    assert token not in document
    assert record.email == "invitee@example.test"
    assert record.role == "member"
    assert store.inspect_invitation(token).invite_id == record.invite_id
    # The digest itself is not a bearer credential.
    digest = accounts._token_digest(token)
    assert store.inspect_invitation(digest) is None
    assert store.inspect_invitation("made-up-token") is None


def test_invitation_is_bound_to_tenant_email_and_role(store: AccountsStore) -> None:
    seed_tenant(store, "tenant-alpha", "组织甲")
    record, token = store.create_invitation(
        tenant_id="tenant-alpha", email="invitee@example.test", role="admin"
    )
    assert record.tenant_id == "tenant-alpha"
    assert record.expires_at > record.created_at
    # Registration ignores any client-supplied email/tenant/role: it consumes
    # exactly what the invitation says.
    user = store.register(token=token, name="受邀者", password=PASSWORD)
    assert user.email == "invitee@example.test"
    assert user.tenant_id == "tenant-alpha"
    assert user.role == "admin"


def test_invitation_targeting_an_existing_email_is_refused(store: AccountsStore) -> None:
    seed_user(store, email="taken@example.test")
    with pytest.raises(AccountsError) as excinfo:
        store.create_invitation(
            tenant_id="tenant-alpha", email="Taken@Example.test", role="member"
        )
    assert excinfo.value.code == "email_in_use"


def test_invitation_is_one_time(store: AccountsStore) -> None:
    seed_tenant(store)
    _record, token = store.create_invitation(
        tenant_id="tenant-alpha", email="invitee@example.test", role="member"
    )
    store.register(token=token, name="受邀者", password=PASSWORD)
    with pytest.raises(AccountsError) as excinfo:
        store.register(token=token, name="另一个人", password=PASSWORD)
    assert excinfo.value.code == "invitation_invalid"
    assert len(raw_rows(store, "users")) == 1


def test_invitation_race_creates_exactly_one_account(store: AccountsStore) -> None:
    seed_tenant(store)
    _record, token = store.create_invitation(
        tenant_id="tenant-alpha", email="race@example.test", role="member"
    )
    started = threading.Barrier(4)  # fire all attempts together, then let the store serialise

    def attempt(index: int) -> bool:
        started.wait()
        try:
            store.register(token=token, name=f"竞态{index}", password=PASSWORD)
            return True
        except AccountsError as exc:
            assert exc.code == "invitation_invalid"
            return False

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    assert sum(results) == 1
    assert len(raw_rows(store, "users")) == 1


def test_expired_invitation_is_refused(store: AccountsStore, clock: Clock) -> None:
    seed_tenant(store)
    _record, token = store.create_invitation(
        tenant_id="tenant-alpha", email="late@example.test", role="member", ttl_seconds=60
    )
    clock.advance(61)
    with pytest.raises(AccountsError) as excinfo:
        store.register(token=token, name="迟到者", password=PASSWORD)
    assert excinfo.value.code == "invitation_invalid"


def test_registration_does_not_create_a_session(store: AccountsStore) -> None:
    seed_tenant(store)
    _record, token = store.create_invitation(
        tenant_id="tenant-alpha", email="fresh@example.test", role="member"
    )
    store.register(token=token, name="新账号", password=PASSWORD)
    assert raw_rows(store, "sessions") == []


def test_disabled_tenant_blocks_registration(store: AccountsStore) -> None:
    seed_tenant(store)
    _record, token = store.create_invitation(
        tenant_id="tenant-alpha", email="late@example.test", role="member"
    )
    store.set_tenant_status("tenant-alpha", TENANT_DISABLED)
    with pytest.raises(AccountsError) as excinfo:
        store.register(token=token, name="迟到者", password=PASSWORD)
    assert excinfo.value.code == "tenant_disabled"


def test_invitation_requires_a_known_tenant_and_known_role(store: AccountsStore) -> None:
    with pytest.raises(AccountsError) as excinfo:
        store.create_invitation(tenant_id="absent", email="a@example.test", role="member")
    assert excinfo.value.code == "unknown_tenant"
    seed_tenant(store)
    with pytest.raises(AccountsError) as excinfo:
        store.create_invitation(tenant_id="tenant-alpha", email="a@example.test", role="owner")
    assert excinfo.value.code == "invalid_role"


# -- sessions -----------------------------------------------------------------


def test_session_token_is_stored_only_as_a_digest(store: AccountsStore) -> None:
    user = seed_user(store)
    token, csrf, record = store.create_session(
        user_id=user.user_id, absolute_seconds=3600
    )
    document = json.dumps(raw_rows(store, "sessions"))
    assert token not in document
    assert record.session_digest == accounts._token_digest(token)
    assert len(token) >= 32 and len(csrf) >= 24
    # The CSRF token is a double-submit value, deliberately recoverable so
    # ``/api/me`` can hand the same one back on every page load.
    assert store.csrf_token(record.session_digest) == csrf
    assert store.csrf_matches(record.session_digest, csrf) is True
    assert store.csrf_matches(record.session_digest, "wrong-csrf") is False


def test_session_authenticates_and_resolves_identity(store: AccountsStore) -> None:
    seed_user(store, name="成员", role="member")
    user = store.find_user_by_email("member@example.test")
    token, _csrf, _record = store.create_session(
        user_id=user.user_id, absolute_seconds=3600
    )
    context = store.authenticate_session(token, idle_seconds=600)
    assert context is not None
    assert context.user.user_id == user.user_id
    assert context.tenant.tenant_id == "tenant-alpha"
    assert context.tenant.name == "组织甲"
    assert store.authenticate_session("not-a-session", idle_seconds=600) is None


def test_session_idle_and_absolute_expiry(store: AccountsStore, clock: Clock) -> None:
    user = seed_user(store)
    token, _csrf, _record = store.create_session(
        user_id=user.user_id, absolute_seconds=3600
    )
    clock.advance(601)
    assert store.authenticate_session(token, idle_seconds=600) is None

    token2, _csrf2, _record2 = store.create_session(
        user_id=user.user_id, absolute_seconds=3600
    )
    for _ in range(6):
        clock.advance(500)
        assert store.authenticate_session(token2, idle_seconds=600) is not None
    clock.advance(1200)  # idle alive, absolute deadline passed
    assert store.authenticate_session(token2, idle_seconds=600) is None


def test_session_revocation_and_disabled_identities(store: AccountsStore) -> None:
    user = seed_user(store)
    token, _csrf, record = store.create_session(
        user_id=user.user_id, absolute_seconds=3600
    )
    assert store.session_active(record.session_digest, idle_seconds=600) is True
    assert store.revoke_session(token) is True
    assert store.session_active(record.session_digest, idle_seconds=600) is False
    assert store.authenticate_session(token, idle_seconds=600) is None
    assert store.revoke_session(token) is False

    token2, _csrf2, record2 = store.create_session(
        user_id=user.user_id, absolute_seconds=3600
    )
    store.set_user_status(user.user_id, USER_DISABLED)
    assert store.session_active(record2.session_digest, idle_seconds=600) is False
    assert store.authenticate_session(token2, idle_seconds=600) is None
    store.set_user_status(user.user_id, USER_ACTIVE)

    token3, _csrf3, record3 = store.create_session(
        user_id=user.user_id, absolute_seconds=3600
    )
    store.set_tenant_status("tenant-alpha", TENANT_DISABLED)
    assert store.session_active(record3.session_digest, idle_seconds=600) is False


def test_verify_credentials_returns_disabled_users_for_the_caller_to_judge(
    store: AccountsStore,
) -> None:
    user = seed_user(store)
    store.set_user_status(user.user_id, USER_DISABLED)
    found = store.verify_credentials("member@example.test", PASSWORD)
    assert found is not None and found.status == USER_DISABLED


def test_session_writes_are_thread_safe(store: AccountsStore) -> None:
    seed_user(store)
    user = store.find_user_by_email("member@example.test")

    def create(_index: int) -> str:
        token, _csrf, _record = store.create_session(
            user_id=user.user_id, absolute_seconds=3600
        )
        return token

    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(create, range(16)))
    assert len(set(tokens)) == 16
    assert all(store.authenticate_session(token, idle_seconds=600) is not None for token in tokens)


# -- rate limiting ------------------------------------------------------------


def test_rate_limiter_enforces_limit_within_window() -> None:
    limiter = RateLimiter(limit=3, window=60.0)
    now = 1000.0
    assert [limiter.allow("a@example.test", now=now) for _ in range(3)] == [True, True, True]
    assert limiter.allow("a@example.test", now=now) is False
    assert limiter.allow("other@example.test", now=now) is True
    assert limiter.retry_after("a@example.test", now=now) > 0
    assert limiter.allow("a@example.test", now=now + 61) is True


def test_rate_limiter_is_bounded_and_forgets_old_keys() -> None:
    limiter = RateLimiter(limit=1, window=10.0, max_entries=32)
    for index in range(200):
        limiter.allow(f"key-{index}", now=1000.0 + index)
    assert limiter.size <= 32


def test_rate_limiter_reset_clears_one_key() -> None:
    limiter = RateLimiter(limit=1, window=60.0)
    assert limiter.allow("key", now=0.0) is True
    assert limiter.allow("key", now=0.0) is False
    limiter.reset("key")
    assert limiter.allow("key", now=0.0) is True
