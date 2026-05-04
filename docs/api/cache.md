# Cache API

The cache API provides endpoints for monitoring and managing the in-memory caches used by proxbox-api.

## Cache Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /cache` | Inspect all caches (Proxbox and NetBox GET cache) with metrics and sample keys |
| `GET /cache/metrics` | Get NetBox GET cache metrics as JSON |
| `GET /cache/metrics/prometheus` | Get NetBox GET cache metrics in Prometheus exposition format |
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
  ]
}
```

### GET /cache/metrics

Returns NetBox GET cache metrics:

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
  "oldest_entry_age_seconds": 45.2
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
