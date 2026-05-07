#!/usr/bin/env bash
set -euo pipefail

RSSHUB_DIR="${HOME}/.openclaw/workspace-stream-gen/rsshub"

if [[ ! -d "${RSSHUB_DIR}" ]]; then
  echo "[ERROR] RSSHub directory not found: ${RSSHUB_DIR}" >&2
  exit 1
fi

cd "${RSSHUB_DIR}"

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "[ERROR] Neither 'docker compose' nor 'docker-compose' is available." >&2
  exit 1
fi

echo "[RSSHub] Pulling latest images..."
"${COMPOSE[@]}" pull

echo "[RSSHub] Restarting services..."
"${COMPOSE[@]}" up -d

echo "[RSSHub] Update complete."
