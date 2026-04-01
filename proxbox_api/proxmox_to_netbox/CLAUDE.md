# proxbox_api/proxmox_to_netbox Directory Guide

## Purpose

Provides schema-driven normalization from Proxmox raw payloads into NetBox create and update payload bodies.

## Architecture Rule

**ALL normalization and parsing MUST be done inside Pydantic schemas.**

That means:
- Parsing logic such as disk config parsing and size conversions lives in schema validators and computed fields.
- Normalization functions are schema methods or computed properties.
- Route handlers and `normalize.py` should only orchestrate; they should not parse raw strings.
- New sync features should add schemas first, then wire them in routes and services.

## Modules and Responsibilities

- `__init__.py`: Public exports for VM transformation entrypoints and schema helpers.
- `errors.py`: Domain exceptions for transformation failures.
- `proxmox_schema.py`: Reads the generated Proxmox OpenAPI artifact used as the source contract.
- `netbox_schema.py`: Fetches and caches the NetBox OpenAPI contract, with a docs-derived fallback.
- `models.py`: Pydantic v2 input/output models with normalization and validation logic.
- `normalize.py`: Orchestration logic that validates source and target schema contracts.
- `schemas/`: Schema-driven parsing modules, currently focused on disk parsing.
- `mappers/`: Mapping modules that convert normalized Proxmox models into NetBox request bodies.

## Data Flow

1. Load generated Proxmox OpenAPI to assert source operation availability.
2. Resolve NetBox schema contract from live endpoint, cache, or fallback rules.
3. Parse Proxmox raw payloads into Pydantic schemas, for example `vm_config.disks`.
4. Validate schemas with Pydantic validators and computed fields.
5. Emit validated NetBox payload dictionaries ready for API create operations.

## Extension Guidance

- Always add parsing and normalization logic to Pydantic schemas first.
- Keep transformation logic in Pydantic models and mappers, not in route handlers.
- Favor explicit status and type maps plus unit conversions to keep sync output deterministic.
- Use computed fields to expose derived data, such as parsed disks or normalized tags.
- Keep `normalize.py` for orchestration only; it should call schema methods and properties.
