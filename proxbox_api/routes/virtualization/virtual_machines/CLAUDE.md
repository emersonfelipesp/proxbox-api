@AGENTS.md

## LXC DHCP runtime address discovery

LXC interfaces now use the read-only Proxmox
`GET /nodes/{node}/lxc/{vmid}/interfaces` endpoint in full VM network sync,
interface-only sync, and IP-only sync. Unlike QEMU, containers do not require
a guest agent. `services/proxmox_helpers.py` validates responses with the pinned
proxmox-sdk models and adapts both `ip-addresses` and legacy `inet`/`inet6` CIDRs
to the existing guest-interface shape. Existing MAC/name matching, IP filtering,
NetBox interface ownership, and core IP reconciliation remain authoritative.
Unavailable or malformed runtime data logs a secret-free warning and preserves
static-config fallback; cancellation propagates. No new API parameters, settings,
database migrations, or netbox-proxbox compatibility-floor changes are required.
Regression coverage: `tests/test_lxc_runtime_interfaces.py` and the LXC cases in
`tests/test_qemu_guest_agent_sync.py`.
