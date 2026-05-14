"""FastAPI application factory: middleware, routers, OpenAPI, and lifespan."""

from __future__ import annotations

import ipaddress
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from proxbox_api import __version__
from proxbox_api.app import bootstrap
from proxbox_api.app.cache_routes import register_cache_routes
from proxbox_api.app.cors import build_cors_origins
from proxbox_api.app.exceptions import register_exception_handlers
from proxbox_api.app.full_update import register_full_update_routes
from proxbox_api.app.root_meta import root_meta_router
from proxbox_api.app.websockets import register_websocket_routes
from proxbox_api.auth import check_auth_header_with_session, get_session_factory
from proxbox_api.exception import ProxboxException
from proxbox_api.log_buffer import configure_buffer_logger
from proxbox_api.logger import logger
from proxbox_api.openapi_custom import custom_openapi_builder
from proxbox_api.routes.admin import router as admin_router
from proxbox_api.routes.auth import router as auth_router
from proxbox_api.routes.dcim import router as dcim_router
from proxbox_api.routes.extras import router as extras_router
from proxbox_api.routes.intent import router as intent_router
from proxbox_api.routes.intent.deletion_requests import router as deletion_requests_router
from proxbox_api.routes.netbox import router as netbox_router
from proxbox_api.routes.proxmox import router as proxmox_router
from proxbox_api.routes.proxmox.cluster import router as px_cluster_router
from proxbox_api.routes.proxmox.ha import router as px_ha_router
from proxbox_api.routes.proxmox.nodes import router as px_nodes_router
from proxbox_api.routes.proxmox.replication import router as px_replication_router
from proxbox_api.routes.proxmox.runtime_generated import register_generated_proxmox_routes
from proxbox_api.routes.proxmox_actions import router as proxmox_actions_router
from proxbox_api.routes.sync.active import router as sync_active_router
from proxbox_api.routes.sync.individual import router as sync_individual_router
from proxbox_api.routes.virtualization import router as virtualization_router
from proxbox_api.routes.virtualization.virtual_machines import router as virtual_machines_router

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


AUTH_EXEMPT_PATHS = frozenset(
    {
        "/",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/health",
        "/meta",
        "/auth/register-key",
        "/auth/bootstrap-status",
    }
)


def _load_trusted_proxies() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    raw = os.environ.get("PROXBOX_TRUSTED_PROXIES", "").strip()
    if not raw:
        return ()
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            networks.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid PROXBOX_TRUSTED_PROXIES entry: %s", token)
    return tuple(networks)


_TRUSTED_PROXIES = _load_trusted_proxies()


def _peer_is_trusted(peer_ip: str) -> bool:
    if not _TRUSTED_PROXIES:
        return False
    try:
        peer = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    return any(peer in net for net in _TRUSTED_PROXIES)


def resolve_client_ip(request: Request) -> str:
    """Return the originating client IP, trusting X-Forwarded-For only from configured proxies.

    Set PROXBOX_TRUSTED_PROXIES to a comma-separated list of CIDRs / IPs to enable header trust.
    Without this env var, the peer IP is always returned, which prevents per-IP rate-limit
    and brute-force-lockout bypass via spoofed X-Forwarded-For headers.
    """
    peer_ip = request.client.host if request.client else "unknown"
    if not _peer_is_trusted(peer_ip):
        return peer_ip
    forwarded = request.headers.get("X-Forwarded-For")
    if not forwarded:
        return peer_ip
    # Walk right-to-left, skipping trusted-proxy hops, to find the first untrusted client.
    candidates = [token.strip() for token in forwarded.split(",") if token.strip()]
    for candidate in reversed(candidates):
        if not _peer_is_trusted(candidate):
            return candidate
    return candidates[0] if candidates else peer_ip


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory rate limiting per IP address.

    Limits requests to a configurable rate per minute.
    """

    def __init__(self, app: FastAPI, requests_per_minute: int = 60) -> None:
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self.window_size = 60.0
        self._requests: dict[str, list[float]] = defaultdict(list)

    def _clean_old_requests(self, ip: str, now: float) -> None:
        cutoff = now - self.window_size
        self._requests[ip] = [t for t in self._requests[ip] if t > cutoff]
        # Remove IP entry if no requests remain (prevent unbounded dict growth)
        if not self._requests[ip]:
            del self._requests[ip]

    def _get_client_ip(self, request: Request) -> str:
        return resolve_client_ip(request)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path.startswith("/static/") or path in AUTH_EXEMPT_PATHS:
            return await call_next(request)

        now = time.time()
        ip = self._get_client_ip(request)
        self._clean_old_requests(ip, now)

        if len(self._requests[ip]) >= self.requests_per_minute:
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Please try again later."},
                headers={"Retry-After": "60"},
            )

        self._requests[ip].append(now)
        return await call_next(request)


_DOCS_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "frame-ancestors 'none'"
)
_DEFAULT_CSP = "default-src 'self'; frame-ancestors 'none'"
_DOCS_PATHS = frozenset({"/docs", "/redoc"})


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        csp = _DOCS_CSP if request.url.path in _DOCS_PATHS else _DEFAULT_CSP
        response.headers["Content-Security-Policy"] = csp
        return response


class APIKeyAuthMiddleware(BaseHTTPMiddleware):
    """Middleware to enforce API key authentication on protected routes."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        if path in AUTH_EXEMPT_PATHS or path.startswith("/static/"):
            return await call_next(request)

        api_key = request.headers.get("X-Proxbox-API-Key")
        client_ip = self._get_client_ip(request)

        session_factory = get_session_factory(request.app)
        with session_factory() as session:
            authorized, error_message = check_auth_header_with_session(session, api_key, client_ip)

        if not authorized:
            status_code = 429 if "Too many failed" in (error_message or "") else 401
            return JSONResponse(
                status_code=status_code,
                content={"detail": error_message},
                headers={"Retry-After": "300"} if status_code == 429 else {},
            )

        return await call_next(request)

    def _get_client_ip(self, request: Request) -> str:
        return resolve_client_ip(request)


# Legacy module-level placeholders (some tooling may read these names).
configuration = None
default_config: dict = {}
plugin_configuration: dict = {}
proxbox_cfg: dict = {}
PROXBOX_PLUGIN_NAME: str = "netbox_proxbox"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        register_generated_proxmox_routes(app)
    except ProxboxException as error:
        logger.warning(
            "Generated Proxmox proxy routes were not mounted: %s",
            error.message,
            extra={"detail": error.detail},
        )
        strict = os.environ.get("PROXBOX_STRICT_STARTUP", "").lower() in ("1", "true", "yes")
        if strict:
            raise

    from proxbox_api.proxmox_to_netbox.proxmox_schema import available_proxmox_sdk_versions

    bundled = available_proxmox_sdk_versions()
    logger.info(
        "Bundled Proxmox OpenAPI schema versions available: %s",
        ", ".join(bundled) if bundled else "(none)",
    )

    await _run_bootstrap_pass(app)

    yield


async def _run_bootstrap_pass(app: FastAPI) -> None:
    """Resolve the ``ensure_netbox_objects`` flag and run NetBox bootstrap.

    Stores a :class:`BootstrapStatus` on ``app.state.bootstrap_status`` so the
    full-update SSE stream can emit the ``bootstrap_done`` frame on every
    subsequent run. NetBox-connectivity failures are logged but do not abort
    startup — the existing inline ``_ensure_*`` helpers in the sync path stay
    as a defensive fallback.
    """
    from proxbox_api.app.netbox_session import get_raw_netbox_session
    from proxbox_api.runtime_settings import get_bool
    from proxbox_api.services.netbox_bootstrap import BootstrapStatus, run_netbox_bootstrap

    enabled = get_bool(
        settings_key="ensure_netbox_objects",
        env="PROXBOX_ENSURE_NETBOX_OBJECTS",
        default=True,
    )

    if not enabled:
        app.state.bootstrap_status = BootstrapStatus(
            ok=True,
            skipped=True,
            reason="ensure_netbox_objects=false",
        )
        logger.info("NetBox bootstrap skipped via ensure_netbox_objects flag")
        return

    try:
        nb = get_raw_netbox_session()
    except Exception as exc:  # noqa: BLE001
        logger.warning("NetBox bootstrap skipped: no NetBox session available (%s)", exc)
        app.state.bootstrap_status = BootstrapStatus(
            ok=False,
            skipped=True,
            reason=f"no_netbox_session: {exc}",
        )
        return

    try:
        status = await run_netbox_bootstrap(nb, enabled=True)
    except Exception as exc:  # noqa: BLE001
        logger.error("NetBox bootstrap pass failed: %s", exc)
        app.state.bootstrap_status = BootstrapStatus(
            ok=False,
            skipped=False,
            reason=f"bootstrap_error: {exc}",
        )
        return

    app.state.bootstrap_status = status


def create_app() -> FastAPI:
    """Build and configure the Proxbox FastAPI application."""
    bootstrap.init_database_and_netbox()

    app = FastAPI(
        title="Proxbox Backend",
        description="## Proxbox Backend made in FastAPI framework",
        version=__version__,
        lifespan=_lifespan,
        docs_url=None,
        redoc_url=None,
    )

    def custom_openapi():
        return custom_openapi_builder(app)

    app.openapi = custom_openapi

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    static_dir = os.path.join(base_dir, "static")
    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
    else:
        logger.info(
            "Static asset directory not found; skipping /static mount", extra={"path": static_dir}
        )

    @app.get("/docs", include_in_schema=False)
    async def custom_swagger_ui() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title=f"{app.title} - Swagger UI",
            swagger_js_url="/static/swagger-ui/swagger-ui-bundle.js",
            swagger_css_url="/static/swagger-ui/swagger-ui.css",
            swagger_favicon_url="/static/swagger-ui/favicon.png",
        )

    @app.get("/redoc", include_in_schema=False)
    async def custom_redoc() -> HTMLResponse:
        return get_redoc_html(
            openapi_url="/openapi.json",
            title=f"{app.title} - ReDoc",
            redoc_js_url="/static/swagger-ui/redoc.standalone.js",
            redoc_favicon_url="/static/swagger-ui/favicon.png",
            with_google_fonts=False,
        )

    origins = build_cors_origins(bootstrap.netbox_endpoints)
    logger.info("CORS allow_origins configured (%d entries)", len(origins))

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Accept-Language",
            "Content-Type",
            "X-Proxbox-API-Key",
            "X-Proxbox-Actor",
            "X-Requested-With",
        ],
    )

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(APIKeyAuthMiddleware)

    rate_limit_str = os.environ.get("PROXBOX_RATE_LIMIT", "60")
    try:
        rate_limit = max(1, int(rate_limit_str))
    except ValueError:
        rate_limit = 60
    app.add_middleware(RateLimitMiddleware, requests_per_minute=rate_limit)

    register_exception_handlers(app)

    configure_buffer_logger("proxbox")

    app.include_router(root_meta_router)
    app.include_router(auth_router)

    features = {
        token.strip().lower()
        for token in os.environ.get("PROXBOX_FEATURES", "").split(",")
        if token.strip()
    }
    pbs_only = features == {"pbs"}

    if not pbs_only:
        register_cache_routes(app)
        register_full_update_routes(app)
        register_websocket_routes(app)
        app.include_router(admin_router, prefix="/admin", tags=["admin"])
        app.include_router(netbox_router, prefix="/netbox", tags=["netbox"])
        app.include_router(px_nodes_router, prefix="/proxmox/nodes", tags=["proxmox / nodes"])
        app.include_router(px_cluster_router, prefix="/proxmox/cluster", tags=["proxmox / cluster"])
        app.include_router(px_ha_router, prefix="/proxmox/cluster", tags=["proxmox / ha"])
        app.include_router(px_replication_router, prefix="/proxmox", tags=["proxmox / replication"])
        app.include_router(
            proxmox_actions_router, prefix="/proxmox", tags=["proxmox / operational verbs"]
        )
        app.include_router(proxmox_router, prefix="/proxmox", tags=["proxmox"])
        app.include_router(dcim_router, prefix="/dcim", tags=["dcim"])
        app.include_router(virtualization_router, prefix="/virtualization", tags=["virtualization"])
        app.include_router(
            virtual_machines_router,
            prefix="/virtualization/virtual-machines",
            tags=["virtualization / virtual-machines"],
        )
        app.include_router(extras_router, prefix="/extras", tags=["extras"])
        app.include_router(intent_router, prefix="/intent", tags=["intent"])
        app.include_router(deletion_requests_router, prefix="/intent", tags=["intent"])
        app.include_router(
            sync_individual_router, prefix="/sync/individual", tags=["sync / individual"]
        )
        app.include_router(sync_active_router)

    try:
        from proxbox_api.pbs import admin_router as pbs_admin_router  # noqa: PLC0415
        from proxbox_api.pbs import router as pbs_router  # noqa: PLC0415
    except ImportError as exc:
        logger.info("PBS subpackage unavailable; /pbs/* routes disabled (%s)", exc)
    else:
        app.include_router(pbs_admin_router, prefix="/pbs", tags=["pbs"])
        app.include_router(pbs_router, prefix="/pbs", tags=["pbs"])

    return app
