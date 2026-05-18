#!/bin/sh
set -e

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

CERT_DIR="${MKCERT_CERT_DIR:-/certs}"

if [ -f "$CERT_DIR/cert.pem" ] && [ -f "$CERT_DIR/key.pem" ]; then
  echo "Custom certificates detected in $CERT_DIR — skipping mkcert generation."
  if [ -f "$CERT_DIR/key-pkcs8.pem" ]; then
    KEY_PKCS8="$CERT_DIR/key-pkcs8.pem"
  else
    # granian requires PKCS#8; try to write alongside the custom certs, fall
    # back to /tmp when the mount is read-only.
    if openssl pkcs8 -topk8 -nocrypt \
        -in "$CERT_DIR/key.pem" \
        -out "$CERT_DIR/key-pkcs8.pem" 2>/dev/null; then
      chmod 600 "$CERT_DIR/key-pkcs8.pem" 2>/dev/null || true
      KEY_PKCS8="$CERT_DIR/key-pkcs8.pem"
    else
      openssl pkcs8 -topk8 -nocrypt \
        -in "$CERT_DIR/key.pem" \
        -out /tmp/key-pkcs8.pem
      chmod 600 /tmp/key-pkcs8.pem
      KEY_PKCS8="/tmp/key-pkcs8.pem"
    fi
  fi
else
  mkdir -p "$CERT_DIR"

  mkcert -install

  tmp=$(mktemp)
  trap 'rm -f "$tmp"' EXIT
  printf '%s\n' localhost 127.0.0.1 > "$tmp"
  if [ -n "${MKCERT_EXTRA_NAMES:-}" ]; then
    echo "$MKCERT_EXTRA_NAMES" | tr ',' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | grep -v '^$' >> "$tmp"
    echo "$MKCERT_EXTRA_NAMES" | tr ' ' '\n' | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' | grep -v '^$' >> "$tmp"
  fi

  list=$(sort -u "$tmp" | tr '\n' ' ')

  # shellcheck disable=SC2086
  mkcert -cert-file "$CERT_DIR/cert.pem" -key-file "$CERT_DIR/key.pem" $list

  # granian requires PKCS#8 format; mkcert generates PKCS#1 (traditional) by default.
  openssl pkcs8 -topk8 -nocrypt \
    -in "$CERT_DIR/key.pem" \
    -out "$CERT_DIR/key-pkcs8.pem"
  chmod 600 "$CERT_DIR/key-pkcs8.pem"
  KEY_PKCS8="$CERT_DIR/key-pkcs8.pem"
fi

PORT="${PORT:-8000}"

# Sanitize PROXBOX_BIND_HOST: strip surrounding ASCII quotes and whitespace.
# Compose list-form `- KEY="::"` does NOT strip the quotes, so the literal
# value reaches the container as `"::"` and crashes binding.
HOST=$(printf '%s' "${PROXBOX_BIND_HOST:-0.0.0.0}" \
  | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
        -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" \
        -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
[ -z "$HOST" ] && HOST=0.0.0.0

exec /app/.venv/bin/granian \
  --interface asgi \
  --host "$HOST" \
  --port "${PORT}" \
  --ws \
  --ssl-certificate "$CERT_DIR/cert.pem" \
  --ssl-keyfile "$KEY_PKCS8" \
  --ssl-protocol-min tls1.2 \
  proxbox_api.main:app
