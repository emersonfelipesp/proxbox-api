# Custom Field Recovery

`proxbox-api` owns a declarative NetBox custom-field inventory used by VM,
node, hardware-discovery, disk, interface, and IP synchronization. Startup
bootstrap and the extras reconcile routes consume the same inventory.

## When to use this

Run a forced reconcile after an upgrade if sync fails with an error such as
`proxmox_last_updated` missing, or if an operator deleted or edited Proxbox
custom fields in the NetBox UI.

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

This bypasses and refreshes the process-local custom-field cache, re-reads
live NetBox, creates missing fields, and patches drifted managed attributes.
It is idempotent: repeated calls should not churn NetBox when the fields
already match.

The legacy `GET /extras/extras/custom-fields/create` route remains available
for older callers, but new automation should use the POST route.

## After reconcile

1. Re-run the sync that failed.
2. If the POST route fails with `netbox_overwhelmed`, wait and retry.
3. If warnings remain, verify the NetBox token can read and write
   `/api/extras/custom-fields/`.

Operator-added `object_types` on Proxbox custom fields are preserved. The
reconcile path unions the declared object types with the live NetBox value
before patching, so manually added scopes are not removed.

`GET /clear-cache` also invalidates the custom-field cache, but it does not
reconcile NetBox. Use the POST route when fields are missing or drifted.
