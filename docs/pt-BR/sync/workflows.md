# Fluxos de Sincronizacao

## Full update

Endpoint:

- `GET /full-update`

Fluxo:

1. Cria sync-process no NetBox.
2. Sincroniza nodes Proxmox em devices NetBox.
3. Sincroniza storages Proxmox em registros de storage do plugin NetBox.
4. Sincroniza VMs Proxmox em VMs NetBox.
5. Sincroniza discos virtuais das VMs descobertas.
6. Sincroniza backups de VMs.
7. Sincroniza snapshots de VMs.
8. Atualiza runtime e status final.

## Sync de VMs

Endpoint principal:

- `GET /virtualization/virtual-machines/create`

## Sync de backups

- `GET /virtualization/virtual-machines/backups/create`
- `GET /virtualization/virtual-machines/backups/all/create`
- `GET /virtualization/virtual-machines/backups/all/create/stream`

## Sync de snapshots

- `GET /virtualization/virtual-machines/snapshots/create`
- `GET /virtualization/virtual-machines/snapshots/all/create`
- `GET /virtualization/virtual-machines/snapshots/all/create/stream`

## Sync de storage

- `GET /virtualization/virtual-machines/storage/create`
- `GET /virtualization/virtual-machines/storage/create/stream`
