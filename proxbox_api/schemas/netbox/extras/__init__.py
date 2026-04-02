"""NetBox extras schema models such as tags."""

from proxbox_api.schemas._base import ProxboxBaseModel


class TagSchema(ProxboxBaseModel):
    name: str
    slug: str
    color: str
    description: str | None = None
    object_types: list[str] | None = None
