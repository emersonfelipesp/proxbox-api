"""Cloud-init driven VM provisioning schemas."""

import ipaddress
from collections.abc import Mapping
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from proxbox_api.routes.intent.cloud_init import CloudInitPayload
from proxbox_api.schemas.cloud_image_security import (
    CloudImageSSHExecutionTarget as CloudImageSSHExecutionTarget,
)
from proxbox_api.schemas.cloud_image_security import (
    is_valid_hostname as _is_valid_hostname,
)
from proxbox_api.schemas.cloud_image_security import (
    normalize_ssh_fingerprint,
    normalize_ssh_host,
    normalize_ssh_identity_file,
    normalize_ssh_user,
)
from proxbox_api.settings_client import get_settings
from proxbox_api.ssrf import validate_endpoint_url


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


class CloudImageSourceBuildCommand(str, Enum):
    """Allowlisted source-build recipes exposed at the HTTP boundary."""

    pfsense_memstickserial = "pfsense_memstickserial"
    opnsense_dvd = "opnsense_dvd"
    PFSENSE_MEMSTICKSERIAL = "pfsense_memstickserial"
    OPNSENSE_DVD = "opnsense_dvd"

    @property
    def argv(self) -> tuple[str, ...]:
        """Return the fixed, server-installed recipe wrapper argv."""

        if self == CloudImageSourceBuildCommand.pfsense_memstickserial:
            return ("/usr/local/libexec/proxbox/build-pfsense-memstickserial",)
        return ("/usr/local/libexec/proxbox/build-opnsense-dvd",)

    @property
    def source_root(self) -> str:
        """Return the canonical, root-owned source root for this recipe."""

        if self == CloudImageSourceBuildCommand.pfsense_memstickserial:
            return "/opt/proxbox/image-sources/pfsense"
        return "/opt/proxbox/image-sources/opnsense"

    @property
    def artifact_relative_path(self) -> str:
        """Return the fixed recipe output path beneath :attr:`source_root`."""

        if self == CloudImageSourceBuildCommand.pfsense_memstickserial:
            return "artifacts/pfsense-memstickserial.img"
        return "artifacts/opnsense-dvd.img"

    @property
    def product_type(self) -> "ProxmoxProductType":
        if self == CloudImageSourceBuildCommand.pfsense_memstickserial:
            return ProxmoxProductType.pfsense
        return ProxmoxProductType.opnsense


def provider_requires_snippets(provider: CloudImageBuildProvider) -> bool:
    """Return the single provider-derived snippet requirement."""

    return provider != CloudImageBuildProvider.proxmox_iso


CloudImageProductType = ProxmoxProductType


class CloudImageBuildTarget(BaseModel):
    """Canonical target shared by planning, preflight, and execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_node: str = Field(
        ...,
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    vmid: int = Field(..., ge=100)
    provider: CloudImageBuildProvider
    image_storage: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    vm_storage: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    snippets_storage: str | None = Field(
        None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )

    @model_validator(mode="after")
    def validate_snippets_target(self) -> "CloudImageBuildTarget":
        if self.snippets_required and not self.snippets_storage:
            raise ValueError("snippets_storage is required for this provider.")
        return self

    @property
    def snippets_required(self) -> bool:
        return provider_requires_snippets(self.provider)

    @property
    def image_content_type(self) -> str | None:
        """Configured image-storage content, if the provider persists source media."""

        if self.provider == CloudImageBuildProvider.proxmox_iso:
            return "iso"
        return None

    def storage_requirements(self) -> tuple[tuple[str, str, str], ...]:
        """Return normalized (role, storage, content) requirements."""

        requirements: list[tuple[str, str, str]] = [("vm", self.vm_storage, "images")]
        if self.image_content_type is not None:
            requirements.insert(0, ("image", self.image_storage, self.image_content_type))
        if self.snippets_required and self.snippets_storage:
            requirements.append(("snippets", self.snippets_storage, "snippets"))
        return tuple(requirements)


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
    source_build_command: CloudImageSourceBuildCommand | None = None
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
    name: str = Field(
        "cloud-image-template",
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    target_node: str | None = Field(
        None,
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    image_url: str | None = Field(
        "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
        description="HTTP(S) URL for the source cloud image",
    )
    image_filename: str | None = Field(
        None,
        description="Filename to store in Proxmox import storage; .img is normalized to .qcow2",
    )
    image_storage: str = Field(
        "local", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    )
    vm_storage: str = Field(
        "local-zfs",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    snippets_storage: str = Field(
        "local", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
    )
    snippets_dir: str = Field("/var/lib/vz/snippets", min_length=1)
    memory_mb: int = Field(512, ge=64)
    cores: int = Field(1, ge=1)
    disk_size_gb: int | None = Field(None, ge=1)
    bridge: str = Field("vmbr0", min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_.:-]*$")
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
    source_tree_path: str | None = Field(
        None,
        max_length=512,
        description=(
            "Deprecated assertion only. Source builds use the server-owned canonical recipe root."
        ),
    )
    source_build_command: CloudImageSourceBuildCommand | None = Field(
        None,
        description=(
            "Allowlisted source-build recipe. Arbitrary shell command strings are rejected."
        ),
    )
    source_artifact_path: str | None = Field(
        None,
        max_length=512,
        description=(
            "Deprecated assertion only. Source builds use the server-owned recipe artifact."
        ),
    )
    execute: bool | None = None
    preflight_plan_token: str | None = Field(
        None,
        min_length=64,
        max_length=4096,
        repr=False,
        description="Signed, expiring plan returned by the exact preflight endpoint.",
    )
    include_sensitive_preview: bool = Field(
        False,
        description=(
            "Return the generated script and source material for an explicitly non-executing "
            "review request. The preview can contain credentials from cloud-init or signed URLs; "
            "it is rejected unless execute is explicitly false."
        ),
    )
    ssh_host: str | None = None
    ssh_user: str = "root"
    ssh_port: int = Field(22, ge=1, le=65535)
    ssh_identity_file: str | None = None
    ssh_known_host_fingerprint: str | None = None
    ssh_authorized_keys: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_storage_alias(cls, value: object) -> object:
        """Accept ``storage`` in v0.0.21 while keeping ``vm_storage`` authoritative."""

        if not isinstance(value, Mapping) or "storage" not in value:
            return value
        normalized = dict(value)
        legacy = normalized.pop("storage")
        current = normalized.get("vm_storage")
        if current is not None and str(current) != str(legacy):
            raise ValueError("storage and vm_storage must match during the 0.0.21 transition.")
        normalized["vm_storage"] = legacy
        return normalized

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
        if self.include_sensitive_preview and self.execute is not False:
            raise ValueError(
                "include_sensitive_preview requires execute=false explicitly; "
                "sensitive previews are unavailable to executable requests."
            )
        if self.source_build_command is not None:
            if self.provider != CloudImageBuildProvider.source_tree:
                raise ValueError("source_build_command requires provider=source_tree.")
            if self.source_build_command.product_type != self.product_type:
                raise ValueError(
                    "source_build_command is not compatible with the selected product_type."
                )
        return self

    @field_validator("snippets_dir")
    @classmethod
    def validate_legacy_snippets_dir(cls, value: str) -> str:
        if value != "/var/lib/vz/snippets":
            raise ValueError(
                "Custom snippets_dir mappings are unsupported; snippet paths are resolved "
                "from snippets_storage with pvesm path."
            )
        return value

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
        return normalize_ssh_host(value)

    @field_validator("ssh_user")
    @classmethod
    def validate_ssh_user(cls, value: str) -> str:
        return normalize_ssh_user(value)

    @field_validator("ssh_identity_file")
    @classmethod
    def validate_ssh_identity_file(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return normalize_ssh_identity_file(value)

    @field_validator("ssh_known_host_fingerprint")
    @classmethod
    def validate_ssh_known_host_fingerprint(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return normalize_ssh_fingerprint(value)

    def build_target(
        self,
        *,
        provider: CloudImageBuildProvider,
    ) -> CloudImageBuildTarget:
        """Return the canonical target consumed by preflight and rendering."""

        snippets_required = provider_requires_snippets(provider)
        return CloudImageBuildTarget(
            target_node=self.target_node or "pve-host",
            vmid=self.vmid,
            provider=provider,
            image_storage=self.image_storage,
            vm_storage=self.vm_storage,
            snippets_storage=self.snippets_storage if snippets_required else None,
        )


class PackerFindingSeverity(str, Enum):
    """Severity shared by Packer preflight and build diagnostics."""

    info = "info"
    warning = "warning"
    error = "error"


class PackerPreflightCapability(str, Enum):
    """Read-only capabilities verified before a Cloud Image Pipeline build."""

    endpoint_session = "endpoint_session"
    node_online = "node_online"
    image_storage_images = "image_storage_images"
    image_storage_iso = "image_storage_iso"
    vm_storage_images = "vm_storage_images"
    snippets_storage_snippets = "snippets_storage_snippets"
    vmid_available = "vmid_available"


class PackerPreflightCapabilityStatus(str, Enum):
    passed = "passed"
    failed = "failed"
    unsupported = "unsupported"


class PackerFinding(BaseModel):
    """Stable, secret-free diagnostic returned at the Packer API boundary."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1, max_length=64)
    severity: PackerFindingSeverity
    target: str = Field(..., min_length=1, max_length=320)
    message: str = Field(..., min_length=1, max_length=512)


class PackerPreflightCapabilityResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: PackerPreflightCapability
    status: PackerPreflightCapabilityStatus
    target: str = Field(..., min_length=1, max_length=320)


class CloudImageTemplatePreflightRequest(BaseModel):
    """Versioned, read-only validation request for a Cloud Image Pipeline target."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1.0"] = "1.0"
    endpoint_id: int = Field(..., ge=1, description="Persisted proxbox-api endpoint primary key.")
    target_node: str = Field(
        ...,
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    vmid: int = Field(..., ge=100)
    provider: CloudImageBuildProvider = CloudImageBuildProvider.release_image
    image_storage: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    vm_storage: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    snippets_storage: str | None = Field(
        None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    recipe_digest: str | None = Field(
        None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description=(
            "Opaque keyed binding returned by a non-executing server-rendered build plan. "
            "Required to issue an executable signed plan; omission retains read-only v1 "
            "readiness compatibility."
        ),
    )
    snippets_required: bool | None = Field(
        None,
        description=(
            "Deprecated compatibility assertion. The provider is authoritative for whether "
            "snippet storage is required."
        ),
    )

    @model_validator(mode="after")
    def validate_snippets_target(self) -> "CloudImageTemplatePreflightRequest":
        expected = provider_requires_snippets(self.provider)
        if self.snippets_required is not None and self.snippets_required != expected:
            raise ValueError("snippets_required must match the provider-derived requirement.")
        if expected and not self.snippets_storage:
            raise ValueError("snippets_storage is required for this provider.")
        return self

    def build_target(self) -> CloudImageBuildTarget:
        return CloudImageBuildTarget(
            target_node=self.target_node,
            vmid=self.vmid,
            provider=self.provider,
            image_storage=self.image_storage,
            vm_storage=self.vm_storage,
            snippets_storage=(
                self.snippets_storage if provider_requires_snippets(self.provider) else None
            ),
        )


class CloudImageTemplatePreflightResponse(BaseModel):
    """Typed result for the Packer preflight v1 contract."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["1.0"] = "1.0"
    endpoint_id: int
    target_node: str
    vmid: int
    ready: bool
    writes_enabled: bool
    recipe_digest: str | None = None
    plan_id: str | None = None
    plan_digest: str | None = None
    plan_token: str | None = Field(default=None, repr=False)
    expires_at: float | None = None
    capabilities: list[PackerPreflightCapabilityResult] = Field(default_factory=list)
    findings: list[PackerFinding] = Field(default_factory=list)


class CloudImageTemplateSensitivePreview(BaseModel):
    """Opt-in preview that can contain caller secrets and must not be logged."""

    model_config = ConfigDict(extra="forbid")

    warning: Literal[
        "Sensitive preview: may contain credentials, signed URLs, keys, and cloud-init secrets."
    ] = "Sensitive preview: may contain credentials, signed URLs, keys, and cloud-init secrets."
    image_url: str | None = None
    source_tree_path: str | None = None
    source_artifact_path: str | None = None
    generated_userdata: str | None = None
    first_boot_script: str | None = None
    build_script: str
    commands: list[str] = Field(default_factory=list)


class CloudImageTemplateExecutionSummary(BaseModel):
    """Bounded execution metadata; raw process output is deliberately excluded."""

    model_config = ConfigDict(extra="forbid")

    attempted: bool = False
    enabled: bool = False
    exit_code: int | None = None
    stdout_bytes: int = Field(0, ge=0)
    stderr_bytes: int = Field(0, ge=0)
    stdout_lines: int = Field(0, ge=0)
    stderr_lines: int = Field(0, ge=0)
    cancellation_attempted: bool = False
    cancellation_succeeded: bool | None = None


class CloudImageBuildOperationResponse(BaseModel):
    """Secret-free durable operation state returned to operators."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    endpoint_id: int
    target_node: str
    vmid: int
    provider: CloudImageBuildProvider
    state: Literal["leased", "running", "completed", "failed", "cancelled", "recovery_required"]
    recipe_digest: str
    plan_digest: str
    verified: bool
    recovery_required: bool
    cancel_requested: bool
    cancellation_succeeded: bool | None = None
    error_code: str | None = None
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    updated_at: float


class CloudImageTemplateBuildResponse(BaseModel):
    """Secret-safe v2 build response for the Cloud Image Pipeline."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["2.0"] = "2.0"
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
    pipeline_name: str = "Cloud Image Build Pipeline"
    product_type: Optional[ProxmoxProductType] = None
    product_version: Optional[str] = None
    provider: Optional[CloudImageBuildProvider] = None
    recipe_digest: str = ""
    operation_id: str | None = None
    verified: bool = False
    recovery_required: bool = False
    operator_instructions: str = ""
    execution_enabled: bool = False
    returncode: Optional[int] = None
    execution: CloudImageTemplateExecutionSummary = Field(
        default_factory=CloudImageTemplateExecutionSummary
    )
    diagnostics: list[PackerFinding] = Field(default_factory=list)
    sensitive_preview: CloudImageTemplateSensitivePreview | None = None


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
        return normalize_ssh_host(value)

    @field_validator("ssh_user")
    @classmethod
    def validate_ssh_user(cls, value: str) -> str:
        return normalize_ssh_user(value)

    @field_validator("ssh_identity_file")
    @classmethod
    def validate_ssh_identity_file(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return normalize_ssh_identity_file(value)


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
