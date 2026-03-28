# Documentacao do proxbox-api

`proxbox-api` e um backend FastAPI que conecta fluxos de infraestrutura do Proxmox com modelos e objetos de plugin do NetBox.

Esta documentacao cobre instalacao, configuracao, arquitetura, referencias de API, fluxos de sincronizacao e contribuicao.

## O que este servico faz

- Armazena dados locais de bootstrap para conexoes NetBox e Proxmox em SQLite.
- Expoe APIs REST para gerenciamento de endpoints NetBox e Proxmox.
- Expoe endpoints de dados Proxmox e orquestracao de sincronizacao.
- Fornece endpoints WebSocket para feedback de sincronizacoes longas.
- Inclui extensao OpenAPI gerada para contratos do API viewer do Proxmox.

## Idioma

- Idioma padrao: Ingles.
- Traducao opcional: Portugues Brasileiro (`pt-BR`) pelo seletor de idioma.
