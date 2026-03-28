from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from proxbox_api import templates
from proxbox_api.routes.netbox import GetNetBoxEndpoint

router = APIRouter()

@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def admin(request: Request, netbox_endpoints: GetNetBoxEndpoint):
    return templates.TemplateResponse(
        request=request,
        name="admin/index.html",
        context={
            "name": "Proxbox",
            "netbox_endpoints": netbox_endpoints
        }
    )