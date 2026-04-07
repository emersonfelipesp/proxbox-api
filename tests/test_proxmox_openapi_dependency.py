"""Tests for proxmox-openapi dependency wiring."""

from __future__ import annotations


def test_proxmox_openapi_mock_entrypoint_is_importable() -> None:
    import proxmox_openapi.mock_main

    assert proxmox_openapi.mock_main.app is not None
