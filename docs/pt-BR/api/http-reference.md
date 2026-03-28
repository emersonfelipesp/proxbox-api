# Referencia HTTP da API

Esta pagina resume os principais endpoints HTTP do `proxbox-api`.

## Utilitarios

- `GET /`
- `GET /cache`
- `GET /clear-cache`
- `GET /sync-processes`
- `POST /sync-processes`

## Rotas NetBox

- `POST /netbox/endpoint`
- `GET /netbox/endpoint`
- `GET /netbox/endpoint/{netbox_id}`
- `PUT /netbox/endpoint/{netbox_id}`
- `DELETE /netbox/endpoint/{netbox_id}`
- `GET /netbox/status`
- `GET /netbox/openapi`

Regra singleton:

- Apenas um endpoint NetBox pode ser criado.

## Rotas Proxmox

- CRUD de endpoints: `/proxmox/endpoints*`
- Sessao e descoberta: `/proxmox/sessions`, `/proxmox/version`, `/proxmox/`
- Cluster e nodes: `/proxmox/cluster/*`, `/proxmox/nodes/*`
- Viewer codegen: `/proxmox/viewer/*`

## Rotas de sincronizacao

- `/dcim/*`
- `/virtualization/*`
- `/extras/*`
