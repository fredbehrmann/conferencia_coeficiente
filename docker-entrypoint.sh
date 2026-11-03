#!/bin/sh
# Prepara a pasta de dados e roda a aplicação sem privilégios de root.
set -e
DADOS="${CONFERENCIA_DADOS:-/dados}"
mkdir -p "$DADOS"
if [ "$(id -u)" = "0" ]; then
    # Volumes da Railway (e pastas novas do host) chegam com dono root: passa para o usuário da aplicação.
    if [ "$(stat -c %u "$DADOS")" != "1000" ]; then
        chown -R 1000:1000 "$DADOS"
    fi
    exec setpriv --reuid=1000 --regid=1000 --init-groups "$@"
fi
exec "$@"
