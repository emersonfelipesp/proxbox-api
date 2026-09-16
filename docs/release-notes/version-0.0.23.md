# proxbox-api 0.0.23

## Summary

proxbox-api 0.0.23 moves the backend to proxmox-sdk 0.0.15, which is certified against Proxmox VE 9.2.20, and adopts the SDK's public bounded-read contract. It also makes Open vSwitch interface synchronization safe for NetBox, makes application lifespan runtime ownership concurrency-safe, binds Ceph approval-status recovery to the approval participants, and hardens the release and promotion lane with an ancestor-blob promotion guard and source-bound public release creation.

## Delivered requirements

- Pin `proxmox-sdk==0.0.15` in every dependency slot (`proxmox-sdk`, `proxmox-sdk[pbs]`, `proxmox-sdk[pdm]`).
- Replace the pre-0.0.15 bounded-read compatibility path with a thin boundary over the SDK's public `get_bounded()`; encode boolean query values as `0`/`1` and drop `None` values before the call, because the SDK forwards query values verbatim and the HTTP client rejects Python booleans. Without this the metrics pull route would answer 503 on the new SDK.
- Map the SDK's response-too-large and unsupported-encoding failures to the backend's typed errors; propagate every other SDK failure unchanged.
- Explain a refused HTTP redirect during Proxmox session setup with the refused status, the redirect target host, and the instruction to configure the final Proxmox API address. proxmox-sdk 0.0.15 refuses every HTTP 3xx before reading a body so credentials never follow a redirect.
- Map Proxmox Open vSwitch interface kinds to safe NetBox interface types while preserving cable and connected-state blockers through authoritative conflict recovery; reconcile Linux and Open vSwitch topology authoritatively, including physical OVSPort membership and stale relationship removal.
- Give every application lifespan a generation-bound database runtime owner so overlapping applications cannot dispose one another's engines, leases, or authentication identity, with explicit acquisition, shared bootstrap publication, final disposal, cancellation handling, failure poisoning, and cross-event-loop behavior.
- Require a trusted `X-Proxbox-Actor` for Ceph v2 approval-status recovery and disclose approval metadata only to the persisted requester or approver; unrelated actors and unknown approvals receive the same 404 response.
- Add a base-owned, read-only promotion-history guard that rejects changed paths restoring superseded blobs from exact `main` history, scoped to pull requests targeting `main`.
- Bind public GitHub Release creation to an existing final or post-release tag and the exact production-approved commit, loading release notes from the approved remote commit.
- Bound release validation to two loadgroup-aware test workers, retain Python 3.13 branch coverage and duration telemetry, and persist fail-closed coverage artifacts.
- Synchronize the dependency-security remediation into the integration branch so later promotions cannot restore vulnerable Next.js, frontend transitive, or Python documentation dependencies.
- Pre-warm and bound the asynchronous SQLite connection pool used by the valid-authentication burst regression, with deterministic exception groups for cleanup failures.

## Security and operational impact

The Proxmox SDK boundary no longer follows redirects on any transport, so an endpoint placed behind a redirecting proxy fails closed with an actionable message instead of forwarding credentials. Bounded metric reads keep their byte limit, identity encoding, and redirect refusal inside the SDK. Ceph approval-status recovery discloses nothing to actors outside the approval. The promotion guard and source-bound release helper keep mutation credentials outside candidate-schedulable workflows and prevent a promotion from silently restoring superseded files.

Production deployments use the immutable package from the Gitea Package Registry. The previous healthy package image remains the rollback point until deployment health, installed version, and source identity have been validated.

## Compatibility and migration

The release is additive for API consumers. Existing synchronization routes, configuration, and the metrics query contracts remain compatible. Operators whose Proxmox endpoint address answers with an HTTP redirect must configure the final Proxmox API address; the SDK no longer follows redirects. Ceph v2 approval-status recovery now requires the authenticated gateway to supply `X-Proxbox-Actor`. No database migration is required in proxbox-api. The certified pairing is `proxmox-sdk 0.0.15` and `netbox-sdk 0.0.13`.

## Validation

Every included change passed its focused test suites, the complete backend suite, Ruff lint and formatting, the configured type-check scope, strict documentation builds, lock verification, per-function cyclomatic complexity analysis with Radon 6.0.1, and the capped adversarial review, followed by exact-head hosted CI and staging deployment. A loopback test drives the real proxmox-sdk HTTPS transport to prove the boolean query encoding. The promotion diff received a final adversarial review before release. Release candidate and final package evidence is recorded by the release workflows.

## Known limitations

Backend-local generated Proxmox models are not regenerated in this release; the SDK's regenerated models are consumed through the SDK boundary only. proxmox-sdk still forwards boolean query values verbatim on its own bounded reads, so the encoding lives in the backend boundary until the SDK ships it.

## Maintenance and retirement

Retain the source commit, package digests, workflow attestations, deployment proof, production health evidence, and this release record. Regenerating the backend-local Proxmox models against the SDK output remains a tracked follow-up.
