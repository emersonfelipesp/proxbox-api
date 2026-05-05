"""Admin route registration and templates for proxbox-api."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from proxbox_api import templates
from proxbox_api.routes.admin import encryption, logs
from proxbox_api.routes.netbox import GetNetBoxEndpoint

router = APIRouter()

router.include_router(logs.router)
router.include_router(encryption.router)


def _sanitize_endpoint_for_display(endpoint: object) -> dict:
    """Return a dict with sensitive fields removed/masked for display."""
    result = {}
    for key in ("name", "ip_address", "domain", "port", "token_version", "verify_ssl"):
        value = getattr(endpoint, key, None)
        if value is not None:
            result[key] = value
    return result


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def admin(request: Request, netbox_endpoints: GetNetBoxEndpoint):
    sanitized = [_sanitize_endpoint_for_display(ep) for ep in netbox_endpoints]
    return templates.TemplateResponse(
        request=request,
        name="admin/index.html",
        context={"name": "Proxbox", "netbox_endpoints": sanitized},
    )
