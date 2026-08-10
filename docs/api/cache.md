# Cache API

The cache API provides endpoints for monitoring and managing the in-memory caches used by proxbox-api.

## Cache Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /cache` | Inspect caches and nested reconciliation/auth metrics with sample cache keys |
| `GET /cache/metrics` | Get cache, reconciliation, and aggregate authentication metrics as JSON |
| `GET /cache/metrics/prometheus` | Get the same metric families in Prometheus exposition format |
| `GET /clear-cache` | Clear both Proxbox and NetBox GET caches |

## Response Schemas

### GET /cache

Returns a combined view of all caches:

```json
{
  "proxbox_cache": { ... },
  "netbox_get_cache_metrics": {
    "hits": 150,
    "misses": 50,
    "hit_rate": 75.0,
    "invalidations": 10,
    "evictions_ttl": 5,
    "evictions_size": 3,
    "evictions_bytes": 2500,
    "current_entries": 50,
    "current_bytes": 5242880,
    "max_entries": 4096,
    "max_bytes": 52428800,
    "ttl_seconds": 60.0,
    "oldest_entry_age_seconds": 45.2
  },
  "netbox_get_cache_sample": [
    {"api_id": 123456, "path": "/api/dcim/devices/", "query": ""}
  ],
  "auth_lockout_metrics": {
    "proxbox_auth_failures_total": 8,
    "proxbox_auth_capacity_rejections_total": 1,
    "proxbox_auth_orphan_compactions_total": 2,
    "proxbox_auth_bucket_rows": 6,
    "proxbox_auth_verifications_in_flight": 2,
    "proxbox_auth_expired_orphan_reservations": 1
  }
}
```

### GET /cache/metrics

Returns flattened cache, reconciliation, and aggregate authentication metrics.
The authentication fields include durable counters plus current failure-row and
reservation gauges:

```json
{
  "hits": 150,
  "misses": 50,
  "hit_rate": 75.0,
  "invalidations": 10,
  "evictions_ttl": 5,
  "evictions_size": 3,
  "evictions_bytes": 2500,
  "current_entries": 50,
  "current_bytes": 5242880,
  "max_entries": 4096,
  "max_bytes": 52428800,
  "ttl_seconds": 60.0,
  "oldest_entry_age_seconds": 45.2,
  "proxbox_auth_failures_total": 8,
  "proxbox_auth_capacity_rejections_total": 1,
  "proxbox_auth_orphan_compactions_total": 2,
  "proxbox_auth_bucket_rows": 6,
  "proxbox_auth_verifications_in_flight": 2,
  "proxbox_auth_expired_orphan_reservations": 1
}
```

### GET /cache/metrics/prometheus

Returns cache metrics in Prometheus format:

```
# HELP proxbox_cache_hits Total number of cache hits
# TYPE proxbox_cache_hits counter
proxbox_cache_hits 150
# HELP proxbox_cache_misses Total number of cache misses
# TYPE proxbox_cache_misses counter
proxbox_cache_misses 50
# HELP proxbox_cache_hit_rate Cache hit rate percentage
# TYPE proxbox_cache_hit_rate gauge
proxbox_cache_hit_rate 75.0
...
# HELP proxbox_auth_verifications_in_flight Current unexpired bcrypt token leases
# TYPE proxbox_auth_verifications_in_flight gauge
proxbox_auth_verifications_in_flight 2
...
```

## Metrics Reference

| Metric | Type | Description |
|--------|------|-------------|
| `hits` | counter | Total cache hits |
| `misses` | counter | Total cache misses |
| `hit_rate` | gauge | Cache hit rate percentage |
| `invalidations` | counter | Number of cache invalidations |
| `evictions_ttl` | counter | Entries evicted due to TTL expiry |
| `evictions_size` | counter | Entries evicted due to entry count limit |
| `evictions_bytes` | counter | Bytes evicted due to byte limit |
| `current_entries` | gauge | Current number of cached entries |
| `current_bytes` | gauge | Current cache size in bytes |
| `max_entries` | gauge | Maximum allowed entries |
| `max_bytes` | gauge | Maximum allowed bytes |
| `ttl_seconds` | gauge | Current TTL setting |
| `oldest_entry_age_seconds` | gauge | Age of oldest entry |
| `proxbox_auth_failures_total` | counter | Rejected authentication attempts across all buckets, including members of a coalesced concurrent cohort |
| `proxbox_auth_lockouts_total` | counter | Credential buckets that entered lockout |
| `proxbox_auth_source_lockouts_total` | counter | Normalized sources that exhausted their aggregate failure budget |
| `proxbox_auth_recoveries_total` | counter | Buckets cleared through explicit local recovery operations |
| `proxbox_auth_capacity_rejections_total` | counter | Verification admissions rejected by a per-bucket/global in-flight limit plus failed identities whose bounded credential/source row partition could not persist |
| `proxbox_auth_orphan_compactions_total` | counter | Expired reservation tokens compacted after the supported one-hour cleanup horizon |
| `proxbox_auth_active_lockouts` | gauge | Credential buckets currently locked |
| `proxbox_auth_active_source_lockouts` | gauge | Source budgets currently locked |
| `proxbox_auth_bucket_rows` | gauge | Current durable credential/source failure rows across both bounded partitions |
| `proxbox_auth_verifications_in_flight` | gauge | Unexpired per-token reservations currently consuming bcrypt concurrency capacity |
| `proxbox_auth_expired_orphan_reservations` | gauge | Expired crash-token rows retained within the supported cleanup horizon; they do not consume capacity or permit accounting after the terminal deadline |

Authentication metrics are aggregate and intentionally have no source,
credential, bucket, or reservation-token labels. This prevents API-key
fingerprints and high-cardinality client identities from entering telemetry.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXBOX_NETBOX_GET_CACHE_TTL` | 60.0 | Cache TTL in seconds (0 to disable) |
| `PROXBOX_NETBOX_GET_CACHE_MAX_ENTRIES` | 4096 | Maximum cached entries |
| `PROXBOX_NETBOX_GET_CACHE_MAX_BYTES` | 52428800 | Maximum cache size in bytes (50MB) |
| `PROXBOX_DEBUG_CACHE` | 0 | Enable debug logging (1 to enable) |

## Example: Prometheus Scraping

Add this to your Prometheus configuration:

```yaml
scrape_configs:
  - job_name: 'proxbox-api'
    static_configs:
      - targets: ['proxbox-api:8000']
    metrics_path: '/cache/metrics/prometheus'
```
