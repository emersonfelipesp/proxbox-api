# Security Policy and Threat Model

## Purpose and scope

This document defines the security policy and threat model for proxbox-api. It helps operators, vulnerability reporters, and maintainers distinguish a genuine boundary violation from an authorized infrastructure operation.

proxbox-api is a private FastAPI service used by NetBox Proxbox. It stores local configuration, authenticates service requests, connects to NetBox and Proxmox, synchronizes inventory, relays console traffic, and executes narrowly gated operational workflows. These capabilities are powerful by design and must remain behind explicit authentication, authorization, endpoint, approval, and audit boundaries.

This model is grounded in the [NetBox threat model](https://github.com/netbox-community/netbox/blob/main/THREAT_MODEL.md), uses the same boundary-first approach, and applies a lightweight STRIDE analysis. NetBox's policy does not cover this separate service, and NetBox maintainers are not responsible for proxbox-api reports.

## Supported versions

Security fixes are made against the latest released version and the current development branch. Before reporting, reproduce the issue on one of those versions. Older releases may be asked to upgrade before a report is evaluated.

## Reporting a vulnerability

Do not open a public issue, pull request, discussion, or chat thread for a suspected vulnerability.

Send a confidential report to [Emerson Felipe](mailto:emersonfelipe.2003@gmail.com) with the subject `proxbox-api security report`. Include:

- the affected version and deployment topology;
- prerequisites and required credentials or permissions;
- reproducible steps or a minimal proof of concept;
- the expected and observed result;
- the security impact and the boundary crossed;
- any proposed mitigation or patch.

Do not include production credentials, API tokens, private keys, database contents, or sensitive infrastructure data. Use synthetic values and redact logs. The maintainer will acknowledge the report, validate its scope, coordinate a fix and release when necessary, and agree on disclosure timing with the reporter. Do not disclose the issue publicly before coordinated remediation.

Reports must describe a confirmed, reproducible exploit. An automated scanner result without a realistic exploit path is not sufficient.

## Supported deployment model

This threat model assumes all of the following:

- proxbox-api is a private backend service. It is reachable only by NetBox Proxbox, a trusted authorization gateway, health monitoring, and service administrators. Restricted end users do not receive direct network access or backend API keys.
- Every non-bootstrap application route requires a valid API key or another documented route-specific credential. A backend API key is a broad administrative service credential; it does not carry route, endpoint, tenant, or user grants. The one-time first-key bootstrap route is reachable only during a controlled initial deployment and becomes unavailable after initialization.
- The reverse proxy terminates TLS, applies network request limits, and forwards client addresses only from explicitly configured trusted proxies. The application does not infer proxy trust from loopback or topology.
- The service database, credential-encryption key, lockout identity key, process environment, filesystem, worker controls, and backups are accessible only to trusted service administrators.
- NetBox and Proxmox endpoints use TLS with certificate verification in production. Operators configure exact endpoint authorities and do not permit untrusted redirects.
- NetBox, Proxmox, SSH, and companion credentials belong to dedicated least-privilege identities. Their upstream authorization remains an independent boundary.
- Infrastructure operators, host administrators, database administrators, and anyone who can read process secrets, key files, or the service database are fully trusted.
- Mutation controls are route-specific, not a universal default-off switch. Proxmox operational routes and Ceph workflows apply their documented gates, while Firecracker provisioning routes are mounted by default and can prepare assets, create a microVM, and start it for a valid administrative client with accepted upstream credentials. Operators who do not authorize Firecracker provisioning must block those routes at the trusted gateway or network boundary. Plans, single-use approvals, and fresh authority checks apply only to workflows that explicitly implement them; they are not universal API middleware.
- Ceph write execution remains disabled until a verified authenticated gateway derives and overwrites `X-Proxbox-Actor`, prevents clients from selecting actor identities, and blocks direct client access to Ceph write routes. Both Ceph execution flags remain false until that gateway is deployed as specified in the [Ceph write approval guide](docs/operations/ceph-write-approvals.md).
- Host hardening, firewall policy, backups, resource quotas, log retention, and availability controls are deployment responsibilities.

A report that depends on public anonymous exposure, a malicious host administrator, direct database modification, stolen operator credentials, unsafe bootstrap exposure, or deliberately disabled TLS verification is outside this supported model unless the report demonstrates a separate boundary bypass.

## Assets and trust boundaries

The protected assets include API-key hashes, credential ciphertext, encryption and lockout keys, NetBox tokens, Proxmox credentials, SSH credential material, console tickets, endpoint configuration, synchronization state, plans and approvals, task identifiers, audit records, inventory data, and the authority to mutate managed infrastructure.

The principal boundaries are:

1. **Client or reverse proxy to proxbox-api.** API-key authentication, trusted-proxy parsing, lockout accounting, route validation, and request limits protect entry to the service. API-key authentication establishes a trusted administrative service client; it does not enforce per-user, per-route, per-endpoint, or per-tenant authorization. Restricted callers must use a gateway that enforces their identity and permissions and keeps them from reaching proxbox-api directly.
2. **Bootstrap to initialized service.** The unauthenticated first-key operation is valid only while no active key exists. Concurrent or repeated registration must fail closed.
3. **proxbox-api to its database and key material.** Database path selection, permissions, migrations, encryption, key binding, rotation, and recovery must not silently create a fresh authority or expose plaintext.
4. **proxbox-api to NetBox Proxbox.** A dedicated NetBox token and the plugin's permissions constrain inventory reads and writes. Settings, capability, and credential responses are untrusted until validated.
5. **proxbox-api to Proxmox and execution hosts.** Exact endpoint selection, TLS verification, least-privilege credentials, `allow_writes`, and route-specific capability gates constrain the surfaces that implement them. Selected workflows add plans, approvals, revision binding, and fresh upstream checks. Other direct mutation routes can execute after their narrower gates succeed. Firecracker provisioning is mounted by default and depends on administrative API-key trust, accepted upstream credentials, and external gateway or network authorization rather than the Proxmox or Ceph write switches.
6. **Browser console relay.** The backend obtains and mediates short-lived upstream tickets. The browser must not receive reusable Proxmox credentials or arbitrary relay authority.
7. **Task and worker execution.** A queued operation must stay bound to the identity and state that its specific workflow declares. Workflows that implement reviewed plans, approvals, configuration revisions, or audit identities must preserve those bindings; their presence in one workflow does not protect another route automatically.
8. **RPC, SSH, image, Ceph, and companion integrations.** Each integration has a separate capability and audit boundary. Presence of credentials or a route does not grant execution authority.

## Trusted and untrusted actors

| Actor | Trust | Security posture |
| --- | --- | --- |
| proxbox-api process, database, and configured workers | Trusted | They execute service code and hold application state or secrets. |
| Infrastructure and service administrators | Trusted | Host, database, environment, filesystem, or secret access implies full control. |
| NetBox Proxbox with a valid service key | Trusted administrative service principal | A backend key grants broad service access. NetBox permissions are not encoded in the key, so NetBox or a trusted gateway must prevent less-privileged users from invoking unauthorized backend routes. |
| Other API-key holders | Trusted service administrators | Keys have no route, endpoint, tenant, or user scope. Do not issue them to restricted clients. Route-specific endpoint, write, approval, and upstream gates still apply where implemented. |
| Restricted users behind an authenticated gateway | Untrusted to proxbox-api | The gateway must derive their identity, enforce route and object authorization, overwrite trusted identity headers, and block direct backend access. Tenant fields alone do not provide isolation. |
| Proxmox and NetBox service identities | Trusted for configured upstream permissions | Upstream authorization must use least privilege and remains authoritative. |
| API requests, forwarded headers, remote responses, inventory values, guest-agent data, uploaded artifacts, generated schemas, and error bodies | Untrusted data | Validate before persistence, rendering, logging, code generation, path construction, or command dispatch. |
| Unauthenticated or Internet-adjacent parties | Untrusted | They must not reach the service except for deliberately exposed non-sensitive health checks and the controlled initial bootstrap. |

## Privileged-by-design behavior

The following behavior is intentional when an authenticated and authorized operator enables the corresponding capability:

- storing encrypted upstream credentials and using them to connect to NetBox, Proxmox, SSH, or companion services;
- discovering infrastructure and creating or updating NetBox inventory;
- opening bounded outbound requests to exact operator-configured endpoints;
- returning infrastructure data that the authenticated service identity may read;
- relaying a permission-approved browser console without exposing reusable upstream credentials;
- queuing synchronization, reconciliation, image, RPC, Ceph, VM, storage, backup, or other operational tasks;
- executing a managed-system mutation after all checks documented for that specific route succeed. Some workflows implement plans and single-use approvals; direct mutation routes rely on narrower backend gates plus authorization by NetBox or a trusted gateway.

These capabilities are not vulnerabilities merely because they access an internal system or change managed infrastructure. A vulnerability exists when an unauthorized actor obtains the capability, a declared gate is bypassed, the action differs from the reviewed request, authority changes without revalidation, or a secret crosses its intended boundary.

## In-scope vulnerabilities

Examples include:

- API-key authentication or lockout bypass, including reopening bootstrap after initialization or creating an additional unauthenticated key;
- trusting spoofed forwarding headers from an untrusted peer or merging distinct hostile sources into a victim's lockout identity contrary to the documented policy;
- horizontal or vertical privilege escalation across endpoint, gateway-enforced tenant or user scope, route, write, console, plan, approval, task, or companion boundaries;
- server-side request forgery, redirect following, URL or hostname injection, DNS/authority substitution, or credential forwarding to a destination other than the exact configured endpoint;
- exposure of plaintext credentials, API keys, console tickets, private keys, authorization headers, recoverable hashes, or encryption material through APIs, logs, errors, metrics, traces, generated artifacts, or backups;
- cryptographic or database-state flaws that permit ciphertext substitution, stale-key reuse, cross-endpoint decryption, silent database replacement, bootstrap reset, or unsafe key rotation and recovery;
- SQL/ORM injection, command injection, path traversal, unsafe deserialization, code-generation injection, cross-site scripting against a non-consenting user, or shell argument injection;
- replay, race, confused-deputy, or time-of-check/time-of-use flaws that bypass a single-use approval, endpoint revision, authority refresh, cancellation, or task ownership check in a workflow that declares that control;
- a disabled endpoint, `allow_writes=false`, `rpc_only` mode, absent capability, expired approval, or failed precondition still permitting network access or a mutation on a route that declares that control;
- bypass of tenant, object, route, or actor controls enforced by the trusted gateway or an explicitly scoped workflow;
- console relay origin confusion, ticket leakage, cross-VM access, or continued access after authorization revocation;
- dependency vulnerabilities with a realistic exploit path through a supported proxbox-api deployment.

## Out-of-scope issues and intended behavior

The following are normally outside this project's vulnerability scope:

- actions performed by a trusted host administrator, database administrator, or holder of valid credentials and every documented capability required for the action;
- direct modification of the database, key files, environment, executable, or service unit by an administrator;
- rate limiting at the public network edge, TLS termination, firewalling, host patching, backups, and resource quotas that belong to the reverse proxy or deployment platform;
- client-IP spoofing caused by configuring an untrusted address as a trusted proxy or by enabling another server's ambient proxy-header processing contrary to the deployment guide;
- denial of service that requires trusted administrative access or deployment outside documented capacity and network controls;
- failures caused solely by unsupported NetBox, Proxmox, Python, database, SDK, or plugin versions;
- use of intentionally disabled certificate verification in a production or hostile network;
- legitimate reads or writes permitted by the route-specific proxbox-api gates and the configured upstream service identity, when invoked by a trusted administrative client or authorized gateway;
- an automated dependency or source scan without a confirmed reachable exploit path.

## Lightweight STRIDE analysis

| Category | proxbox-api posture |
| --- | --- |
| Spoofing | API keys authenticate service clients; trusted-proxy rules establish network source; TLS and exact endpoint validation establish upstream identity. Bootstrap is a one-time initialization boundary. |
| Tampering | Pydantic validation, database transactions, route-specific write gates, and upstream authorization constrain changes. Selected workflows also enforce endpoint revisions, plans, approvals, and idempotency; direct mutation routes require external caller authorization. |
| Repudiation | Request, task, plan, approval, run, and upstream task identifiers provide audit linkage. Logs and telemetry must remain secret-free and synchronized in time. |
| Information disclosure | Credentials are hashed or encrypted as appropriate. Responses, errors, logs, metrics, traces, console relays, and generated files must redact sensitive material. |
| Denial of service | Authentication admission limits, lockout budgets, bounded concurrency, timeouts, task controls, proxy limits, and host quotas protect availability. Deployment capacity remains the operator's responsibility. |
| Elevation of privilege | A backend API key grants broad administrative access and does not encode tenant or user scope. Route-specific endpoint, capability, approval, task, gateway, and upstream controls remain authoritative where implemented. Any unintended bypass of a declared control is in scope. |

## Operator security checklist

- Keep proxbox-api, NetBox Proxbox, SDKs, and runtime dependencies on supported releases.
- Keep proxbox-api and its database private; expose only the required reverse-proxy surface.
- Complete first-key bootstrap from a controlled network, verify initialization, and retain at least two managed recovery keys before rotating one.
- Treat every backend API key as an administrative credential. Do not issue keys or direct network access to restricted users; route them through an authenticated authorization gateway.
- Require TLS and certificate verification for NetBox, Proxmox, and service-to-service connections.
- Configure `PROXBOX_TRUSTED_PROXIES` narrowly and keep framework proxy-header processing disabled as documented.
- Use dedicated least-privilege NetBox, Proxmox, SSH, and companion identities.
- Protect the database, credential-encryption key, lockout identity key, environment, and backups with restrictive ownership and permissions.
- Keep `allow_writes`, interactive execution, Ceph execution, and other available route-specific gates disabled until explicitly required. These controls do not disable Firecracker provisioning.
- Block Firecracker provisioning routes at the authenticated gateway or network boundary unless every holder of a backend API key is authorized to prepare assets and create and start microVMs.
- Keep both Ceph write execution flags false until an authenticated gateway derives and overwrites `X-Proxbox-Actor`, prevents direct route access, and has passed the Ceph approval guide's rollout gates.
- Apply external authorization to direct mutation routes that do not implement a plan and single-use approval. For workflows that do implement approvals, require a fresh plan and approval and never treat a retry as authorization.
- Rotate credentials after suspected exposure and review logs, traces, metrics, and generated artifacts for secret leakage.
- Preserve audit and task records for incident response, and synchronize system clocks.

## Relationship to upstream policies

NetBox vulnerabilities must be reported under the [NetBox security policy](https://github.com/netbox-community/netbox/security/policy). Proxmox, FastAPI, Python, SDK, or dependency vulnerabilities that do not arise from proxbox-api integration should be reported to their respective maintainers. If the affected boundary is unclear, report privately to this project first; maintainers will coordinate with the appropriate upstream project.
