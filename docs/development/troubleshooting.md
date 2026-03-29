# Troubleshooting

## No NetBox endpoint found

Symptom:

- API routes that need NetBox session fail with message indicating no endpoint configured.

Resolution:

1. Create NetBox endpoint with `POST /netbox/endpoint`.
2. Verify it exists with `GET /netbox/endpoint`.

## Proxmox endpoint auth validation errors

Symptom:

- `400` with `Provide password or both token_name/token_value`.
- `400` with `token_name and token_value must be provided together`.

Resolution:

- Provide password auth, or provide complete token pair.

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

## Sync endpoints return partial data

Symptom:

- Some objects sync while others fail.

Resolution:

- Inspect API logs for per-object exceptions.
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
- For large inventories, consider using lower `PROXBOX_VM_SYNC_MAX_CONCURRENCY` values to reduce NetBox API pressure and avoid cascading timeouts.
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
