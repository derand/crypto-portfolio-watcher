#!/usr/bin/env bash
# Thin wrapper around ./pw: same CLI, same defaults, inside the container.
#   docker compose run --rm pw config-check   ->  ./pw config-check
set -euo pipefail

# An explicit path or a shell means "get me inside", not "run a subcommand".
case "${1:-}" in
    /*|bash|sh|python) exec "$@" ;;
esac

# The subcommand is not always $1: global flags come before it (./pw -v scan).
subcommand=""
args=("$@")
i=0
while [ $i -lt ${#args[@]} ]; do
    case "${args[$i]}" in
        -c|--config|-e|--env) i=$((i + 2)) ;;   # flag plus its value
        -*)                   i=$((i + 1)) ;;
        *)                    subcommand="${args[$i]}"; break ;;
    esac
done

# init-db only syncs config into SQLite - it never fetches, and it is
# idempotent. Running it before the long-lived commands is what makes a fresh
# data/ volume work, and what keeps a config edit from being silently ignored:
# after editing portfolio.yaml a restart is enough, no manual init-db.
case "$subcommand" in
    watch|scan|bot)
        # A bind mount created by the Docker daemon belongs to root, and sqlite
        # dies on it with a traceback about "unable to open database file".
        # Say it plainly instead.
        if [ -d /app/data ] && [ ! -w /app/data ]; then
            echo "entrypoint: /app/data is not writable by uid $(id -u):$(id -g)" >&2
            echo "entrypoint: on the host, next to docker-compose.yml, run:" >&2
            echo "entrypoint:   sudo chown -R \$(id -u):\$(id -g) data" >&2
            echo "entrypoint: and put the same PUID/PGID in .env" >&2
            exit 1
        fi
        if [ "${INIT_DB_ON_START:-true}" = "true" ]; then
            python -m portfolio.cli "${@:1:$i}" init-db
        fi
        ;;
esac

exec python -m portfolio.cli "$@"
