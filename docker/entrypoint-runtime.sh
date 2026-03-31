#!/bin/sh
set -e

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

PORT="${PORT:-8000}"
sed -e "s/__PORT__/${PORT}/g" /etc/proxbox/nginx-http.conf.template > /etc/nginx/conf.d/proxbox.conf
exec /usr/bin/supervisord -n -c /etc/supervisor/supervisord.conf
