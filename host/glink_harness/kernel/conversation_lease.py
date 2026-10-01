"""The conversation sandbox lease: the host-side record of a paused sandbox.

阶段 2（docs/adr/0020）的台账事实。宿主持有对话级权威状态（阶段 1，
docs/adr/0018）；一个**暂停**的 E2B 沙箱只是这份状态的热缓存：它保存着上一轮
run 结束时的状态根与工作区，让下一次追问可以省掉「新沙箱 + 回灌」的冷路径。
本模块只承载台账的**数据与文件事实**——

* :data:`LEASE_FILENAME`（``conversations/<对话根>/lease.json``，与 ``state/``
  平级）：谁（sandbox id）、属哪个对话、状态代数（generation）、何时暂停、
  何时到期，以及待销毁的被替换沙箱（``retiredSandboxIds``）；
* 原子读写（0600 + ``os.replace``），失败即失败，不吞成「没有租约」；
* TTL 常量：闲置租约的默认寿命与硬上限（配置见
  :attr:`~glink_harness.config.HostConfig.conversation_lease_ttl_seconds`）。

沙箱侧操作（写代数标记、清理凭据副本、pause、销毁兜底）在
:mod:`glink_harness.kernel.acp_sandbox_park`；对账销毁在
:mod:`glink_harness.runs.resource_ledger`。generation 的语义：每轮 run 的回收发布
成功后 +1，租约与沙箱内标记文件成对携带——追问只有在两侧代数一致时才走
热路径（防沙箱被外部替换/重置后宿主仍当作热缓存）。
"""

from __future__ import annotations

from glink_harness.contracts import conversation_guard

import json
import logging
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger("glink_harness.kernel.conversation_lease")

#: The lease record's filename, beside the conversation's ``state/`` directory.
LEASE_FILENAME = "lease.json"
#: The lease record's schema version.
LEASE_VERSION = 1

#: 闲置租约的默认寿命（秒）。正常追问间隔是分钟级；2 小时覆盖「午休后回来
#: 继续问」的节奏，同时让一台不再被使用的对话沙箱在同一天内被对账销毁。
DEFAULT_LEASE_TTL_SECONDS = 2 * 60 * 60
#: 闲置租约寿命的硬上限（秒）。对齐 E2B Pro 档 ``set_timeout`` 的 24h 上限量级
#: （paused 沙箱的保留期与计费模型在 SDK 内无文档——docs/adr/0020 未覆盖项），
#: 防止把热缓存配置成无限期的云端占用。
MAX_LEASE_TTL_SECONDS = 24 * 60 * 60


class LeaseError(RuntimeError):
    """A lease record could not be read or written honestly."""


class LeaseCorruptError(LeaseError):
    """The lease file exists but cannot be read as an honest record.

    A corrupt record is **not** "no lease": the paused sandbox it named is
    still real and billing somewhere, and deleting the file would silently
    orphan it (the exact fail-closed hole this type closes). Callers treat a
    corrupt record as an unknown state — park/candidate paths degrade to the
    cold path, the sweep logs and keeps the file, and the delete-conversation
    link refuses to remove the directory while one is unreadable.
    """


@dataclass(frozen=True, slots=True)
class ConversationLease:
    """One paused sandbox held for a conversation's next follow-up.

    ``generation`` is the state algebra the sandbox carries: the recovery that
    published authoritative state round ``run_id`` also wrote this number into
    the sandbox's marker file, so a follow-up may take the hot path only when
    the lease and the sandbox marker agree on it. ``retired_sandboxIds`` names
    sandboxes this lease replaced but could not destroy — the reconciliation
    sweep retries them (fail-closed: they stay on the books until confirmed
    gone, never silently forgotten).
    """

    conversation_run: str
    sandbox_id: str
    generation: int
    template: str
    pack: str
    run_id: str
    paused_at: float
    expires_at: float
    retired_sandbox_ids: tuple[str, ...] = ()

    def is_expired(self, now: float | None = None) -> bool:
        """True once the idle lease's TTL has passed."""
        return (now if now is not None else time.time()) >= self.expires_at

    def to_json(self) -> dict[str, Any]:
        """The durable record's JSON shape (sorted keys, no secrets)."""
        return {
            "version": LEASE_VERSION,
            "conversationRun": self.conversation_run,
            "sandboxId": self.sandbox_id,
            "generation": self.generation,
            "template": self.template,
            "pack": self.pack,
            "runId": self.run_id,
            "pausedAt": self.paused_at,
            "expiresAt": self.expires_at,
            "retiredSandboxIds": list(self.retired_sandbox_ids),
        }

    def payload(self) -> dict[str, Any]:
        """The run-record view of the lease (the terminal frame's ``cleanup``)."""
        payload: dict[str, Any] = {
            "parked": True,
            "sandbox": self.sandbox_id,
            "generation": self.generation,
            "expiresAt": self.expires_at,
        }
        if self.retired_sandbox_ids:
            payload["retiredSandboxes"] = list(self.retired_sandbox_ids)
        return payload


def read_lease(conversation_dir: Path) -> ConversationLease | None:
    """The conversation's lease, or ``None`` when there is none to resume.

    "None" means exactly one thing: no lease file exists. A file that exists
    but cannot be parsed raises :class:`LeaseCorruptError` — treating that as
    "no lease" would silently orphan the paused sandbox it named (and drop
    its ``retiredSandboxIds`` from the books), so the callers keep the file
    and the unknown state fail-closed.
    """
    with conversation_guard(conversation_dir):
        return peek_lease(conversation_dir)


def peek_lease(conversation_dir: Path) -> ConversationLease | None:
    """:func:`read_lease` without taking the conversation's guard.

    For host-wide counting only (the park cap): the caller already holds its
    *own* conversation's guard and must never wait on another conversation's
    (that one may be held across a slow cloud destroy, and waiting on it from
    inside a guard orders two locks arbitrarily). The record is replaced with
    ``os.replace``, so an unguarded read sees either version, never a torn one.
    """
    path = Path(conversation_dir) / LEASE_FILENAME
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LeaseCorruptError(f"对话租约不可读: {type(exc).__name__}: {exc}") from exc
    try:
        raw = json.loads(raw_text)
    except ValueError as exc:
        raise LeaseCorruptError(f"对话租约不是合法 JSON: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("version") != LEASE_VERSION:
        raise LeaseCorruptError("对话租约版本或结构不认识")
    lease = _lease_from_mapping(raw)
    if lease is None:
        raise LeaseCorruptError("对话租约字段校验失败")
    return lease


def write_lease(conversation_dir: Path, lease: ConversationLease) -> None:
    """Atomically write the lease record (0600, ``os.replace``)."""
    with conversation_guard(conversation_dir):
        directory = Path(conversation_dir)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = directory / f".{LEASE_FILENAME}.staging"
        payload = json.dumps(lease.to_json(), ensure_ascii=False, sort_keys=True)
        try:
            descriptor = os.open(
                staging,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                os.write(descriptor, payload.encode("utf-8") + b"\n")
            finally:
                os.close(descriptor)
            os.replace(staging, directory / LEASE_FILENAME)
        except OSError as exc:
            raise LeaseError(f"对话租约写入失败: {type(exc).__name__}: {exc}") from exc


def remove_lease(conversation_dir: Path) -> None:
    """Delete the lease record (best-effort; the sweep re-derives the truth)."""
    with conversation_guard(conversation_dir):
        try:
            (Path(conversation_dir) / LEASE_FILENAME).unlink(missing_ok=True)
        except OSError:
            pass


def settle_lease_after_destroy(conversation_dir: Path) -> bool:
    """Clear the lease after its parked sandbox was confirmed destroyed.

    The record goes away only when nothing is left on the books: a lease with
    pending ``retiredSandboxIds`` is rewritten as expired-now (the destroyed
    sandbox stays as the probe-idempotent target alongside the retired list)
    so the very next sweep destroys the rest and then removes the file —
    destroying one sandbox must never drop the others from the ledger.
    Returns whether the record is fully settled (gone).
    """
    with conversation_guard(conversation_dir):
        lease = read_lease(conversation_dir)
        if lease is None:
            return True
        if not lease.retired_sandbox_ids:
            remove_lease(conversation_dir)
            return True
        write_lease(conversation_dir, replace(lease, expires_at=time.time()))
        return False


def expire_lease_now(conversation_dir: Path) -> ConversationLease | None:
    """Mark the lease as expired so the next sweep destroys the sandbox.

    Used when a live sandbox could not be destroyed on the spot (the hot path
    degraded but the kill failed): the record stays — fail-closed, the sandbox
    is not forgotten — with an expiry that makes the very next reconciliation
    tick retry the destroy.
    """
    with conversation_guard(conversation_dir):
        try:
            lease = read_lease(conversation_dir)
        except LeaseCorruptError as exc:
            # 损坏的租约改写不了，也不删除：保留原文并显式告警，沙箱处置交给
            # 操作员（fail-closed：不把不可读当作无租约静默放过）。
            logger.warning("对话租约不可读，保留待人工处置: %s", exc)
            return None
        if lease is None:
            return None
        expired = replace(lease, expires_at=time.time())
        try:
            write_lease(conversation_dir, expired)
        except LeaseError:
            return lease
        return expired


def _lease_from_mapping(raw: Mapping[str, Any]) -> ConversationLease | None:
    """Validate one parsed record into a lease; ``None`` when unusable."""
    texts: dict[str, str] = {}
    for key in ("conversationRun", "sandboxId", "template", "pack", "runId"):
        value = raw.get(key)
        if not isinstance(value, str) or not value:
            return None
        texts[key] = value
    numbers: dict[str, float] = {}
    for key in ("generation", "pausedAt", "expiresAt"):
        value = raw.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        numbers[key] = float(value)
    if numbers["generation"] < 1 or numbers["expiresAt"] < numbers["pausedAt"]:
        return None
    retired = raw.get("retiredSandboxIds")
    if not isinstance(retired, list) or any(
        not isinstance(item, str) or not item for item in retired
    ):
        # 遗忘待销毁沙箱就是静默孤儿（对账再也没有它的名字）：整个记录按损坏拒绝。
        return None
    return ConversationLease(
        conversation_run=texts["conversationRun"],
        sandbox_id=texts["sandboxId"],
        generation=int(numbers["generation"]),
        template=texts["template"],
        pack=texts["pack"],
        run_id=texts["runId"],
        paused_at=numbers["pausedAt"],
        expires_at=numbers["expiresAt"],
        retired_sandbox_ids=tuple(retired),
    )
