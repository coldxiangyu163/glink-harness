"""Composition root of the glink-harness HTTP host.

``python -m glink_harness.app`` (and the ``glink-harness`` console script) start here:
this module puts the HTTP face on top of the execution core that
:mod:`glink_harness.service` assembles (ownership, identity material, bus,
store, manager and the startup reconciliation — usable without any HTTP face,
multi-harness-plan §6 R2) and owns nothing beyond that face. The HTTP surface
is split by responsibility and composed into one request handler:

* :mod:`glink_harness.web_routes` — routing and the run/health API,
* :mod:`glink_harness.web_sse` — the bounded event-stream surface,
* :mod:`glink_harness.web_security` — identity, session and request protection,
* :mod:`glink_harness.web_http` — bodies, responses, static assets, access logging,
* :mod:`glink_harness.cli` — the CLI entry point and operator subcommands.

Run events are never stored here — the bus is the live fan-out plus a bounded
replay window; durable run metadata, the committed event log and artifacts
belong to :mod:`glink_harness.store`. A host process owns its data root exclusively
(:mod:`glink_harness.ownership`) before it reconciles or serves anything.
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .accounts import AccountsStore
from .config import HostConfig
from .service import ExecutionService, build_execution_service
from .static_files import WebAssets
from .web_http import HttpPlumbingMixin
from .web_routes import RoutesMixin
from .web_security import RequestSecurityMixin
from .web_sse import SseMixin, StreamLimits

# Re-exported so ``python -m glink_harness.app``, the ``glink-harness`` script and the
# existing import paths keep working after the split.
from .cli import (  # noqa: E402 - re-export after the composition imports
    # 这四项都是再导出（public seam），不是本模块内部使用，所以逐个豁免 F401。
    create_invite_command as _create_invite_command,  # noqa: F401 - re-export
    load_config as load_config,  # noqa: F401 - the one patched configuration seam
    main as main,
    resource_command as _resource_command,  # noqa: F401 - re-export
)

logger = logging.getLogger("glink_harness.host")

#: See :attr:`HostRequestHandler.timeout`.
REQUEST_SOCKET_TIMEOUT_SECONDS = 60.0


class GlinkHarnessServer(ThreadingHTTPServer):
    """Threaded HTTP server: the HTTP face over one :class:`ExecutionService`.

    The handler mixins read the frozen config, bus, store and manager through
    this server's flat attributes, re-exposed from the service it carries, so
    the mixins stay as they were. Teardown is delegated to
    :meth:`ExecutionService.close`: the core shuts down through one path
    whether or not an HTTP face was ever built on top of it.
    """

    daemon_threads = True
    allow_reuse_address = True
    #: Accept a burst of concurrent connections: the admission ceiling (not the
    #: listen backlog) must be what refuses a request, otherwise a client sees a
    #: connection reset instead of an explainable 429.
    request_queue_size = 64

    def __init__(self, service: ExecutionService, assets: WebAssets) -> None:
        self.service = service
        self.config = service.config
        self.bus = service.bus
        self.store = service.store
        self.manager = service.manager
        self.assets = assets
        self.ownership = service.ownership
        #: Frozen multi-tenant trust material; ``None`` in local mode.
        self.tenancy = service.tenancy
        #: Identity backend for accounts mode; ``None`` in gateway and local mode.
        self.auth = service.auth
        #: The account store, when accounts mode is active (tests and operator
        #: tooling read it through the server that owns it).
        self.accounts: AccountsStore | None = service.accounts
        self.stopping = threading.Event()
        #: The bounded SSE budget (global, plus per tenant when in multi-tenant
        #: mode); the stream handler releases its slot on every exit path.
        self.streams = StreamLimits(
            global_limit=service.config.max_global_streams,
            tenant_limit=service.config.max_tenant_streams,
        )
        super().__init__((service.config.host, service.config.port), HostRequestHandler)

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            # One teardown path for the execution core: in-flight runs are
            # cancelled and awaited first (manager.shutdown), then the ledger
            # sweep, the egress listener, the run store, the identity material
            # and the data-root lock come down — in that order.
            self.service.close()


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
    server_version = "glink-harness"
    #: Per-socket-operation deadline (``StreamRequestHandler.setup`` applies it):
    #: an idle keep-alive connection, a slow request line/headers/body, or a
    #: client that stopped reading an SSE stream releases its thread instead of
    #: holding it forever. An idle SSE stream still writes heartbeat comments
    #: (``web_sse.SSE_HEARTBEAT_SECONDS``), so it is not cut.
    timeout = REQUEST_SOCKET_TIMEOUT_SECONDS

    server: GlinkHarnessServer


def create_server(
    config: HostConfig,
    *,
    client_factory: Callable[..., Any] | None = None,
    docker_probe: Callable[[str], tuple[bool, str]] | None = None,
    sandbox_probe: Callable[[str], tuple[bool, str]] | None = None,
) -> GlinkHarnessServer:
    """Build a ready-to-serve host (used by tests and by ``main``).

    The execution core — ownership, identity material, bus, store, manager and
    the startup reconciliation — is assembled by
    :func:`glink_harness.service.build_execution_service` under its own
    ordering contract (acquire the data-root lock first, load identity next,
    reconcile before returning). This function only puts the HTTP face on top
    of that core: the port is bound last, and only then does the periodic
    resource sweep start.

    ``client_factory`` and the two identity probes exist for tests: production
    always talks to the real ``pi`` binary, the real Docker CLI and the real E2B
    SDK. The probes are what the online resource-ledger sweep calls; they never
    run inside the admission lock.
    """
    service = build_execution_service(
        config,
        client_factory=client_factory,
        docker_probe=docker_probe,
        sandbox_probe=sandbox_probe,
    )
    try:
        server = GlinkHarnessServer(service, WebAssets(config.web_root))
        # After startup reconciliation: one sweep immediately, then every
        # configured interval (0 disables it). Background, daemon, never inside
        # the admission lock.
        service.manager.start_resource_watch()
        return server
    except BaseException:
        service.close()
        raise


if __name__ == "__main__":
    raise SystemExit(main())
