"""Cloud Portal provisioning routes."""

from proxbox_api.routes.cloud.provision import router as provision_router
from proxbox_api.routes.cloud.templates import router as templates_router

__all__ = ("provision_router", "templates_router")
