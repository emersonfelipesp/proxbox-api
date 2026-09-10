# Proxmox Console Sessions

This document is the canonical `proxbox-api` implementation guide for the private console-session broker used by the NMS browser console. It explains how the service selects a Proxmox guest, requests a `vncproxy` or `termproxy` ticket, constructs the upstream WebSocket URL, exports one bounded authentication value to the trusted relay, preserves endpoint TLS policy, and handles failures.

This endpoint is service-to-service. Its response contains short-lived credentials and must never be returned directly to browser JavaScript. `nms-backend` is the trusted consumer: it stores the response in a one-time server-side Redis relay ticket and exposes only an opaque NMS stream token to the browser.

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
nms browser
    |
    | opaque NMS stream token only
    v
nms-backend trusted relay
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

`proxbox-api` does not authorize an end user against a NetBox virtual machine. `nms-backend` performs that caller-scoped object authorization and maps any NetBox endpoint relation to the `proxbox-api` database ID before calling this route. This service trusts its authenticated service caller to supply an already authorized `endpoint_id` and then validates that the endpoint exists locally.

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

The route deliberately returns enough data for a trusted relay to open the Proxmox WebSocket. It is not a public browser-session schema. `nms-backend` must transform it into its much smaller public response.

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

`resolve_async()` normalizes synchronous and asynchronous SDK result styles. The route does not start, stop, or mutate the guest configuration; it requests only the ephemeral Proxmox console proxy.

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

`ProxmoxWebSocketAuth.value` and `ConsoleWebSocketAuth.value` use `repr=False` so ordinary object rendering does not reveal the credential. The route returns the value only to the trusted relay. `nms-backend` validates its kind and bounds again, stores it inside the one-time ticket, and attaches it only to the server-side WebSocket handshake.

Do not add a second authentication field, expose the value in logs, or move it into a browser response or URL.

## WebSocket URL construction

Both noVNC and terminal sessions connect to the Proxmox `vncwebsocket` endpoint:

```text
wss://{host}:{proxmox_port}/api2/json/nodes/{node}/{vm_type}/{vmid}/vncwebsocket
    ?port={console_port}&vncticket={percent-encoded-ticket}
```

`_build_ws_url()` uses `quote(ticket, safe="")`. An empty safe set is required because Proxmox tickets commonly contain characters that would otherwise change query-string parsing. The host and HTTPS port come from the persisted endpoint, while the node, type, and VMID come from the schema-validated request.

`nms-backend` validates that the URL uses `wss://` before storing it. Browser code never receives this URL.

## TLS policy

`verify_ssl` comes only from the stored `ProxmoxEndpoint`. This service returns it to the trusted relay alongside the private upstream URL. The relay applies it only when creating the Proxmox WebSocket client.

The request schema intentionally has no TLS-verification field. A caller cannot downgrade certificate verification per console request. Prefer verified certificates; use a persisted `verify_ssl=false` endpoint only when the deployment has explicitly accepted that endpoint-specific risk.

## Failure and logging behavior

| Condition | Result |
|---|---|
| Request schema or LXC/noVNC violation | HTTP 422 before endpoint or Proxmox access |
| Unknown local endpoint | HTTP 404 |
| Stored endpoint cannot create a session | Sanitized HTTP 502 |
| `ProxmoxAPIError` from `vncproxy`/`termproxy` | HTTP 502 with a broker detail intended only for the trusted backend; `nms-backend` maps it to a bounded browser-safe message |
| Unexpected proxy-call exception | Fixed HTTP 502 `Proxmox console request failed.` |
| Missing or malformed ticket/port | Fixed HTTP 502 `Proxmox did not return a ticket/port.` |
| WebSocket authentication cannot be prepared | Fixed HTTP 502 `Unable to authenticate the Proxmox console stream.` |

Logs identify the endpoint or guest tuple and exception class needed for service diagnosis. Never log response bodies, ticket values, `ws_url`, authorization values, cookies, or credential-bearing exception text. The outer NMS relay applies an additional sanitization boundary before any failure reaches the browser.

## Security invariants

- Keep this endpoint service-authenticated and server-to-server; never call it directly from browser JavaScript.
- Keep end-user object authorization in `nms-backend` before this route is called.
- Keep `endpoint_id` defined as the local proxbox-api database ID.
- Keep request models `extra="forbid"` and the QEMU/LXC mode matrix explicit.
- Obtain host, port, credentials, authentication mode, and TLS policy from the stored endpoint only.
- Percent-encode the complete ticket with `safe=""`.
- Return exactly one bounded authentication kind/value and keep secret values out of repr and logs.
- Do not add credentials to query parameters beyond Proxmox's required one-time `vncticket` in the private upstream URL.
- Keep the browser-facing schema in `nms-backend`; do not reuse `ConsoleSessionResponse` as a public contract.
- Keep failures sanitized across the relay boundary.

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

Run the focused gate with:

```bash
uv run pytest -q tests/proxmox/test_console_route.py
```

## Change checklist

When the console broker changes:

1. update this guide, the short HTTP reference, `README.md`, and the related `CLAUDE.md`/`AGENTS.md` context;
2. verify the request and private response schemas against `nms-backend`;
3. preserve API-token and password-session behavior;
4. verify all three supported workload/console combinations;
5. test malformed SDK responses and ports;
6. verify secrets remain absent from public schemas, repr output, logs, and browser responses;
7. run the focused test above and the repository's normal quality gates; and
8. coordinate any cross-service contract change with the NMS relay before deployment.
