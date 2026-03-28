# Instalacao

Esta pagina documenta formas suportadas para executar o `proxbox-api`.

## Requisitos

- Python 3.10+
- `uv` (recomendado) ou `pip`
- Acesso de rede aos destinos NetBox e Proxmox

## Opcao 1: Docker

```bash
docker pull emersonfelipesp/proxbox-api:latest
docker run -d -p 8000:8000 --name proxbox-api emersonfelipesp/proxbox-api:latest
```

## Opcao 2: Codigo-fonte local

```bash
git clone https://github.com/emersonfelipesp/proxbox-api.git
cd proxbox-api
pip install -e .
uv run fastapi run --host 0.0.0.0 --port 8000
```

Alternativa:

```bash
uv run uvicorn proxbox_api.main:app --host 0.0.0.0 --port 8000 --reload
```
