"""Cloud Portal provisioning routes."""

from proxbox_api.routes.cloud.azure_vhd_imports import router as azure_vhd_imports_router
from proxbox_api.routes.cloud.catalog import versions_router
from proxbox_api.routes.cloud.firecracker import router as firecracker_router
from proxbox_api.routes.cloud.image_factory import router as image_factory_router
from proxbox_api.routes.cloud.provision import router as provision_router
from proxbox_api.routes.cloud.provision_stream import stream_router as provision_stream_router
from proxbox_api.routes.cloud.pve_template import router as pve_template_router
from proxbox_api.routes.cloud.qemu_templates import router as qemu_templates_router
from proxbox_api.routes.cloud.template_images import router as template_images_router
from proxbox_api.routes.cloud.templates import router as templates_router

__all__ = (
    "azure_vhd_imports_router",
    "provision_router",
    "provision_stream_router",
    "firecracker_router",
    "image_factory_router",
    "pve_template_router",
    "qemu_templates_router",
    "template_images_router",
    "templates_router",
    "versions_router",
)
