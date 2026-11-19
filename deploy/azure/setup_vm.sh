#!/usr/bin/env bash
# Run ON the Azure VM (Ubuntu 22.04), once: installs Docker, fetches the code, starts Caddy (HTTPS in front of the UI).
#
# UNTESTED: written from documentation, not run on a real VM. Read it first; fix it when a step turns out different.
#
#   ssh azureuser@<fqdn>
#   curl -fsSLO https://raw.githubusercontent.com/<owner>/<repo>/main/deploy/azure/setup_vm.sh   # or copy it with scp
#   bash setup_vm.sh <git clone URL> <fqdn>
#
# It does NOT create .env (secrets): copy it from your laptop afterwards, see README.md step 3.
set -euo pipefail

REPO_URL="${1:?usage: setup_vm.sh <git clone URL> <fqdn>}"
DOMAIN="${2:?usage: setup_vm.sh <git clone URL> <fqdn>}"
APP_DIR="$HOME/jenkins_guardian"

echo "[1/4] packages and Docker"
sudo apt-get update -y
sudo apt-get install -y ca-certificates curl git
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo usermod -aG docker "$USER"   # takes effect at the next login

echo "[2/4] the code in $APP_DIR"
if [ -d "$APP_DIR/.git" ]; then git -C "$APP_DIR" pull --ff-only; else git clone "$REPO_URL" "$APP_DIR"; fi

echo "[3/4] Caddy: HTTPS for $DOMAIN in front of the UI (port 3001 on this machine)"
sudo docker rm -f caddy >/dev/null 2>&1 || true
sudo docker run -d --name caddy --restart unless-stopped --network host \
  -e DOMAIN="$DOMAIN" \
  -v "$APP_DIR/deploy/azure/Caddyfile:/etc/caddy/Caddyfile:ro" \
  -v caddy_data:/data \
  caddy:2

echo "[4/4] values you need for .env on this machine"
echo "  DOCKER_GID=$(getent group docker | cut -d: -f3)"
echo "  UI_COOKIE_SECURE=true"
echo "  GRAFANA_PUBLIC_URL=https://$DOMAIN/grafana"
echo
echo "Next: log out and back in (so the docker group applies), copy .env to $APP_DIR/.env, then:"
echo "  cd $APP_DIR && docker compose up -d --build && python3 scripts/preflight.py --build"
