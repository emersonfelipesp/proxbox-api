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
    memory_mb: Optional[int] = Field(
        None,
        ge=64,
        description="VM memory in MiB (Proxmox 'memory' convention; field name kept for API compatibility)",
    )
    cores: Optional[int] = Field(None, ge=1)
    disk_gb: Optional[int] = Field(None, ge=1)
    full_clone: bool = True


class CloudVMProvisionResponse(BaseModel):
    new_vmid: int
    clone_upid: Optional[str] = None
    config_upid: Optional[str] = None
    resize_upid: Optional[str] = None
    start_upid: Optional[str] = None
    status: str  # "started" | "stopped" (failures raise HTTPException)
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


class CloudImageTemplateBuildRequest(BaseModel):
    """Create a Proxmox cloud-init VM template from a downloadable cloud image."""

    model_config = ConfigDict(extra="forbid")

    endpoint_id: int = Field(..., description="ProxmoxEndpoint primary key")
    vmid: int = Field(..., ge=100, description="Template VMID to create")
    name: str = Field(..., min_length=1, max_length=128)
    target_node: str = Field(..., min_length=1)
    image_url: str = Field(
        "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
        min_length=1,
        description="HTTP(S) URL for the source cloud image",
    )
    image_filename: str | None = Field(
        None,
        description="Filename to store in Proxmox import storage; .img is normalized to .qcow2",
    )
    image_storage: str = Field("local", min_length=1)
    vm_storage: str = Field("local-zfs", min_length=1)
    memory_mb: int = Field(512, ge=64)
    cores: int = Field(1, ge=1)
    bridge: str = Field("vmbr0", min_length=1)
    ciuser: str = Field("ubuntu", min_length=1, max_length=64)
    os_type: str = Field("l26", min_length=1)
    cpu: str | None = Field("host")
    verify_image_certificates: bool = True
    description: str | None = Field(None, max_length=8192)


class CloudImageTemplateBuildResponse(BaseModel):
    endpoint_id: int
    target_node: str
    vmid: int
    name: str
    status: str
    image_volid: str
    download_upid: Optional[str] = None
    create_upid: Optional[str] = None
    template_upid: Optional[str] = None
    boot: Optional[str] = None
    scsi0: Optional[str] = None
    ide2: Optional[str] = None
