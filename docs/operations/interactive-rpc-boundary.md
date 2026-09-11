# Interactive RPC-Only Boundary

The default `PROXBOX_EXECUTION_MODE=rpc_only` refuses unrestricted SSH and
Proxmox console capabilities and the two legacy actorless synchronization
WebSockets. This is a scoped runtime boundary, not a claim that every operation
in the backend already has an audited RPC implementation.

## Process configuration

The composition root reads these operator-only values once, without contacting
NetBox, consulting cached plugin settings, or changing endpoint flags:

```dotenv
PROXBOX_EXECUTION_MODE=rpc_only
PROXBOX_EXECUTION_GENERATION=operator-selected-cutover-generation
```

The only accepted modes are the exact strings `rpc_only` and `legacy`. An
absent mode selects `rpc_only`; an empty, misspelled, differently cased, or
whitespace-padded mode is a configuration error. A supplied generation must be
1–128 ASCII letters, digits, periods, underscores, colons, or hyphens, beginning
with a letter or digit. A missing generation does not prevent inventory-only
startup, but never establishes cutover readiness. There is no generated worker
default, plugin-setting fallback, hot activation endpoint, or automatic legacy
fallback. `PROXBOX_FEATURES` controls router availability independently and
cannot permit interactive execution.

An operator may explicitly select `legacy` for a compatibility deployment.
That mode preserves real service authentication, existing caller/object checks
in companion services, endpoint ID spaces, SSH access-method restrictions,
pinned host keys, and persisted Proxmox TLS/authentication policy. It is not an
RPC approval exception and never reports local RPC-only readiness.

## Enforced surfaces

| Surface | RPC-only behavior | Explicit legacy behavior |
|---|---|---|
| `POST /ssh/sessions` | Static HTTP 403 before ticket creation or retained inline credentials | Existing service-key authentication and one-use ticket creation |
| `WS /ssh/sessions/{session_id}/ws` | Close 1008 before ticket consumption or credential acquisition, including valid old tickets | Bounded ticket authentication before stored NetBox configuration; inline one-shot material opens no NetBox provider |
| `POST /proxmox/console/sessions` | Static HTTP 403 before endpoint resolution, decryption, connection, or proxy creation | Private QEMU noVNC, QEMU terminal, and LXC terminal contracts with an owned Proxmox client |
| `WS /ws` and `WS /ws/virtual-machines` | Close 1008 before authentication or effectful providers | Bounded API-key authentication before NetBox tokens, Proxmox sessions, collectors, and tag reconciliation |

The HTTP denial body is `{"detail":"Interactive execution is unavailable."}`.
A refusal may precede normal authentication and body validation because it
grants nothing. It is not evidence that a supplied API key is valid. The
counter WebSocket, independent inventory/discovery paths, and host-key-only
scan retain their separate contracts. Generated `/proxmox/api2/*` non-GET
dispatch remains unconditionally denied in both modes; no generic lease or
mode flag bypass is added.

The exact-path ASGI boundary runs before FastAPI dependency resolution. Service
checks independently require an active owned admission. For synchronization
WebSockets, the pinned FastAPI solver runs the route-level authentication
dependency before the handler's existing provider graph. The real mounted ASGI
ordering tests must pass on dependency upgrades; moving authentication into a
wrapper with eager client subdependencies is not equivalent.

## Ownership and shutdown

One worker owns each complete guarded request or WebSocket lifetime, including
authentication waits, credential acquisition, SSH connect, PTY creation, relay
pumps, and teardown. Local quiesce is one-way: it refuses new admissions,
cancels active operations, prevents subsequent stdin/resize/output forwarding,
and releases only that worker's pending SSH entries and inline references.
Deleting a ticket alone is never active-session revocation. Python reference
release does not claim secure memory erasure.

Acquisition is tracked separately from its waiting caller. A resource returned
after caller cancellation still belongs to cleanup and cannot be delivered.
Cleanup survives repeated cancellation and is bounded; unfinished cleanup
remains counted and sets sticky `remote_outcome_unknown`. SSH process close
waits after termination and, if necessary, after kill. A confirmed local
transport close does not prove that previously submitted remote work was
rolled back. Console proxy creation grants future interactive write capability
even though it does not change guest configuration. Quiesce during an
ambiguous console request retains explicit uncertainty and does not return
the private capability.

The FastAPI lifespan quiesces this local runtime before disposing its database.
This is not a fleet activation protocol or permission to disconnect production
sessions. A restart-only policy cannot revoke another old worker's live SSH
process or a console token already held by an old NMS relay.

Legacy synchronization also retains every Proxmox acquisition independently,
before the shared yield dependency can register its normal cleanup. A sibling
failure cannot abandon an already-acquired client, and a late connector remains
owned until it closes or becomes explicit unresolved cleanup. Admission teardown
settles acquisition tasks before closing its registered clients; an exit stack
alone is not sufficient. Independent inventory calls retain their existing
provider cleanup and do not require an interactive admission.

## Local status and aggregate cutover

`GET /execution-policy` uses the existing local API-key authentication. It
does not acquire managed providers or count itself as an interactive lifetime.
Its closed, secret-free schema is:

| Field | Meaning |
|---|---|
| `component` | Fixed `proxbox-api` ownership label |
| `capability` | Fixed `interactive-rpc-boundary-v1` contract |
| `mode`, `generation` | Process-pinned policy and operator generation, or null generation |
| `quiescing` | One-way local shutdown state |
| `active`, `cleanup_active` | Locally owned operation and residual cleanup counts |
| `remote_outcome_unknown` | Sticky local uncertainty, even after active resources disappear |
| `local_ready` | Only this component's scoped interactive denial boundary: RPC-only, generation present, not quiescing, no active/cleanup work, and no local uncertainty |
| `aggregate_ready` | Always false; this component does not certify peers or fleet cutover |

The NMS companion uses `NMS_PROXBOX_EXECUTION_MODE` and the same
`PROXBOX_EXECUTION_GENERATION`, but owns its Redis consumer and relay status
independently. A coordinator must verify compatible published capabilities,
matching generations, all ingress/caller paths, and the disposition of every
legacy-capable worker and active session before declaring fleet readiness.
Missing NMS/plugin support, unknown workers, unresolved remote effects, or
partial rollout remain unready. Restarting a process does not resolve prior
fleet uncertainty. Never silently roll back to permissive legacy mode.

This component does not implement NMS Redis ticket revocation, plugin
pre-persistence admission, queued synchronization quarantine, the complete
operation-to-RPC catalog, or the aggregate coordinator. Those remain explicit
release prerequisites for the parent runtime integration. No previously
published package minimum is asserted to satisfy these new capabilities.

## Verification

The native and mounted suites are `tests/test_interactive_policy.py`,
`tests/test_interactive_boundary.py`, `tests/test_interactive_ssh_lifetime.py`,
`tests/test_interactive_console_lifetime.py`,
`tests/test_interactive_provider_lifetime.py`,
`tests/test_ssh_terminal.py`, and `tests/proxmox/test_console_route.py`. They
use disposable local state, including a real loopback AsyncSSH transport.
Full core coverage retains the repository's branch-inclusive 65.40% threshold
and separate generated-route scope. Hosted CI waiver does not waive native
tests, security mutation evidence, complexity, or independent review.
