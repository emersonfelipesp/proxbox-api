ARG APP_MODULE=proxbox_api.main:app

# Build dependencies and the app into a virtualenv with uv from the checked-out repo.
FROM python:3.13-slim-bookworm AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Build from the local repository so the image always matches the checked-out commit.
COPY README.md pyproject.toml ./
COPY proxbox_api ./proxbox_api

RUN uv venv --seed /app/.venv && \
    /app/.venv/bin/python -m pip install --upgrade pip && \
    /app/.venv/bin/pip install '.[playwright]'

# Application tree + venv only (shared by HTTP and HTTPS images).
FROM python:3.13-slim-bookworm AS runtime-base

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PATH="/app/.venv/bin:$PATH" \
    PORT=8000 \
    PYTHONUNBUFFERED=1

COPY --from=builder /app/.venv /app/.venv

# Create minimal directories for compatibility (no local source needed)
RUN mkdir -p /app/scripts

EXPOSE 8000

# Default image: nginx listens on PORT (default 8000), proxies to uvicorn on 127.0.0.1:8001.
FROM runtime-base AS runtime

ARG APP_MODULE

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    nginx \
    supervisor \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /etc/nginx/sites-enabled/default \
 && rm -f /etc/nginx/conf.d/default.conf

COPY docker/nginx/proxbox-http.conf.template /etc/proxbox/nginx-http.conf.template
COPY docker/supervisor/supervisord.conf /etc/supervisor/supervisord.conf
COPY docker/supervisor/proxbox.conf /etc/supervisor/conf.d/proxbox.conf
COPY docker/entrypoint-runtime.sh /usr/local/bin/docker-entrypoint-runtime.sh
RUN chmod +x /usr/local/bin/docker-entrypoint-runtime.sh

ENV APP_MODULE=${APP_MODULE}

ENTRYPOINT ["/usr/local/bin/docker-entrypoint-runtime.sh"]
CMD []

# mkcert variant: nginx terminates TLS on PORT; same uvicorn backend.
# Extra SANs: MKCERT_EXTRA_NAMES. Persist CA: CAROOT + volume.
FROM runtime AS mkcert

ARG MKCERT_VERSION=1.4.4

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libnss3-tools \
 && rm -rf /var/lib/apt/lists/* \
 && ARCH=$(dpkg --print-architecture) \
 && curl -fsSL -o /usr/local/bin/mkcert \
    "https://github.com/FiloSottile/mkcert/releases/download/v${MKCERT_VERSION}/mkcert-v${MKCERT_VERSION}-linux-${ARCH}" \
 && chmod +x /usr/local/bin/mkcert

COPY docker/nginx/proxbox-https.conf.template /etc/proxbox/nginx-https.conf.template
COPY docker/entrypoint-mkcert.sh /usr/local/bin/docker-entrypoint-mkcert.sh
RUN chmod +x /usr/local/bin/docker-entrypoint-mkcert.sh

ENV MKCERT_CERT_DIR=/certs

ENTRYPOINT ["/usr/local/bin/docker-entrypoint-mkcert.sh"]

# `docker build .` without --target uses nginx+HTTP image.
FROM runtime
