# Sincronizacao de Task History

A sincronizacao de task history copia linhas finais do arquivo de tarefas do
Proxmox VE para o modelo de task history do netbox-proxbox. O backend e dono da
coleta, resolucao de identidade, deduplicacao e reconciliacao; o chamador apenas
decide se a etapa de VM executa esse trabalho ou se uma etapa dedicada do
full-update sera a unica dona.

## Modelo de coleta e reconciliacao

O servico em lote usa coleta limitada e orientada por node:

1. Carrega as VMs relevantes do NetBox. Uma execucao selecionada deduplica e
   ordena os IDs, envia no maximo 100 IDs por requisicao e codifica o filtro
   multivalorado do NetBox como valores repetidos (`?id=1&id=2`), nunca como um
   texto separado por virgulas. Os resultados tambem sao deduplicados entre os
   lotes, evitando ler todo o ambiente e limites de tamanho da requisicao.
2. Carrega uma vez o sidecar de estado de sync das VMs, associa por
   `virtual_machine` e resolve a propriedade por
   `(proxmox_endpoint_raw_id, proxmox_cluster_name, proxmox_vm_id)`.
3. Seleciona somente os nodes dos endpoints/clusters que possuem essas VMs.
4. Percorre uma vez o arquivo de cada node selecionado, com `source=archive`,
   `limit=500`, offsets crescentes em `start` e um `until` fixado no inicio da
   execucao.
5. Associa as linhas em memoria, deduplica por UPID e reconcilia todos os
   payloads em uma operacao bulk no NetBox.

Todas as paginas compartilham um unico semaforo
`PROXBOX_PROXMOX_FETCH_CONCURRENCY`. Nao existe limite arbitrario de paginas: o
Proxmox rotaciona o arquivo, entao o sync percorre tudo ate uma pagina curta ou
vazia. Guardas de pagina repetida e de nenhum UPID novo encerram com seguranca
quando um endpoint ignora o offset e marcam a execucao como degradada. Leituras
de listas do NetBox seguem os links `next` fornecidos pelo servidor; assim, um
limite de pagina do servidor nao trunca o snapshot. Links ou conteudo de pagina
repetidos falham de forma fechada em vez de devolver um conjunto parcial que
poderia provocar criacoes duplicadas.

As linhas de arquivo ja contem `status` e `endtime` finais. O servico espelha o
status do arquivo nos campos NetBox `status` e `exitstatus` e grava
`task_state="stopped"`, sem uma consulta de status por UPID. Uma falha bulk
tambem nao vira requisicoes individuais ao NetBox. Assim, o numero de
requisicoes cresce por nodes e paginas, nao por `VMs × nodes × tasks`.

## Seguranca de identidade

O sidecar tipado de estado de sync da VM e autoritativo para VMID, ID bruto do
endpoint, tipo de VM e nome do cluster Proxmox. Um sidecar malformado ou
duplicado pertencente a uma VM relevante para a execucao falha de forma fechada
e nunca e mascarado por custom fields; um pedido selecionado nao falha por um
sidecar corrompido que pertence apenas a outra VM. A identidade legada em custom
fields so e considerada quando nao existe linha no sidecar (ou a rota opcional
nao pode ser lida) e `custom_fields_enabled=true`.

Quando o scan completo do sidecar termina com sucesso, uma VM do NetBox sem
sidecar nem identidade legada utilizavel e considerada nao gerenciada e e
ignorada. Uma VM explicitamente selecionada sem identidade continua fatal. Uma
leitura indisponivel/transiente do sidecar com custom fields desabilitados
tambem e fatal porque a propriedade nao pode ser verificada. Uma tarefa do
endpoint 11 nunca pode cair por fallback em uma VM ligada ao endpoint 22. O
fallback legado `(cluster_name, vmid)` exige uma VM globalmente unica e uma
unica sessao/endpoint para o nome do cluster. Colisoes reais de propriedade
exata/legada e fontes duplicadas de cluster sao ignoradas e marcam a execucao
como degradada; VMIDs nao relacionados no arquivo sao skips normais e nao
contam como erro.

Um UPID visto nos nodes antigo e novo da mesma VM apos migracao e deduplicado.
O mesmo UPID resolvido para donos diferentes e ambiguo, e ignorado e marca a
execucao como degradada. Registros existentes ligados a VM ou tipo errados sao
corrigidos porque `virtual_machine` e `vm_type` podem ser atualizados.

## Propriedade da API e compatibilidade

As rotas de criacao de VM e suas variantes SSE, inclusive as rotas direcionadas
`/{netbox_vm_id}/create`, expoem `sync_task_history: bool = true`:

- Omitido ou `true`: preserva o comportamento standalone e executa um unico
  agregado para as VMs reconciliadas com sucesso. Uma falha fatal nesse agregado
  e propagada por REST/SSE; a rota de VM nao pode terminar silenciosamente. Se
  a coleta reconcilia linhas mas retorna `degraded=true`, o REST standalone
  responde HTTP 502 depois de preservar essas linhas. O SSE mantem o resumo de
  warning da etapa para tornar a cobertura parcial visivel.
- `false`: a etapa de VM nao executa task history porque outra etapa e a dona.

`/full-update` e `/full-update/stream` chamam explicitamente a etapa de VM com
`sync_task_history=false` e depois executam uma unica etapa dedicada para todas
as VMs. Um lote selecionado envia apenas IDs de VMs reconciliadas com sucesso,
evitando consultar endpoints nao relacionados. A consulta preliminar de VMs no
NetBox usa os mesmos lotes limitados com valores repetidos e falha de forma
fechada se qualquer lote nao puder ser lido. Uma reconciliacao direcionada a
uma VM tambem le a tabela de task history exatamente uma vez: o schema de task
history do NetBox usa UPID como chave de
consulta global, e um filtro por VM esconderia um UPID existente ligado a VM
errada, impedindo a correcao automatica. A coleta dos nodes continua limitada
ao escopo selecionado.

A rota dedicada
`/virtualization/virtual-machines/task-history/create/stream` aceita o mesmo
escopo `netbox_vm_ids` separado por virgulas. Omissao seleciona todo o ambiente;
um valor explicitamente vazio ou totalmente invalido seleciona zero VMs e nunca
pode ampliar a execucao para todo o ambiente. A rota aceita apenas
`fetch_max_concurrency >= 1` e limpa o memo de indisponibilidade do sidecar no
inicio de cada requisicao, permitindo testar novamente uma rota que antes
respondeu 404/501.

O campo de resposta `created` e mantido por compatibilidade, mas representa o
total de linhas de task history reconciliadas na execucao: criadas, atualizadas
ou ja inalteradas.

Implante primeiro o backend e depois altere o plugin orquestrador para enviar
`sync_task_history=false`. Plugins antigos continuam compativeis porque a
omissao vale `true`. Backends antigos ignoram o parametro desconhecido, portanto
implantar o plugin primeiro pode manter temporariamente o trabalho duplicado
anterior, embora os registros continuem idempotentes.

## Execucoes degradadas

Uma falha em pagina posterior ou em um node, uma pagina repetida, uma pagina
sem UPID novo, cobertura parcial de endpoint/cluster, ambiguidade de propriedade
ou UPID com donos diferentes preserva as linhas seguras ja coletadas,
retornando `degraded=true` e `errors`. Cada escopo solicitado e comparado com
as sessoes/status descobertos e precisa ter pelo menos um node; escopos
parcialmente ausentes continuam de forma degradada, enquanto a ausencia de todos
os escopos solicitados e fatal. O
resumo SSE da etapa publica essa contagem em `failed`, deixando a cobertura
incompleta visivel mesmo quando a etapa parcial termina e persiste as linhas. A
proxima execucao oferece consistencia eventual para a cauda ausente. Falha ao
listar VMs, ausencia de nodes utilizaveis, falha de todos os nodes selecionados
ou falha da reconciliacao bulk global levanta `ProxboxException`; REST/SSE
reporta etapa com falha e `complete.ok=false`, nao um zero enganoso.
Cancelamento e propagado e nenhuma reconciliacao no NetBox e iniciada.

Operadores devem tratar `degraded=true` como sinal para inspecao/retry e revisar
no log o node, offset e numero de linhas preservadas. Um arquivo normalmente
vazio nao e degradado.
