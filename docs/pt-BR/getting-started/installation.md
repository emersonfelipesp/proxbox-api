# Instalacao

Esta pagina documenta formas suportadas para executar o `proxbox-api`.

## Requisitos

- Python 3.11+
- `uv` (recomendado) ou `pip`
- Acesso de rede aos destinos NetBox e Proxmox

## Opcao 1: Docker (recomendado para inicio rapido)

Todas as imagens Docker sao baseadas em **Alpine** (menor footprint). Tres variantes estao disponiveis:

| Variante | Tags | Descricao |
|---------|------|-----------|
| **Raw** (padrao) | `latest`, `<versao>` | Uvicorn puro, somente HTTP. Imagem menor. |
| **Nginx** | `latest-nginx`, `<versao>-nginx` | nginx encerra HTTPS via mkcert; proxy para uvicorn. |
| **Granian** | `latest-granian`, `<versao>-granian` | Granian (servidor ASGI em Rust) com TLS nativo via mkcert. |

### Imagem Raw — somente HTTP (padrao)

Opcao mais simples. Sem proxy na frente, HTTP puro. Ideal para desenvolvimento local ou atras de um reverse proxy proprio.

```bash
docker pull emersonfelipesp/proxbox-api:latest
docker run -d -p 8000:8000 --name proxbox-api emersonfelipesp/proxbox-api:latest
```

URL do servico:

- <http://127.0.0.1:8000>

### Imagem Nginx — HTTPS com mkcert

O nginx encerra HTTPS usando certificados [mkcert](https://github.com/FiloSottile/mkcert) gerados automaticamente e faz proxy para o uvicorn dentro do container.

```bash
docker pull emersonfelipesp/proxbox-api:latest-nginx
docker run -d -p 8443:8000 --name proxbox-api-nginx \
  emersonfelipesp/proxbox-api:latest-nginx
```

URL do servico:

- <https://127.0.0.1:8443> (autoassinado, confiavel no host do container)

#### Conectando netbox-proxbox a imagem nginx

A imagem nginx e somente HTTPS — requisicoes HTTP simples para a porta TLS retornam um corpo JSON `400` com `{"error":"plain_http_on_https_port", ...}` (gerado pelo codigo interno `497` do nginx). Ao configurar o **FastAPI Endpoint** no plugin `netbox-proxbox` do NetBox, defina:

| Campo | Valor |
|-------|-------|
| **Usar HTTPS** | ✓ habilitado |
| **Verificar SSL** | ✗ desabilitado (ao usar o certificado mkcert embutido) |
| **Porta** | a porta do host mapeada para a porta `8000` do container (normalmente `8800` ou `8443`) |

As opcoes `Usar HTTPS` e `Verificar SSL` sao independentes no
`netbox-proxbox >= 0.0.16` — consulte a
[issue #352](https://github.com/emersonfelipesp/netbox-proxbox/issues/352) para
mais contexto. Versoes anteriores do plugin acoplam os dois flags, tornando a
combinacao imagem-nginx + certificado-autoassinado inalcancavel.

### Imagem Granian — HTTPS com mkcert (sem nginx)

[Granian](https://github.com/emmett-framework/granian) e um servidor ASGI baseado em Rust com TLS nativo e HTTP/2. Um unico processo gerencia tudo — sem nginx ou supervisord.

```bash
docker pull emersonfelipesp/proxbox-api:latest-granian
docker run -d -p 8443:8000 --name proxbox-api-granian \
  emersonfelipesp/proxbox-api:latest-granian
```

URL do servico:

- <https://127.0.0.1:8443>

### Variaveis de ambiente Docker em tempo de execucao

Comuns a todas as imagens (`raw`, `nginx`, `granian`):

| Variavel | Padrao | Descricao |
|----------|--------|-----------|
| `PORT` | `8000` | Porta em que o servidor escuta |
| `PROXBOX_BIND_HOST` | `0.0.0.0` | Endereco ao qual o servidor faz bind. Use `::` para dual-stack IPv4 + IPv6. Respeitado pelas imagens `raw` e `granian`; a imagem `nginx` escuta em ambas as pilhas incondicionalmente. |

Especificas do mkcert (apenas para as imagens `nginx` e `granian`):

| Variavel | Padrao | Descricao |
|----------|--------|-----------|
| `MKCERT_CERT_DIR` | `/certs` | Diretorio onde os certificados sao armazenados |
| `MKCERT_EXTRA_NAMES` | — | SANs extras (separados por virgula ou espaco), ex: `proxbox.lan,10.0.0.5` |
| `CAROOT` | — | Monte um volume aqui para persistir a CA local entre reinicializacoes |

Exemplo com SANs extras:

```bash
docker run -d -p 8443:8000 --name proxbox-api-tls \
  -e MKCERT_EXTRA_NAMES='myhost.local,192.168.1.10' \
  emersonfelipesp/proxbox-api:latest-nginx
```

### Montando certificados personalizados

As imagens `nginx` e `granian` detectam certificados pre-existentes na inicializacao.
Se `cert.pem` **e** `key.pem` ja estiverem presentes dentro de `$MKCERT_CERT_DIR` (padrao `/certs`),
a geracao do mkcert e **ignorada completamente** — o container usa esses arquivos diretamente.
Isso permite montar seus proprios certificados assinados por CA, Let's Encrypt ou corporativos sem
nenhuma flag especial.

```bash
docker run -d -p 8443:8000 --name proxbox-api-nginx \
  -v ./certs:/certs:ro \
  emersonfelipesp/proxbox-api:latest-nginx
```

O diretorio `./certs` deve conter no minimo:

| Arquivo | Descricao |
|---------|-----------|
| `cert.pem` | Certificado codificado em PEM (pode ser uma cadeia completa) |
| `key.pem` | Chave privada codificada em PEM (PKCS#1 ou PKCS#8) |

Exemplo com Docker Compose:

```yaml
services:
  proxbox-api:
    image: emersonfelipesp/proxbox-api:latest-nginx
    container_name: proxbox-api
    restart: unless-stopped
    ports:
      - "8443:8000"
    volumes:
      - ./certs:/certs:ro
```

#### Imagem Granian e chaves PKCS#8

A camada TLS do Granian requer a chave privada no **formato PKCS#8**.
O entrypoint trata isso automaticamente:

1. Se `key-pkcs8.pem` estiver presente no diretorio montado → usa diretamente.
2. Se `key-pkcs8.pem` estiver ausente e o diretorio for **gravavel** → converte `key.pem`
   no local e escreve `key-pkcs8.pem` la.
3. Se `key-pkcs8.pem` estiver ausente e o diretorio for **somente leitura** → converte e escreve
   em `/tmp/key-pkcs8.pem` (efemero; nao persistido entre reinicializacoes do container).

Para pre-converter sua chave e evitar o fallback para `/tmp`:

```bash
openssl pkcs8 -topk8 -nocrypt -in ./certs/key.pem -out ./certs/key-pkcs8.pem
```

Em seguida, monte o diretorio contendo os tres arquivos (`cert.pem`, `key.pem`, `key-pkcs8.pem`).

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

### Build a partir do codigo-fonte

```bash
git clone https://github.com/emersonfelipesp/proxbox-api.git
cd proxbox-api

docker build -t proxbox-api:raw .                          # raw (padrao)
docker build --target nginx -t proxbox-api:nginx .         # nginx
docker build --target granian -t proxbox-api:granian .     # granian
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

Clone o repositorio:

```bash
git clone https://github.com/emersonfelipesp/proxbox-api.git
cd proxbox-api
```

Instale as dependencias de runtime:

```bash
pip install -e .
```

Ou use `uv`:

```bash
uv sync
```

Execute a API:

```bash
uv run fastapi run proxbox_api.main:app --host 0.0.0.0 --port 8000
```

Alternativa com uvicorn:

```bash
uv run uvicorn proxbox_api.main:app --host 0.0.0.0 --port 8000 --reload
```

O comando `fastapi run` nao expoe opcoes de TLS; para HTTPS no proprio processo use **uvicorn** com `--ssl-certfile` / `--ssl-keyfile`, ou **nginx/Caddy** na frente (recomendado para certificados reais).

## TLS sem Docker

### Certificados locais (mkcert)

Para HTTPS confiavel somente na sua propria maquina:

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
