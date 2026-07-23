"""Version catalog for the Cloud Image Build Pipeline."""

from __future__ import annotations

from fastapi import APIRouter

from proxbox_api.schemas.cloud_provision import (
    CloudImageBuildProvider,
    CloudImageProductType,
    CloudImageSourceBuildCommand,
    CloudImageVersionEntry,
)

versions_router = APIRouter()


PRODUCT_CATALOG: dict[CloudImageProductType, list[CloudImageVersionEntry]] = {
    CloudImageProductType.PVE: [
        CloudImageVersionEntry(
            version="9.1.11",
            label="Proxmox VE 9.1 installer ISO",
            product_type=CloudImageProductType.PVE,
            default_provider=CloudImageBuildProvider.PROXMOX_ISO,
            supported_providers=[CloudImageBuildProvider.PROXMOX_ISO],
            os_family="proxmox-pve",
            os_release="9",
            image_url="https://enterprise.proxmox.com/iso/proxmox-ve_9.1-1.iso",
            notes=(
                "Proxmox VE must be installed from the official installer ISO; "
                "do not build PVE by installing proxmox-ve on a Debian cloud image."
            ),
        ),
        CloudImageVersionEntry(
            version="8.4",
            label="Proxmox VE 8.4 installer ISO",
            product_type=CloudImageProductType.PVE,
            default_provider=CloudImageBuildProvider.PROXMOX_ISO,
            supported_providers=[CloudImageBuildProvider.PROXMOX_ISO],
            os_family="proxmox-pve",
            os_release="8",
            image_url="https://enterprise.proxmox.com/iso/proxmox-ve_8.4-1.iso",
            notes=(
                "Proxmox VE must be installed from the official installer ISO; "
                "do not build PVE by installing proxmox-ve on a Debian cloud image."
            ),
        ),
    ],
    CloudImageProductType.PBS: [
        CloudImageVersionEntry(
            version="4.2",
            label="Proxmox Backup Server 4.2 on Debian Trixie",
            product_type=CloudImageProductType.PBS,
            default_provider=CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE,
            supported_providers=[CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE],
            os_family="proxmox-pbs",
            os_release="trixie",
            image_url=(
                "https://cloud.debian.org/images/cloud/trixie/latest/"
                "debian-13-genericcloud-amd64.qcow2"
            ),
            debian_codename="trixie",
            package_name="proxmox-backup-server",
            repo_component="pbs-no-subscription",
            repo_suite="pbs",
            service_name="proxmox-backup-proxy",
            notes="Latest PBS ISO on the Proxmox download page is 4.2-1.",
        ),
        CloudImageVersionEntry(
            version="3.3",
            label="Proxmox Backup Server 3.3 on Debian Bookworm",
            product_type=CloudImageProductType.PBS,
            default_provider=CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE,
            supported_providers=[CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE],
            os_family="proxmox-pbs",
            os_release="bookworm",
            image_url=(
                "https://cloud.debian.org/images/cloud/bookworm/latest/"
                "debian-12-genericcloud-amd64.qcow2"
            ),
            debian_codename="bookworm",
            package_name="proxmox-backup-server",
            repo_component="pbs-no-subscription",
            repo_suite="pbs",
            service_name="proxmox-backup-proxy",
        ),
    ],
    CloudImageProductType.PDM: [
        CloudImageVersionEntry(
            version="1.0",
            label="Proxmox Datacenter Manager 1.0 on Debian Bookworm",
            product_type=CloudImageProductType.PDM,
            default_provider=CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE,
            supported_providers=[CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE],
            os_family="proxmox-pdm",
            os_release="bookworm",
            image_url=(
                "https://cloud.debian.org/images/cloud/bookworm/latest/"
                "debian-12-genericcloud-amd64.qcow2"
            ),
            debian_codename="bookworm",
            package_name="proxmox-datacenter-manager",
            repo_component="pdm-test",
            repo_suite="pdm",
            service_name="proxmox-datacenter-api",
        ),
    ],
    CloudImageProductType.PFSENSE: [
        CloudImageVersionEntry(
            version="2.8.1",
            label="pfSense CE 2.8.1",
            product_type=CloudImageProductType.PFSENSE,
            default_provider=CloudImageBuildProvider.RELEASE_IMAGE,
            supported_providers=[
                CloudImageBuildProvider.RELEASE_IMAGE,
                CloudImageBuildProvider.SOURCE_TREE,
            ],
            os_family="pfsense",
            os_release="2.8.1",
            image_url=(
                "https://atxfiles.netgate.com/mirror/downloads/"
                "pfSense-CE-memstick-serial-2.8.1-RELEASE-amd64.img.gz"
            ),
            compression="gz",
            source_tree_path="/opt/proxbox/image-sources/pfsense",
            source_build_command=CloudImageSourceBuildCommand.PFSENSE_MEMSTICKSERIAL,
            notes="pfSense release-image URLs can be overridden per request.",
        ),
        CloudImageVersionEntry(
            version="2.8.0",
            label="pfSense CE 2.8.0",
            product_type=CloudImageProductType.PFSENSE,
            default_provider=CloudImageBuildProvider.RELEASE_IMAGE,
            supported_providers=[
                CloudImageBuildProvider.RELEASE_IMAGE,
                CloudImageBuildProvider.SOURCE_TREE,
            ],
            os_family="pfsense",
            os_release="2.8.0",
            image_url=(
                "https://atxfiles.netgate.com/mirror/downloads/"
                "pfSense-CE-memstick-serial-2.8.0-RELEASE-amd64.img.gz"
            ),
            compression="gz",
            source_tree_path="/opt/proxbox/image-sources/pfsense",
            source_build_command=CloudImageSourceBuildCommand.PFSENSE_MEMSTICKSERIAL,
        ),
    ],
    CloudImageProductType.OPNSENSE: [
        CloudImageVersionEntry(
            version="26.1.8",
            label="OPNsense 26.1.8",
            product_type=CloudImageProductType.OPNSENSE,
            default_provider=CloudImageBuildProvider.RELEASE_IMAGE,
            supported_providers=[
                CloudImageBuildProvider.RELEASE_IMAGE,
                CloudImageBuildProvider.SOURCE_TREE,
            ],
            os_family="opnsense",
            os_release="26.1",
            image_url=(
                "https://mirror.wdc1.us.leaseweb.net/opnsense/releases/26.1/"
                "OPNsense-26.1-serial-amd64.img.bz2"
            ),
            compression="bz2",
            source_tree_path="/opt/proxbox/image-sources/opnsense",
            source_build_command=CloudImageSourceBuildCommand.OPNSENSE_DVD,
            notes=(
                "Release images are installed from the 26.1 series and can be updated by "
                "OPNsense after first boot."
            ),
        ),
        CloudImageVersionEntry(
            version="26.1",
            label="OPNsense 26.1 serial image",
            product_type=CloudImageProductType.OPNSENSE,
            default_provider=CloudImageBuildProvider.RELEASE_IMAGE,
            supported_providers=[
                CloudImageBuildProvider.RELEASE_IMAGE,
                CloudImageBuildProvider.SOURCE_TREE,
            ],
            os_family="opnsense",
            os_release="26.1",
            image_url=(
                "https://mirror.wdc1.us.leaseweb.net/opnsense/releases/26.1/"
                "OPNsense-26.1-serial-amd64.img.bz2"
            ),
            sha256="aaca6d4c44371673c555be354317533cf91ced86fc86c026716325c29c451d79",
            compression="bz2",
            source_tree_path="/opt/proxbox/image-sources/opnsense",
            source_build_command=CloudImageSourceBuildCommand.OPNSENSE_DVD,
        ),
    ],
    CloudImageProductType.FIRECRACKER: [
        CloudImageVersionEntry(
            version="debian-12",
            label="Firecracker Host (Debian 12 Bookworm)",
            product_type=CloudImageProductType.FIRECRACKER,
            default_provider=CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE,
            supported_providers=[CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE],
            os_family="debian",
            os_release="bookworm",
            image_url=(
                "https://cloud.debian.org/images/cloud/bookworm/latest/"
                "debian-12-generic-amd64.qcow2"
            ),
            debian_codename="bookworm",
            os_codename="bookworm",
            notes="Proxmox VM template pre-configured as a Firecracker microVM host.",
        ),
        CloudImageVersionEntry(
            version="ubuntu-24.04",
            label="Firecracker Host (Ubuntu 24.04 LTS Noble)",
            product_type=CloudImageProductType.FIRECRACKER,
            default_provider=CloudImageBuildProvider.UBUNTU_CLOUD_IMAGE,
            supported_providers=[CloudImageBuildProvider.UBUNTU_CLOUD_IMAGE],
            os_family="ubuntu",
            os_release="noble",
            image_url=(
                "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
            ),
            os_codename="noble",
            notes="Proxmox VM template pre-configured as a Firecracker microVM host.",
        ),
    ],
}


def catalog_payload() -> dict[str, list[dict[str, object]]]:
    """Return JSON-serialisable catalog data keyed by product type."""
    return {
        product_type.value: [entry.model_dump(mode="json") for entry in entries]
        for product_type, entries in PRODUCT_CATALOG.items()
    }


def find_product_version(
    product_type: CloudImageProductType,
    version: str | None = None,
) -> CloudImageVersionEntry:
    """Find a catalog entry; when version is omitted the first entry is the default."""
    entries = PRODUCT_CATALOG.get(product_type, [])
    if not entries:
        raise KeyError(f"No Cloud Image Build Pipeline entries for {product_type.value}")
    if version is None:
        return entries[0]
    for entry in entries:
        if entry.version == version:
            return entry
    raise KeyError(f"Unsupported {product_type.value} version: {version}")


@versions_router.get(
    "/templates/versions",
    summary="List Cloud Image Build Pipeline product versions",
    response_model=dict[str, list[dict[str, object]]],
)
async def list_product_versions() -> dict[str, list[dict[str, object]]]:
    return catalog_payload()
