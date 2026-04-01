"""Schemas for NetBox session settings and connection details."""

from pydantic import RootModel

from proxbox_api.schemas._base import ProxboxBaseModel


class NetboxSessionSettingsSchema(ProxboxBaseModel):
    virtualmachine_role_id: int
    node_role_id: int
    site_id: int


class NetboxSessionSchema(ProxboxBaseModel):
    domain: str
    http_port: int
    token: str
    settings: NetboxSessionSettingsSchema | None = None


CreateDefaultBool = RootModel[bool | None]
