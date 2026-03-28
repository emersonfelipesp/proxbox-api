"""Proxmox API Viewer to OpenAPI and Pydantic generation package."""

from proxbox_api.proxmox_codegen.pipeline import generate_proxmox_codegen_bundle

__all__ = ["generate_proxmox_codegen_bundle"]
