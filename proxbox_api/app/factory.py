"""FastAPI application factory: middleware, routers, OpenAPI, and lifespan."""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from proxbox_api.app import bootstrap
from proxbox_api.app.cache_routes import register_cache_routes
from proxbox_api.app.cors import build_cors_origins
from proxbox_api.app.exceptions import register_exception_handlers
from proxbox_api.app.full_update import register_full_update_routes
from proxbox_api.app.root_meta import root_meta_router
from proxbox_api.app.websockets import register_websocket_routes
from proxbox_api.exception import ProxboxException
from proxbox_api.log_buffer import configure_buffer_logger
from proxbox_api.logger import logger
from proxbox_api.openapi_custom import custom_openapi_builder
from proxbox_api.routes.admin import router as admin_router
from proxbox_api.routes.dcim import router as dcim_router
from proxbox_api.routes.extras import router as extras_router
from proxbox_api.routes.netbox import router as netbox_router
from proxbox_api.routes.proxmox import router as proxmox_router
from proxbox_api.routes.proxmox.cluster import router as px_cluster_router
from proxbox_api.routes.proxmox.nodes import router as px_nodes_router
from proxbox_api.routes.proxmox.replication import router as px_replication_router
from proxbox_api.routes.proxmox.runtime_generated import register_generated_proxmox_routes
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
    }
)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory rate limiting per IP address.

    Limits requests to a configurable rate per minute.
    """

    def __init__(self, app, requests_per_minute: int = 60):
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self.window_size = 60.0
        self._requests: dict[str, list[float]] = defaultdict(list)

    def _clean_old_requests(self, ip: str, now: float) -> None:
        cutoff = now - self.window_size
        self._requests[ip] = [t for t in self._requests[ip] if t > cutoff]

    def _get_client_ip(self, request: Request) -> str:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    async def dispatch(self, request: Request, call_next):
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


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        return response


class APIKeyAuthMiddleware(BaseHTTPMiddleware):
    """Middleware to enforce API key authentication on protected routes."""

    _failed_attempts: dict[str, tuple[int, float]] = {}
    _lockout_duration = 300
    _max_failed_attempts = 5

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path in AUTH_EXEMPT_PATHS or path.startswith("/static/"):
            return await call_next(request)

        api_key = request.headers.get("X-Proxbox-API-Key")
        client_ip = self._get_client_ip(request)

        if self._is_locked_out(client_ip):
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "Too many failed authentication attempts. Please try again later."
                },
                headers={"Retry-After": "300"},
            )

        raw_key = os.environ.get("PROXBOX_API_KEY", "").strip()
        dev_mode = os.environ.get("PROXBOX_DEV_MODE", "false").lower() in ("true", "1", "yes")

        if not raw_key:
            if dev_mode:
                return await call_next(request)
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "API key not configured. Set PROXBOX_API_KEY environment variable or enable PROXBOX_DEV_MODE for development."
                },
            )

        stored_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        provided_hash = hashlib.sha256(api_key.encode()).hexdigest() if api_key else ""

        if not secrets.compare_digest(provided_hash, stored_hash):
            self._record_failed_attempt(client_ip)
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key."},
            )

        self._clear_failed_attempts(client_ip)
        return await call_next(request)

    def _get_client_ip(self, request: Request) -> str:
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _is_locked_out(self, ip: str) -> bool:
        if ip not in self._failed_attempts:
            return False
        attempts, first_attempt_time = self._failed_attempts[ip]
        if attempts >= self._max_failed_attempts:
            if time.time() - first_attempt_time < self._lockout_duration:
                return True
            self._clear_failed_attempts(ip)
        return False

    def _record_failed_attempt(self, ip: str) -> None:
        now = time.time()
        if ip not in self._failed_attempts:
            self._failed_attempts[ip] = (1, now)
        else:
            attempts, first_attempt_time = self._failed_attempts[ip]
            if now - first_attempt_time > self._lockout_duration:
                self._failed_attempts[ip] = (1, now)
            else:
                self._failed_attempts[ip] = (attempts + 1, first_attempt_time)

    def _clear_failed_attempts(self, ip: str) -> None:
        self._failed_attempts.pop(ip, None)


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
    yield


def create_app() -> FastAPI:
    """Build and configure the Proxbox FastAPI application."""
    bootstrap.init_database_and_netbox()

    app = FastAPI(
        title="Proxbox Backend",
        description="## Proxbox Backend made in FastAPI framework",
        version="0.0.1",
        lifespan=_lifespan,
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
    register_cache_routes(app)
    register_full_update_routes(app)
    register_websocket_routes(app)

    app.include_router(admin_router, prefix="/admin", tags=["admin"])
    app.include_router(netbox_router, prefix="/netbox", tags=["netbox"])
    app.include_router(px_nodes_router, prefix="/proxmox/nodes", tags=["proxmox / nodes"])
    app.include_router(px_cluster_router, prefix="/proxmox/cluster", tags=["proxmox / cluster"])
    app.include_router(px_replication_router, prefix="/proxmox", tags=["proxmox / replication"])
    app.include_router(proxmox_router, prefix="/proxmox", tags=["proxmox"])
    app.include_router(dcim_router, prefix="/dcim", tags=["dcim"])
    app.include_router(virtualization_router, prefix="/virtualization", tags=["virtualization"])
    app.include_router(
        virtual_machines_router,
        prefix="/virtualization/virtual-machines",
        tags=["virtualization / virtual-machines"],
    )
    app.include_router(extras_router, prefix="/extras", tags=["extras"])
    app.include_router(
        sync_individual_router, prefix="/sync/individual", tags=["sync / individual"]
    )

    return app
