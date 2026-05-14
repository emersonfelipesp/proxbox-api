# Troubleshooting

## No NetBox endpoint found

Symptom:

- API routes that need NetBox session fail with message indicating no endpoint configured.

Resolution:

1. Create NetBox endpoint with `POST /netbox/endpoint`.
2. Verify it exists with `GET /netbox/endpoint`.
3. If the startup bootstrap was intentionally skipped, confirm `PROXBOX_SKIP_NETBOX_BOOTSTRAP` is not set.

## Proxmox endpoint auth validation errors

Symptom:

- `400` with `Provide password or both token_name/token_value`.
- `400` with `token_name and token_value must be provided together`.

Resolution:

- Provide password auth, or provide the complete token pair.
- For the NetBox endpoint, remember token v1 uses `token` only, while token v2 requires both `token_key` and `token`.

## Generated Proxmox route mount failures

Symptom:

- Startup logs mention that generated Proxmox routes could not be mounted.

Resolution:

- Confirm the generated OpenAPI snapshot exists under `proxbox_api/generated/proxmox/latest/openapi.json`.
- Rebuild the live Proxmox contract with `POST /proxmox/viewer/generate` if the snapshot is missing.
- If the app should fail closed instead of continuing, enable `PROXBOX_STRICT_STARTUP`.

## CORS issues in frontend integration

Symptom:

- Browser blocks requests due to CORS policy.

Resolution:

- Ensure NetBox endpoint domain is set correctly.
- Confirm frontend origin is included in app CORS origin list.
- Confirm requests target expected API host and port.

## NetBox self-signed certificate errors

Symptom:

- Logs show `certificate verify failed` while fetching `ProxboxPluginSettings` or other NetBox API data.

Resolution:

- For production, install a certificate trusted by the proxbox-api runtime.
- For private lab deployments, set the stored NetBox endpoint `verify_ssl` field to `false`. This disables certificate verification for both normal NetBox API calls and plugin-settings/runtime-settings fetches.

## NetBox Cache Issues

### Stale data after sync

Symptom:

- NetBox UI shows old values after sync completes.

Resolution:

```bash
curl http://localhost:8000/clear-cache
```

### High cache miss rate

Symptom:

- `/cache` endpoint shows >80% miss rate.

Resolution:

1. Increase `PROXBOX_NETBOX_GET_CACHE_TTL`:
   ```bash
   export PROXBOX_NETBOX_GET_CACHE_TTL=300  # 5 minutes
   ```
2. Check query patterns: identical queries are required for cache hits (same path, same query params).
3. Monitor with: `curl http://localhost:8000/cache/metrics`

### Cache performance debugging

Enable debug logging:

```bash
export PROXBOX_DEBUG_CACHE=1
```

Then check application logs for cache HIT/MISS/INVALIDATE messages.

## Proxmox connection failures

Symptom:

- Connection exceptions during session instantiation.

Resolution:

- Validate Proxmox host, port, and credentials.
- Check `verify_ssl` behavior and certificates.
- Confirm API user/token permissions in Proxmox.
- For multi-endpoint deployments, confirm the correct `source` and endpoint target selection values are being passed.

### Reading the new PVE 9 auth-failure detail (issue #417)

Symptom:

- `HTTP 401 Authentication failed!` against a Proxmox VE 9.x cluster, with a `Detail` field that used to read `"Unknown error."`.

Resolution:

- The `Detail` shown in the NetBox UI now mirrors the upstream PVE response. Read it as `HTTP <code> <status> — <body> — <errors JSON>`:
  - `no such realm` → the `user@realm` you typed is wrong (`root@pam` vs `root@pve` vs the realm name configured on the cluster).
  - `permission check failed` → role is missing `VM.GuestAgent.Audit` (PVE 9), `Datastore.Audit`, `Sys.Audit`, or `VM.Audit`.
  - `authentication failure` → password or token value is wrong, or has expired / been rotated on the Proxmox side.
- Stored credentials are no longer leaked into auth attempts after a credential switch: on the NetBox-side endpoint edit form, use the **"Clear stored API token on save"** and **"Clear stored password on save"** checkboxes to wipe the unused secret before saving. The form rejects rows that end up with neither a password nor a complete `(token name, token value)` pair.
- The aiohttp `ClientSession` is now closed on every auth failure path (domain probe, IP fallback, both attempts failing). If you still see `Unclosed client session` warnings in proxbox-api logs after an auth failure, you are running an older build — re-check the installed package version.

## Sync endpoints return partial data

Symptom:

- Some objects sync while others fail.

Resolution:

- Inspect API logs for per-object exceptions with `GET /admin/logs`.
- Validate required NetBox objects and plugin models exist.
- Re-run sync using WebSocket mode for live visibility.

## Database state concerns

Note:

- Current startup DB behavior creates missing tables without dropping data.

If needed:

- Backup `database.db` before schema experiments.

## SSE streaming issues

### Empty or stalled stream

Symptom:

- SSE `/stream` endpoint connects but no events arrive.

Resolution:

- Verify NetBox and Proxmox endpoints are configured (`GET /netbox/endpoint`, `GET /proxmox/endpoints`).
- Check API logs for exceptions during the sync task.
- Confirm the HTTP client is not buffering (use `Accept: text/event-stream` header).
- Ensure `Cache-Control: no-cache` is respected by any intermediate proxy.

### Stream timeout

Symptom:

- SSE stream disconnects before sync completes.

Resolution:

- Increase client-side timeout or use streaming-aware HTTP client.
- For large inventories, consider using lower `PROXBOX_VM_SYNC_MAX_CONCURRENCY` and `PROXBOX_NETBOX_WRITE_CONCURRENCY` values to reduce NetBox API pressure and avoid cascading timeouts.
- Check `PROXBOX_NETBOX_TIMEOUT` is sufficient for your NetBox server response time.

### SSE response contains hop-by-hop header error

Symptom:

- HTTP 500 with `AssertionError: Hop-by-hop header not allowed` in Django proxy environments.

Resolution:

- SSE responses must not include `Connection: keep-alive` header when served through WSGI middleware (e.g., Django plugin proxy).
- Ensure stream responses only use `Cache-Control: no-cache` and `X-Accel-Buffering: no` headers.

### Stream returns error event instead of complete

Symptom:

- Sync finishes with `event: error` followed by `event: complete` with `ok: false`.

Resolution:

- Check the `error` and `detail` fields in the error event payload.
- Common causes: NetBox API unreachable, Proxmox auth failure, missing required NetBox plugin models.
- Re-run with WebSocket mode (`/ws`) for more verbose logging if needed.
