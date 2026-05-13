# proxbox_api/types Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/types/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Shared type aliases, structural protocols, and typed data structures used throughout the `proxbox_api` package. Centralizing these here prevents circular imports and gives a single place to adjust domain types.

## Files

| File | Role |
|------|------|
| `aliases.py` | Scalar type aliases: `VMID`, `ClusterName`, `IPAddress`, `MACAddress`, `NodeName`, `VLANId`, `VMStatus`, `SyncStatus`, `RecordID`. These are thin `TypeAlias` declarations that improve readability and enable type-checker narrowing without runtime overhead. |
| `protocols.py` | Structural `Protocol` definitions: `NetBoxRecord`, `ProxmoxResource`, `SyncResult`, `TagLike`. Route handlers and service functions should accept these protocols rather than concrete classes where possible. Use when working with NetBox SDK objects that support duck-typing. |
| `structured_dicts.py` | TypedDict definitions for common data structures: `ProxboxSettingsDict`, `SyncResultDict`, `DevicePayloadDict`, `VMPayloadDict`, and others. Use when dictionary structure and field types are important for type checking. |
| `__init__.py` | Re-exports all aliases, protocols, and TypedDicts for convenience (`from proxbox_api.types import VMID, NetBoxRecord, VMPayloadDict`). |

## Usage Guidelines

### For Domain Type Aliases

- Import from package root: `from proxbox_api.types import VMID, RecordID`
- Add new aliases when a primitive value has stable domain identity (specific ID, name string)
- Use for function parameters to improve type clarity:
  ```python
  def load_vm(vm_id: VMID) -> dict[str, Any]:
      """Load Proxmox VM by ID."""
  ```

### For Protocols

- Use when working with multiple object types that share an interface
- Example: `NetBoxRecord` works with devices, sites, clusters, etc.
- Prefer protocols over concrete classes for better flexibility:
  ```python
  # Instead of: def process(record: NetBoxDevice) -> None
  def process(record: NetBoxRecord) -> None:  # Accepts any NetBox object
      print(record.id, record.name)
  ```

### For TypedDicts

- Use when dictionary structure is important for type checking
- Use when you need to document field presence/optionality with `NotRequired`
- Available TypedDicts:
  - `ProxboxSettingsDict` - Plugin settings structure
  - `SyncResultDict` - Sync operation results
  - `DevicePayloadDict` - NetBox device payloads
  - `VMPayloadDict` - NetBox VM payloads
  - `InterfacePayloadDict` - Network interface payloads
  - `ProxmoxDeviceDict` - Proxmox node data
  - `ProxmoxVMDict` - Proxmox VM data
  - And others for storage, networks, caching

- Example usage:
  ```python
  from proxbox_api.types import VMPayloadDict

  def build_payload() -> VMPayloadDict:
      return {
          "name": "vm-01",
          "status": "running",
          "cluster": 1,
      }
  ```

## Typing Best Practices in proxbox_api

### Avoid Generic `object` Type

Replace `object` with more specific types:

```python
# ✗ Avoid
def process(data: object) -> object:
    ...

# ✓ Use specific types
def process(data: dict[str, Any]) -> dict[str, Any]:
    ...

# ✓ Better - use TypedDict
def process(data: ProxmoxDeviceDict) -> SyncResultDict:
    ...
```

### Use Protocols for Duck-Typing

When working with NetBox SDK objects:

```python
from proxbox_api.types import NetBoxRecord

def extract_names(records: list[NetBoxRecord]) -> list[str]:
    """Works with any NetBox object (device, cluster, site, etc.)."""
    return [r.name for r in records if r.name]
```

### Async Callbacks Need Awaitable Types

```python
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

async def retry_async(
    coro: Callable[[], Awaitable[T]],
    max_retries: int = 3,
) -> T:
    """Generic async retry preserving return type."""
    return await coro()
```

### Service Functions Should Have Clear Return Types

Instead of generic returns:

```python
# ✗ Avoid
async def create_device(...) -> object:
    ...

# ✓ Use specific types or TypedDict
async def create_device(...) -> NetBoxRecord:
    ...

# ✓ Or TypedDict for payload structures
def build_payload(...) -> DevicePayloadDict:
    ...
```

## MyPy Configuration

Current settings in `pyproject.toml` are being gradually tightened:

```toml
[tool.mypy]
python_version = "3.11"
check_untyped_defs = true       # ✓ Enabled
strict_optional = true          # ✓ Enabled
strict_equality = true          # ✓ Enabled
disallow_untyped_defs = false   # Gradual migration
disallow_untyped_calls = false  # Gradual migration
disallow_incomplete_defs = false # Gradual migration
```

Highest strictness applied to:
- `proxbox_api/app/**/*.py` - App factory and middleware
- `proxbox_api/services/sync/device_ensure.py` - Core device sync
- `proxbox_api/session/proxmox_core.py` - Session factories

## Do Not Modify

- Do not add runtime logic here — keep module as pure type declarations only
- All exports should be type-only (no values at runtime beyond type metadata)
