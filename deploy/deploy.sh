#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${DEPLOY_DIR}"

if [[ ! -f .env ]]; then
  echo "Missing deploy/.env. Run: ./init-env.sh <public-domain>" >&2
  exit 2
fi
if grep -q 'REPLACE_WITH_RANDOM_SECRET' .env; then
  echo "deploy/.env still contains placeholder secrets" >&2
  exit 2
fi

docker compose config --quiet
docker compose up -d --build
./approve-relay.sh
docker compose ps
