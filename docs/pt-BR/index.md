# Documentacao do proxbox-api

`proxbox-api` e um backend FastAPI que conecta fluxos de infraestrutura do Proxmox com modelos e objetos de plugin do NetBox.

Esta documentacao cobre instalacao, configuracao, arquitetura, referencias de API, fluxos de sincronizacao e contribuicao.

## O que este servico faz

- Armazena dados locais de bootstrap em SQLite para conexoes NetBox e Proxmox.
- Expoe APIs REST para gerenciamento de endpoints, verificacao de status e rotas geradas dinamicamente do contrato Proxmox.
- Expoe endpoints de descoberta e orquestracao de sync do Proxmox, alem de sync individual por objeto.
- Fornece endpoints WebSocket e SSE para feedback de sincronizacao em tempo real.
- Inclui extensao OpenAPI gerada para os contratos do API viewer do Proxmox.

## Principais capacidades

- Bootstrap do endpoint NetBox com suporte a token v1 e v2.
- CRUD de endpoints Proxmox com senha ou par de token.
- Coleta de dados de cluster, node, storage, VM, backup, snapshot e replication.
- Sincronizacao de VM, interfaces, IPs, discos, storages e backups para o NetBox.
- Inspecao de logs do admin, cache e fluxo full-update.

## Idioma

- Idioma padrao: Ingles.
- Traducao opcional: Portugues Brasileiro (`pt-BR`) pelo seletor de idioma.
