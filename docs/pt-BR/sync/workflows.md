# Fluxos de Sincronizacao

## Full update

Endpoint:

- `GET /full-update`

Fluxo:

1. Sincroniza nodes Proxmox em devices NetBox.
2. Sincroniza storages Proxmox em registros de storage do plugin NetBox.
3. Sincroniza VMs Proxmox em VMs NetBox.
4. Sincroniza historico de tarefas.
5. Sincroniza discos virtuais das VMs descobertas.
6. Sincroniza backups de VMs.
7. Sincroniza snapshots de VMs.
8. Sincroniza interfaces e IPs dos nodes.
9. Sincroniza interfaces e IPs das VMs.

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
