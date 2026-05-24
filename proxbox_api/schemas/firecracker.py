"""Firecracker host-agent and Cloud provisioning schemas."""

from __future__ import annotations

from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FirecrackerNetworkMode(str, Enum):
    nat = "nat"
    bridge = "bridge"


class FirecrackerMicroVMAction(str, Enum):
    start = "start"
    stop = "stop"
    pause = "pause"
    resume = "resume"
    reboot = "reboot"
    delete = "delete"


class FirecrackerImageBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_id: int | None = None
    name: str = Field(..., min_length=1, max_length=255)
    architecture: str = Field("x86_64", min_length=1, max_length=32)
    kernel_image_url: str = Field(..., min_length=1)
    kernel_image_sha256: str = Field(..., min_length=64, max_length=64)
    rootfs_image_url: str = Field(..., min_length=1)
    rootfs_image_sha256: str = Field(..., min_length=64, max_length=64)
    default_kernel_args: str = ""
    default_user: str = Field("cloud-user", min_length=1, max_length=64)

    @field_validator("kernel_image_sha256", "rootfs_image_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if any(char not in "0123456789abcdefABCDEF" for char in value):
            raise ValueError("sha256 fields must be hexadecimal")
        return value.lower()


class FirecrackerNetworkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: FirecrackerNetworkMode = FirecrackerNetworkMode.nat
    bridge_name: str | None = None
    tap_name: str | None = None
    guest_ip: str | None = None
    gateway: str | None = None
    nameservers: list[str] = Field(default_factory=list)


class FirecrackerAssetPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image: FirecrackerImageBundle


class FirecrackerAssetPrepareResponse(BaseModel):
    kernel_image_path: str
    rootfs_image_path: str
    kernel_ready: bool = True
    rootfs_ready: bool = True


class FirecrackerHostAgentHealth(BaseModel):
    ok: bool = True
    status: str = "ready"
    firecracker_version: str | None = None
    kvm_available: bool = True


class FirecrackerHostCapabilities(BaseModel):
    supports_nat: bool = True
    supports_bridge: bool = False
    max_vcpus: int = Field(0, ge=0)
    max_memory_mib: int = Field(0, ge=0)
    max_disk_mib: int = Field(0, ge=0)
    available_vcpus: int = Field(0, ge=0)
    available_memory_mib: int = Field(0, ge=0)
    available_disk_mib: int = Field(0, ge=0)


class FirecrackerMicroVMCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    microvm_id: UUID = Field(default_factory=uuid4)
    name: str = Field(..., min_length=1, max_length=255)
    image: FirecrackerImageBundle
    network: FirecrackerNetworkRequest = Field(default_factory=FirecrackerNetworkRequest)
    vcpus: int = Field(1, ge=1)
    memory_mib: int = Field(512, ge=64)
    disk_mib: int = Field(1024, ge=1)
    ssh_authorized_keys: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FirecrackerMicroVMState(BaseModel):
    microvm_id: UUID
    name: str
    status: str
    network_mode: FirecrackerNetworkMode = FirecrackerNetworkMode.nat
    guest_ip: str | None = None
    mac_address: str | None = None
    vcpus: int = 1
    memory_mib: int = 512
    disk_mib: int = 1024


class FirecrackerMicroVMMetrics(BaseModel):
    microvm_id: UUID
    cpu_time_us: int = 0
    memory_rss_mib: int = 0
    rx_bytes: int = 0
    tx_bytes: int = 0


class FirecrackerProvisionRequest(BaseModel):
    """Cloud-facing request passed from NMS Backend to proxbox-api."""

    model_config = ConfigDict(extra="forbid")

    host_agent_base_url: str = Field(..., min_length=1)
    host_agent_token: str | None = Field(None, max_length=4096)
    host_id: int | None = None
    host_pool_id: int | None = None
    image: FirecrackerImageBundle
    netbox_microvm_id: int | None = None
    microvm_id: UUID = Field(default_factory=uuid4)
    name: str = Field(..., min_length=1, max_length=255)
    tenant_id: int | None = None
    network: FirecrackerNetworkRequest = Field(default_factory=FirecrackerNetworkRequest)
    vcpus: int = Field(1, ge=1)
    memory_mib: int = Field(512, ge=64)
    disk_mib: int = Field(1024, ge=1)
    ssh_authorized_keys: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    start_after_provision: bool = True

    @property
    def instance_ref(self) -> str | None:
        if self.netbox_microvm_id is None:
            return None
        return f"firecracker:{self.netbox_microvm_id}"


class FirecrackerProvisionResponse(BaseModel):
    ok: bool = True
    microvm_id: UUID
    instance_ref: str | None = None
    host_id: int | None = None
    host_pool_id: int | None = None
    image_id: int | None = None
    status: str
    guest_ip: str | None = None
    detail: str | None = None
