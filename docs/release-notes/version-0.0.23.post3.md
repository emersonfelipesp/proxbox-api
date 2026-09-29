# proxbox-api 0.0.23.post3

## Summary

This post-release improves synchronization reliability and throughput for large
multi-cluster Proxmox estates. It isolates cluster, datacenter, firewall, node,
and virtual-machine synchronization endpoints so one failing scope does not
abort unrelated work, and it adds bounded concurrency and timeout controls for
operators who need to tune large deployments.

Node synchronization now accepts the plugin's validated Device name template
and consistently derives names from node, cluster, cluster slug, and endpoint
identity. The default `{node}` template preserves existing names.

## Synchronization reliability

- Isolate endpoint and record failures while retaining explicit failed-resource
  reporting and retryable status information.
- Bound concurrent Proxmox fetches, NetBox writes, virtual-machine processing,
  and device reconciliation with configurable limits.
- Preserve endpoint-specific timeout and retry behavior without leaking one
  endpoint's policy into another concurrent synchronization.
- Reduce repeated NetBox and Proxmox discovery work across large batches while
  retaining authoritative per-record validation.

## Node Device naming

- Accept a validated node Device name template from netbox-proxbox.
- Support `{node}`, `{cluster}`, `{cluster_slug}`, and `{endpoint}` placeholders.
- Reject malformed, empty, or unsafe rendered names before writing to NetBox.
- Preserve the existing node name when the default `{node}` template is used.

## Configuration and operations

New synchronization concurrency, timeout, and batching controls are documented
in the large multi-cluster deployment guide. Defaults preserve existing small-
estate behavior. Operators should change one group of limits at a time and
observe backend latency, NetBox database load, and synchronization failure
counts before increasing concurrency further.

## Compatibility and migration

This release does not add a database migration and does not change the public
authentication contract. It remains compatible with `proxmox-sdk 0.0.15`,
`netbox-sdk 0.0.13`, and the netbox-proxbox `0.0.27` release line. Deploy this
backend before netbox-proxbox `0.0.27.post1` so the plugin can safely transmit
the new naming and tuning settings.

## Upgrade

Deploy the exact `proxbox-api 0.0.23.post3` package or published container
through the approved release workflow and restart every backend worker. Verify
the health endpoint, then run one node synchronization and one virtual-machine
synchronization before resuming scheduled full-estate jobs.
