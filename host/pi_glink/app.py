"""Composition root of the pi-glink HTTP host.

``python -m pi_glink.app`` (and the ``pi-glink`` console script) start here:
this module builds the server object, wires its ownership/identity/storage
dependencies and owns the data-root lifecycle. The HTTP surface is split by
responsibility and composed into one request handler:

* :mod:`pi_glink.web_routes` — routing and the run/health API,
* :mod:`pi_glink.web_sse` — the bounded event-stream surface,
* :mod:`pi_glink.web_security` — identity, session and request protection,
* :mod:`pi_glink.web_http` — bodies, responses, static assets, access logging,
* :mod:`pi_glink.cli` — the CLI entry point and operator subcommands.

Run events are never stored here — the bus is the live fan-out plus a bounded
replay window; durable run metadata, the committed event log and artifacts
belong to :mod:`pi_glink.store`. A host process owns its data root exclusively
(:mod:`pi_glink.ownership`) before it reconciles or serves anything.
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .accounts import AccountsStore
from .auth import AccountsAuth
from .config import ConfigError, HostConfig, load_config
from .events import EventBus
from .runs import RunManager
from .ownership import DataRootOwnership
from .static_files import WebAssets
from .store import RunStore
from .tenancy import Tenancy, build_tenancy
from .web_http import HttpPlumbingMixin
from .web_routes import RoutesMixin
from .web_security import RequestSecurityMixin
from .web_sse import SseMixin, StreamLimits

# Re-exported so ``python -m pi_glink.app``, the ``pi-glink`` script and the
# existing import paths keep working after the split.
from .cli import (  # noqa: E402 - re-export after the composition imports
    create_invite_command as _create_invite_command,
    load_config as load_config,  # noqa: F401 - the one patched configuration seam
    main as main,
    resource_command as _resource_command,
)

logger = logging.getLogger("pi_glink.host")


class PiGlinkServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying the frozen config, bus, store and manager.

    When built by :func:`create_server` it also carries the data-root
    ownership, released by :meth:`server_close`.
    """

    daemon_threads = True
    allow_reuse_address = True
    #: Accept a burst of concurrent connections: the admission ceiling (not the
    #: listen backlog) must be what refuses a request, otherwise a client sees a
    #: connection reset instead of an explainable 429.
    request_queue_size = 64

    def __init__(
        self,
        config: HostConfig,
        bus: EventBus,
        store: RunStore,
        manager: RunManager,
        assets: WebAssets,
        *,
        ownership: DataRootOwnership | None = None,
        tenancy: Tenancy | None = None,
        auth: AccountsAuth | None = None,
    ) -> None:
        self.config = config
        self.bus = bus
        self.store = store
        self.manager = manager
        self.assets = assets
        self.ownership = ownership
        #: Frozen multi-tenant trust material; ``None`` in local mode.
        self.tenancy = tenancy
        #: Identity backend for accounts mode; ``None`` in gateway and local mode.
        self.auth = auth
        #: The account store, when accounts mode is active (tests and operator
        #: tooling read it through the server that owns it).
        self.accounts: AccountsStore | None = auth.store if auth is not None else None
        self.stopping = threading.Event()
        #: The bounded SSE budget (global, plus per tenant when in multi-tenant
        #: mode); the stream handler releases its slot on every exit path.
        self.streams = StreamLimits(
            global_limit=config.max_global_streams,
            tenant_limit=config.max_tenant_streams,
        )
        super().__init__((config.host, config.port), HostRequestHandler)

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            # The ledger sweep is host lifecycle: stop it before the manager and
            # the data-root lock go away, so no probe runs against a closed host.
            self.manager.stop_resource_watch()
            # The egress listener only exists for live grants; close it with
            # the host so no forwarder outlives the web surface.
            self.manager.egress.close()
            if self.tenancy is not None:
                self.tenancy.close()
            if self.ownership is not None:
                self.ownership.release()


class HostRequestHandler(
    HttpPlumbingMixin,
    RequestSecurityMixin,
    SseMixin,
    RoutesMixin,
    BaseHTTPRequestHandler,
):
    """The one request handler, composed from the responsibility modules.

    Mixin order matters only in that the shared plumbing precedes the standard
    library base class; the mixins themselves define disjoint methods.
    """

    protocol_version = "HTTP/1.1"
    server_version = "pi-glink"

    server: PiGlinkServer


def create_server(
    config: HostConfig,
    *,
    client_factory: Callable[..., Any] | None = None,
    docker_probe: Callable[[str], tuple[bool, str]] | None = None,
    sandbox_probe: Callable[[str], tuple[bool, str]] | None = None,
) -> PiGlinkServer:
    """Build a ready-to-serve host (used by tests and by ``main``).

    ``client_factory`` and the two identity probes exist for tests: production
    always talks to the real ``pi`` binary, the real Docker CLI and the real E2B
    SDK. The probes are what the online resource-ledger sweep calls; they never
    run inside the admission lock.

    Ordering is part of the safety contract:

    1. the data-root lock is acquired **first**, before any read or write under
       the data root — including account-database creation and migration;
    2. identity material is loaded (a missing or over-permissive key file
       refuses to start instead of falling back to the unauthenticated local
       surface);
    3. the run store is reconciled and the port is bound last;
    4. only then does the periodic resource sweep start.
    """
    # Ownership comes first: only a process that holds the data root may
    # conclude that a ``running`` record has no live owner, or create/rewrite
    # the account database. A second host on the same data root (same or
    # another port) stops here, before touching any record.
    ownership = DataRootOwnership.acquire(config.data_root)
    tenancy: Tenancy | None = None
    try:
        # Accounts mode opens/creates/migrates the SQLite database here, under
        # the lock; gateway mode only reads files.
        tenancy = build_tenancy(config)
        if config.accounts_enabled and (tenancy is None or tenancy.accounts is None):
            # Defensive: never serve accounts mode without its store.
            raise ConfigError("PI_GLINK_AUTH_MODE=accounts 账号库初始化失败")
        # Accounts mode gets its session backend here; gateway and local mode
        # have no in-app login at all.
        auth = AccountsAuth(config, tenancy.accounts) if config.accounts_enabled else None
        bus = EventBus()
        store = RunStore(config.data_root / "runs")
        manager = RunManager(
            config,
            bus,
            store,
            client_factory=client_factory,
            ownership=ownership,
            credential_store=tenancy.credentials if tenancy is not None else None,
            docker_probe=docker_probe,
            sandbox_probe=sandbox_probe,
        )
        # Runs left ``running`` by a previous process cannot continue; close
        # them out honestly before the host serves history. External leftovers
        # are not touched here — their identity is recorded for the operator,
        # and the periodic sweep below is the only thing that may confirm one.
        manager.reconcile()
        server = PiGlinkServer(
            config,
            bus,
            store,
            manager,
            WebAssets(config.web_root),
            ownership=ownership,
            tenancy=tenancy,
            auth=auth,
        )
        # After startup reconciliation: one sweep immediately, then every
        # configured interval (0 disables it). Background, daemon, never inside
        # the admission lock.
        manager.start_resource_watch()
        return server
    except BaseException:
        if tenancy is not None:
            tenancy.close()
        ownership.release()
        raise


if __name__ == "__main__":
    raise SystemExit(main())
