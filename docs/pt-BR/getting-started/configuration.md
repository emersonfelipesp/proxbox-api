# Configuracao

O `proxbox-api` usa SQLite para configuracao local e dependencias de execucao.

## Endpoint NetBox (singleton)

- `POST /netbox/endpoint`
- `GET /netbox/endpoint`
- `PUT /netbox/endpoint/{netbox_id}`
- `DELETE /netbox/endpoint/{netbox_id}`

Apenas um endpoint NetBox e permitido.

## Endpoints Proxmox (multiplos)

- `POST /proxmox/endpoints`
- `GET /proxmox/endpoints`
- `GET /proxmox/endpoints/{endpoint_id}`
- `PUT /proxmox/endpoints/{endpoint_id}`
- `DELETE /proxmox/endpoints/{endpoint_id}`

Regras de autenticacao:

- Forneca `password`, ou `token_name` + `token_value`.
- `token_name` e `token_value` devem ser enviados juntos.
