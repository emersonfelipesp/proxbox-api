"""Schemas for the Cloud Image Build Pipeline."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


class CloudImageProductType(str, Enum):
    """Products supported by the Cloud Image Build Pipeline."""

    PVE = "pve"
    PBS = "pbs"
    PDM = "pdm"
    PFSENSE = "pfsense"
    OPNSENSE = "opnsense"


class CloudImageBuildProvider(str, Enum):
    """How the source image artifact is produced before Proxmox import."""

    DEBIAN_CLOUD_IMAGE = "debian_cloud_image"
    RELEASE_IMAGE = "release_image"
    SOURCE_TREE = "source_tree"


class CloudImageVersionEntry(BaseModel):
    """Catalog entry returned by ``GET /cloud/templates/versions``."""

    version: str
    label: str
    product_type: CloudImageProductType
    default_provider: CloudImageBuildProvider
    supported_providers: list[CloudImageBuildProvider]
    architecture: str = "amd64"
    os_family: str
    os_release: str = ""
    image_url: str | None = None
    checksum_url: str | None = None
    sha256: str | None = None
    compression: str | None = None
    source_tree_path: str | None = None
    source_build_command: str | None = None
    debian_codename: str | None = None
    package_name: str | None = None
    repo_component: str | None = None
    repo_suite: str | None = None
    service_name: str | None = None
    notes: str = ""


class CloudImageTemplateBuildRequest(BaseModel):
    """Request body for creating or previewing a reusable template image."""

    product_type: CloudImageProductType = CloudImageProductType.PVE
    product_version: str | None = None
    provider: CloudImageBuildProvider | None = None
    endpoint_id: int | None = None
    target_node: str | None = None
    vmid: int = Field(default=9000, ge=100)
    name: str = "cloud-image-template"
    storage: str = "local-lvm"
    image_storage: str = "local"
    snippets_storage: str = "local"
    snippets_dir: str = "/var/lib/vz/snippets"
    bridge: str = "vmbr0"
    memory_mb: int = Field(default=4096, ge=512)
    cores: int = Field(default=2, ge=1)
    disk_size_gb: int | None = Field(default=None, ge=1)
    ciuser: str = "cloud-user"
    hostname: str = "cloud-image-template"
    domain: str = "nmulti.local"
    node_cidr: str | None = None
    gateway: str | None = None
    nameservers: list[str] = Field(default_factory=lambda: ["1.1.1.1", "8.8.8.8"])
    ssh_authorized_keys: list[str] = Field(default_factory=list)
    image_url: str | None = None
    checksum_url: str | None = None
    sha256: str | None = None
    source_tree_path: str | None = None
    source_build_command: str | None = None
    source_artifact_path: str | None = None
    debian_image_url: str | None = None
    debian_release: str = "bookworm"
    pve_version_pin: str | None = None
    create_vm: bool = True
    execute: bool = False
    ssh_host: str | None = None
    ssh_user: str = "root"
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_identity_file: str | None = None
    verify_image_certificates: bool = True

    @field_validator("nameservers")
    @classmethod
    def _nameservers_not_empty(cls, value: list[str]) -> list[str]:
        return value or ["1.1.1.1", "8.8.8.8"]


class CloudImageTemplateBuildResponse(BaseModel):
    """Response returned by the Cloud Image Build Pipeline endpoint."""

    pipeline_name: str = "Cloud Image Build Pipeline"
    status: str
    product_type: CloudImageProductType
    product_version: str
    provider: CloudImageBuildProvider
    endpoint_id: int | None = None
    target_node: str | None = None
    vmid: int
    template_vmid: int
    name: str
    image_url: str | None = None
    source_tree_path: str | None = None
    source_artifact_path: str | None = None
    generated_userdata: str | None = None
    first_boot_script: str | None = None
    build_script: str
    commands: list[str]
    operator_instructions: str
    execution_enabled: bool = False
    returncode: int | None = None
    stdout: str | None = None
    stderr: str | None = None
