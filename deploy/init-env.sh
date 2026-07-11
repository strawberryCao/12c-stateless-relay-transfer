#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${DEPLOY_DIR}/.env"
PUBLIC_HOST="${1:-}"

if [[ -z "${PUBLIC_HOST}" ]]; then
  echo "Usage: ./init-env.sh <public-domain>" >&2
  exit 2
fi
if [[ -f "${ENV_FILE}" ]]; then
  echo "Refusing to overwrite existing ${ENV_FILE}" >&2
  exit 1
fi

python3 - "${ENV_FILE}" "${PUBLIC_HOST}" <<'PY'
import secrets
import sys
from pathlib import Path

path = Path(sys.argv[1])
host = sys.argv[2].strip()
if not host or "/" in host or "://" in host:
    raise SystemExit("public-domain must be a DNS name without scheme or path")

values = {
    "PUBLIC_HOST": host,
    "PUBLIC_ORIGIN": f"https://{host}",
    "CONSOLE_PORT": "8070",
    "REGISTRY_ADMIN_API_KEY": secrets.token_urlsafe(48),
    "RELAY_ADMIN_API_KEY": secrets.token_urlsafe(48),
    "REGISTRY_BLOCK_AUTH_MASTER_KEY": secrets.token_urlsafe(32),
}
path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
path.chmod(0o600)
PY

echo "Created ${ENV_FILE} with mode 600"
