# proxbox_api/types Directory Guide

## Purpose

Shared type aliases and structural protocols used throughout the `proxbox_api` package. Centralizing these here prevents circular imports and gives a single place to adjust domain types.

## Files

| File | Role |
|------|------|
| `aliases.py` | Scalar type aliases: `VMID`, `ClusterName`, `IPAddress`, `MACAddress`, `NodeName`, `VLANId`, `VMStatus`, `SyncStatus`, `RecordID`. These are thin `TypeAlias` declarations that improve readability and enable type-checker narrowing without runtime overhead. |
| `protocols.py` | Structural `Protocol` definitions: `NetBoxRecord`, `ProxmoxResource`, `SyncResult`, `TagLike`. Route handlers and service functions should accept these protocols rather than concrete classes where possible. |
| `__init__.py` | Re-exports all aliases and protocols for convenience (`from proxbox_api.types import VMID, NetBoxRecord`). |

## Usage Guidelines

- Import from the package root: `from proxbox_api.types import VMID, SyncResult`.
- Add new aliases to `aliases.py` when a primitive value has a stable domain identity (e.g., a specific ID or name string).
- Add new protocols to `protocols.py` when multiple unrelated classes share a structural interface that route handlers or utilities depend on.
- Do not add runtime logic here — keep this module as pure type declarations.
