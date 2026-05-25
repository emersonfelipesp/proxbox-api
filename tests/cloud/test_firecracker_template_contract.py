"""Contract tests for Firecracker host VM template catalog entries."""

from __future__ import annotations

from proxbox_api.routes.cloud.catalog import PRODUCT_CATALOG, find_product_version
from proxbox_api.routes.cloud.cloud_init_templates import generate_firecracker_userdata
from proxbox_api.schemas.cloud_provision import (
    CloudImageBuildProvider,
    CloudImageProductType,
    ProxmoxProductType,
)


def test_firecracker_enum_member():
    assert ProxmoxProductType.firecracker.value == "firecracker"
    assert ProxmoxProductType.FIRECRACKER.value == "firecracker"


def test_ubuntu_cloud_image_provider():
    assert CloudImageBuildProvider.ubuntu_cloud_image.value == "ubuntu_cloud_image"
    assert CloudImageBuildProvider.UBUNTU_CLOUD_IMAGE.value == "ubuntu_cloud_image"


def test_firecracker_catalog_entries_present():
    entries = PRODUCT_CATALOG.get(CloudImageProductType.FIRECRACKER, [])
    assert len(entries) == 2
    versions = {e.version for e in entries}
    assert versions == {"debian-12", "ubuntu-24.04"}


def test_firecracker_debian_entry():
    entry = find_product_version(CloudImageProductType.FIRECRACKER, "debian-12")
    assert entry.os_family == "debian"
    assert entry.os_release == "bookworm"
    assert entry.os_codename == "bookworm"
    assert entry.default_provider == CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE
    assert "cloud.debian.org" in entry.image_url


def test_firecracker_ubuntu_entry():
    entry = find_product_version(CloudImageProductType.FIRECRACKER, "ubuntu-24.04")
    assert entry.os_family == "ubuntu"
    assert entry.os_release == "noble"
    assert entry.os_codename == "noble"
    assert entry.default_provider == CloudImageBuildProvider.UBUNTU_CLOUD_IMAGE
    assert "cloud-images.ubuntu.com" in entry.image_url


def test_firecracker_version_lookup_default():
    entry = find_product_version(CloudImageProductType.FIRECRACKER)
    assert entry.version == "debian-12"


def test_firecracker_userdata_debian():
    userdata = generate_firecracker_userdata("debian", "bookworm")
    assert "#cloud-config" in userdata
    assert "firecracker" in userdata.lower()
    assert "qemu-kvm" in userdata
    assert "/usr/local/bin/firecracker" in userdata
    assert "/usr/local/bin/jailer" in userdata
    assert "/opt/cni/bin" in userdata
    assert "kvm" in userdata


def test_firecracker_userdata_ubuntu():
    userdata = generate_firecracker_userdata("ubuntu", "noble")
    assert "#cloud-config" in userdata
    assert "Ubuntu" in userdata
    assert "qemu-kvm" in userdata
    assert "/usr/local/bin/firecracker" in userdata
