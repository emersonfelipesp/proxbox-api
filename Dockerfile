# Build dependencies and the app into a virtualenv with uv from the checked-out repo.
FROM python:3.13-alpine AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# build-base ensures C extensions (httptools, uvloop, etc.) can compile if no
# musllinux wheel is available for the target arch.
# git is required for DEV_OVERRIDES that install SDK branches during CI smoke runs.
RUN apk add --no-cache build-base git

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Build from the local repository so the image always matches the checked-out commit.
COPY README.md pyproject.toml uv.lock ./
COPY proxbox_api ./proxbox_api

ARG DEV_OVERRIDES=""
RUN uv sync --frozen --no-dev --no-editable && \
    if [ -n "${DEV_OVERRIDES}" ]; then uv pip install --python /app/.venv/bin/python ${DEV_OVERRIDES}; fi

# Optional native reconciliation engine build. This intentionally stays out of
# the default builder so the published latest/version images remain Python-only.
FROM builder AS builder-pyo3-rust

RUN apk add --no-cache cargo rust

COPY proxbox-reconcile-rs ./proxbox-reconcile-rs

RUN uv pip install --python /app/.venv/bin/python ./proxbox-reconcile-rs && \
    /app/.venv/bin/python -c "from proxbox_api.services.sync.reconciliation.rust_bridge import rust_available; assert rust_available()"

# Application tree + venv only (shared by Python-only runtime images).
FROM python:3.13-alpine AS runtime-base

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PORT=8000 \
    PYTHONUNBUFFERED=1

COPY --from=builder /app/.venv /app/.venv

# openssh-client provides `ssh`, required by the Cloud Image Build Pipeline to
# run remote `qm`/`pvesm` commands on Proxmox hosts (cicustom snippet bake).
# Baked into the image so it survives container recreation and redeploys.
RUN apk add --no-cache openssh-client && \
    mkdir -p /app/scripts /data

EXPOSE 8000
VOLUME ["/data"]

# Application tree + venv only (shared by PyO3/Rust runtime images).
FROM python:3.13-alpine AS runtime-base-pyo3-rust

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PORT=8000 \
    PYTHONUNBUFFERED=1 \
    PROXBOX_RECONCILIATION_ENGINE=rust

COPY --from=builder-pyo3-rust /app/.venv /app/.venv

# libgcc for the Rust extension; openssh-client for the Cloud Image Build
# Pipeline's remote `qm`/`pvesm` execution (cicustom snippet bake).
RUN apk add --no-cache libgcc openssh-client && \
    mkdir -p /app/scripts /data && \
    /app/.venv/bin/python -c "from proxbox_reconcile_rs._native import build_vm_operation_queue_json"

EXPOSE 8000
VOLUME ["/data"]

# Default image: raw uvicorn, no proxy, HTTP only. Smallest possible image.
FROM runtime-base AS raw

COPY docker/entrypoint-raw.sh /usr/local/bin/docker-entrypoint-raw.sh
RUN chmod +x /usr/local/bin/docker-entrypoint-raw.sh

ENV PROXBOX_BIND_HOST=0.0.0.0
ENTRYPOINT ["/usr/local/bin/docker-entrypoint-raw.sh"]
CMD []

# Experimental raw image: raw uvicorn plus the PyO3/Rust reconciliation engine.
FROM runtime-base-pyo3-rust AS raw-pyo3-rust

COPY docker/entrypoint-raw.sh /usr/local/bin/docker-entrypoint-raw.sh
RUN chmod +x /usr/local/bin/docker-entrypoint-raw.sh

ENV PROXBOX_BIND_HOST=0.0.0.0
ENTRYPOINT ["/usr/local/bin/docker-entrypoint-raw.sh"]
CMD []

# nginx image: nginx terminates HTTPS with mkcert certs, proxies to uvicorn on 127.0.0.1:8001.
# Extra SANs: MKCERT_EXTRA_NAMES. Persist CA: CAROOT + volume.
FROM raw AS nginx

ARG MKCERT_VERSION=1.4.4

# TARGETARCH is set automatically by BuildKit (amd64, arm64, etc.)
ARG TARGETARCH

RUN apk add --no-cache \
    nginx \
    supervisor \
    ca-certificates \
    curl \
    nss-tools \
  && rm -f /etc/nginx/conf.d/default.conf \
  && curl --retry 5 --retry-delay 2 --retry-all-errors -fsSL -o /usr/local/bin/mkcert \
     "https://github.com/FiloSottile/mkcert/releases/download/v${MKCERT_VERSION}/mkcert-v${MKCERT_VERSION}-linux-${TARGETARCH}" \
  && chmod +x /usr/local/bin/mkcert

COPY docker/nginx/proxbox-https.conf.template /etc/proxbox/nginx-https.conf.template
COPY docker/supervisor/supervisord.conf /etc/supervisor/supervisord.conf
COPY docker/supervisor/proxbox.conf /etc/supervisor/conf.d/proxbox.conf
COPY docker/entrypoint-nginx.sh /usr/local/bin/docker-entrypoint-nginx.sh
RUN chmod +x /usr/local/bin/docker-entrypoint-nginx.sh

ENV MKCERT_CERT_DIR=/certs

ENTRYPOINT ["/usr/local/bin/docker-entrypoint-nginx.sh"]
CMD []

# Experimental nginx image: nginx plus the PyO3/Rust reconciliation engine.
FROM raw-pyo3-rust AS nginx-pyo3-rust

ARG MKCERT_VERSION=1.4.4
ARG TARGETARCH

RUN apk add --no-cache \
    nginx \
    supervisor \
    ca-certificates \
    curl \
    nss-tools \
  && rm -f /etc/nginx/conf.d/default.conf \
  && curl --retry 5 --retry-delay 2 --retry-all-errors -fsSL -o /usr/local/bin/mkcert \
     "https://github.com/FiloSottile/mkcert/releases/download/v${MKCERT_VERSION}/mkcert-v${MKCERT_VERSION}-linux-${TARGETARCH}" \
  && chmod +x /usr/local/bin/mkcert

COPY docker/nginx/proxbox-https.conf.template /etc/proxbox/nginx-https.conf.template
COPY docker/supervisor/supervisord.conf /etc/supervisor/supervisord.conf
COPY docker/supervisor/proxbox.conf /etc/supervisor/conf.d/proxbox.conf
COPY docker/entrypoint-nginx.sh /usr/local/bin/docker-entrypoint-nginx.sh
RUN chmod +x /usr/local/bin/docker-entrypoint-nginx.sh

ENV MKCERT_CERT_DIR=/certs

ENTRYPOINT ["/usr/local/bin/docker-entrypoint-nginx.sh"]
CMD []

# granian image: granian ASGI server with native TLS via mkcert. No nginx, no supervisor.
# Smaller than the nginx image; single process handles TLS + HTTP/2 + WebSockets.
FROM runtime-base AS granian

ARG MKCERT_VERSION=1.4.4
ARG TARGETARCH

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN apk add --no-cache \
    ca-certificates \
    curl \
    nss-tools \
    openssl \
  && uv pip install --python /app/.venv/bin/python 'granian>=2.7.0' \
  && curl --retry 5 --retry-delay 2 --retry-all-errors -fsSL -o /usr/local/bin/mkcert \
     "https://github.com/FiloSottile/mkcert/releases/download/v${MKCERT_VERSION}/mkcert-v${MKCERT_VERSION}-linux-${TARGETARCH}" \
  && chmod +x /usr/local/bin/mkcert

COPY docker/entrypoint-granian.sh /usr/local/bin/docker-entrypoint-granian.sh
RUN chmod +x /usr/local/bin/docker-entrypoint-granian.sh

ENV MKCERT_CERT_DIR=/certs

ENTRYPOINT ["/usr/local/bin/docker-entrypoint-granian.sh"]
CMD []

# Experimental granian image: granian plus the PyO3/Rust reconciliation engine.
FROM runtime-base-pyo3-rust AS granian-pyo3-rust

ARG MKCERT_VERSION=1.4.4
ARG TARGETARCH

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN apk add --no-cache \
    ca-certificates \
    curl \
    nss-tools \
    openssl \
  && uv pip install --python /app/.venv/bin/python 'granian>=2.7.0' \
  && curl --retry 5 --retry-delay 2 --retry-all-errors -fsSL -o /usr/local/bin/mkcert \
     "https://github.com/FiloSottile/mkcert/releases/download/v${MKCERT_VERSION}/mkcert-v${MKCERT_VERSION}-linux-${TARGETARCH}" \
  && chmod +x /usr/local/bin/mkcert

COPY docker/entrypoint-granian.sh /usr/local/bin/docker-entrypoint-granian.sh
RUN chmod +x /usr/local/bin/docker-entrypoint-granian.sh

ENV MKCERT_CERT_DIR=/certs

ENTRYPOINT ["/usr/local/bin/docker-entrypoint-granian.sh"]
CMD []

# `docker build .` without --target uses the raw (uvicorn-only) image.
FROM raw
