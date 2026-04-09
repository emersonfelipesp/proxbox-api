# API de Cache

A API de cache fornece endpoints para monitorar e gerenciar os caches em memória usados pelo proxbox-api.

## Endpoints de Cache

| Endpoint | Descrição |
|----------|-----------|
| `GET /cache` | Inspecionar todos os caches (Proxbox e NetBox GET cache) com métricas e chaves de exemplo |
| `GET /cache/metrics` | Obter métricas do cache NetBox GET como JSON |
| `GET /cache/metrics/prometheus` | Obter métricas do cache NetBox GET em formato Prometheus |
| `GET /clear-cache` | Limpar ambos os caches Proxbox e NetBox GET |

## Esquemas de Resposta

### GET /cache

Retorna uma visualização combinada de todos os caches:

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

Retorna métricas do cache NetBox GET:

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

Retorna métricas em formato Prometheus:

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

## Referência de Métricas

| Métrica | Tipo | Descrição |
|---------|------|------------|
| `hits` | counter | Total de acertos no cache |
| `misses` | counter | Total de erros no cache |
| `hit_rate` | gauge | Porcentagem de acertos no cache |
| `invalidations` | counter | Número de invalidações de cache |
| `evictions_ttl` | counter | Entradas evicadas por expiração de TTL |
| `evictions_size` | counter | Entradas evicadas por limite de quantidade |
| `evictions_bytes` | counter | Bytes evitados por limite de tamanho |
| `current_entries` | gauge | Número atual de entradas em cache |
| `current_bytes` | gauge | Tamanho atual do cache em bytes |
| `max_entries` | gauge | Máximo de entradas permitidas |
| `max_bytes` | gauge | Máximo de bytes permitidos |
| `ttl_seconds` | gauge | Configuração atual de TTL |
| `oldest_entry_age_seconds` | gauge | Idade da entrada mais antiga |

## Variáveis de Ambiente

| Variável | Padrão | Descrição |
|----------|--------|------------|
| `PROXBOX_NETBOX_GET_CACHE_TTL` | 60.0 | TTL do cache em segundos (0 para desabilitar) |
| `PROXBOX_NETBOX_GET_CACHE_MAX_ENTRIES` | 4096 | Máximo de entradas em cache |
| `PROXBOX_NETBOX_GET_CACHE_MAX_BYTES` | 52428800 | Máximo de tamanho do cache em bytes (50MB) |
| `PROXBOX_DEBUG_CACHE` | 0 | Habilitar log de debug (1 para habilitar) |

## Exemplo: Prometheus Scraping

Adicione isso à sua configuração do Prometheus:

```yaml
scrape_configs:
  - job_name: 'proxbox-api'
    static_configs:
      - targets: ['proxbox-api:8000']
    metrics_path: '/cache/metrics/prometheus'
```
