# proxbox-api 0.0.27

## Summary

This release adds a write-gated route for configuring Proxmox InfluxDB metric
servers and keeps application-owned telemetry providers working when the
OpenTelemetry SDK is disabled.

## Fixes and improvements

- **Configure Proxmox InfluxDB metric servers.** `PUT
  /proxmox/metrics/influx/servers/{config_id}` updates one metric server on a
  Proxmox cluster. It requires an explicit `endpoint_id` whose
  `allow_writes` setting is enabled and the `X-Proxbox-Actor` header. The
  identifier is validated so it cannot alter the Proxmox request path, and the
  body is allow-listed and rejects unknown fields. `server`, `port` and `token`
  are required on every update: Proxmox requires the first two, and the token is
  required because Proxmox keeps the stored credential when it is omitted, so a
  changed destination, bucket, organization or certificate setting could
  otherwise redirect it. Organization, bucket and path-prefix values reject
  query and path delimiters. Every attempt and outcome is audited with the actor
  and field names only; a failed update is reported as possibly partially
  applied. Tokens and submitted values are never logged or returned.
- **Telemetry providers survive a disabled SDK.** When `OTEL_SDK_DISABLED` is
  set, application telemetry controls are copied before automatic
  configuration, tracing, metrics and logs are disabled, so providers that the
  caller owns are preserved. This covers the main service, the Firecracker agent
  and the standalone mock.

## Compatibility and migration

This release does not add a database migration and does not change the public
authentication contract. The new route is additive.

## Upgrade

Deploy the exact `proxbox-api 0.0.27` package or published container through
the approved release workflow and restart every backend worker. Verify the
health endpoint, then run one node synchronization and one virtual-machine
synchronization before resuming scheduled full-estate jobs.
