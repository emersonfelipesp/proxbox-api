#!/bin/sh
set -e

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

# Sanitize PROXBOX_BIND_HOST: strip surrounding ASCII single/double quotes
# and whitespace. Compose list-form `- KEY="::"` does NOT strip the quotes,
# so the literal value reaches the container as `"::"` and crashes uvicorn
# with `[Errno -2] Name does not resolve`.
HOST=$(printf '%s' "${PROXBOX_BIND_HOST:-0.0.0.0}" \
  | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
        -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" \
        -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
[ -z "$HOST" ] && HOST=0.0.0.0

exec uvicorn proxbox_api.main:app --host "$HOST" --port "${PORT:-8000}"
