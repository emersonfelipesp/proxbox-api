"""Cloud-init driven VM provisioning schemas."""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from proxbox_api.routes.intent.cloud_init import CloudInitPayload


class CloudVMProvisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint_id: int = Field(..., description="ProxmoxEndpoint primary key")
    template_vmid: int = Field(..., ge=100, description="Source template VMID on Proxmox")
    new_vmid: int = Field(..., ge=100, description="Destination VMID (caller reserves)")
    new_name: str = Field(..., min_length=1, max_length=128)
    target_node: str = Field(..., min_length=1)
    cloud_init: CloudInitPayload
    start_after_provision: bool = True
    storage: Optional[str] = Field(None, description="Clone destination storage pool")
    memory_mb: Optional[int] = Field(None, ge=64)
    cores: Optional[int] = Field(None, ge=1)
    full_clone: bool = True


class CloudVMProvisionResponse(BaseModel):
    new_vmid: int
    clone_upid: Optional[str] = None
    config_upid: Optional[str] = None
    start_upid: Optional[str] = None
    status: str  # "started" | "stopped" | "failed"
    detail: Optional[str] = None


class CloudTemplateSummary(BaseModel):
    id: int
    name: str
    slug: str
    cluster_id: Optional[int] = None
    cluster_name: Optional[str] = None
    source_vmid: int
    os_family: str
    os_release: str = ""
    default_ciuser: str = "cloud-user"
    is_active: bool = True
    allowed_tenant_ids: list[int] = Field(default_factory=list)


class CloudTemplateListResponse(BaseModel):
    count: int
    results: list[CloudTemplateSummary]
