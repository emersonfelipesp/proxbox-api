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
