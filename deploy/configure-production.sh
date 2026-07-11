#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 4 ]]; then
  echo "usage: configure-production.sh APP_DOMAIN API_DOMAIN RELAY_DOMAIN ACME_EMAIL" >&2
  exit 2
fi

APP_DOMAIN_VALUE="$1"
API_DOMAIN_VALUE="$2"
RELAY_DOMAIN_VALUE="$3"
ACME_EMAIL_VALUE="$4"
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$DEPLOY_DIR/.env"

for domain in "$APP_DOMAIN_VALUE" "$API_DOMAIN_VALUE" "$RELAY_DOMAIN_VALUE"; do
  if [[ ! "$domain" =~ ^[A-Za-z0-9.-]+$ ]] || [[ "$domain" != *.* ]]; then
    echo "invalid domain: $domain" >&2
    exit 1
  fi
done

if [[ ! "$ACME_EMAIL_VALUE" =~ ^[^[:space:]@]+@[^[:space:]@]+\.[^[:space:]@]+$ ]]; then
  echo "invalid ACME email" >&2
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  bash "$DEPLOY_DIR/init-env.sh"
fi

python3 - "$ENV_FILE" "$APP_DOMAIN_VALUE" "$API_DOMAIN_VALUE" "$RELAY_DOMAIN_VALUE" "$ACME_EMAIL_VALUE" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
updates = {
    "APP_DOMAIN": sys.argv[2],
    "API_DOMAIN": sys.argv[3],
    "RELAY_DOMAIN": sys.argv[4],
    "ACME_EMAIL": sys.argv[5],
}

lines = path.read_text(encoding="utf-8").splitlines()
seen: set[str] = set()
out: list[str] = []
for line in lines:
    if "=" in line and not line.lstrip().startswith("#"):
        key = line.split("=", 1)[0].strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
            continue
    out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY

chmod 600 "$ENV_FILE"
bash "$DEPLOY_DIR/deploy.sh"
