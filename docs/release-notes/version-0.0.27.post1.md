# proxbox-api 0.0.27.post1

## Summary

This post-release hardens write authorization, makes sync deletions fail safe,
and stops a transient NetBox failure from weakening network policy. It follows a
deep code review of 0.0.27. It adds no features.

## Security fixes

- **Write gates on previously ungated routes.** `POST /proxmox/cluster/ha/disarm`
  and `/ha/arm`, the custom CPU model routes, and the API token regenerate route
  now require `ProxmoxEndpoint.allow_writes` on the target cluster and the
  `X-Proxbox-Actor` header. HA arm and disarm keep their response shape; clusters
  whose endpoint has writes disabled are reported as skipped and are not touched.
  Sessions loaded from NetBox are never authorised for these writes, because the
  NetBox object id they carry overlaps the local endpoint id space.
- **Encryption key replacement is guarded.** `POST /admin/encryption/key` and
  `/admin/encryption/generate` now refuse with HTTP 409 while any encrypted
  credential is stored, as the delete route already did. The check covers every
  table whose secrets use the shared key: NetBox and Proxmox endpoints, PBS and
  PDM endpoints, Prometheus sources, Ceph dashboard endpoints and Ceph external
  clusters. Replacing the key would otherwise make those credentials
  undecryptable. Short-lived browser console relay tickets are not covered and
  simply expire. The guard checks stored ciphertext at the moment of the request; it
  does not coordinate with credential writes that are still in flight or with other
  backend workers, which cache the key per process. Replace the key only in a
  maintenance window with no endpoint changes under way, and restart every backend
  worker afterwards.
- **SSRF checks.** IPv4-mapped, 6to4 and Teredo IPv6 addresses are checked as
  written and as the IPv4 they embed, so `::ffff:127.0.0.1` is blocked like
  `127.0.0.1` and an operator's explicit IPv6 deny range still applies to them.
  Operator allow ranges match the address as written and a true IPv4-mapped form
  only; they do not whitelist 6to4 or Teredo addresses that merely embed an
  allowed IPv4. An IP-literal host that cannot be parsed is now refused instead of
  allowed. A failure to load the registered-endpoint list is logged.
- **Upstream error details.** The custom CPU model and token routes no longer
  return raw upstream exception text.

## Data integrity fixes

- **Settings fallback.** A failed or timed-out NetBox settings fetch no longer
  caches permissive defaults for the normal interval. The last good settings are
  served, or defaults are used for about ten seconds so the next call retries.
  Previously one transient failure could silently drop the operator's SSRF policy.
- **NetBox GET cache.** Cache entries are keyed by a per-client token instead of
  an object address that can be reused, and are purged when a client is closed. A
  list traversal no longer caches a snapshot that a concurrent write invalidated,
  a lost-response create retry no longer reads a stale cached lookup, and cached
  records can no longer be corrupted by editing nested values.
- **Virtual disks.** Stale-disk cleanup only deletes disks that Proxbox wrote
  (tagged `proxbox`), skips cleanup when Proxmox reports no parsed disks, and keeps
  the record of any disk that is still present in the Proxmox configuration but
  could not be represented, such as a passthrough disk without a size.
- **Backups.** Stale-backup cleanup for a cluster owner now runs only when all of
  these hold: its storage listing was read, every storage entry had a usable
  `content` field, every backup-capable storage is served by at least one node
  that was enumerated, and the backup reads for that owner succeeded. Otherwise
  that owner's NetBox backup records are left alone. A NetBox backup whose volume
  Proxmox still lists is also kept when that listing row could not be classified into
  an owner (for example it has no `vmid` or no `content` field). Storage node and content
  matching compares exact names instead of substrings, and one malformed existing
  backup row no longer aborts the whole backup sync.
- **Single-VM sync.** A failed or empty Proxmox resource lookup now stops the sync
  instead of writing placeholder values such as `vm-<vmid>` and zero memory.

## Compatibility and upgrade notes

This release adds no database migration. Callers that relied on the removed
behavior need to adapt:

- The HA arm and disarm routes now return HTTP 422 when `X-Proxbox-Actor` is
  missing. For clusters whose endpoint has writes disabled they return the usual
  list with `status="skipped"` and `error="endpoint_writes_disabled"` and make no
  upstream call. The custom CPU model and token regenerate routes return HTTP 422
  without the actor header and HTTP 403 (`endpoint_writes_disabled`) when the
  target endpoint has writes disabled. Enable `allow_writes` on the endpoint first.
  Endpoints that come from NetBox rather than the local database are treated as
  write-disabled for these routes.
- Upstream error responses from the custom CPU model and token routes now carry a
  fixed object (`reason: proxmox_upstream_error`) instead of the exception text.
- Replacing the encryption key while encrypted credentials exist now returns 409.
- Virtual disks that an operator added in NetBox without the `proxbox` tag are no
  longer deleted by sync.

Deploy the exact `proxbox-api 0.0.27.post1` package or container through the
approved release workflow and restart every backend worker, then run one node
synchronization and one virtual-machine synchronization.
