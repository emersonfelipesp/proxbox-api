"""Cloud Portal provisioning routes."""

from proxbox_api.routes.cloud.provision import router as provision_router
from proxbox_api.routes.cloud.pve_template import router as pve_template_router
from proxbox_api.routes.cloud.template_images import router as template_images_router
from proxbox_api.routes.cloud.templates import router as templates_router

__all__ = (
    "provision_router",
    "pve_template_router",
    "template_images_router",
    "templates_router",
)
