# proxbox-api 0.0.22

## Summary

proxbox-api 0.0.22 adds authenticated, bounded transports for querying Proxmox metrics from InfluxDB and directly from the Proxmox cluster metrics API. These contracts support deterministic source reconciliation in netbox-proxbox while keeping provider credentials, network access, validation, normalization, and error handling inside the backend.

## Delivered requirements

- Add `POST /proxmox/metrics/influx/query` for structured InfluxDB v2 queries.
- Add `POST /proxmox/metrics/pull/query` for direct collection from one configured Proxmox endpoint through `cluster/metrics/export`.
- Normalize both transports into stable metric rows with canonical identity fields.
- Bound request scope, DNS resolution, total execution time, streamed bytes, normalized bytes, and returned rows.
- Reject redirects, ambient proxies, unexpected content encodings, provider error envelopes, ambiguous endpoint selection, and unsupported parameters.
- Preserve compatibility with the certified `proxmox-sdk==0.0.13` dependency while preferring its public bounded method when a newer compatible SDK provides it.
- Verify the repository-linked Gitea release manifest after upload even when Gitea reports an already-applied package link as HTTP 400.
- Isolate GitHub CLI configuration in the runner's private temporary directory so public tag promotion does not depend on the host root configuration.
- Isolate Git global and XDG configuration in runner-private paths so GitHub credential-helper setup does not read or write host root files.
- Materialize the verified fetched tag as the exact local tag ref before GitHub promotion, so the push preserves the annotated tag object instead of addressing a missing ref.

## Security and operational impact

Both routes require the existing proxbox-api API key. InfluxDB tokens and Proxmox credentials remain server-side and are excluded from stable failure responses. The InfluxDB transport pins the validated DNS result, disables redirects and environment proxies, requests identity encoding, and enforces one overall deadline. The direct transport uses a fixed provider operation and bounds the response before materialization.

Production deployments use the immutable package from the Gitea Package Registry. The previous healthy package image remains the rollback point until deployment health, installed version, source identity, and both route contracts have been validated.

## Compatibility and migration

The release is additive. Existing synchronization routes and configuration remain compatible. Consumers can adopt either transport independently; netbox-proxbox can select InfluxDB-only, pull-only, or reconciled behavior. No database migration is required in proxbox-api.

## Validation

The feature passed focused metrics tests, the complete backend suite with coverage above the repository threshold, Ruff lint and formatting, compile checks, strict documentation builds, lock verification, per-function cyclomatic complexity analysis, three capped adversarial review rounds, staging health and OpenAPI probes, and authentication-boundary checks. The release-publishing recovery path also has positive and fail-closed regression coverage for Gitea's link response. Production promotion validates the host-issued receipt's complete signed schema, pinned public-key identity, and Ed25519 signature before accepting or publishing deployment evidence. Release candidate and final package evidence are recorded by the release workflows.

## Known limitations

Direct pull supports only the bounded parameters implemented by the Proxmox cluster metrics export API. Historical aggregation remains an InfluxDB capability. The compatibility adapter can be removed after the certified Proxmox SDK baseline includes the public bounded transport.

## Maintenance and retirement

Retain the source commit, package digests, workflow attestations, deployment proof, production health evidence, and this release record. A future release may retire the compatibility adapter after all supported installations use a Proxmox SDK with the bounded public method.
