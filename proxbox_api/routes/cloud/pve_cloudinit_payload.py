"""Render PVE-installer cloud-init payloads.

This module produces three cloud-init artifacts that, when written into
Proxmox's snippet storage and wired through ``--cicustom``, drive an
unattended Proxmox VE install on top of a Debian 12 (Bookworm) base image:

* ``user-data`` — APT pin + repo + runcmd that installs ``proxmox-ve``.
* ``network-config`` — Netplan v2 that creates the ``vmbr0`` bridge.
* ``meta-data`` — declares the cloud-init ``instance-id``; a fresh value
  here is what forces cloud-init to re-run on a clone.

Source-of-truth for the static template lives in the ``nms`` repository
under ``cloud-init/pve-install/``. This module embeds the same content as
Python multi-line strings so ``proxbox-api`` does not need an out-of-tree
file at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from textwrap import dedent

DEFAULT_PVE_VERSION_PIN = "9.1.11"
DEFAULT_DEBIAN_RELEASE = "bookworm"
DEFAULT_NIC_NAME = "ens18"
DEFAULT_BRIDGE_NAME = "vmbr0"
DEFAULT_HOSTNAME = "pve-node-01"
DEFAULT_DOMAIN = "nmulti.local"
DEFAULT_NODE_CIDR = "10.0.30.50/24"
DEFAULT_GATEWAY = "10.0.30.1"
DEFAULT_NAMESERVERS = ("1.1.1.1", "8.8.8.8")


@dataclass(frozen=True)
class PVECloudInitPayloads:
    user_data: str
    network_config: str
    meta_data: str


def _format_ssh_keys(keys: list[str] | tuple[str, ...]) -> str:
    if not keys:
        return '      - "ssh-ed25519 AAAA... replace-with-operator-key"'
    lines = []
    for key in keys:
        key_clean = key.strip().replace('"', '\\"')
        lines.append(f'      - "{key_clean}"')
    return "\n".join(lines)


def render_user_data(
    *,
    hostname: str = DEFAULT_HOSTNAME,
    domain: str = DEFAULT_DOMAIN,
    nic_name: str = DEFAULT_NIC_NAME,
    pve_version_pin: str = DEFAULT_PVE_VERSION_PIN,
    debian_release: str = DEFAULT_DEBIAN_RELEASE,
    ssh_authorized_keys: list[str] | tuple[str, ...] = (),
) -> str:
    """Render the PVE-installer cloud-init ``user-data`` payload.

    The result is a ``#cloud-config`` document that, once cloud-init runs
    against a freshly-booted Debian 12 cloud image, will:

    1. Insert an FQDN/IP entry in ``/etc/hosts`` (required by pve-manager's
       postinstall — it calls ``hostname --fqdn`` during ``dpkg configure``).
    2. Add the Proxmox no-subscription APT repo and pin ``pve-manager`` /
       ``proxmox-ve`` / ``pve-kernel-6.8`` at the requested version.
    3. Install ``proxmox-ve``, ``postfix``, ``open-iscsi``, ``chrony``.
    4. Remove the stock Debian kernel and ``os-prober`` (hypervisor hygiene).
    5. Run ``update-grub`` so the PVE kernel becomes the default entry.
    6. Reboot into the PVE kernel.
    """
    fqdn = f"{hostname}.{domain}"
    ssh_block = _format_ssh_keys(ssh_authorized_keys)

    return dedent(
        f"""\
        #cloud-config
        # Unattended Proxmox VE {pve_version_pin} installation on top of Debian 12 ({debian_release}).
        hostname: {hostname}
        fqdn: {fqdn}
        manage_etc_hosts: false

        bootcmd:
          - |
            NODE_IP=$(ip -4 addr show {nic_name} | grep -oP '(?<=inet )\\d+\\.\\d+\\.\\d+\\.\\d+' | head -1)
            if [ -n "$NODE_IP" ] && ! grep -q "{fqdn}" /etc/hosts; then
              echo "${{NODE_IP}}  {fqdn}  {hostname}" >> /etc/hosts
            fi

        package_update: false
        package_upgrade: false

        write_files:
          - path: /etc/apt/preferences.d/pve-pin
            permissions: '0644'
            content: |
              Package: pve-manager
              Pin: version {pve_version_pin}*
              Pin-Priority: 1001

              Package: proxmox-ve
              Pin: version {pve_version_pin}*
              Pin-Priority: 1001

              Package: pve-kernel-6.8
              Pin: version *
              Pin-Priority: 1001

        users:
          - name: root
            ssh_authorized_keys:
{ssh_block}

        runcmd:
          - rm -f /etc/apt/sources.list.d/pve-no-subscription.list /etc/apt/sources.list.d/pve-enterprise.list /etc/apt/sources.list.d/pve-enterprise.sources /etc/apt/sources.list.d/ceph.list /etc/apt/sources.list.d/ceph.sources
          - DEBIAN_FRONTEND=noninteractive apt-get update -y
          - DEBIAN_FRONTEND=noninteractive apt-get install -y curl gnupg ca-certificates
          - curl -fsSL https://enterprise.proxmox.com/debian/proxmox-release-{debian_release}.gpg -o /etc/apt/trusted.gpg.d/proxmox-release-{debian_release}.gpg
          - printf '%s\\n' 'deb http://download.proxmox.com/debian/pve {debian_release} pve-no-subscription' > /etc/apt/sources.list.d/pve-no-subscription.list
          - DEBIAN_FRONTEND=noninteractive apt-get update -y
          - printf 'grub-pc grub-pc/install_devices multiselect %s\\n' "$root_disk" | debconf-set-selections
          - printf 'grub-pc grub-pc/install_devices_empty boolean false\\n' | debconf-set-selections
          - DEBIAN_FRONTEND=noninteractive apt-get install -y proxmox-ve postfix open-iscsi chrony
          - DEBIAN_FRONTEND=noninteractive apt-get remove -y linux-image-amd64 os-prober
          - update-grub

        power_state:
          mode: reboot
          message: "PVE {pve_version_pin} installation complete — rebooting into PVE kernel"
          timeout: 30
          condition: true
        """
    )


def render_network_config(
    *,
    nic_name: str = DEFAULT_NIC_NAME,
    bridge_name: str = DEFAULT_BRIDGE_NAME,
    node_cidr: str = DEFAULT_NODE_CIDR,
    gateway: str = DEFAULT_GATEWAY,
    nameservers: tuple[str, ...] = DEFAULT_NAMESERVERS,
    search_domain: str = DEFAULT_DOMAIN,
) -> str:
    """Render the cloud-init Netplan v2 ``network-config`` payload."""
    ns_addrs = ", ".join(nameservers)
    return dedent(
        f"""\
        version: 2
        ethernets:
          {nic_name}:
            dhcp4: false

        bridges:
          {bridge_name}:
            interfaces: [{nic_name}]
            addresses: [{node_cidr}]
            gateway4: {gateway}
            nameservers:
              addresses: [{ns_addrs}]
              search: [{search_domain}]
            parameters:
              stp: false
              forward-delay: 0
        """
    )


def render_meta_data(
    *,
    instance_id: str,
    hostname: str = DEFAULT_HOSTNAME,
) -> str:
    """Render the cloud-init ``meta-data`` payload.

    ``instance_id`` is the load-bearing field: cloud-init silently skips
    re-running if a clone inherits the same value, so callers must pass a
    fresh identifier (e.g. ``f"{hostname}-{int(time.time())}"``) when
    rendering meta-data for a new clone.
    """
    return dedent(
        f"""\
        instance-id: {instance_id}
        local-hostname: {hostname}
        """
    )


def render_all(
    *,
    hostname: str = DEFAULT_HOSTNAME,
    domain: str = DEFAULT_DOMAIN,
    nic_name: str = DEFAULT_NIC_NAME,
    bridge_name: str = DEFAULT_BRIDGE_NAME,
    pve_version_pin: str = DEFAULT_PVE_VERSION_PIN,
    debian_release: str = DEFAULT_DEBIAN_RELEASE,
    node_cidr: str = DEFAULT_NODE_CIDR,
    gateway: str = DEFAULT_GATEWAY,
    nameservers: tuple[str, ...] = DEFAULT_NAMESERVERS,
    instance_id: str | None = None,
    ssh_authorized_keys: list[str] | tuple[str, ...] = (),
) -> PVECloudInitPayloads:
    """Convenience: render all three payloads in one call."""
    resolved_iid = instance_id or hostname
    return PVECloudInitPayloads(
        user_data=render_user_data(
            hostname=hostname,
            domain=domain,
            nic_name=nic_name,
            pve_version_pin=pve_version_pin,
            debian_release=debian_release,
            ssh_authorized_keys=ssh_authorized_keys,
        ),
        network_config=render_network_config(
            nic_name=nic_name,
            bridge_name=bridge_name,
            node_cidr=node_cidr,
            gateway=gateway,
            nameservers=nameservers,
            search_domain=domain,
        ),
        meta_data=render_meta_data(
            instance_id=resolved_iid,
            hostname=hostname,
        ),
    )
