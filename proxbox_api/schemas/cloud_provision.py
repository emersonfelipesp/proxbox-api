"""Cloud-init driven VM provisioning schemas."""

import ipaddress
import os
import re
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from proxbox_api.routes.intent.cloud_init import CloudInitPayload
from proxbox_api.settings_client import get_settings
from proxbox_api.ssrf import validate_endpoint_url

_DEFAULT_SSH_KEY_DIR = Path("/etc/proxbox/ssh_keys")
_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_SSH_USER_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_-]{0,63}$")


def _is_valid_hostname(value: str) -> bool:
    if not value or len(value) > 253:
        return False
    hostname = value[:-1] if value.endswith(".") else value
    if not hostname:
        return False
    return all(_HOST_LABEL_RE.fullmatch(label) for label in hostname.split("."))


def _ssh_key_dir() -> Path:
    configured = os.environ.get("PROXBOX_SSH_KEY_DIR", "").strip()
    return Path(configured).resolve() if configured else _DEFAULT_SSH_KEY_DIR.resolve()


class ProxmoxProductType(str, Enum):
    pve = "pve"
    pbs = "pbs"
    pdm = "pdm"
    pfsense = "pfsense"
    opnsense = "opnsense"
    firecracker = "firecracker"
    PVE = "pve"
    PBS = "pbs"
    PDM = "pdm"
    PFSENSE = "pfsense"
    OPNSENSE = "opnsense"
    FIRECRACKER = "firecracker"


class CloudImageBuildProvider(str, Enum):
    debian_cloud_image = "debian_cloud_image"
    ubuntu_cloud_image = "ubuntu_cloud_image"
    proxmox_iso = "proxmox_iso"
    release_image = "release_image"
    source_tree = "source_tree"
    DEBIAN_CLOUD_IMAGE = "debian_cloud_image"
    UBUNTU_CLOUD_IMAGE = "ubuntu_cloud_image"
    PROXMOX_ISO = "proxmox_iso"
    RELEASE_IMAGE = "release_image"
    SOURCE_TREE = "source_tree"


CloudImageProductType = ProxmoxProductType


class AzureVmGeneration(str, Enum):
    gen1 = "gen1"
    gen2 = "gen2"
    GEN1 = "gen1"
    GEN2 = "gen2"


class AzureVhdGuestProfile(str, Enum):
    linux_standard = "linux_standard"
    windows_first_boot_safe = "windows_first_boot_safe"
    LINUX_STANDARD = "linux_standard"
    WINDOWS_FIRST_BOOT_SAFE = "windows_first_boot_safe"


class CloudImageVersionEntry(BaseModel):
    version: str
    label: str
    product_type: ProxmoxProductType
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
    os_codename: str | None = None
    package_name: str | None = None
    repo_component: str | None = None
    repo_suite: str | None = None
    service_name: str | None = None
    notes: str = ""


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
    sockets: Optional[int] = Field(None, ge=1)
    disk_gb: Optional[int] = Field(None, ge=1)
    bridge: Optional[str] = Field(
        None,
        min_length=1,
        max_length=64,
        description="Override the cloned VM's net0 bridge, for example vmbr1.",
    )
    vlan_tag: Optional[int] = Field(
        None,
        ge=1,
        le=4094,
        description="Optional Proxmox VLAN tag to apply to net0.",
    )
    enforce_cloud_network: bool = Field(
        False,
        description=(
            "When true, allocate the next IP from the configured cloud customer "
            "NetBox prefix and force the configured bridge, VLAN tag, and gateway."
        ),
    )
    enable_agent: bool = Field(
        True,
        description=(
            "Enable the QEMU guest agent (agent=enabled=1) on the cloned VM so "
            "Proxmox can read guest IPs and do graceful shutdowns. Default True."
        ),
    )
    full_clone: bool = True


class CloudVMProvisionResponse(BaseModel):
    new_vmid: int
    clone_upid: Optional[str] = None
    config_upid: Optional[str] = None
    resize_upid: Optional[str] = None
    start_upid: Optional[str] = None
    status: str  # "started" | "stopped" (failures raise HTTPException)
    detail: Optional[str] = None


class CloudQemuTemplate(BaseModel):
    """Live QEMU VM template discovered from Proxmox cluster state."""

    id: int = Field(
        ..., description="Alias for source_vmid so frontend selectors have a stable key"
    )
    endpoint_id: int
    endpoint_name: str
    cluster_name: str | None = None
    source_vmid: int = Field(..., ge=100)
    vmid: int = Field(..., ge=100)
    name: str
    node: str
    target_node: str
    status: str | None = None
    template: bool = True
    cloud_init: bool = True
    cloud_init_drives: list[str] = Field(default_factory=list)
    cicustom: str | None = None
    tags: str | None = None
    memory_mb: int | None = None
    maxdisk_bytes: int | None = None
    description: str | None = None
    live_source: bool = True


class CloudQemuTemplateListResponse(BaseModel):
    """`/cloud/vm/templates` response for live Proxmox QEMU Cloud-Init templates."""

    count: int
    results: list[CloudQemuTemplate] = Field(default_factory=list)


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

    endpoint_id: int | None = Field(None, description="ProxmoxEndpoint primary key")
    vmid: int = Field(9000, ge=100, description="Template VMID to create")
    name: str = Field("cloud-image-template", min_length=1, max_length=128)
    target_node: str | None = Field(None, min_length=1)
    image_url: str | None = Field(
        "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
        description="HTTP(S) URL for the source cloud image",
    )
    image_filename: str | None = Field(
        None,
        description="Filename to store in Proxmox import storage; .img is normalized to .qcow2",
    )
    image_storage: str = Field("local", min_length=1)
    vm_storage: str = Field("local-zfs", min_length=1)
    storage: str = Field("local-lvm", min_length=1)
    snippets_storage: str = Field("local", min_length=1)
    snippets_dir: str = Field("/var/lib/vz/snippets", min_length=1)
    memory_mb: int = Field(512, ge=64)
    cores: int = Field(1, ge=1)
    disk_size_gb: int | None = Field(None, ge=1)
    bridge: str = Field("vmbr0", min_length=1)
    ciuser: str = Field("ubuntu", min_length=1, max_length=64)
    hostname: str = Field("cloud-image-template", min_length=1, max_length=128)
    domain: str = Field("nmulti.local", min_length=1, max_length=128)
    node_cidr: str | None = None
    gateway: str | None = None
    nameservers: list[str] = Field(default_factory=lambda: ["1.1.1.1", "8.8.8.8"])
    search_domain: str | None = Field(
        None,
        max_length=253,
        description="DNS search domain to render into generated cloud-init user-data.",
    )
    os_type: str = Field("l26", min_length=1)
    cpu: str | None = Field("host")
    verify_image_certificates: bool = True
    description: str | None = Field(None, max_length=8192)
    user_data_yaml: str | None = Field(
        None,
        max_length=65536,
        description=(
            "Verbatim cloud-init #cloud-config user-data to bake into the template via a "
            "Proxmox cicustom snippet. When set, the build runs through the SSH pipeline "
            "(which can write snippets) instead of the catalog/product flow, so the cloud-config "
            "actually executes on first boot of cloned VMs."
        ),
    )
    product_type: ProxmoxProductType = ProxmoxProductType.pve
    product_version: str | None = Field(
        None, description="Proxmox product version; None = latest in catalog"
    )
    install_qemu_guest_agent: bool | None = Field(
        None,
        description=(
            "Install and enable qemu-guest-agent in generated product cloud-init. "
            "None uses the product default."
        ),
    )
    install_zabbix_agent2: bool | None = Field(
        None,
        description=(
            "Install and enable Zabbix Agent 2 in generated product cloud-init. "
            "None uses the product default."
        ),
    )
    zabbix_server: str = Field(
        "zabbix.nmulti.cloud",
        min_length=1,
        max_length=253,
        description="Zabbix server endpoint for generated zabbix_agent2.conf.",
    )
    pve_version_pin: str | None = None
    debian_release: str = "bookworm"
    provider: CloudImageBuildProvider | None = None
    checksum_url: str | None = None
    sha256: str | None = None
    source_tree_path: str | None = None
    source_build_command: str | None = None
    source_artifact_path: str | None = None
    execute: bool | None = None
    ssh_host: str | None = None
    ssh_user: str = "root"
    ssh_port: int = Field(22, ge=1, le=65535)
    ssh_identity_file: str | None = None
    ssh_authorized_keys: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_product_provider_contract(self) -> "CloudImageTemplateBuildRequest":
        if (
            self.product_type == ProxmoxProductType.pve
            and self.provider == CloudImageBuildProvider.debian_cloud_image
        ):
            raise ValueError(
                "PVE products must use provider=proxmox_iso; "
                "debian_cloud_image builds are not supported for Proxmox VE."
            )
        return self

    @field_validator("image_url")
    @classmethod
    def validate_image_url_ssrf(cls, value: str | None) -> str | None:
        if value is None:
            return value
        safe, reason = validate_endpoint_url(value, get_settings())
        if not safe:
            raise ValueError(f"image_url rejected by SSRF protection: {reason}")
        return value

    @field_validator("nameservers")
    @classmethod
    def validate_nameservers(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        normalized: list[str] = []
        for raw in value:
            server = str(raw).strip()
            ipaddress.ip_address(server)
            if server in seen:
                raise ValueError("nameservers must not contain duplicates")
            seen.add(server)
            normalized.append(server)
        return normalized

    @field_validator("search_domain")
    @classmethod
    def validate_search_domain(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().rstrip(".")
        if not cleaned:
            return None
        if not _is_valid_hostname(cleaned):
            raise ValueError("search_domain must be a valid DNS search domain")
        return cleaned

    @field_validator("zabbix_server")
    @classmethod
    def validate_zabbix_server(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("zabbix_server is required")
        try:
            ipaddress.ip_address(cleaned)
        except ValueError:
            if not _is_valid_hostname(cleaned):
                raise ValueError("zabbix_server must be a valid DNS name or IP address")
        return cleaned

    @field_validator("ssh_host")
    @classmethod
    def validate_ssh_host(cls, value: str | None) -> str | None:
        if value is None:
            return value
        host = value.strip()
        if not host:
            raise ValueError("ssh_host must be a non-empty hostname or IP address.")
        if host.startswith("-"):
            raise ValueError("ssh_host must not start with '-' or resemble an ssh option.")
        if "%" in host:
            raise ValueError("ssh_host must not include an IPv6 zone identifier.")
        try:
            ipaddress.ip_address(host)
            return host
        except ValueError:
            pass
        if not _is_valid_hostname(host):
            raise ValueError("ssh_host must be a valid hostname, IPv4 address, or IPv6 address.")
        return host

    @field_validator("ssh_user")
    @classmethod
    def validate_ssh_user(cls, value: str) -> str:
        if not _SSH_USER_RE.fullmatch(value):
            raise ValueError("ssh_user must match ^[a-zA-Z0-9_][a-zA-Z0-9_-]{0,63}$.")
        return value

    @field_validator("ssh_identity_file")
    @classmethod
    def validate_ssh_identity_file(cls, value: str | None) -> str | None:
        if value is None:
            return value
        resolved = Path(value).resolve()
        allowed_dir = _ssh_key_dir()
        try:
            resolved.relative_to(allowed_dir)
        except ValueError as exc:
            raise ValueError(
                f"ssh_identity_file must resolve under PROXBOX_SSH_KEY_DIR ({allowed_dir})."
            ) from exc
        return str(resolved)


class CloudImageTemplateBuildResponse(BaseModel):
    endpoint_id: Optional[int] = None
    target_node: Optional[str] = None
    vmid: int
    template_vmid: Optional[int] = None
    name: str
    status: str
    image_volid: str = ""
    download_upid: Optional[str] = None
    create_upid: Optional[str] = None
    template_upid: Optional[str] = None
    boot: Optional[str] = None
    scsi0: Optional[str] = None
    ide2: Optional[str] = None
    generated_userdata: Optional[str] = None
    pipeline_name: str = "Cloud Image Build Pipeline"
    product_type: Optional[ProxmoxProductType] = None
    product_version: Optional[str] = None
    provider: Optional[CloudImageBuildProvider] = None
    image_url: Optional[str] = None
    source_tree_path: Optional[str] = None
    source_artifact_path: Optional[str] = None
    first_boot_script: Optional[str] = None
    build_script: str = ""
    commands: list[str] = Field(default_factory=list)
    operator_instructions: str = ""
    execution_enabled: bool = False
    returncode: Optional[int] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None


class AzureVhdImportRequest(BaseModel):
    """Plan or execute an Azure-exported VHD import into a Proxmox VM shell."""

    model_config = ConfigDict(extra="forbid")

    endpoint_id: int | None = Field(
        None,
        description="Configured ProxmoxEndpoint primary key; required when execute=true.",
    )
    target_node: str = Field(..., min_length=1, description="Proxmox node that will own the VM.")
    vmid: int = Field(..., ge=100, description="Destination VMID to create.")
    name: str = Field(..., min_length=1, max_length=128, description="Destination VM name.")
    azure_vhd_url: str = Field(..., min_length=1, description="Azure SAS URL for the exported VHD.")
    source_vhd_filename: str | None = Field(
        None,
        description="Optional filename override for the downloaded Azure VHD artifact.",
    )
    vm_storage: str = Field("local-zfs", min_length=1, description="Target Proxmox VM storage.")
    bridge: str = Field("vmbr0", min_length=1)
    vlan_tag: int | None = Field(None, ge=1, le=4094)
    memory_mb: int = Field(8192, ge=64)
    cores: int = Field(4, ge=1)
    cpu: str = Field("host", min_length=1)
    vm_generation: AzureVmGeneration = AzureVmGeneration.gen2
    guest_profile: AzureVhdGuestProfile = AzureVhdGuestProfile.linux_standard
    enable_agent: bool = True
    description: str | None = Field(None, max_length=8192)
    execute: bool = False
    ssh_host: str | None = None
    ssh_user: str = "root"
    ssh_port: int = Field(22, ge=1, le=65535)
    ssh_identity_file: str | None = None

    @field_validator("azure_vhd_url")
    @classmethod
    def validate_azure_vhd_url_ssrf(cls, value: str) -> str:
        safe, reason = validate_endpoint_url(value, get_settings())
        if not safe:
            raise ValueError(f"azure_vhd_url rejected by SSRF protection: {reason}")
        return value

    @field_validator("ssh_host")
    @classmethod
    def validate_ssh_host(cls, value: str | None) -> str | None:
        if value is None:
            return value
        host = value.strip()
        if not host:
            raise ValueError("ssh_host must be a non-empty hostname or IP address.")
        if host.startswith("-"):
            raise ValueError("ssh_host must not start with '-' or resemble an ssh option.")
        if "%" in host:
            raise ValueError("ssh_host must not include an IPv6 zone identifier.")
        try:
            ipaddress.ip_address(host)
            return host
        except ValueError:
            pass
        if not _is_valid_hostname(host):
            raise ValueError("ssh_host must be a valid hostname, IPv4 address, or IPv6 address.")
        return host

    @field_validator("ssh_user")
    @classmethod
    def validate_ssh_user(cls, value: str) -> str:
        if not _SSH_USER_RE.fullmatch(value):
            raise ValueError("ssh_user must match ^[a-zA-Z0-9_][a-zA-Z0-9_-]{0,63}$.")
        return value

    @field_validator("ssh_identity_file")
    @classmethod
    def validate_ssh_identity_file(cls, value: str | None) -> str | None:
        if value is None:
            return value
        resolved = Path(value).resolve()
        allowed_dir = _ssh_key_dir()
        try:
            resolved.relative_to(allowed_dir)
        except ValueError as exc:
            raise ValueError(
                f"ssh_identity_file must resolve under PROXBOX_SSH_KEY_DIR ({allowed_dir})."
            ) from exc
        return str(resolved)


class AzureVhdImportResponse(BaseModel):
    pipeline_name: str = "Azure VHD Import Pipeline"
    status: str
    endpoint_id: int | None = None
    target_node: str
    vmid: int
    name: str
    vm_generation: AzureVmGeneration
    guest_profile: AzureVhdGuestProfile
    bios: str
    machine: str | None = None
    disk_interface: str
    network_model: str
    boot_order: str
    azure_vhd_url: str
    source_vhd_filename: str
    source_vhd_path: str
    qcow2_filename: str
    qcow2_path: str
    build_script: str = ""
    commands: list[str] = Field(default_factory=list)
    follow_up_steps: list[str] = Field(default_factory=list)
    operator_instructions: str = ""
    execution_enabled: bool = False
    returncode: int | None = None
    stdout: str | None = None
    stderr: str | None = None


class PVETemplateBuildRequest(BaseModel):
    """Build an unattended Proxmox VE installer template on a Proxmox host.

    The resulting VM, once booted, runs cloud-init against a Debian 12 cloud
    image and installs a pinned ``proxmox-ve`` release on top. The endpoint
    handles the parts it can do through the Proxmox API directly (image
    download + VM create with ``--cicustom``) and returns the rendered
    cloud-init snippet content alongside the operator-side instructions
    needed to drop the snippets into ``/var/lib/vz/snippets/`` on the host.
    """

    model_config = ConfigDict(extra="forbid")

    endpoint_id: int = Field(..., description="ProxmoxEndpoint primary key")
    vmid: int = Field(..., ge=100, description="Template VMID to create")
    name: str = Field("debian12-pve-tmpl", min_length=1, max_length=128)
    target_node: str = Field(..., min_length=1)
    debian_image_url: str = Field(
        "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
        min_length=1,
        description="HTTP(S) URL for the Debian 12 generic cloud image",
    )
    image_filename: str | None = Field(
        None,
        description="Filename to store in Proxmox import storage; .img is normalized to .qcow2",
    )
    image_storage: str = Field("local", min_length=1)
    vm_storage: str = Field("local-lvm", min_length=1)
    snippets_storage: str = Field("local", min_length=1)
    bridge: str = Field("vmbr0", min_length=1)
    memory_mb: int = Field(4096, ge=512)
    cores: int = Field(4, ge=1)
    nic_name: str = Field("ens18", min_length=1)
    hostname: str = Field("pve-node-01", min_length=1)
    domain: str = Field("nmulti.local", min_length=1)
    node_cidr: str = Field("10.0.30.50/24", min_length=1)
    gateway: str = Field("10.0.30.1", min_length=1)
    nameservers: list[str] = Field(default_factory=lambda: ["1.1.1.1", "8.8.8.8"])
    pve_version_pin: str = Field("9.1.11", min_length=1)
    debian_release: str = Field("bookworm", min_length=1)
    ssh_authorized_keys: list[str] = Field(
        default_factory=list,
        description="Operator SSH public keys to inject as root authorized_keys",
    )
    verify_image_certificates: bool = True
    create_vm: bool = Field(
        True,
        description="If false, only render cloud-init payloads and do not touch Proxmox.",
    )


class PVETemplateBuildResponse(BaseModel):
    """Result of a PVE template build request.

    The response always carries the rendered cloud-init payloads so callers
    (UI or operator) can persist them into the host's snippets directory.
    ``status`` summarizes whether the API created the VM, or just rendered
    the payloads (``create_vm=false``), or detected an existing VMID.
    """

    endpoint_id: int
    target_node: str
    vmid: int
    name: str
    status: str
    image_volid: str
    snippet_user_data_path: str
    snippet_network_config_path: str
    snippet_meta_data_path: str
    user_data: str
    network_config: str
    meta_data: str
    qm_cicustom: str
    operator_instructions: str
    download_upid: Optional[str] = None
    create_upid: Optional[str] = None
