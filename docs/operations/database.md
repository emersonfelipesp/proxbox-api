# Database Operations

`proxbox-api` stores bootstrap and runtime state in one local SQLite file. The
database target is resolved once during FastAPI lifespan startup, before any
request can be served.

## Configuration contract

| Input | Accepted value |
|-------|----------------|
| `PROXBOX_DATABASE_PATH` | Absolute filesystem path, for example `/var/lib/proxbox-api/database.db` |
| `DATABASE_URL` | Absolute local URL using `sqlite`, `sqlite+pysqlite`, or `sqlite+aiosqlite`, for example `sqlite:////var/lib/proxbox-api/database.db` |
| `PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY` | Emergency value `1` only; authorizes one audited fresh-control-plane startup when a different legacy database still exists |
| Neither, outside a container | `$XDG_DATA_HOME/proxbox/database.db`, or `~/.local/share/proxbox/database.db` when XDG is unset |
| Neither, in a published container | `/data/database.db` (packaged fallback, not an explicit operator variable) |
| Both | Accepted only when their normalized paths identify the same file |

Relative paths, in-memory databases, URL authorities, URL credentials,
non-SQLite URLs, and conflicting variables are fatal configuration errors.
Every raw `?` delimiter in `DATABASE_URL` is rejected, including an empty or
keyless query, so URL parsing can never silently truncate the filename. The
service never creates a fallback database in its current working directory.

## Startup safety check

Before constructing SQLAlchemy engines or creating application tables, startup
acquires the persistent sibling advisory lock `<database>.startup.lock`. Every
process using the same target then runs this complete boundary serially:

1. Creates only the configured parent directory when it does not exist.
2. Rejects a non-directory parent, a non-file target, or mode-level read-only
   file/directory.
3. Opens the selected SQLite file and requires `journal_mode=WAL`.
4. Creates, writes, and reads a uniquely named internal probe table inside
   `BEGIN IMMEDIATE`, then rolls the transaction back.
5. Confirms that the probe table left no schema residue.
6. Constructs the engines, creates all declared tables, and runs every
   idempotent migration before releasing the advisory lock.

The probe commits no application row or probe table. Enabling WAL can create
or update SQLite's normal database, `-wal`, and `-shm` files. The sibling lock
file intentionally remains in place; do not delete or replace it while any
worker is running. If any step fails, startup exits with an actionable error
and the API does not accept traffic.

Schema inspection is part of the fatal migration boundary. An inspection error
is never interpreted as "migration not needed," and the required post-schema
`NetBoxEndpoint` read must succeed before the service reports ready.

## Containers

Published images supply an internal fallback:

```text
PROXBOX_DEFAULT_DATABASE_PATH=/data/database.db
```

Mount persistent storage at `/data` and ensure the container runtime user can
create the database plus its `-wal`, `-shm`, and `.startup.lock` sidecars:

```bash
docker run -d --name proxbox-api \
  -p 8000:8000 \
  -v proxbox-data:/data \
  emersonfelipesp/proxbox-api:latest
```

A read-only volume or an existing `/data/database.db` without write access is
an expected hard startup failure. Fix the volume ownership or mount mode; do
not point the service at a temporary alternate file.

The packaged fallback is consulted only when neither `PROXBOX_DATABASE_PATH`
nor `DATABASE_URL` is set. A custom absolute `DATABASE_URL` therefore replaces
the container default without a conflicting second operator setting.

## systemd

Use a dedicated state directory and make the database path explicit. A unit
can use systemd's `StateDirectory` ownership management:

```ini
[Service]
User=proxbox-api
Group=proxbox-api
StateDirectory=proxbox-api
StateDirectoryMode=0750
Environment=PROXBOX_DATABASE_PATH=/var/lib/proxbox-api/database.db
ExecStart=/opt/proxbox-api/.venv/bin/uvicorn proxbox_api.main:app --host 127.0.0.1 --port 8000
```

After changing the unit, run `systemctl daemon-reload` and restart the service.
The service account needs write and directory-search access to
`/var/lib/proxbox-api`; read-only hardening may protect the rest of the
filesystem as long as this state directory remains writable.

`DATABASE_URL=sqlite:////var/lib/proxbox-api/database.db` is supported for a
legacy unit. During migration, either remove it or set
`PROXBOX_DATABASE_PATH` to the identical file. Divergent values intentionally
stop startup.

## Moving an existing database

Treat the SQLite file and its WAL state as one consistency boundary:

1. Stop every `proxbox-api` process that uses the database.
2. Take a recoverable backup of the current database. With the service stopped,
   preserve the database together with any existing `-wal` and `-shm` files;
   an online copy must instead use SQLite's backup API.
3. Create the destination directory and grant it to the service account.
4. Copy the consistent database set to the new destination and retain the
   original backup until validation is complete.
5. Configure exactly one target, or configure both variables to the same target.
6. Start the configured workers. Their target-specific advisory lock serializes
   each probe, table creation, and migration boundary; confirm the
   startup-verification success logs, `/health`, and expected endpoint records.

Never run old and new service instances against different copies during the
migration. That creates two valid but divergent control-plane databases.

Older builds selected `/data/database.db` whenever `/data` was writable and
could fall back to `./database.db` otherwise. Startup now checks both legacy
locations for **every** selected target, including explicit path/URL settings.
If another legacy database exists, a missing, empty, or key-history-free target
is refused because it could reopen unauthenticated API-key bootstrap. A copied
target with a durable API key or the canonical bootstrap-claim row `id=1`
retains the closed boundary and is accepted. Noncanonical claims or incompatible
claim schemas are fatal instead of being accepted as proof. Before upgrading, stop every process, back
up the old database and WAL/SHM sidecars, then configure that absolute path or
move the consistent set. Retain the backup until startup and record validation
succeed.

If an operator intentionally needs a separate fresh control plane while the
legacy file remains, stop every existing worker and isolate the service from
untrusted traffic. Set both `UVICORN_WORKERS=1` and
`PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY=1`, then start exactly one recovery
worker. Multi-worker or unspecified worker-count recovery is rejected before
any database write. Preserve the warning log, which renders the exact selected
and legacy paths. Claim bootstrap and register the first API key, stop that
worker, remove the override, restore the normal worker count, and restart. The
first accepted startup atomically creates the persistent
sibling `<database>.fresh-database-override-used` marker before touching the
database. That marker prevents stale configuration from authorizing a second
fresh database even if the selected target is later deleted or truncated.
Never remove it to re-arm bootstrap; use a separately reviewed new target.
Only the exact value `1` is accepted; the setting is rejected when there is no
legacy conflict or the target already preserves key history. The override does
not migrate or delete legacy data.

## Troubleshooting startup

| Log/error meaning | Operator action |
|-------------------|-----------------|
| Absolute path required | Replace a relative path or URL with an absolute target. |
| Variables select different files | Remove one variable or make the normalized paths identical. |
| Existing legacy implicit database | Configure/migrate that file, or use the documented isolated one-start override for a deliberately fresh control plane; never delete it merely to make startup pass. |
| Override requires `UVICORN_WORKERS=1` | Stop all workers and run the documented isolated single-worker recovery; never launch the override with the normal multi-worker topology. |
| Override already consumed / stale | Remove `PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY`; keep the durable consumption marker. Do not delete marker or target to re-open bootstrap. |
| Startup lock cannot be acquired | Grant the service account access to the target directory and persistent sibling `.startup.lock`; do not remove a live lock file. |
| Migration inspection / required endpoint read failed | Treat the database as unhealthy; restore or repair its schema/filesystem before restarting. |
| Parent is not a directory / target is not a file | Correct the exact configured filesystem object. |
| Directory or file is read-only / not searchable | Correct service-account ownership, mode, ACL, or container mount mode. |
| Not writable with WAL | Check free space, filesystem health and WAL support, and write access for the database plus sidecars. |
| Schema initialization failed | Restore/inspect database integrity and review the preceding startup exception; do not create an empty fallback database. |

Do not repeatedly launch the service while changing targets. Resolve the exact
configured path from the unit/container environment, correct it, and then
perform one controlled restart.
