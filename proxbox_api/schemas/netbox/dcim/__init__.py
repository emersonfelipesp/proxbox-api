"""NetBox DCIM schema models used by API payloads."""

from proxbox_api.enum.netbox.dcim import StatusOptions
from proxbox_api.schemas._base import ProxboxBaseModel
from proxbox_api.schemas.netbox.extras import TagSchema


class SitesSchema(ProxboxBaseModel):
    name: str
    slug: str
    status: StatusOptions
    region: int | None = None
    group: int | None = None
    facility: str | None = None
    asns: list[int] | None = None
    time_zone: str | None = None
    description: str | None = None
    tags: list[TagSchema | int] | None = None
    custom_fields: dict[str, object] | None = None
    physical_address: str | None = None
    shipping_address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    tenant: int | None = None
