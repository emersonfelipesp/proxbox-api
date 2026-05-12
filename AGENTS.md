# proxbox-api Agent Index

## Workspace Context

This file lives at `/root/personal-context/nmulticloud-context/proxbox-api/AGENTS.md` inside the `personal-context` workspace.
Workspace guidance: `/root/personal-context/CLAUDE.md`.
Per-repo deep-dive: `/root/personal-context/claude-reference/proxbox-api.md`.
Submodule layout and cross-repo links: `/root/personal-context/claude-reference/dependency-map.md`.

---

Use the root `CLAUDE.md` first, then open the nearest scoped guide for the code you are changing.

## Required Checks

Run these before pushing anything that touches the backend package:

```bash
rtk ruff check .
rtk ruff format --check .
uv run python -m compileall proxbox_api tests
uv run python -c "import proxbox_api.main"
uv run python -c "from proxbox_api.proxmox_to_netbox.proxmox_schema import load_proxmox_generated_openapi; assert load_proxmox_generated_openapi().get('paths')"
rtk pytest tests
```

If you edit `proxmox-sdk/`, also run:

```bash
cd proxmox-sdk
uv run ruff check .
uv run ruff format --check .
uv run python -m compileall proxmox_sdk tests
uv run python -c "import proxmox_sdk.mock_main"
uv run pytest tests
```

If you edit `nextjs-ui/`, also run:

```bash
cd nextjs-ui
npm run lint
npm run build
```

Fix failures locally before finishing the task.

## Configuration policy

**Prefer DB-backed plugin settings over `.env` variables.**
When adding a new runtime tunable, default to making it a `ProxboxPluginSettings` field
(NetBox-UI-editable, persisted in the NetBox database) and read it via
`proxbox_api.runtime_settings.get_int / get_float / get_bool / get_str`, which already
resolves **env var (override) → `ProxboxPluginSettings` → built-in default** with a
5-minute settings cache (`proxbox_api/settings_client.py::get_settings`).

Only fall back to a pure `.env` variable when the value is needed **before** the NetBox
connection exists or is **operator-only infrastructure** that has no business in the UI:
`PROXBOX_BIND_HOST`, `PROXBOX_RATE_LIMIT`, `PROXBOX_ENCRYPTION_KEY` /
`PROXBOX_ENCRYPTION_KEY_FILE`, `PROXBOX_STRICT_STARTUP`,
`PROXBOX_SKIP_NETBOX_BOOTSTRAP`, `PROXBOX_GENERATED_DIR`,
`PROXBOX_CORS_EXTRA_ORIGINS`. Anything that controls sync behavior, batching,
concurrency, caching, or feature toggles belongs in `ProxboxPluginSettings`.

Do **not** invent shadow config layers (parallel JSON/YAML files, ad-hoc dotenv
sections, module-level constants meant as overrides) to dodge the migration cost.
If the new field needs the model + migration + form + serializer + template wiring on
the `netbox-proxbox` side, do all five — the existing fields in
`netbox-proxbox/netbox_proxbox/models/plugin_settings.py` and migration
`0037_pluginsettings_runtime_tunables.py` show the pattern.

See `CLAUDE.md → Environment Variables → Adding a new tunable` for the full keep-list
and resolution-order details.

## Primary Guide

- `CLAUDE.md`

## Scoped Guides

### Top-level packages
- `proxbox_api/CLAUDE.md`
- `proxmox-sdk/CLAUDE.md`
- `nextjs-ui/CLAUDE.md`
- `nextjs-ui/AGENTS.md`

### Infrastructure
- `.github/CLAUDE.md`
- `docker/CLAUDE.md`
- `docs/CLAUDE.md`
- `tests/CLAUDE.md`
- `scripts/CLAUDE.md`
- `tasks/CLAUDE.md`
- `automation/CLAUDE.md`
- `proxmox-mock/CLAUDE.md`

### proxbox_api subpackages
- `proxbox_api/app/CLAUDE.md`
- `proxbox_api/routes/CLAUDE.md`
- `proxbox_api/routes/admin/CLAUDE.md`
- `proxbox_api/routes/dcim/CLAUDE.md`
- `proxbox_api/routes/extras/CLAUDE.md`
- `proxbox_api/routes/netbox/CLAUDE.md`
- `proxbox_api/routes/proxbox/CLAUDE.md`
- `proxbox_api/routes/proxbox/clusters/CLAUDE.md`
- `proxbox_api/routes/proxmox/CLAUDE.md`
- `proxbox_api/routes/sync/CLAUDE.md`
- `proxbox_api/routes/virtualization/CLAUDE.md`
- `proxbox_api/routes/virtualization/virtual_machines/CLAUDE.md`
- `proxbox_api/services/CLAUDE.md`
- `proxbox_api/services/sync/CLAUDE.md`
- `proxbox_api/services/sync/individual/CLAUDE.md`
- `proxbox_api/session/CLAUDE.md`
- `proxbox_api/schemas/CLAUDE.md`
- `proxbox_api/schemas/netbox/CLAUDE.md`
- `proxbox_api/schemas/netbox/dcim/CLAUDE.md`
- `proxbox_api/schemas/netbox/extras/CLAUDE.md`
- `proxbox_api/schemas/netbox/virtualization/CLAUDE.md`
- `proxbox_api/schemas/virtualization/CLAUDE.md`
- `proxbox_api/enum/CLAUDE.md`
- `proxbox_api/enum/netbox/CLAUDE.md`
- `proxbox_api/enum/netbox/dcim/CLAUDE.md`
- `proxbox_api/enum/netbox/virtualization/CLAUDE.md`
- `proxbox_api/proxmox_codegen/CLAUDE.md`
- `proxbox_api/proxmox_to_netbox/CLAUDE.md`
- `proxbox_api/proxmox_to_netbox/mappers/CLAUDE.md`
- `proxbox_api/proxmox_to_netbox/schemas/CLAUDE.md`
- `proxbox_api/generated/CLAUDE.md`
- `proxbox_api/generated/netbox/CLAUDE.md`
- `proxbox_api/generated/proxmox/CLAUDE.md`
- `proxbox_api/types/CLAUDE.md`
- `proxbox_api/utils/CLAUDE.md`
- `proxbox_api/custom_objects/CLAUDE.md`
- `proxbox_api/diode/CLAUDE.md`
- `proxbox_api/e2e/CLAUDE.md`

## CLAUDE.md Index

Read the nearest scoped guide for the code you are changing.

- [.github/CLAUDE.md](.github/CLAUDE.md)
- [CLAUDE.md](CLAUDE.md)
- [automation/CLAUDE.md](automation/CLAUDE.md)
- [docker/CLAUDE.md](docker/CLAUDE.md)
- [docs/CLAUDE.md](docs/CLAUDE.md)
- [nextjs-ui/CLAUDE.md](nextjs-ui/CLAUDE.md)
- [proxbox_api/CLAUDE.md](proxbox_api/CLAUDE.md)
- [proxbox_api/app/CLAUDE.md](proxbox_api/app/CLAUDE.md)
- [proxbox_api/custom_objects/CLAUDE.md](proxbox_api/custom_objects/CLAUDE.md)
- [proxbox_api/diode/CLAUDE.md](proxbox_api/diode/CLAUDE.md)
- [proxbox_api/e2e/CLAUDE.md](proxbox_api/e2e/CLAUDE.md)
- [proxbox_api/enum/CLAUDE.md](proxbox_api/enum/CLAUDE.md)
- [proxbox_api/enum/netbox/CLAUDE.md](proxbox_api/enum/netbox/CLAUDE.md)
- [proxbox_api/enum/netbox/dcim/CLAUDE.md](proxbox_api/enum/netbox/dcim/CLAUDE.md)
- [proxbox_api/enum/netbox/virtualization/CLAUDE.md](proxbox_api/enum/netbox/virtualization/CLAUDE.md)
- [proxbox_api/generated/CLAUDE.md](proxbox_api/generated/CLAUDE.md)
- [proxbox_api/generated/netbox/CLAUDE.md](proxbox_api/generated/netbox/CLAUDE.md)
- [proxbox_api/generated/proxmox/CLAUDE.md](proxbox_api/generated/proxmox/CLAUDE.md)
- [proxbox_api/proxmox_codegen/CLAUDE.md](proxbox_api/proxmox_codegen/CLAUDE.md)
- [proxbox_api/proxmox_to_netbox/CLAUDE.md](proxbox_api/proxmox_to_netbox/CLAUDE.md)
- [proxbox_api/proxmox_to_netbox/mappers/CLAUDE.md](proxbox_api/proxmox_to_netbox/mappers/CLAUDE.md)
- [proxbox_api/proxmox_to_netbox/schemas/CLAUDE.md](proxbox_api/proxmox_to_netbox/schemas/CLAUDE.md)
- [proxbox_api/routes/CLAUDE.md](proxbox_api/routes/CLAUDE.md)
- [proxbox_api/routes/admin/CLAUDE.md](proxbox_api/routes/admin/CLAUDE.md)
- [proxbox_api/routes/dcim/CLAUDE.md](proxbox_api/routes/dcim/CLAUDE.md)
- [proxbox_api/routes/extras/CLAUDE.md](proxbox_api/routes/extras/CLAUDE.md)
- [proxbox_api/routes/netbox/CLAUDE.md](proxbox_api/routes/netbox/CLAUDE.md)
- [proxbox_api/routes/proxbox/CLAUDE.md](proxbox_api/routes/proxbox/CLAUDE.md)
- [proxbox_api/routes/proxbox/clusters/CLAUDE.md](proxbox_api/routes/proxbox/clusters/CLAUDE.md)
- [proxbox_api/routes/proxmox/CLAUDE.md](proxbox_api/routes/proxmox/CLAUDE.md)
- [proxbox_api/routes/sync/CLAUDE.md](proxbox_api/routes/sync/CLAUDE.md)
- [proxbox_api/routes/virtualization/CLAUDE.md](proxbox_api/routes/virtualization/CLAUDE.md)
- [proxbox_api/routes/virtualization/virtual_machines/CLAUDE.md](proxbox_api/routes/virtualization/virtual_machines/CLAUDE.md)
- [proxbox_api/schemas/CLAUDE.md](proxbox_api/schemas/CLAUDE.md)
- [proxbox_api/schemas/netbox/CLAUDE.md](proxbox_api/schemas/netbox/CLAUDE.md)
- [proxbox_api/schemas/netbox/dcim/CLAUDE.md](proxbox_api/schemas/netbox/dcim/CLAUDE.md)
- [proxbox_api/schemas/netbox/extras/CLAUDE.md](proxbox_api/schemas/netbox/extras/CLAUDE.md)
- [proxbox_api/schemas/netbox/virtualization/CLAUDE.md](proxbox_api/schemas/netbox/virtualization/CLAUDE.md)
- [proxbox_api/schemas/virtualization/CLAUDE.md](proxbox_api/schemas/virtualization/CLAUDE.md)
- [proxbox_api/services/CLAUDE.md](proxbox_api/services/CLAUDE.md)
- [proxbox_api/services/sync/CLAUDE.md](proxbox_api/services/sync/CLAUDE.md)
- [proxbox_api/services/sync/individual/CLAUDE.md](proxbox_api/services/sync/individual/CLAUDE.md)
- [proxbox_api/session/CLAUDE.md](proxbox_api/session/CLAUDE.md)
- [proxbox_api/types/CLAUDE.md](proxbox_api/types/CLAUDE.md)
- [proxbox_api/utils/CLAUDE.md](proxbox_api/utils/CLAUDE.md)
- [proxmox-mock/CLAUDE.md](proxmox-mock/CLAUDE.md)
- [scripts/CLAUDE.md](scripts/CLAUDE.md)
- [tasks/CLAUDE.md](tasks/CLAUDE.md)
- [tests/CLAUDE.md](tests/CLAUDE.md)
