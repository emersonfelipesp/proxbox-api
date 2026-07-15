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

Ela ignora e atualiza o cache local de custom fields, le o NetBox ao vivo,
cria campos ausentes e aplica patch nos atributos gerenciados com drift. A
operacao e idempotente: chamadas repetidas nao devem gerar churn no NetBox
quando os campos ja batem.

A rota legada `GET /extras/extras/custom-fields/create` continua disponivel
para clientes antigos, mas automacoes novas devem usar a rota POST.

## Depois do reconcile

1. Rode novamente a sincronizacao que falhou.
2. Se a rota POST falhar com `netbox_overwhelmed`, aguarde e tente novamente.
3. Se ainda houver warnings, confirme que o token do NetBox pode ler e escrever
   em `/api/extras/custom-fields/`.

`object_types` adicionados por operadores em custom fields do Proxbox sao
preservados. O reconcile faz a uniao entre os object types declarados e o
valor ao vivo do NetBox antes do patch, entao escopos manuais nao sao
removidos.

`GET /clear-cache` tambem invalida o cache de custom fields, mas nao faz
reconcile no NetBox. Use a rota POST quando campos estiverem ausentes ou com
drift.
