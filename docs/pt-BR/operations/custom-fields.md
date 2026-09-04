# Desativacao de Custom Fields

O estado de reflexao do Proxbox e armazenado nos modelos tipados
`Proxbox*SyncState` fornecidos pelo netbox-proxbox. O `proxbox-api` nao cria,
reconcilia, le ou grava mais custom fields do NetBox.

Esta e uma alteracao incompativel da API. As antigas rotas
`POST /extras/custom-fields/reconcile` e
`GET /extras/extras/custom-fields/create` foram removidas junto com as
configuracoes `custom_fields_enabled` e `custom_fields_request_delay`. Os
clientes devem usar as rotas normais de sincronizacao e as APIs de sidecar
tipadas.

O backend nao exclui definicoes ou valores historicos existentes. Depois de
confirmar que todos os componentes Proxbox implantados usam sidecars tipados,
os operadores podem remover esses campos pelo processo normal de mudanca do
NetBox.

`GET /extras/bootstrap-status` continua disponivel e informa os resultados do
bootstrap dos objetos nativos de suporte ainda gerenciados pela sincronizacao.
