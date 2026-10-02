#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${DEPLOY_ENV_FILE:-${APP_DIR}/config/deploy.env}"

fail() {
    printf 'deploy: %s\n' "$1" >&2
    exit 1
}

[[ -f "$ENV_FILE" ]] || fail "configuration file not found: $ENV_FILE"
set -a
source "$ENV_FILE" || fail "could not load configuration file: $ENV_FILE"
set +a

[[ "${ORDERS_PORT:-}" =~ ^[0-9]+$ ]] || fail "ORDERS_PORT must be an integer"
(( ORDERS_PORT >= 1 && ORDERS_PORT <= 65535 )) || fail "ORDERS_PORT must be between 1 and 65535"

for flag in ORDERS_ENABLE_SLOW ORDERS_ENABLE_CPU ORDERS_ENABLE_LEAK; do
    case "${!flag:-}" in
        true|false) ;;
        *) fail "$flag must be true or false" ;;
    esac
done

if [[ -n "${DATABASE_URL:-}" ]]; then
    case "$DATABASE_URL" in
        postgres://*|postgresql://*) ;;
        *) fail "DATABASE_URL must use postgres:// or postgresql://" ;;
    esac
fi

command -v docker >/dev/null 2>&1 || fail "docker is required"
image="${ORDERS_IMAGE:-orders-service:local}"

docker build --tag "$image" "$APP_DIR"
docker rm --force orders-service >/dev/null 2>&1 || true
docker run --detach \
    --name orders-service \
    --restart unless-stopped \
    --publish "${ORDERS_PORT}:8000" \
    --env-file "$ENV_FILE" \
    "$image"

printf 'orders service started on port %s\n' "$ORDERS_PORT"