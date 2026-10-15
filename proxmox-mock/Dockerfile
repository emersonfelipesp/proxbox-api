# Release-only recipe. release/context is created by the approval-gated workflow.
ARG UV_IMAGE
ARG PYTHON_BASE_IMAGE

FROM ${UV_IMAGE} AS uv-source
FROM ${PYTHON_BASE_IMAGE} AS runtime

ARG TARGETPLATFORM
ARG UV_IMAGE
ARG PYTHON_BASE_IMAGE
WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH" \
    PORT=8000 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=uv-source /uv /usr/local/bin/uv
COPY release/context/ /release/

RUN WHEEL_FILENAME="$(python -I -B /release/release_contract.py wheel-filename \
      --manifest /release/release-manifest.toml)" && \
    test "${PYTHON_BASE_IMAGE}" = "$(python -I -B /release/release_contract.py image-ref \
      --manifest /release/release-manifest.toml \
      --platform "${TARGETPLATFORM}" --kind base)" && \
    test "${UV_IMAGE}" = "$(python -I -B /release/release_contract.py image-ref \
      --manifest /release/release-manifest.toml \
      --platform "${TARGETPLATFORM}" --kind uv)" && \
    python -I -B /release/release_contract.py verify-context \
      --manifest /release/release-manifest.toml \
      --platform "${TARGETPLATFORM}" \
      --wheel "/release/dist/${WHEEL_FILENAME}" \
      --requirements /release/requirements.lock \
      --wheelhouse /release/wheelhouse && \
    uv venv --python /usr/local/bin/python /app/.venv && \
    uv pip install --python /app/.venv/bin/python \
      --offline --no-index --require-hashes --only-binary=:all: \
      --find-links /release/wheelhouse --requirements /release/requirements.lock && \
    uv pip install --python /app/.venv/bin/python \
      --offline --no-index --no-deps --require-hashes \
      --find-links /release/dist --requirements /release/application-wheel.txt && \
    uv pip check --python /app/.venv/bin/python && \
    /app/.venv/bin/python -I -B /release/release_contract.py verify-installed \
      --manifest /release/release-manifest.toml --platform "${TARGETPLATFORM}" && \
    /app/.venv/bin/python -I -B -c "import proxmox_mock.main" && \
    rm -rf /release

EXPOSE 8000

CMD ["sh", "-c", "uvicorn proxmox_mock.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
