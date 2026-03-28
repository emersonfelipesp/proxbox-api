# Visao Geral da Arquitetura

`proxbox-api` e organizado em camadas de rotas FastAPI, dependencias de sessao, servicos de sync e esquemas.

## Camadas principais

- API: `proxbox_api/main.py`, `proxbox_api/routes/*`
- Sessao: `proxbox_api/session/*`
- Servicos: `proxbox_api/services/sync/*`
- Persistencia: `proxbox_api/database.py`

## Componentes de runtime

- App FastAPI com roteadores em `/netbox`, `/proxmox`, `/dcim`, `/virtualization` e `/extras`.
- Configuracao de endpoint em SQLite.
- Sessao NetBox por `netbox-sdk`.
- Sessao Proxmox por `proxmoxer`.
