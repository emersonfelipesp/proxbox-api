from __future__ import annotations


def test_proxmox_mock_package_is_importable() -> None:
    import proxmox_mock.main

    assert proxmox_mock.main.app is not None
