# Custom Field Recovery

`proxbox-api` owns a declarative NetBox custom-field inventory used by VM,
node, hardware-discovery, disk, interface, and IP synchronization. Startup
bootstrap and the extras reconcile routes consume the same inventory.

!!! warning "Deprecated — off by default"
    The legacy reflection custom fields are **deprecated**. The typed
    `Proxbox*SyncState` sidecar models are now the standard source of truth, and
    the `custom_fields_enabled` plugin setting defaults to `false`. With the flag
    off, custom-field **reconcile, writes, and reads are all skipped** — the
    reconcile route below returns an empty result and this recovery flow does not
    apply. Set `custom_fields_enabled=true` (a temporary transition option) only
    if you still depend on the custom fields; the paths then run with deprecation
    warnings. No custom-field data is deleted.

## When to use this

Run a forced reconcile after an upgrade if sync fails with an error such as
`proxmox_last_updated` missing, or if an operator deleted or edited Proxbox
custom fields in the NetBox UI — and only while `custom_fields_enabled=true`.

## Check bootstrap status

Use the backend API key:

```bash
curl -fsS \
  -H "X-Proxbox-API-Key: $PROXBOX_API_KEY" \
  http://localhost:8800/extras/bootstrap-status
```

If `ok` is `false`, inspect `warnings`. Startup also logs partial bootstrap
failures at error level.

## Force a live reconcile

The supported recovery path is the POST route:

```bash
curl -fsS -X POST \
  -H "X-Proxbox-API-Key: $PROXBOX_API_KEY" \
  http://localhost:8800/extras/custom-fields/reconcile
```

This bypasses and refreshes the process-local custom-field cache, clears the
custom-field entries from the lower-level NetBox GET cache, re-reads live
NetBox, creates missing fields, and patches drifted managed attributes.
It is idempotent: repeated calls should not churn NetBox when the fields
already match.

The legacy `GET /extras/extras/custom-fields/create` route remains available
for older callers, but new automation should use the POST route.

## After reconcile

1. Re-run the sync that failed.
2. If the POST route fails with `netbox_overwhelmed`, wait and retry.
3. If warnings remain, verify the NetBox token can read and write
   `/api/extras/custom-fields/`.

During ordinary reconcile, operator-added `object_types` on Proxbox custom
fields are preserved. The reconcile path performs one lookup per field and uses
that same live record to union the declared object types with the current
NetBox value before patching, so manually added scopes are not removed. If the
field lookup fails, that field is reported as failed instead of sending a
declared-only `object_types` payload that could shrink operator-added scopes.

## Known limitation

Custom-field reconcile reads a field, merges object types, then writes. NetBox's
REST API does not offer compare-and-swap for this operation, so if an operator
edits a field's object types in the NetBox UI at the exact moment reconcile is
adding a missing object type, the concurrent edit can be overwritten.

The window is milliseconds and only opens when reconcile is actually adding a
missing declared object type. If the declared set is already present,
`object_types` is not written at all. Avoid editing custom-field object types
in the NetBox UI while a sync or reconcile is running.

`GET /clear-cache` also invalidates the custom-field cache, but it does not
reconcile NetBox. Use the POST route when fields are missing or drifted.
