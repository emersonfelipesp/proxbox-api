# proxbox-api 0.0.27.post3

## Summary

This post-release hardens credential and network boundaries, adds endpoint-safe
device ownership and run-correlated synchronization completion, and improves
readiness recovery when NetBox settings permissions are missing. It also adopts
FastAPI 0.143.0 telemetry controls, recognizes NetBox 4.7 as a supported runtime
window, and separates standalone Proxmox mock package publication from protected
manual image publication. It adds no database migrations.

## Release identity

| Component | Release or contract |
| --- | --- |
| proxbox-api | `0.0.27.post3` |
| Paired NetBox plugin | `netbox-proxbox 0.0.29.post5` |
| Proxmox SDK | `proxmox-sdk 0.0.15` |
| NetBox SDK | `netbox-sdk 0.0.13` |
| Python | 3.12 and 3.13 |
| Database migration | None |

## Security and network boundaries

- **Committed endpoints alone become shared SSRF authorities.** NetBox and
  Proxmox endpoint hosts remain request-scoped until their database transaction
  commits. A rejected or rolled-back endpoint can no longer leave its host in
  the process-wide allow cache.
- **SSH uses validated destinations and the credentialed connection's pinned
  key.** Terminal creation, host-key scanning, and fixed SSH commands apply the
  shared SSRF policy and connect only to validated addresses. TCP refusal or
  timeout may advance through a bounded ordered address list, but a host-key or
  authentication failure never falls through to another address. Ambient known
  hosts, SSH client configuration, agents, default keys, GSS, host-based and
  keyboard-interactive authentication, PKCS#11, X.509 trust, and client
  certificates are disabled. The negotiated server key must match the stored
  pin before a PTY or command is opened. IPv6 address family, flow information,
  and interface scope are retained through connection.
- **Firecracker provisioning has a stronger caller and transport boundary.**
  Both provisioning routes require a nonblank `X-Proxbox-Actor`. Connections
  use only validated addresses, preserve the canonical hostname for HTTP Host,
  HTTPS SNI, and certificate verification, retain scoped IPv6 identity, and use
  bounded fallback only for connection refusal and timeout. Upstream bodies and
  malformed response values are replaced with fixed client errors; the
  non-streaming route returns a sanitized HTTP 502, including when a custom
  transport fails while reading the response.
- **Settings reads fail closed without leaking credentials.** Requests carrying
  the NetBox token no longer follow redirects and reject bodies larger than
  4 MiB. An authorization denial retains the last good policy, or uses strict
  SSRF defaults on a cold start. `/health` reports the missing
  `view_proxboxpluginsettings` permission and performs rate-limited refreshes so
  readiness can recover after the permission is granted without a restart.
- **The API-key bootstrap claim remains one-shot.** Unauthenticated
  `POST /auth/register-key` is available only before the first durable key
  registration and cannot reopen after key history exists.

## Credential encryption

- **Plugin-key circular dependencies are rejected.** Creating a NetBox endpoint
  or changing its credentials returns HTTP 409 when the actual encryption
  material is the NetBox plugin key, including when that authority is recovered
  during the request. Unchanged pushes remain no-ops, and connection-only
  changes retain the existing credential ciphertext.
- **Key replacement and credential writes are coordinated.** Every persistent
  credential write captures the durable key generation and local-file identity,
  checks them again at encryption, and conditionally fences the ciphertext
  commit. A stale writer returns HTTP 409 without storing ciphertext. Key
  replacement performs its blocking SQLite work off the event loop; contention
  returns a secret-free HTTP 503 with `Retry-After`.
- **Local key replacement is visible across workers.** Cached keys are checked
  against the key-file identity on every use, and resolved material is checked
  again before cache publication. Atomic publication fsyncs the key file and
  parent directory before clearing the durable pending fence. A failed
  replacement leaves writes blocked until a successful retry, and lock
  contention cannot clear an inherited fence. A retried deletion also proves
  the directory update durable when an earlier attempt already removed the
  file.

## Synchronization and compatibility

- **Device ownership is endpoint-scoped.** The typed device sync-state records
  `proxmox_endpoint_raw_id`. A missing value remains a legacy unclaimed record,
  while a record owned by another endpoint cannot be adopted, patched, or
  restamped. Atomic NULL-to-endpoint claims use the paired plugin's row lock and
  HTTP 409 `endpoint_ownership_conflict` response, and the backend reads every
  successful claim back before proceeding. The single-device path now also
  refuses a rendered name already owned by another device before any write.
- **WebSocket runs can be correlated through completion.** Clients may send a
  bounded JSON command envelope with a `run_id`. Every progress, error, and
  terminal frame carries that identifier, and a full update ends with one
  aggregate result containing both stage outcomes. Nonzero failure counts from
  partially completed required stages reach the terminal result. Legacy
  bare-string commands and response frames remain unchanged.
- **NetBox 4.7 is in the supported runtime window.** Live 4.7.x deployments no
  longer receive the outside-window warning. The generated schema target and
  pinned `netbox-sdk 0.0.13` contract are unchanged.
- **Large synchronization orchestrators were decomposed without a contract
  change.** Full-update and virtual-machine phases now share bounded stage
  runners while retaining request ordering, events, failure isolation,
  cancellation ownership, and public signatures.

The backend and `netbox-proxbox 0.0.29.post5` may be upgraded in either order.
During a mixed-version rollout, existing synchronization contracts remain
available, but atomic device ownership claims and run-correlated WebSocket
completion require both paired releases.

| `netbox-proxbox` behavior | Device ownership behavior |
| --- | --- |
| `0.0.29.post5` with the field and atomic HTTP 409 contract | Supported; exactly one competing endpoint claim is authorized. |
| Older release that omits the field | Supported legacy mode; ownership is unclaimed and unverifiable, so synchronization continues without endpoint enforcement. |
| Version exposing the field without atomic conflict enforcement | Unsupported mismatch; read-back verification narrows the race, but a later writer can overwrite a completed claim. |

## Runtime and release operations

- **Interactive execution remains process-pinned.** `rpc_only` is the default
  and denies unrestricted SSH tickets, terminal consumption, console
  capabilities, and actorless synchronization WebSockets before effectful
  dependencies run. Explicit `legacy` mode retains authentication, endpoint
  restrictions, host-key pins, private console authentication, owned resources,
  cancellation-safe cleanup, and fail-closed checks after blocked operations.
  The authenticated status endpoint reports the local policy and deployment
  generation; local readiness does not certify a fleet cutover or complete RPC
  coverage.
- **Native telemetry is explicit.** FastAPI is pinned to 0.143.0. Automatic OTLP
  export now requires `FASTAPI_OTEL_AUTO_CONFIGURE=true` or the equivalent
  explicit application setting; an exporter endpoint alone does not enable it.
- **Standalone mock releases are package-first.** The published wheel is
  validated from an isolated environment with detached behavioral tests,
  package-index collisions fail closed, and image inputs are bound to immutable
  bases and offline dependency digests. Multi-platform image publication moved
  to a separate protected manual workflow that assembles only the current run's
  captured digests.

## Upgrade notes

1. Upgrade proxbox-api and `netbox-proxbox 0.0.29.post5` in either order, then
   restart every backend worker after installing the exact package or container.
2. Grant the NetBox service account `view_proxboxpluginsettings`. Confirm that
   `/health` recovers after the permission refresh.
3. Before changing the authenticated NetBox endpoint credentials, configure an
   independent `PROXBOX_ENCRYPTION_KEY` or local key file and confirm that
   `GET /admin/encryption/status` reports `env` or `local`.
4. Verify every SSH target remains permitted by the registered-endpoint or
   operator SSRF policy and that its stored host-key fingerprint matches the key
   presented on the credential-bearing connection.
5. Run one node synchronization and one correlated full update. Confirm that
   device ownership is stamped to the selected endpoint and that the final
   WebSocket aggregate identifies the requested run.

## Known limitations and operator actions

- Private or otherwise non-global SSH and Firecracker destinations are refused
  unless a registered endpoint or explicit operator SSRF policy permits them.
  Address fallback covers TCP reachability only; it never bypasses a host-key,
  TLS, HTTP protocol, or authentication failure.
- Key replacement still returns HTTP 409 while encrypted credentials depend on
  the current key. When all workers share the same SQLite database and local key
  file, a maintenance window that blocks endpoint changes is no longer needed.
  Restart every worker if shared-file visibility and cache revalidation cannot
  be guaranteed.
- A missing `view_proxboxpluginsettings` grant keeps cold-start readiness at
  HTTP 503 and applies strict SSRF defaults until a refresh succeeds.
- `rpc_only` local readiness is not evidence that companion services, callers,
  or the fleet are ready for cutover. Validate the shared generation and paired
  capabilities before disabling legacy compatibility.
- Standalone Proxmox mock image publication is no longer automatic after package
  publication. An authorized operator must run the protected manual image lane
  with qualified immutable inputs.
