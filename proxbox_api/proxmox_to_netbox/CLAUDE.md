# proxbox_api/proxmox_to_netbox Directory Guide

## Purpose

Provides schema-driven normalization from raw Proxmox payloads into NetBox create and update bodies.

## Architecture Rule

**ALL normalization and parsing MUST be done inside Pydantic schemas.**

That means:

- parsing logic such as disk config parsing and size conversions lives in schema validators and computed fields
- normalization functions are schema methods or computed properties
- route handlers and `normalize.py` only orchestrate
- new sync features should add schemas first, then wire them into routes and services

## Modules and Responsibilities

- `__init__.py`: public exports for transformation entry points and schema helpers.
- `errors.py`: domain exceptions for transformation failures.
- `proxmox_schema.py`: reads the generated Proxmox OpenAPI artifact used as the source contract.
- `netbox_schema.py`: fetches and caches the NetBox OpenAPI contract, with a docs-derived fallback.
- `models.py`: Pydantic v2 input and output models with normalization and validation logic.
- `normalize.py`: orchestration layer that validates source and target schema contracts.
- `schemas/`: schema-driven parsing modules, currently focused on disk parsing.
- `mappers/`: mapping modules that convert normalized Proxmox models into NetBox request bodies.

## Data Flow

1. Load generated Proxmox OpenAPI to assert source operation availability.
2. Resolve the NetBox schema contract from a live endpoint, cache, or fallback rules.
3. Parse Proxmox raw payloads into Pydantic schemas, for example `vm_config.disks`.
4. Validate schemas with Pydantic validators and computed fields.
5. Emit validated NetBox payload dictionaries ready for API create or update operations.

## Extension Guidance

- Always add parsing and normalization logic to Pydantic schemas first.
- Keep transformation logic in Pydantic models and mappers, not in route handlers.
- Favor explicit status and type maps plus unit conversions to keep sync output deterministic.
- Use computed fields to expose derived data such as parsed disks or normalized tags.
- Keep `normalize.py` for orchestration only; it should call schema methods and properties.
