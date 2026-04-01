"""NetBox virtualization schema models for clusters and types."""

from proxbox_api.enum.netbox.virtualization import ClusterStatusOptions
from proxbox_api.schemas._base import ProxboxBaseModel
from proxbox_api.schemas.netbox.extras import TagSchema


class ClusterTypeSchema(ProxboxBaseModel):
    name: str
    slug: str | None = None
    description: str | None = None
    tags: list[TagSchema] | None = None
    custom_fields: dict[str, object] | None = None


class ClusterSchema(ProxboxBaseModel):
    name: str
    type: int
    group: int | None = None
    site: int | None = None
    status: ClusterStatusOptions
    tenant: int | None = None
    description: str | None = None
    comments: str | None = None
    tags: list[int] | None = None
    custom_fields: dict[str, object] | None = None
