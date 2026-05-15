"""Static contracts for cloud image template build support."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_cloud_template_image_build_route_registered() -> None:
    factory = (ROOT / "proxbox_api/app/factory.py").read_text()
    init = (ROOT / "proxbox_api/routes/cloud/__init__.py").read_text()
    assert "template_images_router" in init
    assert "cloud_template_images_router" in factory


def test_cloud_template_image_build_route_creates_bootable_cloudinit_template() -> None:
    src = (ROOT / "proxbox_api/routes/cloud/template_images.py").read_text()
    for token in (
        '"/templates/images"',
        "download-url",
        "import-from",
        '"boot": "order=scsi0"',
        '"ide2": f"{req.vm_storage}:cloudinit"',
        ".template.post(disk=\"scsi0\")",
    ):
        assert token in src
