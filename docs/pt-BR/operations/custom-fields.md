# Recuperacao de Custom Fields

`proxbox-api` possui um inventario declarativo de custom fields do NetBox
usado por sincronizacao de VMs, nos, hardware discovery, discos, interfaces e
IPs. O bootstrap de startup e as rotas extras usam o mesmo inventario.

## Quando usar

Execute um reconcile forcado depois de um upgrade se a sincronizacao falhar
com erro como `proxmox_last_updated` ausente, ou se um operador removeu ou
editou custom fields do Proxbox na UI do NetBox.

## Verificar status do bootstrap

Use a chave de API do backend:

```bash
curl -fsS \
  -H "X-Proxbox-API-Key: $PROXBOX_API_KEY" \
  http://localhost:8800/extras/bootstrap-status
```

Se `ok` for `false`, inspecione `warnings`. Falhas parciais do bootstrap de
startup tambem sao registradas em log no nivel error.

## Forcar reconcile ao vivo

O caminho suportado de recuperacao e a rota POST:

```bash
curl -fsS -X POST \
  -H "X-Proxbox-API-Key: $PROXBOX_API_KEY" \
  http://localhost:8800/extras/custom-fields/reconcile
```

Ela ignora e atualiza o cache local de custom fields, limpa as entradas de
custom fields do cache GET de baixo nivel do NetBox, le o NetBox ao vivo, cria
campos ausentes e aplica patch nos atributos gerenciados com drift. A operacao
e idempotente: chamadas repetidas nao devem gerar churn no NetBox quando os
campos ja batem.

A rota legada `GET /extras/extras/custom-fields/create` continua disponivel
para clientes antigos, mas automacoes novas devem usar a rota POST.

## Depois do reconcile

1. Rode novamente a sincronizacao que falhou.
2. Se a rota POST falhar com `netbox_overwhelmed`, aguarde e tente novamente.
3. Se ainda houver warnings, confirme que o token do NetBox pode ler e escrever
   em `/api/extras/custom-fields/`.

Durante o reconcile normal, `object_types` adicionados por operadores em custom
fields do Proxbox sao preservados. O reconcile faz uma consulta por campo e usa
esse mesmo registro ao vivo para unir os object types declarados com o valor
atual do NetBox antes do patch, entao escopos manuais nao sao removidos. Se a
consulta do campo falhar, esse campo e reportado como falho em vez de enviar um
payload `object_types` apenas declarado que poderia reduzir escopos adicionados
por operadores.

## Limitacao conhecida

O reconcile de custom fields le um campo, combina object types e depois grava.
A API REST do NetBox nao oferece compare-and-swap para essa operacao, entao se
um operador editar os object types de um campo na UI do NetBox exatamente no
momento em que o reconcile esta adicionando um object type ausente, a edicao
concorrente pode ser sobrescrita.

A janela e de milissegundos e so existe quando o reconcile esta realmente
adicionando um object type declarado ausente. Se o conjunto declarado ja estiver
presente, `object_types` nao e gravado. Evite editar object types de custom
fields na UI do NetBox enquanto uma sincronizacao ou reconcile estiver em
execucao.

`GET /clear-cache` tambem invalida o cache de custom fields, mas nao faz
reconcile no NetBox. Use a rota POST quando campos estiverem ausentes ou com
drift.
