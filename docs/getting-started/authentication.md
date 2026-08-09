# Authentication

`proxbox-api` uses database-backed API key authentication. All API keys are stored in the SQLite database with bcrypt hashing. There is no environment variable authentication — all key management happens through the API endpoints.

## Bootstrap Flow

When the backend starts with a never-initialized database, it returns `needs_bootstrap: true` from the status endpoint:

```bash
curl http://localhost:8800/auth/bootstrap-status
# {"needs_bootstrap": true, "has_db_keys": false}
```

### First Key Registration

The first API key can be registered without authentication (bootstrap mode):

```bash
curl -X POST http://localhost:8800/auth/register-key \
  -H "Content-Type: application/json" \
  -d '{"api_key": "your-secure-api-key-at-least-32-chars", "label": "bootstrap-key"}'
# {"detail": "API key registered."}
```

**Bootstrap is consumed exactly once per database.** The registration commits a
durable singleton bootstrap claim together with the bcrypt hash of the first
key in a single transaction, so two concurrent bootstrap attempts cannot both
succeed — the loser receives a stable `409 Conflict`. Once bootstrap is
consumed, every later call to `/auth/register-key` returns `409 Conflict`,
**including when all keys have since been deactivated or deleted**: inactive
key history and the permanent claim both close the unauthenticated bootstrap
window forever. Databases initialized before the claim existed are backfilled
on startup — any key history permanently closes bootstrap there too.

### Losing All Keys

Because bootstrap never reopens, the API refuses to retire the final active
key: `DELETE /auth/keys/{id}` and `POST /auth/keys/{id}/deactivate` return
`409` with code `last_active_api_key_required` when the target is the only
active key. Create and verify a replacement key first, then retire the old
one. If a database somehow ends up with no active key, recovery is a
database-level operation by the operator (restore a backup or edit the SQLite
`apikey` table directly) — not an unauthenticated HTTP path.

### NetBox Plugin Integration

When you save a `FastAPIEndpoint` in NetBox, the plugin automatically:

1. Generates a 64-character secure token
2. Calls `/auth/bootstrap-status` to check if registration is needed
3. Calls `/auth/register-key` to register the token with the backend
4. Stores the token in NetBox for future authenticated requests

## Using API Keys

All requests (except bootstrap endpoints) require the `X-Proxbox-API-Key` header:

```bash
curl http://localhost:8800/proxmox/endpoints \
  -H "X-Proxbox-API-Key: your-secure-api-key-at-least-32-chars"
```

## Auth-Exempt Endpoints

These endpoints do not require authentication:

| Endpoint | Purpose |
|----------|---------|
| `GET /` | Root metadata |
| `GET /docs` | OpenAPI documentation |
| `GET /redoc` | ReDoc documentation |
| `GET /openapi.json` | OpenAPI schema |
| `GET /health` | Health check |
| `GET /meta` | Service metadata |
| `GET /auth/bootstrap-status` | Check if bootstrap is needed |
| `POST /auth/register-key` | Register first key (only while bootstrap has never been consumed) |

## Key Management Endpoints

All key management endpoints require authentication:

### List API Keys

```bash
curl http://localhost:8800/auth/keys \
  -H "X-Proxbox-API-Key: your-key"
# {"keys": [{"id": 1, "label": "bootstrap-key", "is_active": true, "created_at": 1712345678.123}]}
```

### Create a New Key

```bash
curl -X POST http://localhost:8800/auth/keys \
  -H "X-Proxbox-API-Key: your-key"
# {"id": 2, "label": "", "is_active": true, "created_at": 1712345678.456, "raw_key": "the-newly-generated-key"}
```

The `raw_key` is only returned once — store it securely.

### Deactivate a Key

```bash
curl -X POST http://localhost:8800/auth/keys/1/deactivate \
  -H "X-Proxbox-API-Key: your-key"
# {"id": 1, "label": "bootstrap-key", "is_active": false, "created_at": 1712345678.123}
```

Deactivating the final active key is refused with `409`
(`last_active_api_key_required`) — create and verify another key first.

### Reactivate a Key

```bash
curl -X POST http://localhost:8800/auth/keys/1/activate \
  -H "X-Proxbox-API-Key: your-key"
# {"id": 1, "label": "bootstrap-key", "is_active": true, "created_at": 1712345678.123}
```

### Delete a Key

```bash
curl -X DELETE http://localhost:8800/auth/keys/1 \
  -H "X-Proxbox-API-Key: your-key"
# (204 No Content)
```

Deleting the final active key is refused with `409`
(`last_active_api_key_required`) — create and verify another key first.

## Brute-Force Protection

The backend stores lockout state in SQLite under a composite, secret-free
bucket: normalized network source/trust context plus a server-keyed HMAC
identifier. Reaching one credential bucket's threshold therefore cannot lock a
different key used from the same worker, reverse proxy, or client IP. A separate,
deliberately higher source-abuse threshold still blocks every key from that
source when exhausted. Sync and async authentication share the same state
service. Before bcrypt, each request inserts an independent
durable reservation row with an unguessable token and its own expiry. The row
consumes the per-credential, per-source, and global verification-concurrency
capacity. After bcrypt, an atomic token-scoped delete finalizes that exact row
once: a rejected key converts it to credential/source failure state in the same
transaction, while an accepted key records no failure. Duplicate finalization
cannot consume another request's reservation. Concurrent valid traffic therefore
cannot manufacture a lockout. Exhausted verification capacity returns HTTP 503
with `Retry-After: 1` or WebSocket close code 1013; it does not consume a failure
attempt.

An abandoned crash token expires after at least 60 seconds (or the longer
configured lockout window). Once expired it stops consuming concurrency
capacity. Its row remains available for exactly-once late finalization for one
hour after expiry and is counted by the orphan-reservation metric. Older rows are
compacted into a durable aggregate counter, bounding storage; a finalizer beyond
that documented horizon is ignored. One orphan cannot extend the expiry of
another live token or release newer work.
A second durable source budget bounds attacks that rotate credentials; it is
deliberately higher than the per-credential default. Durable failure rows are
split into independently bounded credential and source partitions. Expired
failure windows are pruned, but saturation never prevents bcrypt for a valid,
previously unseen key because reservations do not require a failure row. A
rejected key is still denied when either partition cannot admit its new identity;
the unpersisted identity is included in aggregate failure and row-capacity
counters rather than evicting another live pre-lockout budget.

- Default threshold: 5 failed attempts (`PROXBOX_AUTH_LOCKOUT_THRESHOLD`, range 1-100)
- Default source budget: 50 failed attempts (`PROXBOX_AUTH_LOCKOUT_SOURCE_THRESHOLD`, range 1-100000)
- Default fixed window: 5 minutes (`PROXBOX_AUTH_LOCKOUT_WINDOW_SECONDS`, range 1-86400)
- Maximum durable bucket rows: 10000 (`PROXBOX_AUTH_LOCKOUT_MAX_BUCKETS`, range 2-1000000)
- Maximum concurrent verifications per credential/source bucket: 32
  (`PROXBOX_AUTH_LOCKOUT_MAX_IN_FLIGHT`, range 1-1024)
- Maximum concurrent verifications across all workers and identities: 256
  (`PROXBOX_AUTH_LOCKOUT_MAX_GLOBAL_IN_FLIGHT`, range 1-4096)
- An opaque identity key is atomically generated in the private sibling
  `database.db.auth-lockout.key` file by default. `PROXBOX_AUTH_LOCKOUT_HMAC_KEY`
  can supply an explicit 32-byte-or-longer value instead. Keep either source
  stable across restarts and separate from rotatable credential encryption.
- Startup records a non-secret fingerprint and generation in SQLite. Once bound,
  a missing/replaced file or different environment key is fatal and is never
  regenerated silently. Every worker validates the same binding under the
  target-specific startup lock and pins the verified key material in process
  memory. Deleting or replacing the source after startup cannot change bucket
  identities in that worker; recovery or rotation requires the offline procedure
  followed by a controlled restart.
- `PROXBOX_TRUSTED_PROXIES` explicitly controls which peer CIDRs may supply
  `X-Forwarded-For`. No address, including localhost, is trusted implicitly.
  Trusted proxies do not bypass authentication or lockout.

The metrics endpoints expose only aggregate, label-free `proxbox_auth_*`
counters and gauges. In addition to failure/lockout/recovery totals and active
lockouts, the lockout service publishes:

- `proxbox_auth_capacity_rejections_total`: verification admissions rejected by
  a per-bucket or global in-flight limit plus failed identities whose bounded
  credential or source row partition could not persist;
- `proxbox_auth_orphan_compactions_total`: expired reservation rows compacted
  after the supported one-hour late-finalization horizon;
- `proxbox_auth_bucket_rows`: current durable credential/source failure rows;
- `proxbox_auth_verifications_in_flight`: unexpired reservation rows currently
  consuming bcrypt capacity; and
- `proxbox_auth_expired_orphan_reservations`: expired crash-token rows retained
  within the supported late-finalization horizon.

Logs and the recovery CLI use 12-character non-authenticating HMAC identifiers;
raw keys and dictionary-testable hashes are never rendered.

### Local lockout recovery

Lockout administration is deliberately local and does not traverse the HTTP
authentication middleware, so it remains usable during an HTTP lockout:

```bash
proxbox-auth-lockout --database /data/database.db list
proxbox-auth-lockout --database /data/database.db clear --id 4a12bc34de56
# Emergency reset of transient lockout state only:
proxbox-auth-lockout --database /data/database.db clear --all
```

The database path is mandatory and must already contain the complete current
lockout schema, including reservation, metric, and key-binding tables; the CLI
validates that schema but never initializes or migrates a database. `list` opens
SQLite in read-only mode. Its output contains source IP/trust context, bucket
type, short bucket and credential identifiers, attempt count, and lock expiry;
it never contains API-key material.

### Offline identity-key recovery or rotation

Prefer restoring the bound key file from backup. If that is impossible, a new
key generation necessarily invalidates all existing opaque bucket IDs. Perform
this explicit reset only while every worker is stopped:

1. Stop every proxbox-api worker and preserve a recoverable database/key backup.
2. Create the replacement key as a regular, non-symlink UTF-8 file containing
   at least 32 bytes and mode `0600`.
3. Run:

   ```bash
   proxbox-auth-lockout --database /data/database.db rebind-key \
     --key-file /data/database.db.auth-lockout.key.new \
     --confirm-reset-lockouts
   ```

4. Configure/install that exact key source for every worker, start the service,
   and verify readiness plus authentication.

The command takes the database startup lock and an exclusive runtime lease. It
refuses to run while any worker is active, validates the existing recovery
schema, atomically clears incompatible lockout buckets and all outstanding
reservation rows, advances the non-secret generation, and never prints key
material. If the binding row itself was lost, recovery creates generation 1
after clearing all opaque state. Do not replace the bound file and roll workers
gradually.

## Security Best Practices

1. **Use strong keys**: At least 32 characters, preferably 64 characters
2. **Store keys securely**: Treat the `raw_key` from `/auth/keys` as a password — store it once
3. **Rotate keys regularly**: Create a new key, update your applications, delete the old one
4. **Use HTTPS in production**: Keys are sent in headers — protect them in transit
5. **Limit key scope**: Create separate keys for different purposes (monitoring, sync, admin)

## Troubleshooting

### "No API key configured"

```
{"detail": "No API key configured. Register a key via POST /auth/register-key or use an existing key."}
```

The database has no API keys. On a never-initialized database, call
`/auth/register-key` with a new key to bootstrap. On a database that was
already bootstrapped once, `/auth/register-key` stays closed (`409`); recover
at the database level instead (restore a backup or repair the `apikey` table).

### "Invalid API key"

Check that:

1. You're sending the `X-Proxbox-API-Key` header
2. The key value matches exactly (no extra spaces or newlines)
3. The key hasn't been deactivated or deleted

### "Too many failed authentication attempts"

Wait for the configured fixed window to expire, or use the local
the explicit-database `proxbox-auth-lockout list` and `clear --id` commands.
Lockout state and aggregate counters are durable SQLite data, so restarting the
backend is not a recovery mechanism.
