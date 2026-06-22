"""Tests for the Cloud Image Build Pipeline catalog and script rendering."""

from __future__ import annotations

import pytest
import yaml
from fastapi import HTTPException

from proxbox_api.routes.cloud.catalog import catalog_payload, find_product_version
from proxbox_api.routes.cloud.template_images import build_pipeline_response
from proxbox_api.schemas.cloud_provision import (
    CloudImageBuildProvider,
    CloudImageProductType,
    CloudImageTemplateBuildRequest,
)


def test_catalog_exposes_firewall_appliance_products():
    catalog = catalog_payload()

    assert "pfsense" in catalog
    assert "opnsense" in catalog
    assert catalog["pfsense"][0]["default_provider"] == "release_image"
    assert "source_tree" in catalog["opnsense"][0]["supported_providers"]


def test_find_product_version_defaults_to_first_entry():
    entry = find_product_version(CloudImageProductType.PFSENSE)

    assert entry.product_type == CloudImageProductType.PFSENSE
    assert entry.version == "2.8.1"


def test_pbs_catalog_defaults_to_current_trixie_entry():
    entry = find_product_version(CloudImageProductType.PBS)

    assert entry.product_type == CloudImageProductType.PBS
    assert entry.version == "4.2"
    assert entry.debian_codename == "trixie"
    assert entry.image_url is not None
    assert "debian-13-genericcloud-amd64.qcow2" in entry.image_url


def test_pbs_cloud_image_pipeline_bakes_dns_qga_and_zabbix_userdata():
    response = build_pipeline_response(
        CloudImageTemplateBuildRequest(
            product_type=CloudImageProductType.PBS,
            product_version="4.2",
            provider=CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE,
            vmid=9400,
            name="pbs42-template",
            hostname="pbs42-template",
            domain="nmulti.cloud",
            search_domain="nmulti.cloud",
            nameservers=["168.0.96.26", "168.0.96.27", "8.8.8.8"],
        )
    )

    assert response.status == "planned"
    assert response.generated_userdata is not None
    userdata = response.generated_userdata
    parsed = yaml.safe_load(userdata)
    assert parsed["resolv_conf"]["nameservers"] == [
        "168.0.96.26",
        "168.0.96.27",
        "8.8.8.8",
    ]
    assert parsed["resolv_conf"]["searchdomains"] == ["nmulti.cloud"]
    assert "debian/pbs trixie pbs-no-subscription" in userdata
    assert "zabbix-release_latest_7.4+debian13_all.deb" in userdata
    assert (
        "DEBIAN_FRONTEND=noninteractive apt-get install -y "
        "proxmox-backup-server qemu-guest-agent zabbix-agent2"
    ) in userdata
    assert "Server=zabbix.nmulti.cloud" in userdata
    assert "systemctl enable qemu-guest-agent" in userdata
    assert "systemctl enable zabbix-agent2" in userdata
    assert "user=local:snippets/pbs42-template-pbs-4.2-user-data.yml" in response.build_script
    assert "--cicustom" in response.build_script


def test_pbs_cloud_image_pipeline_can_disable_default_agents():
    response = build_pipeline_response(
        CloudImageTemplateBuildRequest(
            product_type=CloudImageProductType.PBS,
            product_version="4.2",
            provider=CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE,
            vmid=9401,
            name="pbs42-minimal",
            install_qemu_guest_agent=False,
            install_zabbix_agent2=False,
        )
    )

    assert response.generated_userdata is not None
    assert "qemu-guest-agent" not in response.generated_userdata
    assert "zabbix-agent2" not in response.generated_userdata
    assert "zabbix-release_latest_7.4" not in response.generated_userdata


def test_pfsense_release_pipeline_returns_first_boot_script_and_qm_commands():
    response = build_pipeline_response(
        CloudImageTemplateBuildRequest(
            product_type=CloudImageProductType.PFSENSE,
            product_version="2.8.1",
            provider=CloudImageBuildProvider.RELEASE_IMAGE,
            vmid=9100,
            name="pfsense-template",
            hostname="pfsense-template",
        )
    )

    assert response.pipeline_name == "Cloud Image Build Pipeline"
    assert response.status == "planned"
    assert response.first_boot_script is not None
    assert 'PRODUCT="pfsense"' in response.first_boot_script
    assert "qm create 9100" in response.build_script
    assert "qm set 9100 --agent enabled=1" in response.build_script
    assert "qm template 9100" in response.build_script


def test_pve_version_pin_keeps_cloud_init_top_level_keys_unindented():
    response = build_pipeline_response(
        CloudImageTemplateBuildRequest(
            product_type=CloudImageProductType.PVE,
            product_version="9.1.11",
            provider=CloudImageBuildProvider.DEBIAN_CLOUD_IMAGE,
            vmid=9300,
            name="pve-template",
            pve_version_pin="9.1.11",
        )
    )

    assert response.generated_userdata is not None
    userdata = response.generated_userdata
    parsed = yaml.safe_load(userdata)

    assert "\nwrite_files:\n  - path:" in userdata
    assert "\nruncmd:\n  - curl -fsSL -o /etc/apt/trusted.gpg.d/" in userdata
    assert parsed["resolv_conf"]["nameservers"] == ["1.1.1.1", "8.8.8.8"]
    assert parsed["write_files"] == [
        {
            "path": "/etc/apt/sources.list.d/pve-install-repo.list",
            "content": (
                "deb [arch=amd64] http://download.proxmox.com/debian/pve "
                "bookworm pve-no-subscription\n"
            ),
        },
        {
            "path": "/etc/apt/preferences.d/nmulticloud-pve-pin",
            "content": ("Package: proxmox-ve\nPin: version 9.1.11*\nPin-Priority: 1001\n"),
        },
    ]
    assert parsed["runcmd"] == [
        (
            "curl -fsSL -o /etc/apt/trusted.gpg.d/proxmox-release-bookworm.gpg "
            "https://enterprise.proxmox.com/debian/proxmox-release-bookworm.gpg"
        ),
        (
            "rm -f /etc/apt/sources.list.d/pve-enterprise.list "
            "/etc/apt/sources.list.d/pbs-enterprise.list"
        ),
        "DEBIAN_FRONTEND=noninteractive apt-get update",
        "DEBIAN_FRONTEND=noninteractive apt-get install -y proxmox-ve",
        "systemctl enable pveproxy",
    ]
    assert userdata.index("proxmox-release-bookworm.gpg") < userdata.index(
        "rm -f /etc/apt/sources.list.d/"
    )
    assert "grub-pc/install_devices multiselect /dev/sda" not in userdata
    assert "pve-enterprise.sources" not in userdata


def test_opnsense_source_tree_pipeline_uses_catalog_source_path():
    response = build_pipeline_response(
        CloudImageTemplateBuildRequest(
            product_type=CloudImageProductType.OPNSENSE,
            product_version="26.1.8",
            provider=CloudImageBuildProvider.SOURCE_TREE,
            vmid=9200,
            name="opnsense-template",
        )
    )

    assert response.source_tree_path == "nmulticloud-context/opnsense"
    assert "cd nmulticloud-context/opnsense" in response.build_script
    assert "make dvd" in response.build_script


def test_user_data_yaml_bakes_cicustom_snippet_without_catalog_product():
    """A verbatim user_data_yaml build skips the catalog and writes a cicustom user snippet."""
    custom = "#cloud-config\nruncmd:\n  - echo zabbix-bootstrap\n"
    response = build_pipeline_response(
        CloudImageTemplateBuildRequest(
            name="zabbix-7.4-ubuntu-2604",
            vmid=9010,
            image_url=(
                "https://cloud-images.ubuntu.com/releases/24.04/release/"
                "ubuntu-24.04-server-cloudimg-amd64.img"
            ),
            image_storage="local",
            vm_storage="local",
            storage="local",
            snippets_storage="local",
            user_data_yaml=custom,
        )
    )

    assert response.status == "planned"
    assert response.generated_userdata == custom
    # The cloud-config is materialised as a cicustom *user* snippet (so it runs at
    # first boot) — not merely stuffed into the VM description.
    assert "EOF_USER_DATA" in response.build_script
    assert "echo zabbix-bootstrap" in response.build_script
    assert "--cicustom" in response.build_script
    assert "user=local:snippets/" in response.build_script
    assert "qm set 9010 --agent enabled=1" in response.build_script
    assert "qm template 9010" in response.build_script


def test_execute_requires_environment_opt_in():
    with pytest.raises(HTTPException) as exc:
        build_pipeline_response(
            CloudImageTemplateBuildRequest(
                product_type=CloudImageProductType.PFSENSE,
                execute=True,
                ssh_host="pve.example.test",
            )
        )

    assert exc.value.status_code == 403
