# proxbox_api/proxmox_to_netbox Directory Guide

## Purpose

Provides schema-driven normalization from Proxmox raw payloads into NetBox create/update payload bodies.

## Modules and Responsibilities

- `__init__.py`: Public exports for VM transformation entrypoints.
- `errors.py`: Domain exceptions for transformation failures.
- `proxmox_schema.py`: Reads generated Proxmox OpenAPI artifact used as input contract.
- `netbox_schema.py`: Fetches/caches NetBox OpenAPI (live first), with docs-derived fallback contract.
- `models.py`: Pydantic v2 input/output models with normalization and validation logic.
- `normalize.py`: Orchestration logic that validates source and target schema contracts.
- `mappers/virtual_machine.py`: VM mapper to NetBox request bodies.
- `mappers/interfaces.py`: Placeholder for interface mapping extensions.
- `mappers/ipam.py`: Placeholder for IPAM mapping extensions.

## Data Flow

1. Load generated Proxmox OpenAPI to assert source operation availability.
2. Resolve NetBox schema contract from live endpoint, cache, or fallback rules.
3. Normalize Proxmox raw payloads with Pydantic validators/computed fields.
4. Emit validated NetBox payload dictionaries ready for API create operations.

## Extension Guidance

- Keep transformation logic in Pydantic models and mappers, not in route handlers.
- Favor explicit status/type maps and unit conversions to maintain deterministic sync output.
