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

O backend implementa bloqueio por IP:

- Máximo de 5 tentativas falhas
- Duração do bloqueio: 5 minutos
- O bloqueio é limpo após autenticação bem-sucedida

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

Aguarde 5 minutos para o bloqueio expirar, ou reinicie o backend para resetar o estado de bloqueio em memória.
