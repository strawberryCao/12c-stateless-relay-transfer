#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DEPLOY_DIR"

if [[ -f .env ]]; then
  echo "deploy/.env already exists; refusing to overwrite it."
  exit 1
fi

command -v openssl >/dev/null 2>&1 || {
  echo "openssl is required to generate production secrets." >&2
  exit 1
}

cp .env.example .env
registry_admin="$(openssl rand -hex 32)"
relay_admin="$(openssl rand -hex 32)"
master_key="$(openssl rand -base64 48 | tr '+/' '-_' | tr -d '=\n')"

sed -i "s/REPLACE_WITH_RANDOM_REGISTRY_ADMIN_KEY/${registry_admin}/" .env
sed -i "s/REPLACE_WITH_RANDOM_RELAY_ADMIN_KEY/${relay_admin}/" .env
sed -i "s/REPLACE_WITH_URLSAFE_BASE64_MASTER_KEY/${master_key}/" .env
chmod 600 .env

echo "Created deploy/.env with random secrets."
echo "Edit APP_DOMAIN, API_DOMAIN, RELAY_DOMAIN and ACME_EMAIL before deployment."
