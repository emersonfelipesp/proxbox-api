# Autenticação

O `proxbox-api` usa autenticação por chave API armazenada em banco de dados. Todas as chaves API são armazenadas no banco de dados SQLite com hash bcrypt. Não há autenticação por variável de ambiente — todo gerenciamento de chaves acontece através dos endpoints da API.

## Fluxo de Bootstrap

Quando o backend inicia com um banco de dados nunca inicializado, ele retorna `needs_bootstrap: true` do endpoint de status:

```bash
curl http://localhost:8800/auth/bootstrap-status
# {"needs_bootstrap": true, "has_db_keys": false}
```

### Registro da Primeira Chave

A primeira chave API pode ser registrada sem autenticação (modo bootstrap):

```bash
curl -X POST http://localhost:8800/auth/register-key \
  -H "Content-Type: application/json" \
  -d '{"api_key": "sua-chave-api-segura-com-pelo-menos-32-caracteres", "label": "chave-bootstrap"}'
# {"detail": "API key registered."}
```

**O bootstrap é consumido exatamente uma vez por banco de dados.** O registro
grava uma reivindicação singleton durável de bootstrap junto com o hash bcrypt
da primeira chave em uma única transação, então duas tentativas concorrentes de
bootstrap não podem ambas ter sucesso — a perdedora recebe um `409 Conflict`
estável. Uma vez consumido o bootstrap, toda chamada posterior a
`/auth/register-key` retorna `409 Conflict`, **inclusive quando todas as chaves
já foram desativadas ou removidas**: o histórico de chaves inativas e a
reivindicação permanente fecham para sempre a janela de bootstrap sem
autenticação. Bancos inicializados antes da reivindicação existir são
preenchidos na inicialização — qualquer histórico de chaves também fecha o
bootstrap permanentemente nesses bancos.

### Perdendo Todas as Chaves

Como o bootstrap nunca reabre, a API recusa aposentar a última chave ativa:
`DELETE /auth/keys/{id}` e `POST /auth/keys/{id}/deactivate` retornam `409`
com o código `last_active_api_key_required` quando o alvo é a única chave
ativa. Crie e verifique uma chave substituta primeiro, depois aposente a
antiga. Se um banco de dados de alguma forma ficar sem nenhuma chave ativa, a
recuperação é uma operação em nível de banco de dados feita pelo operador
(restaurar um backup ou editar a tabela SQLite `apikey` diretamente) — não um
caminho HTTP sem autenticação.

### Integração com Plugin NetBox

Quando você salva um `FastAPIEndpoint` no NetBox, o plugin automaticamente:

1. Gera um token seguro de 64 caracteres
2. Chama `/auth/bootstrap-status` para verificar se registro é necessário
3. Chama `/auth/register-key` para registrar o token com o backend
4. Armazena o token no NetBox para requisições autenticadas futuras

## Usando Chaves API

Todas as requisições (exceto endpoints de bootstrap) requerem o header `X-Proxbox-API-Key`:

```bash
curl http://localhost:8800/proxmox/endpoints \
  -H "X-Proxbox-API-Key: sua-chave-api-segura-com-pelo-menos-32-caracteres"
```

## Endpoints Sem Autenticação

Estes endpoints não requerem autenticação:

| Endpoint | Propósito |
|----------|-----------|
| `GET /` | Metadados raiz |
| `GET /docs` | Documentação OpenAPI |
| `GET /redoc` | Documentação ReDoc |
| `GET /openapi.json` | Schema OpenAPI |
| `GET /health` | Verificação de saúde |
| `GET /meta` | Metadados do serviço |
| `GET /auth/bootstrap-status` | Verifica se bootstrap é necessário |
| `POST /auth/register-key` | Registra primeira chave (apenas enquanto o bootstrap nunca foi consumido) |

## Endpoints de Gerenciamento de Chaves

Todos os endpoints de gerenciamento de chaves requerem autenticação:

### Listar Chaves API

```bash
curl http://localhost:8800/auth/keys \
  -H "X-Proxbox-API-Key: sua-chave"
# {"keys": [{"id": 1, "label": "chave-bootstrap", "is_active": true, "created_at": 1712345678.123}]}
```

### Criar uma Nova Chave

```bash
curl -X POST http://localhost:8800/auth/keys \
  -H "X-Proxbox-API-Key: sua-chave"
# {"id": 2, "label": "", "is_active": true, "created_at": 1712345678.456, "raw_key": "a-chave-gerada-automaticamente"}
```

A `raw_key` é retornada apenas uma vez — armazene-a com segurança.

### Desativar uma Chave

```bash
curl -X POST http://localhost:8800/auth/keys/1/deactivate \
  -H "X-Proxbox-API-Key: sua-chave"
# {"id": 1, "label": "chave-bootstrap", "is_active": false, "created_at": 1712345678.123}
```

Desativar a última chave ativa é recusado com `409`
(`last_active_api_key_required`) — crie e verifique outra chave primeiro.

### Reativar uma Chave

```bash
curl -X POST http://localhost:8800/auth/keys/1/activate \
  -H "X-Proxbox-API-Key: sua-chave"
# {"id": 1, "label": "chave-bootstrap", "is_active": true, "created_at": 1712345678.123}
```

### Deletar uma Chave

```bash
curl -X DELETE http://localhost:8800/auth/keys/1 \
  -H "X-Proxbox-API-Key: sua-chave"
# (204 No Content)
```

Remover a última chave ativa é recusado com `409`
(`last_active_api_key_required`) — crie e verifique outra chave primeiro.

## Proteção Contra Brute-Force

O backend persiste o bloqueio no SQLite em um bucket composto e sem segredos:
contexto normalizado de origem/confiança da rede mais um identificador HMAC com
chave do servidor. Atingir o limite de um bucket de credencial não bloqueia
outra chave usada pelo mesmo worker, proxy reverso ou IP. Um limite separado e
deliberadamente maior de abuso por origem ainda bloqueia todas as chaves daquela
origem quando esgotado. A autenticação síncrona e assíncrona compartilha o mesmo
serviço de estado. Antes do bcrypt, cada
requisição insere uma linha de reserva durável independente com token
imprevisível e expiração própria. A linha consome somente a capacidade separada
de verificação por credencial, origem e global. Depois do bcrypt, um `DELETE` atômico
limitado pelo token finaliza exatamente aquela linha uma vez: chave recusada
vira estado de falha de credencial/origem na mesma transação, enquanto chave
aceita não registra falha. Finalização duplicada não consome a reserva de outra
requisição. Assim, tráfego válido concorrente não cria bloqueio. Capacidade
esgotada retorna HTTP 503 com `Retry-After: 1` ou fecha o WebSocket com código
1013, sem consumir tentativa.

Um token abandonado por crash expira após pelo menos 60 segundos (ou a janela
maior configurada). Depois de expirado, ele deixa de consumir capacidade. A
linha permanece disponível por uma hora para finalização tardia exatamente uma
vez e entra na métrica de reservas órfãs. Linhas mais antigas são compactadas em
contador agregado durável para limitar o armazenamento; finalizador além desse
horizonte documentado é ignorado. Uma reserva órfã não estende a expiração de
outro token vivo nem libera trabalho mais novo. Um segundo
orçamento durável por origem limita ataques que rotacionam
credenciais; seu padrão é deliberadamente maior que o limite por credencial. As
linhas duráveis de falha são divididas em partições independentes e limitadas de
credencial e origem. Janelas de falha expiradas são removidas, mas saturação
nunca impede o bcrypt de uma chave válida e ainda desconhecida, porque reservas
não dependem de linha de falha. Uma chave recusada continua negada quando uma
partição não admite a nova identidade; a identidade não persistida entra nos
contadores agregados de falha e capacidade de linhas, sem remover outro
orçamento pré-bloqueio vivo.

- Limite padrão: 5 falhas (`PROXBOX_AUTH_LOCKOUT_THRESHOLD`, intervalo 1-100)
- Orçamento padrão por origem: 50 falhas (`PROXBOX_AUTH_LOCKOUT_SOURCE_THRESHOLD`, intervalo 1-100000)
- Janela fixa padrão: 5 minutos (`PROXBOX_AUTH_LOCKOUT_WINDOW_SECONDS`, intervalo 1-86400)
- Máximo de linhas duráveis: 10000 (`PROXBOX_AUTH_LOCKOUT_MAX_BUCKETS`, intervalo 2-1000000)
- Máximo de verificações concorrentes por bucket de credencial/origem: 32
  (`PROXBOX_AUTH_LOCKOUT_MAX_IN_FLIGHT`, intervalo 1-1024)
- Máximo de verificações concorrentes entre todos os workers e identidades: 256
  (`PROXBOX_AUTH_LOCKOUT_MAX_GLOBAL_IN_FLIGHT`, intervalo 1-4096)
- Por padrão, uma chave opaca é criada atomicamente no arquivo privado irmão
  `database.db.auth-lockout.key`. `PROXBOX_AUTH_LOCKOUT_HMAC_KEY` pode fornecer
  um valor explícito com 32 bytes ou mais. Mantenha a fonte estável entre
  reinícios e separada da chave rotacionável de criptografia.
- O startup grava fingerprint não secreto e geração no SQLite. Depois do bind,
  arquivo ausente/substituído ou chave de ambiente diferente é erro fatal e
  nunca causa regeneração silenciosa. Todo worker valida o mesmo bind sob o lock
  de startup do destino e fixa o material verificado na memória do processo.
  Excluir ou substituir a fonte depois do startup não altera os IDs nesse worker;
  recovery ou rotação exige o procedimento offline seguido de restart
  controlado.
- `PROXBOX_TRUSTED_PROXIES` define explicitamente quais CIDRs de peer podem
  fornecer `X-Forwarded-For`. Nenhum endereço, inclusive localhost, é confiável
  implicitamente. Proxies confiáveis não ignoram autenticação nem bloqueio.

Os endpoints de métricas expõem somente contadores e gauges agregados, sem
labels, com prefixo `proxbox_auth_*`. Além dos totais de falha, bloqueio e
recovery e dos bloqueios ativos, o serviço publica:

- `proxbox_auth_capacity_rejections_total`: admissões de verificação recusadas
  pelo limite por bucket ou global em voo mais identidades com falha cuja partição
  limitada de linhas de credencial ou origem não conseguiu persistir;
- `proxbox_auth_orphan_compactions_total`: reservas expiradas compactadas depois
  do horizonte suportado de uma hora para finalização tardia;
- `proxbox_auth_bucket_rows`: linhas duráveis atuais de falha por
  credencial/origem;
- `proxbox_auth_verifications_in_flight`: reservas não expiradas que consomem
  capacidade bcrypt; e
- `proxbox_auth_expired_orphan_reservations`: reservas expiradas de crash
  mantidas dentro do horizonte suportado para finalização tardia.

Logs e CLI usam identificadores HMAC não autenticantes de 12 caracteres;
chaves e hashes testáveis por dicionário nunca são exibidos.

### Recuperação local de bloqueio

A administração é local e não passa pelo middleware HTTP, portanto continua
disponível durante um bloqueio:

```bash
proxbox-auth-lockout --database /data/database.db list
proxbox-auth-lockout --database /data/database.db clear --id 4a12bc34de56
# Reset emergencial somente do estado transitório de bloqueio:
proxbox-auth-lockout --database /data/database.db clear --all
```

O caminho é obrigatório e precisa apontar para um banco existente com o schema
atual completo, incluindo tabelas de reservas, métricas e bind da chave; a CLI
valida o schema, mas nunca cria nem migra o banco. `list` abre o SQLite em modo
somente leitura. A listagem mostra contexto de origem/confiança, tipo do bucket,
identificadores curtos, tentativas e expiração, mas nunca material da chave API.

### Recovery ou rotação offline da chave de identidade

Prefira restaurar o arquivo vinculado a partir de backup. Se isso for impossível,
uma nova geração invalida todos os IDs opacos existentes. Faça o reset explícito
somente com todos os workers parados:

1. Pare todos os workers e preserve backup recuperável do banco e da chave.
2. Crie a chave substituta como arquivo regular, sem symlink, UTF-8, com pelo
   menos 32 bytes e modo `0600`.
3. Execute:

   ```bash
   proxbox-auth-lockout --database /data/database.db rebind-key \
     --key-file /data/database.db.auth-lockout.key.new \
     --confirm-reset-lockouts
   ```

4. Instale/configure exatamente essa fonte em todos os workers, inicie o serviço
   e valide readiness e autenticação.

O comando adquire o lock de startup e lease exclusivo de runtime. Ele recusa
execução enquanto houver worker ativo, valida o schema existente de recovery,
limpa atomicamente buckets incompatíveis e todas as reservas pendentes, avança
a geração não secreta e nunca imprime a chave. Se a própria linha de bind foi
perdida, o recovery cria a geração 1 depois de limpar todo estado opaco. Não
substitua a chave com rollout gradual de workers.

## Melhores Práticas de Segurança

1. **Use chaves fortes**: Pelo menos 32 caracteres, preferencialmente 64 caracteres
2. **Armazene chaves com segurança**: Trate a `raw_key` de `/auth/keys` como uma senha — armazene uma vez
3. **Rotacione chaves regularmente**: Crie uma nova chave, atualize suas aplicações, delete a antiga
4. **Use HTTPS em produção**: Chaves são enviadas em headers — proteja-as em trânsito
5. **Limite o escopo das chaves**: Crie chaves separadas para propósitos diferentes (monitoramento, sincronização, admin)

## Resolução de Problemas

### "No API key configured"

```
{"detail": "No API key configured. Register a key via POST /auth/register-key or use an existing key."}
```

O banco de dados não tem chaves API. Em um banco nunca inicializado, chame
`/auth/register-key` com uma nova chave para fazer o bootstrap. Em um banco que
já fez bootstrap uma vez, `/auth/register-key` permanece fechado (`409`);
recupere em nível de banco de dados (restaure um backup ou repare a tabela
`apikey`).

### "Invalid API key"

Verifique se:

1. Você está enviando o header `X-Proxbox-API-Key`
2. O valor da chave corresponde exatamente (sem espaços extras ou newlines)
3. A chave não foi desativada ou deletada

### "Too many failed authentication attempts"

Aguarde a janela fixa configurada expirar ou use os comandos locais com banco
explícito `proxbox-auth-lockout --database <caminho> list` e `clear --id`. O estado
e os contadores agregados ficam no SQLite; reiniciar o
backend não é um mecanismo de recuperação.
