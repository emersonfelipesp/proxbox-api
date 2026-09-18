# Proxmox Console Sessions

This document is the canonical `proxbox-api` implementation guide for the private console-session broker used by the management browser console. It explains how the service selects a Proxmox guest, requests a `vncproxy` or `termproxy` ticket, constructs the upstream WebSocket URL, exports one bounded authentication value to the trusted relay, preserves endpoint TLS policy, and handles failures.

The existing `/sessions` endpoint is service-to-service. Its response contains short-lived credentials and must never be returned directly to browser JavaScript. The existing trusted service consumer and its contract remain unchanged.

Open-source standalone deployments can instead use the distinct
`/browser-sessions` plus `/browser-stream` contract. proxbox-api then owns the
one-use relay state and browser WebSocket. It encrypts the complete private
upstream session in shared SQLite and returns only opaque browser-safe routing
data.

Both contracts require explicit `PROXBOX_EXECUTION_MODE=legacy`. The default
`rpc_only` policy refuses browser-session creation before endpoint access and
closes the browser stream before token consumption or upstream connection.

## Contents

1. [Ownership and trust boundary](#ownership-and-trust-boundary)
2. [Supported modes](#supported-modes)
3. [Request and response contracts](#request-and-response-contracts)
4. [Source map](#source-map)
5. [Endpoint and Proxmox session resolution](#endpoint-and-proxmox-session-resolution)
6. [Ticket acquisition](#ticket-acquisition)
7. [Response normalization](#response-normalization)
8. [WebSocket authentication](#websocket-authentication)
9. [WebSocket URL construction](#websocket-url-construction)
10. [TLS policy](#tls-policy)
11. [Failure and logging behavior](#failure-and-logging-behavior)
12. [Security invariants](#security-invariants)
13. [Regression coverage](#regression-coverage)
14. [Change checklist](#change-checklist)

## Ownership and trust boundary

```text
control-plane browser
    |
    | opaque control plane stream token only
    v
trusted-relay-service trusted relay
    |
    | service-authenticated POST /proxmox/console/sessions
    | endpoint_id + vmid + node + vm_type + console_type
    v
proxbox-api
    |
    | load configured endpoint and credentials
    | call Proxmox vncproxy or termproxy
    | prepare private WebSocket authentication
    v
Proxmox API and WebSocket endpoint
```

`proxbox-api` does not authorize an end user against a NetBox virtual machine. `trusted-relay-service` performs that caller-scoped object authorization and maps any NetBox endpoint relation to the `proxbox-api` database ID before calling this route. This service trusts its authenticated service caller to supply an already authorized `endpoint_id` and then validates that the endpoint exists locally.

## Supported modes

| Workload | `vm_type` | `console_type` | Proxmox operation |
|---|---|---|---|
| QEMU/KVM VM | `qemu` | `novnc` | `nodes(node).qemu(vmid).vncproxy.post(websocket=1)` |
| QEMU/KVM VM | `qemu` | `term` | `nodes(node).qemu(vmid).termproxy.post()` |
| LXC container | `lxc` | `term` | `nodes(node).lxc(vmid).termproxy.post()` |

LXC/noVNC is rejected by `ConsoleSessionRequest`. Unknown workload or console values, non-positive IDs, an empty node, and extra JSON fields also fail schema validation before endpoint lookup.

## Request and response contracts

`POST /proxmox/console/sessions` accepts:

```json
{
  "endpoint_id": 1,
  "vmid": 544,
  "node": "pve01",
  "vm_type": "qemu",
  "console_type": "novnc"
}
```

`endpoint_id` is the local `proxbox-api` `ProxmoxEndpoint` database primary key. It is not the NetBox virtual-machine primary key and is not necessarily the `netbox-proxbox` endpoint primary key.

The successful `ConsoleSessionResponse` is private transport material:

| Field | Meaning | Sensitivity |
|---|---|---|
| `ticket` | One-time Proxmox VNC/terminal ticket | Secret; server-side only |
| `port` | Ephemeral console proxy port returned by Proxmox | Private routing metadata |
| `proxmox_host` | Configured Proxmox endpoint host | Private topology |
| `proxmox_port` | Configured Proxmox HTTPS port | Private topology |
| `ws_url` | Fully encoded upstream `wss://` URL | Secret-bearing; includes the ticket |
| `console_type` | `novnc` or `term` | Non-secret routing value |
| `verify_ssl` | Persisted endpoint TLS verification policy | Server-controlled policy |
| `websocket_auth` | Exactly one private `authorization` or `cookie` value | Secret; server-side only |

The route deliberately returns enough data for a trusted relay to open the Proxmox WebSocket. It is not a public browser-session schema. `trusted-relay-service` must transform it into its much smaller public response.

### Standalone browser contract

`POST /proxmox/console/browser-sessions` accepts the same exact endpoint,
guest, and mode selector plus a serialized HTTPS NetBox `origin`:

```json
{
  "endpoint_id": 1,
  "vmid": 544,
  "node": "pve01",
  "vm_type": "qemu",
  "console_type": "novnc",
  "origin": "https://netbox.example"
}
```

Its response contains exactly:

```json
{
  "stream_token": "opaque-random-one-use-value",
  "websocket_path": "/proxmox/console/browser-stream",
  "expires_at": "2026-09-14T14:00:30Z",
  "console_type": "novnc"
}
```

The browser connects to the returned path with the same exact `Origin` header
and offers exactly `binary` plus
`proxbox-token.<stream_token>` as WebSocket subprotocols. The server accepts
only `binary`, so it never echoes the bearer protocol. Tokens are forbidden in
the URI and query string because request targets are commonly access-logged.
Missing, duplicate, malformed, extra, or query-string token forms are rejected
before upstream access. Tokens contain 32 random bytes, have a fixed 30-second
TTL, are stored only as SHA-256 digests, and are consumed atomically under a
SQLite write transaction. Unknown digests are rejected by an indexed read
before any write lock is requested. A matching digest is then re-read under
`BEGIN IMMEDIATE` before deletion, preserving exactly one successful consumer
without letting random token probes contend for the database write lock. The database row contains only the
digest, timestamps, and one Fernet ciphertext. The ciphertext holds the ticket,
upstream URL, authentication value, TLS policy, Origin, and guest binding. At
most 1,024 live rows are retained. Expired, replayed, malformed, wrong-Origin,
and policy-denied sessions fail closed.

The standalone route also requires the current local `ProxmoxEndpoint` row to
remain enabled at both creation and consumption. Disabling an endpoint after a
token is issued burns and rejects that token before any upstream connection.
This browser-only check does not change the existing service-only broker contract.

`PROXBOX_ALLOW_PLAINTEXT_CREDENTIALS` does not apply to relay state. Browser
session creation returns a fixed 503 response until a Fernet key resolves from
the normal encryption-key chain. Creation performs an encrypt/decrypt preflight
before requesting a Proxmox ticket, so unavailable Fernet state cannot consume
an upstream proxy worker. Capacity remains enforced atomically when the final
encrypted row is inserted.

## Source map

| File or symbol | Responsibility |
|---|---|
| `proxbox_api/routes/proxmox/console.py::ConsoleSessionRequest` | Strict endpoint/guest/mode request and LXC/noVNC rejection. |
| `proxbox_api/routes/proxmox/console.py::ConsoleSessionResponse` | Private trusted-relay response contract. |
| `proxbox_api/routes/proxmox/console.py::_load_endpoint()` | Loads the exact local `ProxmoxEndpoint`; missing IDs return 404. |
| `proxbox_api/routes/proxmox/console.py::_connect_endpoint()` | Parses the stored endpoint and creates a `ProxmoxSession`; connection preparation failures become sanitized 502 responses. |
| `proxbox_api/routes/proxmox/console.py::_request_console_proxy()` | Selects node, workload API, and `vncproxy`/`termproxy`. |
| `proxbox_api/routes/proxmox/console.py::_console_ticket()` | Unwraps SDK response shapes and extracts a valid non-empty ticket and port. |
| `proxbox_api/routes/proxmox/console.py::_console_port()` | Accepts an integer or ASCII decimal port in the inclusive range 1–65535; rejects booleans and every other shape. |
| `proxbox_api/routes/proxmox/console.py::_console_websocket_auth()` | Obtains and translates the active session's private WebSocket authentication. |
| `proxbox_api/routes/proxmox/console.py::_build_ws_url()` | Percent-encodes the ticket and constructs the exact Proxmox `vncwebsocket` URL. |
| `proxbox_api/routes/proxmox/console.py::create_console_session()` | Orchestrates the complete endpoint-to-private-response flow. |
| `proxbox_api/routes/proxmox/console.py::create_browser_console_session()` | Converts the private broker result into encrypted one-use shared state and a browser-safe response. |
| `proxbox_api/routes/proxmox/console.py::browser_console_stream()` | Atomically consumes the token, rechecks the endpoint policy seam, binds Origin/subprotocol, and owns relay cleanup. |
| `proxbox_api/database.py::BrowserConsoleRelaySession` | Shared SQLite row containing only token digest, timestamps, and encrypted payload. |
| `proxbox_api/services/console_relay.py` | State limits, Fernet storage, upstream handshake, RFB mediation, frame relay, timeouts, and deterministic cancellation. |
| `proxbox_api/services/console_relay_policy.py` | Inactive create/consume seam reserved for the separately reviewed RPC-only policy. |
| `proxbox_api/session/proxmox_core.py::ProxmoxSession.get_websocket_auth()` | Selects API-token `Authorization` or password-session `PVEAuthCookie` transport. |

## Endpoint and Proxmox session resolution

`_load_endpoint()` calls the async database session with the exact `endpoint_id`. An unknown row returns HTTP 404 and no Proxmox request occurs.

`_connect_endpoint()` converts the database row through `_parse_db_endpoint()` and calls `ProxmoxSession.create()`. Stored endpoint configuration supplies the host, port, credentials, authentication mode, and `verify_ssl` policy. A connection/session-construction exception logs only the endpoint ID and exception class and returns HTTP 502 with `Unable to connect to Proxmox endpoint.`.

The browser must never choose or override these stored credential and TLS values.

## Ticket acquisition

`_request_console_proxy()` constructs the Proxmox resource from the validated request:

```python
guest = px.session.nodes(req.node)
guest = guest.qemu(req.vmid) if req.vm_type == "qemu" else guest.lxc(req.vmid)
```

It then calls:

- `vncproxy.post(websocket=1)` for a graphical QEMU console; or
- `termproxy.post()` for QEMU and LXC terminal consoles.

`resolve_async()` normalizes synchronous and asynchronous SDK result styles. The route does not start, stop, or mutate the guest configuration, but its ephemeral console proxy grants future interactive write capability. The default `rpc_only` process policy therefore refuses both console-session creation contracts before endpoint resolution, decryption, or connection. The successful contracts described here require explicit `legacy` mode and normal service authentication. See [Interactive RPC-Only Boundary](../operations/interactive-rpc-boundary.md).

The request owns its Proxmox client through acquisition, proxy creation, private authentication, and cleanup. Late acquisitions are closed after cancellation; quiesce prevents further authentication requests and private response delivery. An ambiguous proxy outcome remains explicitly uncertain and is not described as rolled back.

## Response normalization

Proxmox SDK versions may return a plain dictionary, a Pydantic-style object with `model_dump()`, a legacy object with `dict()`, or a payload nested under `data`. `_console_ticket()` normalizes those shapes before reading `ticket` and `port`.

The ticket must be a non-empty string. `_console_port()` accepts only:

- an integer from 1 through 65535; or
- an ASCII-only decimal string whose numeric value is in that range.

It explicitly rejects booleans even though Python treats `bool` as an `int` subclass. Missing, malformed, zero, negative, or out-of-range values return the fixed HTTP 502 detail `Proxmox did not return a ticket/port.`.

## WebSocket authentication

A Proxmox console WebSocket upgrade needs both the ticket in the URL and authentication for the active Proxmox session. `ProxmoxSession.get_websocket_auth()` exposes exactly one typed value:

- `kind="authorization"` with the endpoint's API-token authorization value; or
- `kind="cookie"` with the password session's `PVEAuthCookie` value.

`ProxmoxWebSocketAuth.value` and `ConsoleWebSocketAuth.value` use `repr=False` so ordinary object rendering does not reveal the credential. The route returns the value only to the trusted relay. `trusted-relay-service` validates its kind and bounds again, stores it inside the one-time ticket, and attaches it only to the server-side WebSocket handshake.

Do not add a second authentication field, expose the value in logs, or move it into a browser response or URL.

## WebSocket URL construction

Both noVNC and terminal sessions connect to the Proxmox `vncwebsocket` endpoint:

```text
wss://{host}:{proxmox_port}/api2/json/nodes/{node}/{vm_type}/{vmid}/vncwebsocket
    ?port={console_port}&vncticket={percent-encoded-ticket}
```

`_build_ws_url()` uses `quote(ticket, safe="")`. An empty safe set is required because Proxmox tickets commonly contain characters that would otherwise change query-string parsing. The host and HTTPS port come from the persisted endpoint, while the node, type, and VMID come from the schema-validated request.

`trusted-relay-service` validates that the URL uses `wss://` before storing it. Browser code never receives this URL.

## TLS policy

`verify_ssl` comes only from the stored `ProxmoxEndpoint`. This service returns it to the trusted relay alongside the private upstream URL. The relay applies it only when creating the Proxmox WebSocket client.

The request schema intentionally has no TLS-verification field. A caller cannot downgrade certificate verification per console request. Prefer verified certificates; use a persisted `verify_ssl=false` endpoint only when the deployment has explicitly accepted that endpoint-specific risk.

The standalone relay carries this boolean only inside its encrypted payload.
It creates a default verified TLS context when true and a hostname/certificate-
disabled context only when the persisted endpoint value is false. Environment
WebSocket proxies are disabled so private authentication cannot be redirected
through ambient proxy configuration.
Every upstream WebSocket redirect status (`300`, `301`, `302`, `303`, `307`,
or `308`) is refused after the first handshake and before a second connection,
including same-origin redirects. Authorization and cookie credentials are
therefore never replayed to a redirect target.

## Standalone WebSocket and RFB behavior

Both upstream and browser WebSockets must negotiate `binary`; the browser also
offers the one bearer token protocol described above. Authentication
headers, tickets, URLs, handshake messages, application frames, queue depth,
open/handshake/idle/close times, token count, and TTL all have fixed bounds.
Binary and text application frames retain their frame kind in both directions.
When either relay direction finishes, the peer task is cancelled and both are
awaited before the upstream and browser are closed with fixed, secret-free
reasons.

The two frame directions share one connection-wide idle deadline. A valid frame
received in either direction refreshes it, so output-only and input-only
consoles remain active; the relay closes only after both directions are idle.

QEMU noVNC requires one extra trust-boundary step. proxbox-api completes the
upstream RFB 3.8 VNC authentication itself: it selects security type 2,
calculates the DES challenge response from the private ticket, verifies the
upstream success result, and then advertises security type 1 (None) to the
already authenticated, Origin-bound browser connection. Subsequent RFB bytes
are relayed unchanged. The browser therefore never receives or computes with
the Proxmox ticket. QEMU and LXC terminal sessions do not speak RFB and enter
the bounded frame relay directly.

## Failure and logging behavior

| Condition | Result |
|---|---|
| Request schema or LXC/noVNC violation | HTTP 422 before endpoint or Proxmox access |
| Unknown local endpoint | HTTP 404 |
| Stored endpoint cannot create a session | Sanitized HTTP 502 |
| `ProxmoxAPIError` from `vncproxy`/`termproxy` | Fixed HTTP 502 `Proxmox console request failed.`; upstream exception text is never returned |
| Unexpected proxy-call exception | Fixed HTTP 502 `Proxmox console request failed.` |
| Missing or malformed ticket/port | Fixed HTTP 502 `Proxmox did not return a ticket/port.` |
| WebSocket authentication cannot be prepared | Fixed HTTP 502 `Unable to authenticate the Proxmox console stream.` |

Logs identify the endpoint or guest tuple and exception class needed for service diagnosis. Never log response bodies, ticket values, `ws_url`, authorization values, cookies, or credential-bearing exception text. The outer control plane relay applies an additional sanitization boundary before any failure reaches the browser.

## Security invariants

- Keep this endpoint service-authenticated and server-to-server; never call it directly from browser JavaScript.
- Keep end-user object authorization in `trusted-relay-service` before this route is called.
- Keep `endpoint_id` defined as the local proxbox-api database ID.
- Keep request models `extra="forbid"` and the QEMU/LXC mode matrix explicit.
- Obtain host, port, credentials, authentication mode, and TLS policy from the stored endpoint only.
- Bracket IPv6 WebSocket authorities and validate plus percent-encode the node
  as one path segment.
- Percent-encode the complete ticket with `safe=""`.
- Return exactly one bounded authentication kind/value and keep secret values out of repr and logs.
- Do not add credentials to query parameters beyond Proxmox's required one-time `vncticket` in the private upstream URL.
- Keep the browser-facing schema in `trusted-relay-service`; do not reuse `ConsoleSessionResponse` as a public contract.
- Keep failures sanitized across the relay boundary.
- Store standalone state only when Fernet encryption is configured; the
  plaintext credential opt-in is never sufficient.
- Keep the random token opaque, fixed-length, digest-only at rest, one-use,
  short-lived, count-bounded, and atomically consumed across workers.
- Bind consumption to the exact HTTPS Origin and exactly one dedicated token
  protocol alongside `binary`; never place the token in a URI or echo it.
- Disable ambient WebSocket proxies and refuse every upstream redirect before
  a second connection can replay private credentials.
- Re-evaluate the focused endpoint policy seam at create and consume. Keep it
  inactive until the separately reviewed RPC-only policy is merged.
- Require the current endpoint to be enabled at create and consume without
  adding that browser-only rule to the existing service-only broker.
- Complete QEMU noVNC VNC authentication server-side; never send the ticket or
  challenge response inputs to browser JavaScript.

## Regression coverage

`tests/proxmox/test_console_route.py` covers:

- API-token and password-session WebSocket authentication;
- QEMU noVNC, QEMU terminal, and LXC terminal calls;
- `vncproxy(websocket=1)` versus `termproxy()` selection;
- endpoint lookup and connection failures;
- Proxmox and unexpected-error mapping;
- LXC/noVNC and malformed request rejection;
- dictionary, nested, and typed ticket response shapes;
- integer and decimal-string port normalization and invalid-port rejection;
- exact percent-encoding and WebSocket URL construction; and
- private response fields including endpoint TLS policy.

`tests/proxmox/test_browser_console_relay.py` adds native and mounted-ASGI
coverage for the three supported modes, strict type/Origin validation,
encryption absence, encrypted canaries, count/TTL/length bounds, independent-
session atomic consumption, expiry and replay, malformed ciphertext payloads,
URI-free token transport, malformed/duplicate protocol rejection, access-log
canaries, query-token rejection, upstream redirect refusal and credential
non-replay,
policy denial, `binary` negotiation, exact Origin binding, RFB 3.8 mediation,
bidirectional binary/text frames, cancellation/cleanup, and sanitized upstream
failure behavior.

Run the focused gate with:

```bash
uv run pytest -q tests/proxmox/test_console_route.py tests/proxmox/test_browser_console_relay.py
```

## Change checklist

When the console broker changes:

1. update this guide, the short HTTP reference, `README.md`, and the related `CLAUDE.md`/`AGENTS.md` context;
2. verify the request and private response schemas against `trusted-relay-service`;
3. preserve API-token and password-session behavior;
4. verify all three supported workload/console combinations;
5. test malformed SDK responses and ports;
6. verify secrets remain absent from public schemas, repr output, logs, and browser responses;
7. run the focused test above and the repository's normal quality gates; and
8. coordinate any cross-service contract change with the control plane relay before deployment.
