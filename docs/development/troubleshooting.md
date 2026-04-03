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

## Proxmox connection failures

Symptom:

- Connection exceptions during session instantiation.

Resolution:

- Validate Proxmox host, port, and credentials.
- Check `verify_ssl` behavior and certificates.
- Confirm API user/token permissions in Proxmox.
- For multi-endpoint deployments, confirm the correct `source` and endpoint target selection values are being passed.

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
