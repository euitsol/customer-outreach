#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/marketing.europeanit.academy/public_html"
COMPOSE="docker compose -f docker-compose.yml"
LOCK_FILE="/tmp/mail-campaign-deploy.lock"
HEALTH_PORT=8091

log() { echo -e "\n\033[1;34m==> $1\033[0m"; }
fail() { echo -e "\033[1;31mERROR: $1\033[0m" >&2; exit 1; }

if [ -e "$LOCK_FILE" ]; then
    fail "Another deploy is already running"
fi
touch "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

cd "$APP_DIR"
[ -f .env ] || fail ".env not found — refusing to deploy without it"

PREV_COMMIT=$(git rev-parse HEAD)
log "Current commit: $PREV_COMMIT"

log "Fetching latest code"
git fetch origin main
git reset --hard origin/main
NEW_COMMIT=$(git rev-parse HEAD)
log "Reset to: $NEW_COMMIT"

log "Building images"
$COMPOSE build

log "Starting/updating the stack"
$COMPOSE up -d

log "Waiting for web container to respond"
for i in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${HEALTH_PORT}/healthz" >/dev/null 2>&1; then
        break
    fi
    sleep 2
    [ "$i" -eq 30 ] && fail "web container never became healthy — check: $COMPOSE logs web"
done

log "Pruning old images"
docker image prune -f >/dev/null || true

log "Deploy successful: $NEW_COMMIT"