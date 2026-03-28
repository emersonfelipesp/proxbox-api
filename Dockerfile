# Build dependencies and the app into a virtualenv with uv (reproducible via uv.lock).
FROM python:3.12-slim-bookworm AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
COPY proxbox_api ./proxbox_api/

RUN uv sync --frozen --no-dev

# Runtime image: copy only the venv and application sources.
FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PORT=8000 \
    PYTHONUNBUFFERED=1

COPY --from=builder /app/.venv /app/.venv
COPY pyproject.toml uv.lock ./
COPY proxbox_api ./proxbox_api/

EXPOSE 8000

CMD ["sh", "-c", "uvicorn proxbox_api.main:app --host 0.0.0.0 --port ${PORT}"]
