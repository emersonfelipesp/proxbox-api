"""Tests for the Cloud Image Build Pipeline catalog and script rendering."""

from __future__ import annotations

import pytest
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
    assert "\nwrite_files:\n  - path:" in response.generated_userdata
    assert "\nruncmd:\n  - curl" in response.generated_userdata
    assert "\n      Pin-Priority: 1001\n" in response.generated_userdata


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
