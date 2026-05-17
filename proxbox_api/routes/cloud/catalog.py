"""Version catalog for the Cloud Image Build Pipeline."""

from __future__ import annotations

from proxbox_api.schemas.cloud_provision import (
    CloudImageBuildProvider,
    CloudImageProductType,
    CloudImageVersionEntry,
)


PRODUCT_CATALOG: dict[CloudImageProductType, list[CloudImageVersionEntry]] = {
    CloudImageProductType.PVE: [
        CloudImageVersionEntry(
            version="9.1.11",
            label="Proxmox VE 9.1.11 on Debian Bookworm",
            product_type=CloudImageProductType.PVE,
            default_provider=CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE,
            supported_providers=[CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE],
            os_family="proxmox-pve",
            os_release="bookworm",
            image_url=(
                "https://cloud.debian.org/images/cloud/bookworm/latest/"
                "debian-12-genericcloud-amd64.qcow2"
            ),
            debian_codename="bookworm",
            package_name="proxmox-ve",
            repo_component="pve-no-subscription",
            repo_suite="pve",
            service_name="pveproxy",
        ),
        CloudImageVersionEntry(
            version="8.4",
            label="Proxmox VE 8.4 on Debian Bookworm",
            product_type=CloudImageProductType.PVE,
            default_provider=CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE,
            supported_providers=[CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE],
            os_family="proxmox-pve",
            os_release="bookworm",
            image_url=(
                "https://cloud.debian.org/images/cloud/bookworm/latest/"
                "debian-12-genericcloud-amd64.qcow2"
            ),
            debian_codename="bookworm",
            package_name="proxmox-ve",
            repo_component="pve-no-subscription",
            repo_suite="pve",
            service_name="pveproxy",
        ),
    ],
    CloudImageProductType.PBS: [
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
            source_tree_path="nmulticloud-context/pfsense",
            source_build_command="./build.sh memstickserial",
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
            source_tree_path="nmulticloud-context/pfsense",
            source_build_command="./build.sh memstickserial",
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
            source_tree_path="nmulticloud-context/opnsense",
            source_build_command="make dvd",
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
            source_tree_path="nmulticloud-context/opnsense",
            source_build_command="make dvd",
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
