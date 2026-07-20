"""Product-aware QEMU display defaults for generated cloud templates."""

from __future__ import annotations

from dataclasses import dataclass

from proxbox_api.schemas.cloud_provision import ProxmoxProductType


@dataclass(frozen=True)
class CloudTemplateDisplayConfig:
    vga: str
    serial0: str | None = None


def display_config_for_product(product_type: ProxmoxProductType) -> CloudTemplateDisplayConfig:
    """Return the console/display profile for a generated product template."""
    if product_type.value in {"pfsense", "opnsense"}:
        return CloudTemplateDisplayConfig(vga="serial0", serial0="socket")
    return CloudTemplateDisplayConfig(vga="std")


def qemu_display_create_kwargs(product_type: ProxmoxProductType) -> dict[str, str]:
    """Return QEMU create kwargs for the product's display profile."""
    display = display_config_for_product(product_type)
    kwargs = {"vga": display.vga}
    if display.serial0:
        kwargs["serial0"] = display.serial0
    return kwargs
