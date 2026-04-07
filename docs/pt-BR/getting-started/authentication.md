# Autenticação

O `proxbox-api` usa autenticação por chave API armazenada em banco de dados. Todas as chaves API são armazenadas no banco de dados SQLite com hash bcrypt. Não há autenticação por variável de ambiente — todo gerenciamento de chaves acontece através dos endpoints da API.

## Fluxo de Bootstrap

Quando o backend inicia com um banco de dados vazio, ele retorna `needs_bootstrap: true` do endpoint de status:

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

Uma vez que uma chave existe, chamadas subsequentes para `/auth/register-key` retornam `409 Conflict`.

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
| `POST /auth/register-key` | Registra primeira chave (apenas quando nenhuma chave existe) |

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

O banco de dados não tem chaves API. Chame `/auth/register-key` com uma nova chave para fazer o bootstrap.

### "Invalid API key"

Verifique se:

1. Você está enviando o header `X-Proxbox-API-Key`
2. O valor da chave corresponde exatamente (sem espaços extras ou newlines)
3. A chave não foi desativada ou deletada

### "Too many failed authentication attempts"

Aguarde 5 minutos para o bloqueio expirar, ou reinicie o backend para resetar o estado de bloqueio em memória.
