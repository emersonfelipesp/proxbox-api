"""Proxmox cloud-init user-data generator for Debian-backed product builds.

Supports Proxmox products that can be installed from a Debian cloud image via
cloud-init at first boot:
  - Proxmox Backup Server  (pbs)
  - Proxmox Datacenter Manager  (pdm)

Proxmox VE catalog builds intentionally do not use this path; the mounted Cloud
Image Build Pipeline catalog requires the ``proxmox_iso`` provider and official
PVE installer ISO media.

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
    ProxmoxProductType.pbs: [
        ProxmoxProductVersion(
            version="4.2",
            debian_codename="trixie",
            package_name="proxmox-backup-server",
            repo_component="pbs-no-subscription",
            repo_suite="pbs",
            extra_services=["proxmox-backup", "proxmox-backup-proxy"],
        ),
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
    "trixie": (
        "https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2"
    ),
    "bookworm": (
        "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2"
    ),
    "bullseye": (
        "https://cloud.debian.org/images/cloud/bullseye/latest/debian-11-genericcloud-amd64.qcow2"
    ),
}

_UBUNTU_CLOUD_IMAGES: dict[str, str] = {
    "noble": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
    "jammy": "https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
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


def _product_label(product_type: ProxmoxProductType) -> str:
    return {
        ProxmoxProductType.pve: "Proxmox VE",
        ProxmoxProductType.pbs: "Proxmox Backup Server",
        ProxmoxProductType.pdm: "Proxmox Datacenter Manager",
    }.get(product_type, str(product_type))


def _resolve_agent_flags(
    product_type: ProxmoxProductType,
    *,
    install_qemu_guest_agent: bool | None,
    install_zabbix_agent2: bool | None,
) -> tuple[bool, bool]:
    return (
        product_type == ProxmoxProductType.pbs
        if install_qemu_guest_agent is None
        else install_qemu_guest_agent,
        product_type == ProxmoxProductType.pbs
        if install_zabbix_agent2 is None
        else install_zabbix_agent2,
    )


def _install_packages_and_services(
    pv: ProxmoxProductVersion,
    *,
    qga_enabled: bool,
    zabbix_enabled: bool,
) -> tuple[list[str], list[str]]:
    packages = [pv.package_name]
    services = list(pv.extra_services)
    if qga_enabled:
        packages.append("qemu-guest-agent")
        services.append("qemu-guest-agent")
    if zabbix_enabled:
        packages.append("zabbix-agent2")
        services.append("zabbix-agent2")
    return packages, list(dict.fromkeys(services))


def _zabbix_release_url(codename: str) -> str:
    debian_major = {"bullseye": "11", "bookworm": "12", "trixie": "13"}.get(codename, "13")
    return (
        "https://repo.zabbix.com/zabbix/7.4/release/debian/pool/main/z/"
        f"zabbix-release/zabbix-release_latest_7.4+debian{debian_major}_all.deb"
    )


def _append_dns_config(
    lines: list[str],
    *,
    search_domain: str | None,
    nameservers: list[str] | None,
) -> None:
    if not search_domain and not nameservers:
        return
    lines.extend(["manage_resolv_conf: true", "resolv_conf:"])
    if nameservers:
        lines.append("  nameservers:")
        lines.extend(f"    - {server}" for server in nameservers)
    if search_domain:
        lines.extend(["  searchdomains:", f"    - {search_domain}"])


def _append_zabbix_config(lines: list[str], zabbix_server: str) -> None:
    lines.extend(
        [
            "  - |",
            "    set -eu",
            "    install -d /etc/zabbix",
            "    touch /etc/zabbix/zabbix_agent2.conf",
            "    if grep -q '^Server=' /etc/zabbix/zabbix_agent2.conf; then",
            f"      sed -i 's|^Server=.*|Server={zabbix_server}|' /etc/zabbix/zabbix_agent2.conf",
            "    else",
            f"      printf 'Server={zabbix_server}\\n' >> /etc/zabbix/zabbix_agent2.conf",
            "    fi",
            "    if grep -q '^ServerActive=' /etc/zabbix/zabbix_agent2.conf; then",
            (
                f"      sed -i 's|^ServerActive=.*|ServerActive={zabbix_server}|' "
                "/etc/zabbix/zabbix_agent2.conf"
            ),
            "    else",
            f"      printf 'ServerActive={zabbix_server}\\n' >> /etc/zabbix/zabbix_agent2.conf",
            "    fi",
        ]
    )


def generate_cloud_init_userdata(
    product_type: ProxmoxProductType,
    pv: ProxmoxProductVersion,
    *,
    install_qemu_guest_agent: bool | None = None,
    install_zabbix_agent2: bool | None = None,
    zabbix_server: str = "zabbix.nmulti.cloud",
    search_domain: str | None = None,
    nameservers: list[str] | None = None,
) -> str:
    """Generate a ``#cloud-config`` YAML that installs *product_type* at first boot.

    The returned YAML can be passed as a Proxmox ``cicustom`` snippet or provided
    as ``user_data_yaml`` when provisioning a VM from the built template.
    """
    codename = pv.debian_codename
    suite = pv.repo_suite
    component = pv.repo_component
    qga_enabled, zabbix_enabled = _resolve_agent_flags(
        product_type,
        install_qemu_guest_agent=install_qemu_guest_agent,
        install_zabbix_agent2=install_zabbix_agent2,
    )
    install_packages, services = _install_packages_and_services(
        pv,
        qga_enabled=qga_enabled,
        zabbix_enabled=zabbix_enabled,
    )

    gpg_url = f"https://enterprise.proxmox.com/debian/proxmox-release-{codename}.gpg"
    header = (
        f"# cloud-init user-data for {_product_label(product_type)} {pv.version} "
        f"on Debian {codename.capitalize()}\n"
        "# Generated by proxbox-api. Use as cicustom snippet or provisioning user-data.\n"
    )

    lines = ["#cloud-config"]
    _append_dns_config(lines, search_domain=search_domain, nameservers=nameservers)
    lines.extend(
        [
            "package_update: true",
            "package_upgrade: true",
            "write_files:",
            f"  - path: /etc/apt/sources.list.d/{suite}-install-repo.list",
            "    content: |",
            f"      deb [arch=amd64] http://download.proxmox.com/debian/{suite} {codename} {component}",
            "runcmd:",
            f"  - curl -fsSL -o /etc/apt/trusted.gpg.d/proxmox-release-{codename}.gpg {gpg_url}",
        ]
    )
    if zabbix_enabled:
        lines.extend(
            [
                f"  - curl -fsSL -o /tmp/zabbix-release.deb {_zabbix_release_url(codename)}",
                "  - dpkg -i /tmp/zabbix-release.deb",
            ]
        )
    lines.extend(
        [
            "  - apt-get update",
            f"  - DEBIAN_FRONTEND=noninteractive apt-get install -y {' '.join(install_packages)}",
        ]
    )
    if zabbix_enabled:
        _append_zabbix_config(lines, zabbix_server)
    lines.extend(f"  - systemctl enable {svc}" for svc in services)
    lines.extend(["power_state:", "  mode: poweroff", "  condition: True"])

    return header + "\n".join(lines) + "\n"


def generate_firecracker_userdata(os_family: str, os_codename: str) -> str:
    """Generate a ``#cloud-config`` YAML that sets up a Firecracker microVM host.

    Installs KVM prerequisites, Firecracker binary, jailer, firectl, CNI
    plugins, and configures ``/dev/kvm`` permissions. Works on both Debian and
    Ubuntu base images.
    """
    fc_version = "1.12.0"

    header = (
        f"# cloud-init user-data for Firecracker host on {os_family.capitalize()} {os_codename}\n"
        "# Generated by proxbox-api. Use as cicustom snippet or provisioning user-data.\n"
    )

    yaml_body = textwrap.dedent(f"""\
        #cloud-config
        package_update: true
        package_upgrade: true
        packages:
          - qemu-kvm
          - linux-headers-amd64
          - curl
          - jq
          - iptables
          - iproute2
          - bridge-utils
          - net-tools
          - ca-certificates
        runcmd:
          # KVM module and permissions
          - modprobe kvm
          - modprobe kvm_intel || modprobe kvm_amd || true
          - 'echo "KERNEL==\\"kvm\\", GROUP=\\"kvm\\", MODE=\\"0660\\"" > /etc/udev/rules.d/99-kvm.rules'
          - udevadm control --reload-rules && udevadm trigger
          - usermod -aG kvm root || true
          # Firecracker {fc_version}
          - curl -fsSL -o /tmp/firecracker.tgz https://github.com/firecracker-microvm/firecracker/releases/download/v{fc_version}/firecracker-v{fc_version}-x86_64.tgz
          - tar -xzf /tmp/firecracker.tgz -C /tmp
          - install -m 0755 /tmp/release-v{fc_version}-x86_64/firecracker-v{fc_version}-x86_64 /usr/local/bin/firecracker
          - install -m 0755 /tmp/release-v{fc_version}-x86_64/jailer-v{fc_version}-x86_64 /usr/local/bin/jailer
          - rm -rf /tmp/firecracker.tgz /tmp/release-v{fc_version}-x86_64
          # CNI plugins
          - mkdir -p /opt/cni/bin
          - curl -fsSL -o /tmp/cni-plugins.tgz https://github.com/containernetworking/plugins/releases/download/v1.6.2/cni-plugins-linux-amd64-v1.6.2.tgz
          - tar -xzf /tmp/cni-plugins.tgz -C /opt/cni/bin
          - rm -f /tmp/cni-plugins.tgz
          # Kernel and rootfs helper directory
          - mkdir -p /var/lib/firecracker/images
          # Persist KVM modules across reboots
          - echo kvm >> /etc/modules-load.d/kvm.conf
          - echo kvm_intel >> /etc/modules-load.d/kvm.conf
          - echo kvm_amd >> /etc/modules-load.d/kvm.conf
        power_state:
          mode: poweroff
          condition: true
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
