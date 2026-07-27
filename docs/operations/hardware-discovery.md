# Hardware Discovery

Opt-in pass that enriches each Proxmox node's `dcim.Device` and per-NIC
`dcim.Interface` records with chassis and link facts that Proxmox VE does not
expose over its REST API. Facts are gathered over SSH from the node by running
`dmidecode`, `ip -o link show`, and `ethtool`.

This pass is **disabled by default**. With the flag off, zero SSH sockets are
opened during a sync.

## Architecture

```
proxbox-api  (orchestrator — no paramiko import)
  └── proxbox_api/services/hardware_discovery.py
        ├── is_enabled()                    settings flag check
        ├── fetch_credential(node_id)       HTTPS+Bearer to netbox-proxbox
        └── run_for_nodes(nb, nodes, *, bridge)
              imports proxmox_sdk.ssh.RemoteSSHClient
              imports proxmox_sdk.node.hardware.discover_node
              sequential per-node loop, exception → SSE warning frame

proxmox-sdk (library — owns all SSH primitives + parsers)
  ├── proxmox_sdk.ssh.RemoteSSHClient
  └── proxmox_sdk.node.hardware.{dmidecode,ethtool,facts,discover}

netbox-proxbox (NetBox plugin)
  ├── ProxboxPluginSettings.hardware_discovery_enabled
  └── NodeSSHCredential model + REST endpoint
        /api/plugins/proxbox/ssh-credentials/by-node/{node_id}/credentials/
```

The "no `paramiko` under `proxbox_api/`" invariant is pinned by
`tests/test_hardware_discovery_no_paramiko_import.py`, which AST-walks the
package and fails on any import of `paramiko`, `asyncssh`, `fabric`, etc.

## Enabling the pass

1. In NetBox → Plugins → Proxbox → Settings, toggle
   **Hardware discovery enabled**.
2. For each node, create a `NodeSSHCredential` (NetBox → Plugins → Proxbox →
   SSH Credentials). Configure:
   - username
   - private key (ed25519 recommended) or password
   - SHA-256 host-key fingerprint (no TOFU; the fingerprint must be
     captured beforehand and pinned)
   - `sudo_required` (defaults to `True` for `dmidecode`)
3. On the Proxmox node, provision a dedicated `proxbox-discovery` user with a
   sudoers entry restricted to `/usr/sbin/dmidecode -t 1` and
   `/usr/sbin/dmidecode -t 3` only.
4. Trigger a sync. After each node is upserted, the discovery pass runs
   sequentially for nodes that have a primary IP.

See `netbox-proxbox/docs/configuration/hardware-discovery.md` for the operator
walkthrough (key generation, fingerprint pinning UI, node-side `authorized_keys`
`command=` setup).

## SSE frames

On success the orchestrator emits one `hardware_discovery` frame per node via
`WebSocketSSEBridge.emit_hardware_discovery_progress()`:

```json
{
  "type": "hardware_discovery",
  "node": "pve01",
  "node_id": 42,
  "chassis_serial": "ABCD1234",
  "chassis_manufacturer": "Dell Inc.",
  "chassis_product": "PowerEdge R740",
  "nic_count": 4
}
```

On failure the orchestrator emits a generic `item_progress` frame with a
`warning` field. Warning codes:

| Warning | Cause |
|---|---|
| `hardware_discovery_no_primary_ip` | Node has no `primary_ip4`/`primary_ip` set in NetBox. |
| `hardware_discovery_no_credential` | No `NodeSSHCredential` exists for this node id. |
| `hardware_discovery_timeout` | SSH connect or exec timed out. |
| `hardware_discovery_auth_failed` | SSH authentication failed. |
| `host_key_mismatch` | The node's host key does not match the pinned fingerprint. Credentials are NOT sent. |
| `hardware_discovery_failed: <exc>` | Catch-all for any other exception. |

## NetBox custom fields

The hardware-discovery custom fields are part of the canonical Proxbox
custom-field inventory in `proxbox_api/services/custom_fields.py`. Startup
bootstrap and `POST /extras/custom-fields/reconcile` both consume that same
inventory:

| Field | Object | Type |
|---|---|---|
| `hardware_chassis_serial` | `dcim.device` | text |
| `hardware_chassis_manufacturer` | `dcim.device` | text |
| `hardware_chassis_product` | `dcim.device` | text |
| `nic_speed_gbps` | `dcim.interface` | integer |
| `nic_duplex` | `dcim.interface` | text |
| `nic_link` | `dcim.interface` | boolean |

Writes go through the existing drift-detect PATCH path
(`netbox_rest.rest_patch_async`), so a second consecutive successful sync emits
zero `extras.ObjectChange` rows for these fields.

## Physical-NIC MAC addresses

Discovery is the only path that can populate a **physical** NIC's MAC.
`/nodes/{node}/network` exposes an `hwaddress` option for bridges and bonds
only, so the API-only `sync_node_network()` leaves `eno1`-style interfaces
without one. When a discovery run parses `NicFacts.mac_address` (via
`ethtool`/`ip`, both already in the SSH command allowlist),
`reflect_to_netbox()` reconciles it through the same
`services/sync/mac_address.py::reconcile_mac_for_interface` helper the
node-network path uses for bridge/bond MACs — so both produce identical
`dcim.MACAddress` rows and set `primary_mac_address`.

This is a **native** NetBox field, not a custom field, so it is unaffected by
the `custom_fields_enabled` gate. A MAC failure is logged as a warning and
never aborts the discovery run.

Because it rides the discovery path, it inherits that path's gates:
`hardware_discovery_enabled` (default `false`), `access_methods=api_ssh`, and a
complete `NodeSSHCredential` with a pinned host-key fingerprint. Node-network
sync itself stays API-only and opens no SSH sockets.

## Security boundary

- Credentials live encrypted (Fernet) inside netbox-proxbox; plaintext only
  exists in the orchestrator's process memory for the duration of one SSH
  session.
- The credential REST endpoint requires a Bearer token matching
  `FastAPIEndpoint.token`.
- `RemoteSSHClient` in proxmox-sdk enforces:
  - SHA-256 host-key fingerprint pinning (refuses connect on mismatch — no
    TOFU)
  - argv-list-only `run()` (no shell interpolation)
  - command allowlist (`["dmidecode", "ip", "ethtool"]` is enforced by the
    orchestrator)
  - output cap, connect/exec timeouts
  - log-redactor regexes to keep key material out of `caplog`/`logger`
- The orchestrator runs nodes sequentially per cluster so a stalled node
  cannot starve others.

## Test surface

| Test | Pins |
|---|---|
| `tests/test_hardware_discovery_no_paramiko_import.py` | Static AST guard: no SSH library imports under `proxbox_api/`. |
| `tests/test_hardware_discovery_flag_off.py` | Flag off → zero `RemoteSSHClient` constructions. |
| `tests/test_hardware_discovery_orchestrator.py` | Sequential dispatch, success-frame shape, warning-code mapping for every failure class. |
| `tests/test_hardware_discovery_credential_fetch.py` | HTTPS+Bearer URL shape, 404 → `MissingCredential`, 5xx/malformed/non-dict → `HardwareDiscoveryError`, no secret leakage at DEBUG. |
