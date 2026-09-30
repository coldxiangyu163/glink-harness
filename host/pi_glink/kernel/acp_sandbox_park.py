"""Park a finished run's sandbox for the conversation's next follow-up.

阶段 2（docs/adr/0020）的收尾编排：一轮 run 的权威状态回收成功之后，它的
E2B 沙箱不必销毁——**泊车**（pause）之后它就是这个对话状态的热缓存，下一次
追问可以省掉「新沙箱 + 回灌」。本模块只做沙箱侧的事实，不决定何时泊车
（那由 :class:`~pi_glink.kernel.acp_execution.AcpExecutionAdapter` 的收尾
依据回收是否成功决定）：

* :func:`remove_sandbox_credential_copies` — 凭据不随沙箱持久化（ADR
  0015/0016 的泊车形态：凭据每 run 渲染，销毁路径靠沙箱消亡，泊车路径必须
  显式删掉沙箱内副本，追问重新交付）；
* :func:`write_generation_marker` / :func:`read_generation_marker` — 状态
  代数标记（沙箱侧的宿主可读事实；内存快照不承载它，必须落盘），放在状态
  根**外**（回收打包器与回灌都不碰它）；
* :func:`park_sandbox` — 一次完整泊车：算代数、销毁被替换的旧沙箱、清凭据、
  写标记、写租约、pause。任一步失败都销毁沙箱（冷路径语义），不留孤儿。

pause 用 ``keep_memory=False``：热路径的价值在文件系统亲和（状态根与工作区
原样），不在进程存活——agent 的 stdio 无法跨 pause 重连，追问反正要新起
agent 进程再 ``session/resume``；无内存快照则恢复必是磁盘冷启动，杜绝「宿主
已停 agent，快照却复活了旧进程」的双写风险。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import HostConfig
from .acp_e2b_env import RECLAIM_CLEANUP_COMMAND
from .conversation_lease import (
    ConversationLease,
    LeaseCorruptError,
    read_lease,
    write_lease,
)
from .e2b_env import SANDBOX_HOME, SandboxError, is_file_not_found, kill_within_budget

logger = logging.getLogger("pi_glink.kernel.acp_sandbox_park")

#: Where the state-generation marker lives inside the sandbox: below the
#: sandbox's home but **outside** the ACP state root (``/home/user/acp/home``),
#: so neither the reclaim packer nor a backfill ever carries it.
GENERATION_MARKER_PATH = f"{SANDBOX_HOME}/.pi-glink-state-generation"
#: The marker file's schema version.
GENERATION_MARKER_VERSION = 1


@dataclass(frozen=True, slots=True)
class ParkOutcome:
    """One parking attempt's verdict.

    ``ok`` means the sandbox is paused and the lease is on disk — the run's
    resource responsibility ends here (the lease, not the run, now owns the
    sandbox). A failure carries ``reason`` and the sandbox is **destroyed**
    (the cold-path semantics, 阶段 2 前 kill 的行为) unless the destroy itself
    failed, which ``destroyed`` reports honestly.
    """

    ok: bool
    reason: str = ""
    lease: ConversationLease | None = None
    destroyed: bool = True


def write_generation_marker(
    handle: Any, *, conversation_run: str, generation: int
) -> None:
    """Write the sandbox-side state-generation marker (host-authored)."""
    payload = json.dumps(
        {
            "version": GENERATION_MARKER_VERSION,
            "conversation": conversation_run,
            "generation": generation,
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    try:
        handle.files.write(GENERATION_MARKER_PATH, payload)
    except Exception as exc:  # noqa: BLE001 - surfaced as a parking failure
        raise SandboxError(
            f"写入沙箱状态代数标记失败: {type(exc).__name__}: {exc}"
        ) from exc


def read_generation_marker(handle: Any) -> tuple[str, int] | None:
    """The sandbox's ``(conversation, generation)`` marker, or ``None``.

    ``None`` covers every way a sandbox can fail to present the fact the lease
    expects — no marker (a sandbox that was never parked, or reset), an
    unreadable file, a payload that is not the host's own shape. The caller
    treats it as a mismatch and takes the cold path.
    """
    try:
        data = handle.files.read(GENERATION_MARKER_PATH)
    except Exception:  # noqa: BLE001 - absence decides, never raises
        return None
    if isinstance(data, bytes):
        raw_bytes = data
    elif isinstance(data, str):
        raw_bytes = data.encode("utf-8")
    else:
        return None
    try:
        document = json.loads(raw_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(document, dict) or document.get("version") != GENERATION_MARKER_VERSION:
        return None
    conversation = document.get("conversation")
    generation = document.get("generation")
    if not isinstance(conversation, str) or not conversation:
        return None
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        return None
    return conversation, generation


def remove_sandbox_credential_copies(handle: Any, dests: tuple[str, ...]) -> None:
    """Delete the run's rendered credential copies out of the sandbox.

    The destroy path never needed this (the sandbox died with the copies);
    parking keeps the sandbox alive, so the plaintext copies must not persist
    with it — the next run re-renders its own (``upload_home``). Only the known
    relative destinations are removed, never a pattern; a path that is already
    gone is fine. Any other failure is a parking failure: without this step the
    paused sandbox would durably hold credential material the run's boundary
    was built to keep per-run.
    """
    from .acp_e2b_env import SANDBOX_ACP_HOME

    for relative in dests:
        path = f"{SANDBOX_ACP_HOME}/{relative}"
        try:
            handle.files.remove(path)
        except Exception as exc:  # noqa: BLE001 - surfaced as a parking failure
            if is_file_not_found(exc):
                continue
            raise SandboxError(
                f"删除沙箱内凭据副本失败 {relative}: {type(exc).__name__}: {exc}"
            ) from exc


def park_sandbox(
    handle: Any,
    *,
    config: HostConfig,
    conversation_dir: Path,
    conversation_run: str,
    sandbox_id: str,
    template: str,
    pack: str,
    run_id: str,
    credential_dests: tuple[str, ...],
    kill_by_id: Any | None = None,
    now: float | None = None,
) -> ParkOutcome:
    """Park one recovered sandbox as the conversation's hot cache.

    Preconditions the caller owns: the state recovery **succeeded and this run
    published it** (the lease's generation must describe authoritative state,
    not a sandbox the host failed to reclaim), and the agent process is already
    stopped (the client closed before the face teardown reaches here).

    The order is the safety contract: credentials leave the sandbox and the
    marker lands **before** the pause (a paused sandbox accepts no writes), the
    lease lands **before** the pause (a crash between them leaves a lease whose
    sandbox may still be running — which the hot path handles correctly, since
    ``connect`` accepts a running sandbox — instead of a paused sandbox no record
    knows about). A failure at any step destroys the sandbox (cold-path
    semantics); a destroy that itself fails is reported, never papered over.
    """
    ttl = int(config.conversation_lease_ttl_seconds)
    if ttl <= 0:
        # 配置禁用热路径：直接销毁（阶段 2 之前的行为）。
        return _destroy_outcome(handle, "租约 TTL 为 0，热路径已禁用")
    current = None
    try:
        current = read_lease(conversation_dir)
    except LeaseCorruptError as exc:
        # 不可读租约下泊车无法确认旧沙箱归属与挂账名单：不泊车，销毁降级
        # （冷路径语义，宿主权威状态不受影响），损坏文件保留待人工处置。
        logger.warning("对话租约不可读，泊车降级为销毁 run=%s: %s", run_id, exc)
        return _destroy_outcome(handle, f"对话租约不可读，泊车降级: {exc}")
    retired = list(current.retired_sandbox_ids) if current is not None else []
    if current is not None and current.sandbox_id != sandbox_id:
        # 本沙箱替换了旧租约的沙箱：销毁旧的；失败则挂账给对账重试。
        if kill_by_id is not None:
            destroyed, detail = kill_by_id(current.sandbox_id)
            if not destroyed:
                retired.append(current.sandbox_id)
                logger.warning(
                    "旧租约沙箱销毁失败，已挂账对账 run=%s sandbox=%s: %s",
                    run_id,
                    current.sandbox_id,
                    detail,
                )
        else:  # pragma: no cover - production always injects the killer
            retired.append(current.sandbox_id)
    generation = (current.generation + 1) if current is not None else 1
    try:
        remove_sandbox_credential_copies(handle, credential_dests)
        write_generation_marker(handle, conversation_run=conversation_run, generation=generation)
    except SandboxError as exc:
        return _destroy_outcome(handle, str(exc))
    lease = ConversationLease(
        conversation_run=conversation_run,
        sandbox_id=sandbox_id,
        generation=generation,
        template=template,
        pack=pack,
        run_id=run_id,
        paused_at=now if now is not None else time.time(),
        expires_at=(now if now is not None else time.time()) + ttl,
        retired_sandbox_ids=tuple(retired),
    )
    try:
        write_lease(conversation_dir, lease)
    except Exception as exc:  # noqa: BLE001 - destroy, the failure is a fact
        return _destroy_outcome(handle, f"租约写入失败: {type(exc).__name__}: {exc}")
    try:
        # Nothing run-scoped may survive into the parked sandbox (cc 终审
        # S2): the archive token and every transfer scratch file are removed
        # right before the pause — best-effort, a failed cleanup only logs.
        try:
            handle.commands.run(RECLAIM_CLEANUP_COMMAND, timeout=30)
        except Exception:  # noqa: BLE001 - cleanup never fails the park
            logger.warning("泊车前清理沙箱 scratch 失败（忽略）", exc_info=True)
        # ``False`` = already paused (idempotent), still a parked sandbox.
        handle.pause(keep_memory=False)
    except Exception as exc:  # noqa: BLE001 - destroy, the failure is a fact
        from .conversation_lease import remove_lease

        remove_lease(conversation_dir)
        return _destroy_outcome(handle, f"沙箱 pause 失败: {type(exc).__name__}: {exc}")
    return ParkOutcome(ok=True, lease=lease)


def destroy_sandbox(handle: Any) -> bool:
    """Kill one sandbox within the budget; ``False`` when even that failed."""
    try:
        kill_within_budget(handle.kill, what="sandbox")
    except Exception as exc:  # noqa: BLE001 - reported by the caller
        logger.warning("沙箱销毁失败: %s: %s", type(exc).__name__, exc)
        return False
    return True


def _destroy_outcome(handle: Any, reason: str) -> ParkOutcome:
    destroyed = destroy_sandbox(handle)
    return ParkOutcome(ok=False, reason=reason, destroyed=destroyed)
