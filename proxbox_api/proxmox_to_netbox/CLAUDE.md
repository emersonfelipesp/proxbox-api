# proxbox_api/proxmox_to_netbox Directory Guide

## Purpose

Provides schema-driven normalization from Proxmox raw payloads into NetBox create/update payload bodies.

## Architecture Rule

**ALL normalization and parsing MUST be done inside Pydantic schemas.**

This means:
- Parsing logic (e.g., disk config parsing, size conversions) lives in schema validators and computed fields
- Normalization functions are schema methods or computed properties
- Route handlers and `normalize.py` should ONLY orchestrate; no parsing logic
- New sync features should add schemas first, then wire them in routes

## Modules and Responsibilities

- `__init__.py`: Public exports for VM transformation entrypoints and schemas.
- `errors.py`: Domain exceptions for transformation failures.
- `proxmox_schema.py`: Reads generated Proxmox OpenAPI artifact used as input contract.
- `netbox_schema.py`: Fetches/caches NetBox OpenAPI (live first), with docs-derived fallback contract.
- `models.py`: Pydantic v2 input/output models with normalization and validation logic.
- `normalize.py`: Orchestration logic that validates source and target schema contracts. NO parsing logic here.
- `schemas/`: Schema-driven parsing modules (e.g., `disks.py` for disk entry parsing).
- `schemas/__init__.py`: Schema package exports.
- `schemas/disks.py`: ProxmoxDiskEntry schema and disk parsing utilities (Proxmox size strings, config parsing).
- `mappers/virtual_machine.py`: VM mapper to NetBox request bodies.
- `mappers/interfaces.py`: Placeholder for interface mapping extensions.
- `mappers/ipam.py`: Placeholder for IPAM mapping extensions.

## Data Flow

1. Load generated Proxmox OpenAPI to assert source operation availability.
2. Resolve NetBox schema contract from live endpoint, cache, or fallback rules.
3. Parse Proxmox raw payloads into Pydantic schemas (e.g., `vm_config.disks`).
4. Validate schemas with Pydantic validators/computed fields.
5. Emit validated NetBox payload dictionaries ready for API create operations.

## Extension Guidance

- **ALWAYS add parsing/normalization logic to Pydantic schemas first.**
- Keep transformation logic in Pydantic models and mappers, not in route handlers.
- Favor explicit status/type maps and unit conversions to maintain deterministic sync output.
- Use computed fields to expose derived data (e.g., `ProxmoxVmConfigInput.disks`).
- Keep `normalize.py` for orchestration only; it should call schema methods/properties.
