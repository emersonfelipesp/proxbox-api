"""Proxmox product version catalog and cloud-init user-data generator.

Supports three Proxmox products that can be installed from a Debian cloud image via
cloud-init at first boot:
  - Proxmox VE  (pve)
  - Proxmox Backup Server  (pbs)
  - Proxmox Datacenter Manager  (pdm)

Adding a new release requires only one extra ``ProxmoxProductVersion`` entry in
``PRODUCT_CATALOG`` — no other code changes are needed.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter

from proxbox_api.schemas.cloud_provision import ProxmoxProductType

versions_router = APIRouter()


@dataclass(frozen=True)
class ProxmoxProductVersion:
    """A specific release of a Proxmox product, tied to its Debian base."""

    version: str
    debian_codename: str
    package_name: str
    repo_component: str
    repo_suite: str
    extra_services: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "debian_codename": self.debian_codename,
            "package_name": self.package_name,
            "repo_component": self.repo_component,
            "repo_suite": self.repo_suite,
        }


PRODUCT_CATALOG: dict[ProxmoxProductType, list[ProxmoxProductVersion]] = {
    ProxmoxProductType.pve: [
        ProxmoxProductVersion(
            version="9.2",
            debian_codename="bookworm",
            package_name="proxmox-ve",
            repo_component="pve-no-subscription",
            repo_suite="pve",
            extra_services=["pve-cluster", "pvedaemon", "pvestatd"],
        ),
        ProxmoxProductVersion(
            version="8.4",
            debian_codename="bookworm",
            package_name="proxmox-ve",
            repo_component="pve-no-subscription",
            repo_suite="pve",
            extra_services=["pve-cluster", "pvedaemon", "pvestatd"],
        ),
        ProxmoxProductVersion(
            version="8.3",
            debian_codename="bookworm",
            package_name="proxmox-ve",
            repo_component="pve-no-subscription",
            repo_suite="pve",
            extra_services=["pve-cluster", "pvedaemon", "pvestatd"],
        ),
        ProxmoxProductVersion(
            version="7.4",
            debian_codename="bullseye",
            package_name="proxmox-ve",
            repo_component="pve-no-subscription",
            repo_suite="pve",
            extra_services=["pve-cluster", "pvedaemon", "pvestatd"],
        ),
    ],
    ProxmoxProductType.pbs: [
        ProxmoxProductVersion(
            version="3.3",
            debian_codename="bookworm",
            package_name="proxmox-backup-server",
            repo_component="pbs-no-subscription",
            repo_suite="pbs",
            extra_services=["proxmox-backup", "proxmox-backup-proxy"],
        ),
        ProxmoxProductVersion(
            version="3.2",
            debian_codename="bookworm",
            package_name="proxmox-backup-server",
            repo_component="pbs-no-subscription",
            repo_suite="pbs",
            extra_services=["proxmox-backup", "proxmox-backup-proxy"],
        ),
        ProxmoxProductVersion(
            version="2.4",
            debian_codename="bullseye",
            package_name="proxmox-backup-server",
            repo_component="pbs-no-subscription",
            repo_suite="pbs",
            extra_services=["proxmox-backup", "proxmox-backup-proxy"],
        ),
    ],
    ProxmoxProductType.pdm: [
        ProxmoxProductVersion(
            version="1.0",
            debian_codename="bookworm",
            package_name="proxmox-datacenter-manager",
            repo_component="pdm-no-subscription",
            repo_suite="pdm",
            extra_services=["proxmox-datacenter-manager"],
        ),
    ],
}

_DEBIAN_CLOUD_IMAGES: dict[str, str] = {
    "bookworm": (
        "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2"
    ),
    "bullseye": (
        "https://cloud.debian.org/images/cloud/bullseye/latest/debian-11-genericcloud-amd64.qcow2"
    ),
}


def find_product_version(
    product_type: ProxmoxProductType,
    version: str | None,
) -> ProxmoxProductVersion | None:
    """Return the catalog entry for *product_type* + *version*.

    If *version* is ``None`` or empty the latest entry (first in the list) is
    returned.  Returns ``None`` when the product or version is not found.
    """
    versions = PRODUCT_CATALOG.get(product_type)
    if not versions:
        return None
    if not version:
        return versions[0]
    for pv in versions:
        if pv.version == version:
            return pv
    return None


def default_image_url_for(product_version: ProxmoxProductVersion) -> str:
    """Suggest a Debian cloud image URL appropriate for the product version."""
    return _DEBIAN_CLOUD_IMAGES.get(
        product_version.debian_codename,
        _DEBIAN_CLOUD_IMAGES["bookworm"],
    )


def generate_cloud_init_userdata(
    product_type: ProxmoxProductType,
    pv: ProxmoxProductVersion,
) -> str:
    """Generate a ``#cloud-config`` YAML that installs *product_type* at first boot.

    The returned YAML can be passed as a Proxmox ``cicustom`` snippet or provided
    as ``user_data_yaml`` when provisioning a VM from the built template.
    """
    codename = pv.debian_codename
    suite = pv.repo_suite
    component = pv.repo_component
    package = pv.package_name
    services = pv.extra_services

    enable_cmds = "\n".join(f"  - systemctl enable {svc}" for svc in services)
    gpg_url = f"https://enterprise.proxmox.com/debian/proxmox-release-{codename}.gpg"

    product_label = {
        ProxmoxProductType.pve: "Proxmox VE",
        ProxmoxProductType.pbs: "Proxmox Backup Server",
        ProxmoxProductType.pdm: "Proxmox Datacenter Manager",
    }.get(product_type, str(product_type))

    header = (
        f"# cloud-init user-data for {product_label} {pv.version} "
        f"on Debian {codename.capitalize()}\n"
        "# Generated by proxbox-api. Use as cicustom snippet or provisioning user-data.\n"
    )

    yaml_body = textwrap.dedent(f"""\
        #cloud-config
        package_update: true
        package_upgrade: true
        write_files:
          - path: /etc/apt/sources.list.d/{suite}-install-repo.list
            content: |
              deb [arch=amd64] http://download.proxmox.com/debian/{suite} {codename} {component}
        runcmd:
          - curl -fsSL -o /etc/apt/trusted.gpg.d/proxmox-release-{codename}.gpg {gpg_url}
          - apt-get update
          - DEBIAN_FRONTEND=noninteractive apt-get install -y {package}
        {enable_cmds}
        power_state:
          mode: poweroff
          condition: True
        """)

    return header + yaml_body


@versions_router.get(
    "/templates/versions",
    summary="List all known Proxmox product versions",
    response_model=dict[str, list[dict[str, str]]],
)
async def list_product_versions() -> dict[str, list[dict[str, str]]]:
    """Return the version catalog keyed by product type.

    The frontend version dropdown is populated from this endpoint.
    Adding a new Proxmox release to ``PRODUCT_CATALOG`` makes it appear here
    automatically.
    """
    return {
        product_type.value: [pv.to_dict() for pv in versions]
        for product_type, versions in PRODUCT_CATALOG.items()
    }
