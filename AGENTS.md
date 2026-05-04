# proxbox-api Agent Index

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

- [.claude/worktrees/agent-a384764c648af0c29/.github/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/.github/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/automation/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/automation/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/docker/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/docker/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/docs/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/docs/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/nextjs-ui/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/nextjs-ui/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/app/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/app/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/custom_objects/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/custom_objects/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/diode/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/diode/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/e2e/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/e2e/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/enum/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/enum/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/enum/netbox/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/enum/netbox/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/enum/netbox/dcim/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/enum/netbox/dcim/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/enum/netbox/virtualization/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/enum/netbox/virtualization/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/generated/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/generated/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/generated/netbox/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/generated/netbox/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/generated/proxmox/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/generated/proxmox/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/proxmox_codegen/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/proxmox_codegen/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/proxmox_to_netbox/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/proxmox_to_netbox/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/proxmox_to_netbox/mappers/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/proxmox_to_netbox/mappers/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/proxmox_to_netbox/schemas/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/proxmox_to_netbox/schemas/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/admin/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/admin/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/dcim/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/dcim/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/extras/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/extras/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/netbox/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/netbox/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/proxbox/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/proxbox/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/proxbox/clusters/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/proxbox/clusters/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/proxmox/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/proxmox/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/sync/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/sync/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/virtualization/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/virtualization/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/virtualization/virtual_machines/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/routes/virtualization/virtual_machines/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/schemas/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/schemas/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/schemas/netbox/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/schemas/netbox/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/schemas/netbox/dcim/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/schemas/netbox/dcim/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/schemas/netbox/extras/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/schemas/netbox/extras/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/schemas/netbox/virtualization/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/schemas/netbox/virtualization/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/schemas/virtualization/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/schemas/virtualization/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/services/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/services/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/services/sync/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/services/sync/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/services/sync/individual/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/services/sync/individual/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/session/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/session/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/types/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/types/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxbox_api/utils/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxbox_api/utils/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/proxmox-mock/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/proxmox-mock/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/scripts/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/scripts/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/tasks/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/tasks/CLAUDE.md)
- [.claude/worktrees/agent-a384764c648af0c29/tests/CLAUDE.md](.claude/worktrees/agent-a384764c648af0c29/tests/CLAUDE.md)
- [.github/CLAUDE.md](.github/CLAUDE.md)
- [CLAUDE.md](CLAUDE.md)
- [automation/CLAUDE.md](automation/CLAUDE.md)
- [docker/CLAUDE.md](docker/CLAUDE.md)
- [docs/CLAUDE.md](docs/CLAUDE.md)
- [netbox-sdk/.github/CLAUDE.md](netbox-sdk/.github/CLAUDE.md)
- [netbox-sdk/CLAUDE.md](netbox-sdk/CLAUDE.md)
- [netbox-sdk/docs/CLAUDE.md](netbox-sdk/docs/CLAUDE.md)
- [netbox-sdk/netbox_cli/CLAUDE.md](netbox-sdk/netbox_cli/CLAUDE.md)
- [netbox-sdk/netbox_cli/reference/CLAUDE.md](netbox-sdk/netbox_cli/reference/CLAUDE.md)
- [netbox-sdk/netbox_sdk/CLAUDE.md](netbox-sdk/netbox_sdk/CLAUDE.md)
- [netbox-sdk/netbox_sdk/reference/CLAUDE.md](netbox-sdk/netbox_sdk/reference/CLAUDE.md)
- [netbox-sdk/netbox_tui/CLAUDE.md](netbox-sdk/netbox_tui/CLAUDE.md)
- [netbox-sdk/netbox_tui/themes/CLAUDE.md](netbox-sdk/netbox_tui/themes/CLAUDE.md)
- [netbox-sdk/reference/CLAUDE.md](netbox-sdk/reference/CLAUDE.md)
- [netbox-sdk/reference/textual/CLAUDE.md](netbox-sdk/reference/textual/CLAUDE.md)
- [netbox-sdk/tests/CLAUDE.md](netbox-sdk/tests/CLAUDE.md)
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
- [proxmox-sdk/.github/CLAUDE.md](proxmox-sdk/.github/CLAUDE.md)
- [proxmox-sdk/CLAUDE.md](proxmox-sdk/CLAUDE.md)
- [proxmox-sdk/docker/CLAUDE.md](proxmox-sdk/docker/CLAUDE.md)
- [scripts/CLAUDE.md](scripts/CLAUDE.md)
- [tasks/CLAUDE.md](tasks/CLAUDE.md)
- [tests/CLAUDE.md](tests/CLAUDE.md)
