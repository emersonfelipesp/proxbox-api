FROM python:3.13-slim-bookworm AS builder

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY README.md pyproject.toml ./
COPY proxmox_mock ./proxmox_mock

RUN uv venv --seed /app/.venv && \
    /app/.venv/bin/python -m pip install --upgrade pip && \
    /app/.venv/bin/pip install .

FROM python:3.13-slim-bookworm AS runtime

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PORT=8000 \
    PYTHONUNBUFFERED=1

COPY --from=builder /app/.venv /app/.venv

EXPOSE 8000

CMD ["sh", "-c", "uvicorn proxmox_mock.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
