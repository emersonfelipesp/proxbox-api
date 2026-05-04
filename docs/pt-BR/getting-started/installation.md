# Instalacao

Esta pagina documenta formas suportadas para executar o `proxbox-api`.

## Requisitos

- Python 3.11+
- `uv` (recomendado) ou `pip`
- Acesso de rede aos destinos NetBox e Proxmox

## Opcao 1: Docker

```bash
docker pull emersonfelipesp/proxbox-api:latest
docker run -d -p 8000:8000 --name proxbox-api emersonfelipesp/proxbox-api:latest
```

### Bind em IPv6 / dual-stack

Para escutar simultaneamente em IPv4 e IPv6, defina `PROXBOX_BIND_HOST=::`:

```bash
docker run -d -p 8000:8000 -e PROXBOX_BIND_HOST=:: \
  emersonfelipesp/proxbox-api:latest
```

#### Cuidado com aspas no Docker Compose

No formato **lista** de `environment:` do Compose, o valor e usado literalmente — as aspas **nao** sao removidas. Ou seja, `- PROXBOX_BIND_HOST="::"` chega ao container como a string de 4 caracteres `"::"`, o que ja causou o erro `[Errno -2] Name does not resolve`. O container hoje normaliza aspas defensivamente, mas as formas recomendadas sao:

```yaml
environment:
  - PROXBOX_BIND_HOST=::          # formato lista: SEM aspas
```

```yaml
environment:
  PROXBOX_BIND_HOST: "::"         # formato mapa: o YAML remove as aspas
```

## Opcao 2: PyPI

O pacote esta publicado no [PyPI](https://pypi.org/project/proxbox-api/) como `proxbox-api`.

```bash
pip install proxbox-api
```

Ou com `uv`:

```bash
uv add proxbox-api
```

Inicie o servidor apos instalar:

```bash
python -m uvicorn proxbox_api.main:app --host 0.0.0.0 --port 8000
```

## Opcao 3: Codigo-fonte local

```bash
git clone https://github.com/emersonfelipesp/proxbox-api.git
cd proxbox-api
pip install -e .
uv run fastapi run proxbox_api.main:app --host 0.0.0.0 --port 8000
```

Alternativa:

```bash
uv run uvicorn proxbox_api.main:app --host 0.0.0.0 --port 8000 --reload
```

O comando `fastapi run` nao expoe opcoes de TLS; para HTTPS no proprio processo use **uvicorn** com `--ssl-certfile` / `--ssl-keyfile`, ou **nginx/Caddy** na frente (recomendado para certificados reais).

## TLS sem Docker

### Certificados locais (mkcert)

```bash
mkcert -install
mkcert proxbox.backend.local localhost 127.0.0.1 ::1
uv run uvicorn proxbox_api.main:app --host 127.0.0.1 --port 8000 --reload \
  --ssl-keyfile=./proxbox.backend.local+3-key.pem \
  --ssl-certfile=./proxbox.backend.local+3.pem
```

Ajuste os nomes dos arquivos conforme a saida do `mkcert`.

### Certificado publico (Let's Encrypt) ou corporativo

**Recomendado:** terminar TLS no **nginx** ou **Caddy** e manter a API em HTTP em `127.0.0.1:8000`:

```bash
uv run uvicorn proxbox_api.main:app --host 127.0.0.1 --port 8000
```

Configure o proxy com `fullchain.pem` e `privkey.pem` (Let's Encrypt em `/etc/letsencrypt/live/<dominio>/`) e cabecalhos `X-Forwarded-Proto` (e afins). Exemplo completo de bloco `server` do nginx no **README** do repositorio.

**Uvicorn com TLS direto** (implantacoes menores): use a **cadeia completa** em `--ssl-certfile` e a chave em `--ssl-keyfile`:

```bash
uv run uvicorn proxbox_api.main:app --host 0.0.0.0 --port 8443 \
  --ssl-certfile=/etc/letsencrypt/live/api.exemplo.com/fullchain.pem \
  --ssl-keyfile=/etc/letsencrypt/live/api.exemplo.com/privkey.pem
```

Garanta permissoes de leitura para o usuario do processo e renove/recarregue apos atualizar o certificado.

## Verificar instalacao

Abra:

- Root: <http://127.0.0.1:8000/>
- Swagger: <http://127.0.0.1:8000/docs>
- ReDoc: <http://127.0.0.1:8000/redoc>
