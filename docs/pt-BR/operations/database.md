# Operacoes do banco de dados

`proxbox-api` armazena estado de bootstrap e runtime em um unico arquivo
SQLite. O destino e resolvido uma vez no inicio do lifespan FastAPI, antes de
qualquer request ser atendido.

## Contrato de configuracao

| Entrada | Valor aceito |
|---------|---------------|
| `PROXBOX_DATABASE_PATH` | Caminho absoluto, por exemplo `/var/lib/proxbox-api/database.db` |
| `DATABASE_URL` | URL local absoluta com `sqlite`, `sqlite+pysqlite` ou `sqlite+aiosqlite`, por exemplo `sqlite:////var/lib/proxbox-api/database.db` |
| `PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY` | Valor emergencial `1`; autoriza um startup auditado de control plane novo quando existe outro banco legado |
| Nenhuma, fora de container | `$XDG_DATA_HOME/proxbox/database.db` ou `~/.local/share/proxbox/database.db` sem XDG |
| Nenhuma, em imagem publicada | `/data/database.db` (fallback da imagem, nao uma variavel explicita do operador) |
| Ambas | Aceitas somente quando os caminhos normalizados identificam o mesmo arquivo |

Caminhos relativos, bancos em memoria, authority, credenciais, URLs nao SQLite
e variaveis conflitantes sao erros fatais. Todo delimitador `?` literal em
`DATABASE_URL` e recusado, inclusive query vazia ou sem chave, para impedir que
o parser trunque silenciosamente o nome do arquivo. O servico nunca cria um
banco alternativo no diretorio corrente.

## Verificacao segura no startup

Antes de construir engines SQLAlchemy ou criar tabelas, o startup adquire o
lock irmao persistente `<database>.startup.lock`. Todos os processos do mesmo
destino executam em serie esta fronteira completa:

1. Cria somente o diretorio pai configurado quando ele nao existe.
2. Recusa pai que nao seja diretorio, destino que nao seja arquivo ou arquivo/
   diretorio somente-leitura pelos bits de modo.
3. Abre o SQLite selecionado e exige `journal_mode=WAL`.
4. Cria, escreve e le uma tabela interna com nome unico dentro de
   `BEGIN IMMEDIATE`, depois reverte a transacao.
5. Confirma que a tabela de probe nao deixou residuo no schema.
6. Constroi os engines, cria todas as tabelas declaradas e executa todas as
   migrations idempotentes antes de liberar o lock.

O probe nao persiste linha da aplicacao nem tabela interna. Habilitar WAL pode
criar ou atualizar banco, `-wal` e `-shm`. O arquivo de lock permanece de
proposito; nao o exclua nem substitua enquanto houver worker ativo. Se qualquer
etapa falhar, o startup termina com erro acionavel e a API nao aceita trafego.

A inspecao de schema faz parte da fronteira fatal de migration. Erro de
inspecao nunca significa "migration desnecessaria", e a leitura obrigatoria de
`NetBoxEndpoint` apos o schema deve passar antes do servico ficar ready.

## Containers

As imagens publicadas fornecem um fallback interno:

```text
PROXBOX_DEFAULT_DATABASE_PATH=/data/database.db
```

Monte storage persistente em `/data` e garanta que o usuario de runtime possa
criar o banco e os sidecars `-wal`, `-shm` e `.startup.lock`:

```bash
docker run -d --name proxbox-api \
  -p 8000:8000 \
  -v proxbox-data:/data \
  emersonfelipesp/proxbox-api:latest
```

Um volume read-only ou um `/data/database.db` existente sem escrita deve
falhar no startup. Corrija ownership ou modo do mount; nao aponte o servico para
um arquivo temporario alternativo.

O fallback da imagem so e consultado quando `PROXBOX_DATABASE_PATH` e
`DATABASE_URL` nao estao definidos. Portanto, uma `DATABASE_URL` absoluta
customizada substitui o padrao do container sem uma segunda configuracao
conflitante.

## systemd

Use um diretorio de estado dedicado e deixe o caminho explicito. A unit pode
usar o gerenciamento de ownership do `StateDirectory`:

```ini
[Service]
User=proxbox-api
Group=proxbox-api
StateDirectory=proxbox-api
StateDirectoryMode=0750
Environment=PROXBOX_DATABASE_PATH=/var/lib/proxbox-api/database.db
ExecStart=/opt/proxbox-api/.venv/bin/uvicorn proxbox_api.main:app --host 127.0.0.1 --port 8000
```

Apos alterar a unit, execute `systemctl daemon-reload` e reinicie o servico. A
conta do servico precisa de escrita e busca no diretorio
`/var/lib/proxbox-api`; hardening read-only pode proteger o resto do filesystem
desde que esse diretorio continue gravavel.

`DATABASE_URL=sqlite:////var/lib/proxbox-api/database.db` continua compativel
com units legadas. Durante a migracao, remova a variavel ou configure
`PROXBOX_DATABASE_PATH` para o mesmo arquivo. Valores divergentes interrompem o
startup de proposito.

## Movendo um banco existente

Trate o banco e o estado WAL como uma unica fronteira de consistencia:

1. Pare todos os processos `proxbox-api` que usam o banco.
2. Faca backup recuperavel do banco atual. Com o servico parado, preserve o
   banco junto com arquivos `-wal` e `-shm` existentes; copia online deve usar a
   API de backup do SQLite.
3. Crie o diretorio de destino e conceda ownership a conta do servico.
4. Copie o conjunto consistente ao destino e retenha o backup original ate a
   validacao terminar.
5. Configure um unico destino, ou ambas as variaveis para o mesmo destino.
6. Inicie os workers configurados. O lock especifico do destino serializa probe,
   criacao das tabelas e migrations; confirme os logs de verificacao, `/health`
   e os endpoints esperados.

Nunca execute instancias antigas e novas contra copias diferentes durante a
migracao. Isso cria dois bancos de control plane validos, mas divergentes.

Versoes antigas selecionavam `/data/database.db` quando `/data` era gravavel e
podiam usar `./database.db`. O startup agora verifica ambos os locais legados
para **todo** destino selecionado, inclusive path/URL explicito. Se outro banco
legado existe, um destino ausente, vazio ou sem historico de chave e recusado
porque poderia reabrir o bootstrap sem autenticacao. Uma copia com chave de API
ou o claim canonico de bootstrap `id=1` mantem o bootstrap fechado e e aceita.
Claims nao canonicos ou schema incompativel sao fatais, nunca prova valida. Antes
do upgrade, pare os processos, preserve o banco e sidecars WAL/SHM, configure
seu caminho absoluto ou mova o conjunto consistente e mantenha o backup ate a
validacao.

Se o operador precisar intencionalmente de um control plane novo enquanto o
arquivo legado permanece, pare todos os workers e isole o servico de trafego
nao confiavel. Configure `UVICORN_WORKERS=1` junto com
`PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY=1` e inicie exatamente um worker de
recovery. Recovery multi-worker ou sem contagem explicita e recusado antes de
qualquer write. Preserve o warning, que renderiza os caminhos selecionado e
legados. Consuma o bootstrap, registre a primeira chave, pare o worker, remova o
override, restaure a contagem normal e reinicie. O primeiro startup aceito cria
atomicamente o marcador irmao persistente
`<database>.fresh-database-override-used` antes de tocar no banco. Ele impede
que configuracao esquecida autorize outro banco vazio mesmo se o destino for
excluido ou truncado. Nunca apague o marcador para rearmar o bootstrap; use um
novo destino com revisao separada. Somente o valor exato `1` e aceito e a opcao
e recusada sem conflito legado ou quando o destino ja preserva historico de
chave. O override nao migra nem exclui dados antigos.

## Troubleshooting de startup

| Significado do erro | Acao do operador |
|---------------------|------------------|
| Caminho absoluto obrigatorio | Troque caminho ou URL relativo por destino absoluto. |
| Variaveis selecionam arquivos diferentes | Remova uma variavel ou iguale os caminhos normalizados. |
| Banco implicito legado existente | Configure/migre o arquivo ou use o override isolado de um startup para um control plane deliberadamente novo; nao o exclua apenas para liberar o startup. |
| Override exige `UVICORN_WORKERS=1` | Pare todos os workers e use o recovery isolado single-worker documentado; nunca use o override na topologia multi-worker normal. |
| Override consumido / obsoleto | Remova `PROXBOX_ALLOW_FRESH_DATABASE_WITH_LEGACY` e preserve o marcador; nao apague marcador ou destino para reabrir o bootstrap. |
| Lock de startup nao pode ser adquirido | Conceda acesso ao diretorio e ao `.startup.lock` persistente; nao remova um lock em uso. |
| Falha na inspecao de migration / leitura obrigatoria | Trate o banco como unhealthy; restaure ou repare schema/filesystem antes do restart. |
| Pai nao e diretorio / destino nao e arquivo | Corrija exatamente o objeto configurado. |
| Diretorio ou arquivo read-only / sem busca | Corrija ownership, modo, ACL ou mount para a conta do servico. |
| Sem escrita com WAL | Verifique espaco, saude e suporte WAL do filesystem e escrita no banco/sidecars. |
| Falha na inicializacao do schema | Restaure/inspecione a integridade e leia a excecao anterior; nao crie banco fallback vazio. |

Nao reinicie repetidamente enquanto muda destinos. Resolva o caminho exato no
ambiente da unit/container, corrija-o e faca um unico restart controlado.
