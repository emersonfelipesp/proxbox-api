# Fluxos de Sincronizacao

## Full update

Endpoint:

- `GET /full-update`

Fluxo:

1. Cria sync-process no NetBox.
2. Sincroniza nodes Proxmox em devices NetBox.
3. Sincroniza VMs Proxmox em VMs NetBox.
4. Atualiza runtime e status final.

## Sync de VMs

Endpoint principal:

- `GET /virtualization/virtual-machines/create`

## Sync de backups

- `GET /virtualization/virtual-machines/backups/create`
- `GET /virtualization/virtual-machines/backups/all/create`
