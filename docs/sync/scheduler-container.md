# Periodic syncs: the `proxbox-scheduler` container

Tracks [netbox-proxbox#372](https://github.com/emersonfelipesp/netbox-proxbox/issues/372).

`proxbox-api` itself does not run a scheduler — it is an HTTP service
that performs Proxmox→NetBox synchronization on request. To drive
recurring syncs in production, you have two options:

| Option                                                 | Where it runs                | Best for                                                                                               |
| ------------------------------------------------------ | ---------------------------- | ------------------------------------------------------------------------------------------------------ |
| NetBox-side **Schedule Sync** form                     | inside the NetBox container  | on-prem operators who control NetBox; supports `interval` (minute granularity, ≥1 minute floor)        |
| Standalone [`proxbox-scheduler`](https://github.com/emersonfelipesp/netbox-proxbox/tree/v0.0.15/proxbox_scheduler) container | a sidecar in your compose / k8s stack | cron expressions, sub-minute intervals, zero-gap "continuous" reconciliation, managed-NetBox tenants    |

For the on-prem `interval ≥ 60s` case, prefer the NetBox-side form —
it's already shipped, persisted in the NetBox database, and surfaced in
**Background Jobs**.

## How the container talks to proxbox-api

When invoked in `http` mode (the default), `proxbox-scheduler` calls:

```
GET {PROXBOX_API_URL}/full-update/stream
Headers: X-Proxbox-API-Key: ...
Accept: text/event-stream
```

It blocks on the SSE stream until proxbox-api emits a terminal event
(`complete`, `done`, `error`, …) and maps the outcome to success or
failure for the next loop iteration.

`proxbox-api` already enforces `X-Proxbox-API-Key` on `/full-update/*`
via the auth middleware in `proxbox_api/factory.py` and exposes the SSE
stream in `proxbox_api/app/full_update.py`. **No code changes are
required on the proxbox-api side** to support the scheduler — it lives
entirely on the netbox-proxbox plugin side.

## Compose example

The scheduler ships its own example compose snippet at
`netbox-proxbox/proxbox_scheduler/docker-compose.example.yml`. The
minimal addition to a stack that already runs `proxbox-api` looks like:

```yaml
services:
  proxbox-scheduler:
    image: proxbox-scheduler:0.0.15
    restart: unless-stopped
    environment:
      PROXBOX_MODE: "cron=0 */4 * * *"
      PROXBOX_SCHEDULER_TZ: America/Sao_Paulo
      PROXBOX_API_URL: http://proxbox-api:8000
      PROXBOX_API_KEY: ${PROXBOX_API_KEY}
    depends_on:
      - proxbox-api
```

## Modes

| `PROXBOX_MODE`            | Behaviour                                                                                                 |
| ------------------------- | --------------------------------------------------------------------------------------------------------- |
| `off`                     | disabled (container exits 0)                                                                              |
| `interval=<seconds>`      | trigger every `<seconds>` seconds, measured from the *start* of each trigger; sub-minute supported        |
| `continuous`              | back-to-back trigger with configurable error backoff                                                      |
| `cron=<5-field cron>`     | trigger at each cron fire time, evaluated in `PROXBOX_SCHEDULER_TZ`                                       |

## Coordination with NetBox-side scheduling

If you also configure a recurring interval via the NetBox-side
**Schedule Sync** form, the scheduler container can dedup against it —
but only in `exec` mode. The default exec command is:

```
python manage.py proxbox_sync --wait --enqueue-once
```

`--enqueue-once` routes through `ProxboxSyncJob.enqueue_once()`
(inherited from NetBox's `JobRunner`), which short-circuits when a
pending recurring `Job` already exists. `http` invocation cannot use
this dedup — pick one source of truth if you mix the two.

## Backoff on hard error

Any failed trigger (HTTP non-200, connection refused, subprocess exit
code ≠ 0, terminal `error` SSE event) causes the runner to sleep
`PROXBOX_SCHEDULER_BACKOFF_ON_ERROR_SECONDS` (default `30`) before the
next attempt. This prevents a wedged `proxbox-api` from being pounded
with retries in `continuous` mode.

## See also

- `proxbox-scheduler` README and full env-var reference: [`proxbox_scheduler/README.md`](https://github.com/emersonfelipesp/netbox-proxbox/blob/v0.0.15/proxbox_scheduler/README.md) in the netbox-proxbox repo.
- Issue #372 on netbox-proxbox: <https://github.com/emersonfelipesp/netbox-proxbox/issues/372>
