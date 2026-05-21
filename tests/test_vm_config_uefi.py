"""Regression tests for VMConfig fields missing from the static whitelist.

Issue #144: VMs with bios=ovmf (UEFI/EFI boot) caused a ResponseValidationError
because 'bios' was not in VMConfig.model_fields and did not match the dynamic-key
regex ^(scsi|net|ide|unused|smbios)\\d+$.  Several other real QEMU config fields
(sata[n], virtio[n], efidisk0, machine, cpu, etc.) had the same problem.
"""

from __future__ import annotations

import pytest

from proxbox_api.schemas.virtualization import VMConfig

# ---------------------------------------------------------------------------
# The exact payload from issue #144
# ---------------------------------------------------------------------------

ISSUE_144_PAYLOAD: dict[str, object] = {
    "agent": "1",
    "bios": "ovmf",
    "boot": "order=sata0;sata1",
    "cores": 2,
    "cpu": "Cascadelake-Server-noTSX",
    "digest": "1c6a44c158cd25dd20fb45a56bcf2c3258abde1b",
    "efidisk0": "ceph-vm-data:vm-126-disk-0,size=128K",
    "machine": "pc-i440fx-9.0",
    "memory": "3072",
    "meta": "creation-qemu=9.0.2,ctime=1727867268",
    "name": "xx.example.example.com",
    "numa": False,
    "ostype": "win10",
    "scsihw": "virtio-scsi-single",
    "smbios1": "uuid=42393877-a3dd-c53b-c004-7f66e6aa0c9c",
    "sockets": 1,
    "tags": "xxx;xxxxx-xxx;xxxx",
    "vmgenid": "c5b96294-3b88-41c4-a08b-6c20260d578f",
    "sata1": "ceph-vm-data:vm-126-disk-1,size=50G,ssd=1",
    "sata0": "none,media=cdrom",
    "net0": "virtio=00:50:56:b9:02:4d,bridge=v656",
}


def test_vm_config_accepts_issue_144_payload() -> None:
    """VMConfig must not raise for the exact payload reported in issue #144."""
    cfg = VMConfig.model_validate(ISSUE_144_PAYLOAD)
    assert cfg.bios == "ovmf"
    assert cfg.machine == "pc-i440fx-9.0"
    assert cfg.cpu == "Cascadelake-Server-noTSX"
    assert cfg.efidisk0 == "ceph-vm-data:vm-126-disk-0,size=128K"


# ---------------------------------------------------------------------------
# Individual static fields that were missing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("bios", "ovmf"),
        ("bios", "seabios"),
        ("machine", "pc-i440fx-9.0"),
        ("machine", "q35"),
        ("efidisk0", "local-lvm:vm-100-disk-0,size=128K"),
        ("cpu", "Cascadelake-Server-noTSX"),
        ("cpu", "host"),
        ("acpi", True),
        ("acpi", False),
        ("balloon", 512),
        ("hotplug", "network,disk,usb"),
        ("vga", "std"),
        ("kvm", True),
        ("localtime", False),
        ("freeze", True),
        ("tablet", True),
        ("tdf", False),
        ("template", False),
        ("autostart", False),
        ("reboot", True),
        ("protection", False),
        ("onboot", True),
        ("hookscript", "local:snippets/hook.pl"),
        ("lock", "migrate"),
        ("meta", "creation-qemu=9.0.2,ctime=1727867268"),
        ("runningmachine", "pc-i440fx-9.0+pve0"),
        ("runningcpu", "Cascadelake-Server-noTSX"),
        ("vmstate", "local:snippets/vm-100.state"),
        ("vmstatestorage", "local"),
        ("watchdog", "ib700,action=reset"),
        ("rng0", "source=/dev/urandom,max_bytes=1024,period=1000"),
        ("ivshmem", "size=32,name=foo"),
        ("hugepages", "1024"),
        ("keyboard", "en-us"),
        ("audio0", "device=AC97,driver=spice"),
        ("spice_enhancements", "foldersharing=0,videostreaming=off"),
        ("startdate", "2006-06-17T16:01:21"),
        ("startup", "order=1,up=30,down=60"),
        ("shares", 1000),
        ("smp", 4),
        ("vcpus", 2),
        ("tpmstate0", "local-lvm:vm-100-disk-1,size=4M"),
        ("smbios1", "uuid=42393877-a3dd-c53b-c004-7f66e6aa0c9c"),
        ("snaptime", 1727867268),
        ("bootdisk", "sata0"),
        ("cdrom", "local:iso/debian.iso"),
        ("ciuser", "admin"),
        ("cipassword", "hunter2"),
        ("cicustom", "user=local:snippets/user.yml"),
        ("citype", "nocloud"),
        ("ciupgrade", True),
        ("keephugepages", False),
        ("affinity", "0,5,8-11"),
        ("args", "-no-reboot"),
        ("migrate_speed", 100),
        ("migrate_downtime", 0.5),
        ("cpulimit", 2.5),
    ],
)
def test_vm_config_accepts_qemu_static_field(field: str, value: object) -> None:
    cfg = VMConfig.model_validate({"digest": "abc", field: value})
    assert getattr(cfg, field) == value


# ---------------------------------------------------------------------------
# Dynamic numbered fields that were missing from the regex
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,value",
    [
        # sata disks
        ("sata0", "ceph-vm-data:vm-126-disk-0,size=50G"),
        ("sata1", "local:vm-126-disk-1,size=20G,ssd=1"),
        ("sata5", "none,media=cdrom"),
        # virtio disks
        ("virtio0", "local-lvm:vm-100-disk-0,size=32G"),
        ("virtio15", "ceph:vm-100-disk-15,size=10G"),
        # hostpci
        ("hostpci0", "0000:03:00,pcie=1"),
        ("hostpci7", "0000:04:00.1"),
        # USB devices
        ("usb0", "host=10de:1234"),
        ("usb4", "spice"),
        # serial ports
        ("serial0", "socket"),
        ("serial3", "/dev/ttyS0"),
        # parallel ports
        ("parallel0", "/dev/parport0"),
        ("parallel2", "/dev/parport2"),
        # NUMA nodes
        ("numa0", "cpus=0-3,hostnodes=0,memory=1024,policy=bind"),
        # ipconfig cloud-init (ipconfig1+ are dynamic; ipconfig0 is a static field)
        ("ipconfig1", "ip=10.0.0.2/24,gw=10.0.0.1"),
        ("ipconfig3", "ip=192.168.1.100/24,gw=192.168.1.1"),
        # virtiofs
        ("virtiofs0", "dirid=123,cache=auto"),
    ],
)
def test_vm_config_accepts_dynamic_numbered_field(key: str, value: str) -> None:
    cfg = VMConfig.model_validate({"digest": "abc", key: value})
    assert (cfg.model_extra or {}).get(key) == value


# ---------------------------------------------------------------------------
# Hyphenated field aliases (Proxmox returns them with dashes)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,value",
    [
        ("allow-ksm", "1"),
        ("amd-sev", "type=snp"),
        ("intel-tdx", "1"),
        ("running-nets-host-mtu", "net0=1500"),
    ],
)
def test_vm_config_accepts_hyphenated_field(key: str, value: str) -> None:
    """Fields with dash-aliases in the Proxmox API must not be rejected."""
    cfg = VMConfig.model_validate({"digest": "abc", key: value})
    # The value is accessible via model_extra since the Python field name uses underscores
    # but the alias is a dash-name.  Both should resolve without error.
    assert cfg is not None


# ---------------------------------------------------------------------------
# Validation still rejects genuinely unknown keys
# ---------------------------------------------------------------------------


def test_vm_config_rejects_truly_unknown_key() -> None:
    with pytest.raises(ValueError, match="Invalid key"):
        VMConfig.model_validate({"totally_made_up_key": "x"})


def test_vm_config_rejects_unknown_prefixed_numbered_key() -> None:
    with pytest.raises(ValueError, match="Invalid key"):
        VMConfig.model_validate({"xyzbus0": "x"})
