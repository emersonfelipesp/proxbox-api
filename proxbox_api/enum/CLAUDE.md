# proxbox_api/enum Directory Guide

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/proxbox_api/enum/CLAUDE.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

## Purpose

Central enum definitions for Proxmox path options and NetBox value constraints.

## Current Modules

- `proxmox.py`: Proxmox API path and mode choices.
- `netbox/`: NetBox-specific enum groups.

## How These Enums Are Used

- Route modules import enums for query and path validation.
- Schema modules use enums to keep outgoing payloads aligned with upstream API choices.
- The values are serialized across REST, SSE, and WebSocket payloads, so the enum contracts should remain stable.

## Behavior Notes

- **`status_mapping.ProxmoxToNetBoxVMStatus.from_proxmox` accepts both shapes.**
  It is applied to the Proxmox-derived *desired* status **and** to the *existing*
  NetBox record's status when the reconciliation diff is built. The existing
  record is loaded over raw REST, where NetBox serialises a choice field as
  `{"value": ..., "label": ...}`, not a bare string — so the helper unwraps a
  dict before mapping. Without that, `str({...}).lower()` matched no key and
  every existing record silently read back as the `active` default, so a VM
  whose status genuinely became `active` produced no diff and never updated
  (netbox-proxbox issue #617). Keep the unwrap if you touch this mapping;
  regression coverage is in `tests/test_vm_status_reconcile.py`.

- **Every `NetBoxInterfaceType` member must be a value NetBox accepts.**
  `dcim.Interface.type` has no `loopback` choice, so the enum no longer has one.
  A member whose value NetBox does not accept is not a latent typo — it becomes
  a rejected write and a sync stage that silently creates nothing.
  `tests/test_netbox_choice_enum_normalization.py` pins the accepted set against
  a fixed literal transcribed from NetBox's own `OPTIONS` payload, deliberately
  not derived from the enum.
- **Widening the `from_proxmox` mapping table is a migration, not a cleanup.**
  Node sync owns `dcim.Interface.type` and rewrites existing rows, so moving a
  Proxmox type out of the `other` bucket retypes every row already synced under
  it. NetBox validates the whole instance: a row carrying a cable or
  `mark_connected` is legal as `other` and **invalid** once it becomes one of
  the virtual kinds (`virtual`, `bridge`, `lag`), and the phase-one reconcile is
  unguarded, so the rejection aborts node-network sync for the entire node.
  `loopback`, `ovsbridge`, and `ovsbond` were widened and then deliberately
  reverted for exactly this reason. Any future widening has to detect
  incompatible legacy rows and preserve their type with an actionable warning,
  with an integration test covering a cabled row of the affected type.
- **`from_proxmox` is idempotent — mapping an already-mapped member is a no-op.**
  The status crosses two mappers in the live VM sync: the payload builder maps
  the raw Proxmox status, and the model validator maps whatever it is handed.
  The second call used to receive the member the first produced, `str()` it into
  `"ProxmoxToNetBoxVMStatus.offline"`, match no key, and fall through to the
  `active` default — so every stopped and paused VM was recorded as running,
  with no error anywhere. Both `from_proxmox` classmethods now return a member
  unchanged and unwrap any other `Enum` to its value. Keep that if you touch
  them, and keep the helpers that feed them annotated honestly: `_status_value`
  in `services/sync/individual/vm_sync.py`, `VirtualMachine.map_status` in
  `netbox_compat.py`, and `NetBoxVirtualMachineCreateBody.normalize_status` all
  declare `-> str` and must return `.value`, not the member. The live-path
  regression is `tests/test_netbox_choice_enum_normalization.py::TestLiveVirtualMachineStatusPath`,
  which exercises the real payload builder — a helper-level test passed
  throughout the whole time this defect was live.
- **Do not rely on `str()` over a member of these `(str, Enum)` classes.**
  `str()` resolves to `Enum.__str__` and returns the class-qualified name
  (`"NetBoxInterfaceType.bridge"`), not the value. Use `.value`, or hand the
  member to the desired-state normalizers in `proxmox_to_netbox/models.py`,
  which unwrap it.

## Extension Guidance

- Add new members in a backward-compatible way.
- Keep names and values stable once they are used in external payloads.
- Use `str` enums whenever the values are sent to clients or upstream APIs.
