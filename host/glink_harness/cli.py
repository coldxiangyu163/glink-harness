"""Command-line entry point and operator subcommands.

``python -m glink_harness.app`` (and the ``glink-harness`` console script) end up here.
The host lifecycle belongs to :mod:`glink_harness.app`, which owns the composition
root; this module only parses arguments, loads the frozen configuration and
runs one of the three operator paths:

* the server itself (default),
* ``--unreleased`` / ``--mark-released``: the offline resource-ledger view and
  adjudication (requires the host to be stopped),
* ``--create-invite``: the offline onboarding invitation (accounts mode).
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from dataclasses import replace
from typing import Callable

from .accounts import AccountsError
from .config import (
    ConfigError,
    HostConfig,
    load_config,  # noqa: F401 - 经本模块再导出给 glink_harness.app
    validate_host_config,
)
from .events import EventBus
from .runs import RunBusy, RunManager
from .ownership import DataRootBusy, DataRootOwnership
from .store import RunStore
from .tenancy import Tenancy, TenantNotConfigured, build_tenancy, load_identity_material


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="glink-harness",
        description="本地工作台宿主 / local workbench host (pi kernel)",
    )
    parser.add_argument("--host", help="覆盖 LOCAL_WEB_HOST")
    parser.add_argument("--port", type=int, help="覆盖 LOCAL_WEB_PORT")
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="打印生效配置（不含密钥）后退出",
    )
    parser.add_argument(
        "--unreleased",
        action="store_true",
        help="列出执行资源未确认释放的 run 后退出（需宿主未运行）",
    )
    parser.add_argument(
        "--mark-released",
        metavar="RUN_ID",
        help="记录人工裁决：已核对该 run 的执行资源已释放（需宿主未运行，须配合 --note）",
    )
    parser.add_argument("--note", help="人工裁决的核对说明")
    # -- operator onboarding (accounts mode) ---------------------------------
    parser.add_argument(
        "--create-invite",
        action="store_true",
        help="为租户签发一份一次性管理员/成员邀请（需宿主未运行；tenant 策略要求该租户已有模型凭据）",
    )
    parser.add_argument("--tenant", help="--create-invite：租户 id（tenant 策略下必须已有模型凭据）")
    parser.add_argument("--tenant-name", help="--create-invite：组织显示名（默认沿用已有名称）")
    parser.add_argument("--email", help="--create-invite：受邀邮箱")
    parser.add_argument("--name", help="--create-invite：受邀人姓名（仅用于提示）")
    parser.add_argument(
        "--role",
        choices=("admin", "member"),
        default="admin",
        help="--create-invite：受邀角色（默认 admin）",
    )
    parser.add_argument(
        "--invite-ttl-seconds",
        type=int,
        default=None,
        help="--create-invite：邀请有效期秒数（默认使用配置值）",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    config_loader: Callable[[], HostConfig] | None = None,
) -> int:
    """Run the command line.

    ``config_loader`` defaults to the composition root's ``load_config`` so
    ``glink_harness.app.load_config`` stays the one seam tests and operators patch.
    """
    # The composition root is imported lazily: ``glink_harness.app`` re-exports this
    # function, and a module-level import would be circular.
    from . import app as composition_root  # noqa: PLC0415

    loader = config_loader or composition_root.load_config
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.mark_released and not (args.note or "").strip():
        parser.error("--mark-released 需要 --note 说明如何核对了资源释放")
    if args.create_invite:
        missing = [
            name
            for name in ("tenant", "email")
            if not (getattr(args, name) or "").strip()
        ]
        if missing:
            parser.error("--create-invite 需要 " + "、".join(f"--{name}" for name in missing))

    try:
        config = loader()
    except ConfigError as exc:
        print(f"配置错误 / configuration error: {exc}", file=sys.stderr)
        return 2
    if args.host or args.port:
        config = replace(
            config,
            host=args.host or config.host,
            port=args.port or config.port,
        )
    try:
        # The same boundary validation the frozen construction ran: a CLI
        # override must not be able to skip the loopback/range checks.
        validate_host_config(config)
    except ConfigError as exc:
        print(f"配置错误 / configuration error: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.print_config:
        try:
            # Validation only: never open the account database here. Creating or
            # migrating it is a data-root write that requires the host lock.
            load_identity_material(config)
        except ConfigError as exc:
            print(f"配置错误 / configuration error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(config.public_view(), ensure_ascii=False, indent=2))
        return 0
    if args.unreleased or args.mark_released:
        return resource_command(config, args.mark_released, args.note)
    if args.create_invite:
        return create_invite_command(config, args)

    try:
        server = composition_root.create_server(config)
    except DataRootBusy as exc:
        print(f"启动失败 / refused: {exc}", file=sys.stderr)
        return 2
    except ConfigError as exc:
        print(f"配置错误 / configuration error: {exc}", file=sys.stderr)
        return 2
    url = f"http://{config.host}:{server.server_address[1]}/"
    print(f"glink-harness 宿主已启动 / host listening: {url}")
    print(f"执行面 executor: {config.executor}")
    # The banner answers "where does the kernel run", not "is it on this
    # machine's PATH": only inproc runs it locally, so only inproc reports a
    # local lookup. Docker/E2B run it inside the image/template, where
    # "not found" would mislead (the same face-split semantics as
    # /api/health after its fix).
    if config.executor == "inproc":
        pi_path = shutil.which(config.pi_bin)
        print(f"内核 pi: {pi_path or f'未找到 ({config.pi_bin}) / not found'}")
    else:
        print(
            f"内核 kernel: {config.pi_bin}（在镜像/沙箱内执行，本机无需安装 / "
            f"runs inside the {config.executor} image, no local install needed）"
        )
    print(
        "模型 model: "
        + (config.openai_model or "未配置 / unset")
        + (" (已配置密钥 / key set)" if config.has_openai_key else " (缺少密钥 / no key)")
    )
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，正在退出 / interrupted, shutting down")
    finally:
        # Cancel and reap this process's runs before the HTTP server disappears;
        # a run that does not release in time is reported, not hidden.
        unreleased = server.manager.shutdown(timeout=15.0)
        server.server_close()
    if unreleased:
        print(
            "警告：以下 run 的终态或执行资源尚未确认，需要人工对账："
            + ", ".join(unreleased)
            + "（核对后用 --mark-released RUN_ID --note … 记录裁决）",
            file=sys.stderr,
        )
        return 1
    return 0


def create_invite_command(config: HostConfig, args: argparse.Namespace) -> int:
    """Operator onboarding: mint one invitation for a credentialed tenant.

    Runs offline with the same exclusive data-root ownership as the host (so it
    cannot race a live host) and never creates a tenant credential, never
    contacts the network, and never sends mail. The invitation code is printed
    exactly once so the operator can deliver it through a channel they trust.
    """
    if not config.accounts_enabled:
        print(
            "配置错误 / configuration error: 需要 GLINK_TENANCY=multitenant 且 "
            "GLINK_AUTH_MODE=accounts 才能创建站内邀请",
            file=sys.stderr,
        )
        return 2
    # The data root comes first for the same reason as the host: opening the
    # account database creates/migrates it, which must never race a live host.
    try:
        ownership = DataRootOwnership.acquire(config.data_root)
    except DataRootBusy as exc:
        print(f"{exc}；请先停止宿主再创建邀请", file=sys.stderr)
        return 2
    tenancy: Tenancy | None = None
    try:
        try:
            tenancy = build_tenancy(config)
        except ConfigError as exc:
            print(f"配置错误 / configuration error: {exc}", file=sys.stderr)
            return 2
        if tenancy is None or tenancy.accounts is None:
            print("配置错误 / configuration error: 账号库不可用", file=sys.stderr)
            return 2
        tenant = (args.tenant or "").strip()
        if config.shared_model_credentials:
            # The explicit SaaS policy: the invite only needs the frozen host
            # model configuration. No tenant entry and no per-tenant key is
            # consulted (the tenant registry is created on first invitation).
            if not (config.openai_api_key and config.openai_base_url and config.openai_model):
                print(
                    "配置错误 / configuration error: GLINK_MODEL_CREDENTIALS=shared "
                    "需要宿主冻结 OPENAI_API_KEY、OPENAI_BASE_URL、OPENAI_MODEL",
                    file=sys.stderr,
                )
                return 2
        else:
            if tenancy.credentials is None:
                # Defensive: the ``tenant`` policy always loads the store; a
                # missing one is a configuration error, never a fallback.
                print("配置错误 / configuration error: 租户凭据配置不可用", file=sys.stderr)
                return 2
            try:
                tenancy.credentials.require(tenant)
            except TenantNotConfigured as exc:
                print(f"拒绝创建邀请: {exc}", file=sys.stderr)
                return 2
        existing = tenancy.accounts.get_tenant(tenant)
        display = (args.tenant_name or "").strip() or (existing.name if existing else tenant)
        tenancy.accounts.upsert_tenant(tenant, display)
        ttl = args.invite_ttl_seconds or config.invite_ttl_seconds
        try:
            record, code = tenancy.accounts.create_invitation(
                tenant_id=tenant,
                email=args.email,
                role=args.role,
                created_by="operator-cli",
                ttl_seconds=ttl,
            )
        except AccountsError as exc:
            print(f"拒绝创建邀请: {exc.message}", file=sys.stderr)
            return 2
        register_path = f"/register#invite={code}"
        print("邀请已创建（仅本次显示，请通过安全渠道传递；不要在普通日志或工单里留存）：")
        print(f"  组织: {display} ({record.tenant_id})")
        print(f"  邮箱: {record.email}")
        print(f"  角色: {record.role}")
        print(f"  有效期至: {record.expires_at:.0f}（约 {ttl // 3600} 小时）")
        if args.name:
            print(f"  受邀人: {args.name}")
        print(f"  注册入口: {register_path}")
        if config.public_origin:
            print(f"  完整链接: {config.public_origin}{register_path}")
        return 0
    finally:
        if tenancy is not None:
            tenancy.close()
        ownership.release()


def resource_command(config: HostConfig, run_id: str | None, note: str | None) -> int:
    """Operator view and adjudication of unconfirmed execution resources.

    Runs with the same exclusive data-root ownership as the host, so it cannot
    race a live host; it never contacts Docker/E2B or removes anything.
    """
    try:
        ownership = DataRootOwnership.acquire(config.data_root)
    except DataRootBusy as exc:
        print(f"{exc}；请先停止宿主再执行资源裁决", file=sys.stderr)
        return 2
    try:
        manager = RunManager(
            config, EventBus(), RunStore(config.data_root / "runs"), ownership=ownership
        )
        manager.reconcile()
        if run_id:
            try:
                manager.adjudicate_release(run_id, note or "")
            except (KeyError, ValueError, RunBusy) as exc:
                print(f"裁决被拒绝: {exc}", file=sys.stderr)
                return 1
            print(f"已记录人工裁决：run {run_id} 的执行资源已释放")
        print(json.dumps(manager.unreleased_resources(), ensure_ascii=False, indent=2))
        return 0
    finally:
        ownership.release()
