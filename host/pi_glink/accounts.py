"""邀请制账号、邀请与会话的 SQLite 存储。

这是 ``PI_GLINK_AUTH_MODE=accounts`` 的持久身份层：

* 只使用 Python 标准库（``sqlite3``，WAL），单宿主、单文件；文件位于仓库外的
  ``data_root`` 下，与 run/events 的文件存储互不迁移、互不共享；
* 建表只覆盖最小必要模式：``tenants`` / ``users`` / ``invitations`` / ``sessions``；
  邮箱全局唯一（canonical 列唯一索引），一个用户只属于一个租户，角色只有
  ``member`` / ``admin``；
* 账号库及 WAL/SHM 侧车必须是属主独占（文件 0600、专用目录 0700）：库在创建前
  先以受限权限建立，已有宽权限/符号链接一律 fail-closed，宿主从不自动 chmod 真实
  文件或目录（操作员自行迁移与收紧）；
* 邀请码与会话令牌都只存不可逆摘要（``sha256``）；单次原文只在创建成功时返回给
  受权操作者（操作员 CLI 或管理员接口），普通日志、错误信息与数据库中都不出现；
* 密码用 ``hashlib.scrypt``（随机盐），每行记录参数，校验走
  ``hmac.compare_digest``；未知账号与错误密码执行同量的散列工作，避免时序区分；
* 会话同时有绝对有效期与空闲有效期，停用用户/租户、注销都会让会话立即失效；
* 限速器是有界内存里的固定窗口计数，键由调用方给出（邮箱、客户端地址、邀请摘要）。

所有写入串行化在一把可重入锁内，并通过 ``BEGIN IMMEDIATE`` 事务保证
“一次性邀请只创建一个账号”这类跨语句不变式。这里不做多宿主租约：一个
data_root 仍由其宿主进程独占（``pi_glink.ownership``）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

#: 密码散列参数默认值；每行记录自己的参数，将来升级不影响旧账号。
DEFAULT_SCRYPT: dict[str, int | str] = {
    "alg": "scrypt",
    "n": 2**14,
    "r": 8,
    "p": 1,
    "dklen": 32,
}
#: 读回旧参数时的下限保护：低于该值的记录不参与校验（防止伪造弱参数行）。
MIN_SCRYPT_N = 2**10
MIN_PASSWORD_CHARS = 10
MAX_PASSWORD_CHARS = 200
MAX_EMAIL_CHARS = 254
MAX_NAME_CHARS = 64
MIN_TOKEN_BYTES = 24
DEFAULT_INVITE_SECONDS = 48 * 3600
TENANT_ACTIVE = "active"
TENANT_DISABLED = "disabled"
USER_ACTIVE = "active"
USER_DISABLED = "disabled"
ROLE_MEMBER = "member"
ROLE_ADMIN = "admin"
ROLES = frozenset({ROLE_MEMBER, ROLE_ADMIN})
_STATUSES = frozenset({TENANT_ACTIVE, TENANT_DISABLED})
#: 校验成功后把 ``last_seen_at`` 前移的最小间隔，避免每个 SSE 轮询都写库。
TOUCH_INTERVAL_SECONDS = 60.0

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]{1,64}@"
    r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)+$"
)
_DUMMY_SALT = b"\x00" * 16
_DUMMY_EQUALIZER = b"pi-glink-unknown-account-timing-equalizer"


class AccountsError(Exception):
    """A domain refusal carrying a stable machine code for the HTTP layer."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class AccountsSecurityError(AccountsError):
    """The account database (or its directory) is not owner-only.

    Raised *before* any read or migration, so an unsafe file is left exactly as
    it was found: the operator decides whether to move and ``chmod`` it.
    """

    def __init__(self, message: str) -> None:
        super().__init__("insecure_database", message)


def _require_owner_only_directory(directory: Path) -> None:
    """Create the database directory 0700, or refuse an unsafe existing one.

    Existing directories are validated, never ``chmod``-ed: a shared parent may
    belong to other data, and silently tightening it would be a surprise outside
    this component's ownership.
    """
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        pass
    except FileNotFoundError:
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode):
        raise AccountsSecurityError(
            f"账号库目录必须是普通目录（不接受符号链接/文件/设备）: {directory}"
        )
    if info.st_uid != os.geteuid():
        raise AccountsSecurityError(
            f"账号库目录不属于当前用户（uid={info.st_uid}）: {directory}"
        )
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise AccountsSecurityError(
            f"账号库目录权限过宽（要求 0700 或更严，当前 {mode:04o}）: {directory}；"
            "请把账号库移到仅属主可访问的目录（宿主不会自动修改已有目录）"
        )


def _prepare_database(path: Path) -> tuple[int, int]:
    """Create the database owner-only or refuse an unsafe existing file.

    The file is created with ``O_CREAT|O_EXCL|O_NOFOLLOW`` and mode 0600 before
    SQLite opens it, so the first byte SQLite ever writes already lands in a
    private file; an existing file must be a regular, owner-owned, 0600-or-tighter
    file. The returned ``(st_dev, st_ino)`` is checked again after ``connect`` so
    a replacement between validation and open is refused.
    """
    _require_owner_only_directory(path.parent)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        descriptor = os.open(
            path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        os.close(descriptor)
        info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise AccountsSecurityError(
            f"账号库必须是普通文件（不接受符号链接/目录/设备）: {path}"
        )
    if info.st_uid != os.geteuid():
        raise AccountsSecurityError(f"账号库不属于当前用户（uid={info.st_uid}）: {path}")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o077:
        raise AccountsSecurityError(
            f"账号库权限过宽（要求 0600 或更严，当前 {mode:04o}）: {path}；请在宿主停止时"
            "把库移到仅属主可访问的目录并 chmod 600（宿主不会自动修改已有文件）"
        )
    return info.st_dev, info.st_ino


def _check_sidecars(path: Path) -> None:
    """Require owner-only WAL/SHM/journal sidecars.

    SQLite creates these with the database file's mode, so they should already
    be 0600. If one is wider (a legacy database, an unusual umask, a crash), it
    is corrected with ``chmod`` — it is a host-created derivative of our own
    database, not a user file, and it can carry committed pages.
    """
    for suffix in ("-wal", "-shm", "-journal"):
        candidate = Path(f"{path}{suffix}")
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise AccountsSecurityError(
                f"账号库侧车必须是当前用户拥有的普通文件: {candidate}"
            )
        if stat.S_IMODE(info.st_mode) & 0o077:
            try:
                os.chmod(candidate, 0o600)
            except OSError as exc:
                raise AccountsSecurityError(
                    f"账号库侧车权限过宽且无法收紧（{candidate}）: {exc}"
                ) from exc
            if stat.S_IMODE(os.lstat(candidate).st_mode) & 0o077:
                raise AccountsSecurityError(f"账号库侧车权限仍过宽: {candidate}")


@dataclass(frozen=True, slots=True)
class TenantRecord:
    tenant_id: str
    name: str
    status: str
    created_at: float


@dataclass(frozen=True, slots=True)
class UserRecord:
    user_id: str
    tenant_id: str
    email: str
    email_canonical: str
    name: str
    role: str
    status: str
    created_at: float

    @property
    def active(self) -> bool:
        return self.status == USER_ACTIVE


@dataclass(frozen=True, slots=True)
class InvitationRecord:
    invite_id: str
    tenant_id: str
    email: str
    role: str
    created_at: float
    expires_at: float
    created_by: str | None = None
    used_at: float | None = None
    used_by: str | None = None


@dataclass(frozen=True, slots=True)
class SessionRecord:
    session_digest: str
    csrf_token: str
    user_id: str
    tenant_id: str
    created_at: float
    last_seen_at: float
    expires_at: float
    revoked_at: float | None = None
    user_agent: str = ""


@dataclass(frozen=True, slots=True)
class SessionContext:
    """A live session joined with the identity it belongs to."""

    session: SessionRecord
    user: UserRecord
    tenant: TenantRecord


def normalize_email(value: Any) -> str | None:
    """Canonical form used for uniqueness; ``None`` when the input is not an email."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    if len(candidate) > MAX_EMAIL_CHARS or not _EMAIL_RE.match(candidate):
        return None
    return candidate


def _validate_password(password: Any) -> str:
    if not isinstance(password, str):
        raise AccountsError("invalid_password", "密码必须是字符串")
    if len(password) < MIN_PASSWORD_CHARS or len(password) > MAX_PASSWORD_CHARS:
        raise AccountsError(
            "invalid_password", f"密码长度必须在 {MIN_PASSWORD_CHARS}–{MAX_PASSWORD_CHARS} 之间"
        )
    return password


def _validate_name(name: Any) -> str:
    if not isinstance(name, str):
        raise AccountsError("invalid_name", "姓名必须是字符串")
    candidate = name.strip()
    if not candidate or len(candidate) > MAX_NAME_CHARS:
        raise AccountsError("invalid_name", f"姓名长度必须在 1–{MAX_NAME_CHARS} 之间")
    return candidate


def _validate_role(role: Any) -> str:
    if not isinstance(role, str) or role not in ROLES:
        raise AccountsError("invalid_role", "角色必须是 member 或 admin")
    return role


def _scrypt(password: bytes, salt: bytes, params: Mapping[str, Any]) -> bytes:
    """The one place scrypt is called; tests count invocations through it."""
    try:
        n = int(params["n"])
        r = int(params["r"])
        p = int(params["p"])
        dklen = int(params["dklen"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AccountsError("invalid_password", "密码散列参数不可用") from exc
    if n < MIN_SCRYPT_N or n & (n - 1) or r < 1 or p < 1 or dklen < 16:
        raise AccountsError("invalid_password", "密码散列参数超出允许范围")
    return hashlib.scrypt(password, salt=salt, n=n, r=r, p=p, dklen=dklen)


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token(bytes_count: int = 32) -> str:
    return secrets.token_urlsafe(bytes_count)


class RateLimiter:
    """有界固定窗口计数器（进程内）。

    键是调用方定义的字符串（规范化邮箱、客户端地址、邀请摘要……）。窗口内计数
    超过 ``limit`` 即拒绝；条目数超过 ``max_entries`` 时先清理过期窗口，再按窗口
    起点淘汰最旧的键，因此内存占用有硬上限。
    """

    def __init__(self, *, limit: int, window: float, max_entries: int = 512) -> None:
        self.limit = max(1, int(limit))
        self.window = max(1.0, float(window))
        self.max_entries = max(1, int(max_entries))
        self._buckets: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        if len(self._buckets) <= self.max_entries:
            return
        expired = [key for key, (start, _count) in self._buckets.items() if now - start >= self.window]
        for key in expired:
            del self._buckets[key]
        while len(self._buckets) > self.max_entries:
            oldest = min(self._buckets.items(), key=lambda item: item[1][0])[0]
            del self._buckets[oldest]

    def allow(self, key: str, *, now: float | None = None) -> bool:
        stamp = time.time() if now is None else float(now)
        with self._lock:
            self._prune(stamp)
            start, count = self._buckets.get(key, (stamp, 0))
            if stamp - start >= self.window:
                start, count = stamp, 0
            count += 1
            self._buckets[key] = (start, count)
            return count <= self.limit

    def retry_after(self, key: str, *, now: float | None = None) -> float:
        stamp = time.time() if now is None else float(now)
        with self._lock:
            entry = self._buckets.get(key)
            if entry is None:
                return 0.0
            start, count = entry
            if count <= self.limit:
                return 0.0
            return max(0.0, self.window - (stamp - start))

    def reset(self, key: str) -> None:
        with self._lock:
            self._buckets.pop(key, None)

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._buckets)


class AccountsStore:
    """Thread-safe account storage rooted at one SQLite file."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
        scrypt_params: Mapping[str, Any] | None = None,
    ) -> None:
        self.path = Path(path)
        self._clock = clock
        self._params: dict[str, Any] = dict(scrypt_params or DEFAULT_SCRYPT)
        self._lock = threading.RLock()
        # Owner-only creation/validation happens before SQLite sees the path:
        # the database, its directory and later its WAL sidecars must never be
        # group/world readable, and a legacy wide-permission file is refused
        # untouched (see docs/multitenancy-plan.md §3.1).
        self._identity = _prepare_database(self.path)
        connection = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None, timeout=5.0
        )
        self._connection = connection
        try:
            opened = os.lstat(self.path)
            if (opened.st_dev, opened.st_ino) != self._identity:
                raise AccountsSecurityError(
                    f"账号库在检查与打开之间被替换: {self.path}"
                )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            self._migrate()
            _check_sidecars(self.path)
        except BaseException:
            connection.close()
            raise

    # -- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _now(self) -> float:
        return float(self._clock())

    def now(self) -> float:
        """The store's clock reading (callers use this instead of reaching in)."""
        return self._now()

    def _migrate(self) -> None:
        with self._lock:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                  key TEXT PRIMARY KEY,
                  value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tenants (
                  tenant_id TEXT PRIMARY KEY,
                  name TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'active',
                  created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS users (
                  user_id TEXT PRIMARY KEY,
                  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
                  email TEXT NOT NULL,
                  email_canonical TEXT NOT NULL UNIQUE,
                  name TEXT NOT NULL,
                  role TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'active',
                  password_salt BLOB NOT NULL,
                  password_hash BLOB NOT NULL,
                  password_params TEXT NOT NULL,
                  created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS invitations (
                  invite_id TEXT PRIMARY KEY,
                  token_digest TEXT NOT NULL UNIQUE,
                  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
                  email TEXT NOT NULL,
                  email_canonical TEXT NOT NULL,
                  role TEXT NOT NULL,
                  created_at REAL NOT NULL,
                  expires_at REAL NOT NULL,
                  created_by TEXT,
                  used_at REAL,
                  used_by TEXT
                );
                CREATE TABLE IF NOT EXISTS sessions (
                  session_digest TEXT PRIMARY KEY,
                  csrf_token TEXT NOT NULL,
                  user_id TEXT NOT NULL REFERENCES users(user_id),
                  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
                  created_at REAL NOT NULL,
                  last_seen_at REAL NOT NULL,
                  expires_at REAL NOT NULL,
                  revoked_at REAL,
                  user_agent TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS invitations_tenant
                  ON invitations(tenant_id, created_at);
                CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
                """
            )
            self._connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', '1')"
            )

    def _begin(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self._connection.execute("COMMIT")

    def _rollback(self) -> None:
        try:
            self._connection.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass

    # -- tenants --------------------------------------------------------------

    def upsert_tenant(self, tenant_id: str, name: str) -> TenantRecord:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise AccountsError("invalid_tenant", "租户 id 不能为空")
        display = _validate_name(name)
        with self._lock:
            now = self._now()
            self._connection.execute(
                """
                INSERT INTO tenants(tenant_id, name, status, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tenant_id) DO UPDATE SET name = excluded.name
                """,
                (tenant_id, display, TENANT_ACTIVE, now),
            )
            record = self.get_tenant(tenant_id)
        assert record is not None
        return record

    def get_tenant(self, tenant_id: str) -> TenantRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM tenants WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
        return _tenant(row) if row is not None else None

    def set_tenant_status(self, tenant_id: str, status: str) -> bool:
        if status not in _STATUSES:
            raise AccountsError("invalid_status", "租户状态非法")
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE tenants SET status = ? WHERE tenant_id = ?", (status, tenant_id)
            )
            return cursor.rowcount > 0

    # -- users ----------------------------------------------------------------

    def create_user(
        self, *, tenant_id: str, email: Any, name: Any, password: Any, role: Any
    ) -> UserRecord:
        canonical = normalize_email(email)
        if canonical is None:
            raise AccountsError("invalid_email", "邮箱格式不正确")
        display = _validate_name(name)
        secret = _validate_password(password)
        validated_role = _validate_role(role)
        with self._lock:
            return self._insert_user(
                tenant_id=tenant_id,
                email=str(email).strip(),
                canonical=canonical,
                name=display,
                password=secret,
                role=validated_role,
            )

    def _insert_user(
        self, *, tenant_id: str, email: str, canonical: str, name: str, password: str, role: str
    ) -> UserRecord:
        tenant = self.get_tenant(tenant_id)
        if tenant is None:
            raise AccountsError("unknown_tenant", "租户不存在")
        if tenant.status != TENANT_ACTIVE:
            raise AccountsError("tenant_disabled", "租户已停用")
        salt = secrets.token_bytes(16)
        digest = _scrypt(password.encode("utf-8"), salt, self._params)
        params_json = json.dumps(self._params, sort_keys=True)
        user_id = _new_token(12)
        now = self._now()
        try:
            self._connection.execute(
                """
                INSERT INTO users(
                  user_id, tenant_id, email, email_canonical, name, role, status,
                  password_salt, password_hash, password_params, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    tenant_id,
                    email,
                    canonical,
                    name,
                    role,
                    USER_ACTIVE,
                    salt,
                    digest,
                    params_json,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise AccountsError("email_in_use", "该邮箱已经注册") from exc
        return UserRecord(
            user_id=user_id,
            tenant_id=tenant_id,
            email=email,
            email_canonical=canonical,
            name=name,
            role=role,
            status=USER_ACTIVE,
            created_at=now,
        )

    def _find_user_row(self, canonical: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT * FROM users WHERE email_canonical = ?", (canonical,)
        ).fetchone()

    def find_user_by_email(self, email: Any) -> UserRecord | None:
        canonical = normalize_email(email)
        if canonical is None:
            return None
        with self._lock:
            row = self._find_user_row(canonical)
        return _user(row) if row is not None else None

    def get_user(self, user_id: str) -> UserRecord | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return _user(row) if row is not None else None

    def list_users(self, tenant_id: str) -> list[UserRecord]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM users WHERE tenant_id = ? ORDER BY created_at", (tenant_id,)
            ).fetchall()
        return [_user(row) for row in rows]

    def set_user_status(self, user_id: str, status: str) -> bool:
        if status not in _STATUSES:
            raise AccountsError("invalid_status", "用户状态非法")
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE users SET status = ? WHERE user_id = ?", (status, user_id)
            )
            return cursor.rowcount > 0

    def verify_credentials(self, email: Any, password: Any) -> UserRecord | None:
        """Return the matching user (whatever its status) or ``None``.

        Unknown accounts and wrong passwords do exactly the same scrypt work and
        return the same ``None``: the caller cannot leak which one it was, and
        timing cannot distinguish them.
        """
        candidate = password if isinstance(password, str) else ""
        # A password longer than the accepted maximum can never match a stored
        # one; hash a bounded prefix anyway so the work stays comparable.
        bounded = candidate[:MAX_PASSWORD_CHARS].encode("utf-8", "replace")
        canonical = normalize_email(email)
        with self._lock:
            if canonical is None:
                _scrypt(_DUMMY_EQUALIZER, _DUMMY_SALT, self._params)
                return None
            row = self._find_user_row(canonical)
            if row is None:
                _scrypt(bounded, _DUMMY_SALT, self._params)
                return None
            try:
                params = json.loads(row["password_params"])
            except (TypeError, json.JSONDecodeError):
                params = self._params
            computed = _scrypt(bounded, bytes(row["password_salt"]), params)
            if not hmac.compare_digest(computed, bytes(row["password_hash"])):
                return None
            return _user(row)

    # -- invitations ----------------------------------------------------------

    def create_invitation(
        self,
        *,
        tenant_id: str,
        email: Any,
        role: Any,
        created_by: str | None = None,
        ttl_seconds: int | None = None,
    ) -> tuple[InvitationRecord, str]:
        canonical = normalize_email(email)
        if canonical is None:
            raise AccountsError("invalid_email", "邮箱格式不正确")
        validated_role = _validate_role(role)
        lifetime = int(ttl_seconds if ttl_seconds is not None else DEFAULT_INVITE_SECONDS)
        if lifetime <= 0 or lifetime > 30 * 24 * 3600:
            raise AccountsError("invalid_ttl", "邀请有效期超出允许范围")
        with self._lock:
            tenant = self.get_tenant(tenant_id)
            if tenant is None:
                raise AccountsError("unknown_tenant", "租户不存在")
            if tenant.status != TENANT_ACTIVE:
                raise AccountsError("tenant_disabled", "租户已停用")
            if self._find_user_row(canonical) is not None:
                raise AccountsError("email_in_use", "该邮箱已经注册")
            token = _new_token(32)
            now = self._now()
            invite_id = _new_token(12)
            self._connection.execute(
                """
                INSERT INTO invitations(
                  invite_id, token_digest, tenant_id, email, email_canonical, role,
                  created_at, expires_at, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    invite_id,
                    _token_digest(token),
                    tenant_id,
                    str(email).strip(),
                    canonical,
                    validated_role,
                    now,
                    now + lifetime,
                    created_by,
                ),
            )
            record = InvitationRecord(
                invite_id=invite_id,
                tenant_id=tenant_id,
                email=str(email).strip(),
                role=validated_role,
                created_at=now,
                expires_at=now + lifetime,
                created_by=created_by,
            )
        return record, token

    def inspect_invitation(self, token: Any) -> InvitationRecord | None:
        """The invitation a token names, regardless of used/expired state."""
        if not isinstance(token, str) or not token or len(token) > 512:
            return None
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM invitations WHERE token_digest = ?", (_token_digest(token),)
            ).fetchone()
        return _invitation(row) if row is not None else None

    def invitation_usable(self, token: Any) -> bool:
        record = self.inspect_invitation(token)
        if record is None or record.used_at is not None:
            return False
        return record.expires_at > self._now()

    def register(self, *, token: Any, name: Any, password: Any) -> UserRecord:
        """Consume an invitation and create its account — in one transaction.

        The invitation is fetched, the account inserted and the invitation
        marked used inside one ``BEGIN IMMEDIATE`` transaction, so two
        concurrent registrations with the same code cannot both succeed.
        """
        display = _validate_name(name)
        secret = _validate_password(password)
        if not isinstance(token, str) or not token or len(token) > 512:
            raise AccountsError("invitation_invalid", "邀请无效或已使用")
        digest = _token_digest(token)
        with self._lock:
            self._begin()
            try:
                row = self._connection.execute(
                    "SELECT * FROM invitations WHERE token_digest = ?", (digest,)
                ).fetchone()
                if row is None or row["used_at"] is not None:
                    raise AccountsError("invitation_invalid", "邀请无效或已使用")
                record = _invitation(row)
                if record.expires_at <= self._now():
                    raise AccountsError("invitation_invalid", "邀请无效或已使用")
                tenant = self.get_tenant(record.tenant_id)
                if tenant is None or tenant.status != TENANT_ACTIVE:
                    raise AccountsError("tenant_disabled", "租户已停用")
                if self._find_user_row(_canonical_of(record)) is not None:
                    raise AccountsError("email_in_use", "该邮箱已经注册")
                user = self._insert_user(
                    tenant_id=record.tenant_id,
                    email=record.email,
                    canonical=_canonical_of(record),
                    name=display,
                    password=secret,
                    role=record.role,
                )
                cursor = self._connection.execute(
                    """
                    UPDATE invitations SET used_at = ?, used_by = ?
                    WHERE invite_id = ? AND used_at IS NULL
                    """,
                    (self._now(), user.user_id, record.invite_id),
                )
                if cursor.rowcount != 1:
                    raise AccountsError("invitation_invalid", "邀请无效或已使用")
                self._commit()
            except BaseException:
                self._rollback()
                raise
        return user

    # -- sessions -------------------------------------------------------------

    def create_session(
        self,
        *,
        user_id: str,
        absolute_seconds: int,
        idle_seconds: int,
        user_agent: str = "",
    ) -> tuple[str, str, SessionRecord]:
        absolute = max(60, int(absolute_seconds))
        idle = max(30, int(idle_seconds))
        with self._lock:
            user = self.get_user(user_id)
            if user is None:
                raise AccountsError("unknown_user", "用户不存在")
            tenant = self.get_tenant(user.tenant_id)
            if tenant is None or tenant.status != TENANT_ACTIVE or not user.active:
                raise AccountsError("account_disabled", "账号已停用")
            token = _new_token(32)
            csrf = _new_token(24)
            now = self._now()
            record = SessionRecord(
                session_digest=_token_digest(token),
                csrf_token=csrf,
                user_id=user.user_id,
                tenant_id=user.tenant_id,
                created_at=now,
                last_seen_at=now,
                expires_at=now + absolute,
                user_agent=str(user_agent or "")[:256],
            )
            self._connection.execute(
                """
                INSERT INTO sessions(
                  session_digest, csrf_token, user_id, tenant_id, created_at,
                  last_seen_at, expires_at, user_agent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.session_digest,
                    record.csrf_token,
                    record.user_id,
                    record.tenant_id,
                    record.created_at,
                    record.last_seen_at,
                    record.expires_at,
                    record.user_agent,
                ),
            )
        return token, csrf, record

    def _session_row(self, digest: str) -> sqlite3.Row | None:
        return self._connection.execute(
            """
            SELECT s.*, u.tenant_id AS user_tenant_id, u.email AS user_email,
                   u.email_canonical AS user_email_canonical, u.name AS user_name,
                   u.role AS user_role, u.status AS user_status,
                   u.created_at AS user_created_at,
                   t.name AS tenant_name, t.status AS tenant_status,
                   t.created_at AS tenant_created_at
            FROM sessions s
            JOIN users u ON u.user_id = s.user_id
            JOIN tenants t ON t.tenant_id = u.tenant_id
            WHERE s.session_digest = ?
            """,
            (digest,),
        ).fetchone()

    def _context_from_row(self, row: sqlite3.Row, idle_seconds: int, now: float) -> SessionContext | None:
        session = SessionRecord(
            session_digest=row["session_digest"],
            csrf_token=row["csrf_token"],
            user_id=row["user_id"],
            tenant_id=row["tenant_id"],
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
            user_agent=row["user_agent"],
        )
        user = UserRecord(
            user_id=row["user_id"],
            tenant_id=row["user_tenant_id"],
            email=row["user_email"],
            email_canonical=row["user_email_canonical"],
            name=row["user_name"],
            role=row["user_role"],
            status=row["user_status"],
            created_at=row["user_created_at"],
        )
        tenant = TenantRecord(
            tenant_id=user.tenant_id,
            name=row["tenant_name"],
            status=row["tenant_status"],
            created_at=row["tenant_created_at"],
        )
        if session.revoked_at is not None:
            return None
        if session.expires_at <= now:
            return None
        if now - session.last_seen_at > max(1, int(idle_seconds)):
            return None
        if user.status != USER_ACTIVE or tenant.status != TENANT_ACTIVE:
            return None
        return SessionContext(session=session, user=user, tenant=tenant)

    def authenticate_session(self, token: Any, *, idle_seconds: int) -> SessionContext | None:
        """Validate a session token and slide its idle window forward."""
        if not isinstance(token, str) or not token or len(token) > 512:
            return None
        digest = _token_digest(token)
        now = self._now()
        with self._lock:
            row = self._session_row(digest)
            if row is None:
                return None
            context = self._context_from_row(row, idle_seconds, now)
            if context is None:
                return None
            if now - context.session.last_seen_at >= TOUCH_INTERVAL_SECONDS:
                self._connection.execute(
                    "UPDATE sessions SET last_seen_at = ? WHERE session_digest = ?",
                    (now, digest),
                )
                context = SessionContext(
                    session=SessionRecord(
                        **{**_asdict(context.session), "last_seen_at": now}
                    ),
                    user=context.user,
                    tenant=context.tenant,
                )
            return context

    def session_active(self, session_digest: str, *, idle_seconds: int) -> bool:
        """Cheap read-only liveness check used between SSE polls."""
        now = self._now()
        with self._lock:
            row = self._session_row(session_digest)
            return row is not None and self._context_from_row(row, idle_seconds, now) is not None

    def refresh_session(self, session_digest: str, *, idle_seconds: int) -> bool:
        """Liveness check that also renews the idle window (throttled writes).

        Used by long-lived SSE streams: an actively streaming session is
        activity, while true inactivity still expires it. The ``last_seen_at``
        write happens at most once per :data:`TOUCH_INTERVAL_SECONDS`.
        """
        now = self._now()
        with self._lock:
            row = self._session_row(session_digest)
            if row is None:
                return False
            context = self._context_from_row(row, idle_seconds, now)
            if context is None:
                return False
            if now - context.session.last_seen_at >= TOUCH_INTERVAL_SECONDS:
                self._connection.execute(
                    "UPDATE sessions SET last_seen_at = ? WHERE session_digest = ?",
                    (now, session_digest),
                )
            return True

    def session_record(self, session_digest: str) -> SessionRecord | None:
        with self._lock:
            row = self._session_row(session_digest)
        return (
            SessionRecord(
                session_digest=row["session_digest"],
                csrf_token=row["csrf_token"],
                user_id=row["user_id"],
                tenant_id=row["tenant_id"],
                created_at=row["created_at"],
                last_seen_at=row["last_seen_at"],
                expires_at=row["expires_at"],
                revoked_at=row["revoked_at"],
                user_agent=row["user_agent"],
            )
            if row is not None
            else None
        )

    def revoke_session(self, token: Any) -> bool:
        if not isinstance(token, str) or not token:
            return False
        with self._lock:
            cursor = self._connection.execute(
                "UPDATE sessions SET revoked_at = ? WHERE session_digest = ? AND revoked_at IS NULL",
                (self._now(), _token_digest(token)),
            )
            return cursor.rowcount > 0

    def csrf_token(self, session_digest: str) -> str | None:
        """The session's CSRF token.

        Unlike the session token, the CSRF token is stored as-is on purpose: it
        is a *double-submit* value that only matters together with the cookie,
        and ``/api/me`` must be able to hand the same value back on every page
        load (two tabs must not invalidate each other). It is never logged.
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT csrf_token FROM sessions WHERE session_digest = ?", (session_digest,)
            ).fetchone()
        return str(row["csrf_token"]) if row is not None else None

    def csrf_matches(self, session_digest: str, candidate: Any) -> bool:
        if not isinstance(candidate, str) or not candidate:
            return False
        stored = self.csrf_token(session_digest)
        return stored is not None and hmac.compare_digest(stored, candidate)

    def purge_expired_sessions(self) -> int:
        """Housekeeping: delete sessions that can no longer authenticate."""
        now = self._now()
        with self._lock:
            cursor = self._connection.execute(
                "DELETE FROM sessions WHERE expires_at <= ?", (now,)
            )
            return cursor.rowcount


def _canonical_of(record: InvitationRecord) -> str:
    canonical = normalize_email(record.email)
    assert canonical is not None
    return canonical


def _asdict(record: SessionRecord) -> dict[str, Any]:
    return {
        "session_digest": record.session_digest,
        "csrf_token": record.csrf_token,
        "user_id": record.user_id,
        "tenant_id": record.tenant_id,
        "created_at": record.created_at,
        "last_seen_at": record.last_seen_at,
        "expires_at": record.expires_at,
        "revoked_at": record.revoked_at,
        "user_agent": record.user_agent,
    }


def _tenant(row: sqlite3.Row) -> TenantRecord:
    return TenantRecord(
        tenant_id=row["tenant_id"],
        name=row["name"],
        status=row["status"],
        created_at=float(row["created_at"]),
    )


def _user(row: sqlite3.Row) -> UserRecord:
    return UserRecord(
        user_id=row["user_id"],
        tenant_id=row["tenant_id"],
        email=row["email"],
        email_canonical=row["email_canonical"],
        name=row["name"],
        role=row["role"],
        status=row["status"],
        created_at=float(row["created_at"]),
    )


def _invitation(row: sqlite3.Row) -> InvitationRecord:
    return InvitationRecord(
        invite_id=row["invite_id"],
        tenant_id=row["tenant_id"],
        email=row["email"],
        role=row["role"],
        created_at=float(row["created_at"]),
        expires_at=float(row["expires_at"]),
        created_by=row["created_by"],
        used_at=float(row["used_at"]) if row["used_at"] is not None else None,
        used_by=row["used_by"],
    )
