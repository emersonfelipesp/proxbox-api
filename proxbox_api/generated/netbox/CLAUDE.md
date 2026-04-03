# proxbox_api/generated/netbox Directory Guide

## Purpose

Stores cached NetBox OpenAPI schema documents fetched from live NetBox instances.

## Typical Files

- `openapi.json`: last fetched NetBox OpenAPI document used by transformation contracts.
- `__init__.py`: package marker for generated cache artifacts.

## How This Directory Is Used

- `proxbox_api.proxmox_to_netbox.netbox_schema` reads the cached contract when a live schema fetch is unavailable.
- The cached schema acts as a fallback only; live NetBox schema fetches still take precedence when available.
- Regeneration should happen through the normal generator path, not by manual editing.

## Extension Guidance

- Treat files in this directory as generated cache artifacts.
- Keep any fallback contract data aligned with the live schema behavior.
