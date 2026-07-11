#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DEPLOY_DIR"

if [[ ! -f .env ]]; then
  echo "Missing deploy/.env. Run: bash deploy/init-env.sh" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

required=(APP_DOMAIN API_DOMAIN RELAY_DOMAIN ACME_EMAIL REGISTRY_ADMIN_API_KEY RELAY_ADMIN_API_KEY REGISTRY_BLOCK_AUTH_MASTER_KEY)
for name in "${required[@]}"; do
  value="${!name:-}"
  if [[ -z "$value" || "$value" == REPLACE_WITH* ]]; then
    echo "Invalid $name in deploy/.env" >&2
    exit 1
  fi
done

for domain in "$APP_DOMAIN" "$API_DOMAIN" "$RELAY_DOMAIN"; do
  if [[ "$domain" == *.example.com || "$domain" == example.com ]]; then
    echo "Replace example.com domains in deploy/.env before deployment." >&2
    exit 1
  fi
done

command -v docker >/dev/null 2>&1 || {
  echo "Docker is required." >&2
  exit 1
}
docker compose version >/dev/null

compose=(docker compose --env-file .env -f compose.yaml)

"${compose[@]}" config >/dev/null
"${compose[@]}" build
"${compose[@]}" up -d registry relay

wait_exec() {
  local service="$1"
  local command="$2"
  local attempts="${3:-60}"
  for ((i=1; i<=attempts; i++)); do
    if "${compose[@]}" exec -T "$service" sh -lc "$command" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

wait_exec registry "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).read()\"" 60 || {
  "${compose[@]}" logs registry
  echo "Registry did not become healthy." >&2
  exit 1
}

wait_exec relay "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:9090/health', timeout=3).read()\"" 60 || {
  "${compose[@]}" logs relay
  echo "Relay did not become healthy." >&2
  exit 1
}

read_relay_state() {
  "${compose[@]}" exec -T relay python -c '
import json, urllib.request
p = json.load(urllib.request.urlopen("http://127.0.0.1:9090/health", timeout=3))
print("\t".join([
    str(p.get("installId") or ""),
    str(p.get("assignmentStatus") or ""),
    str(bool(p.get("registryApiKeyReady"))).lower(),
    str(bool(p.get("blockAuthKeyReady"))).lower(),
]))
'
}

IFS=$'\t' read -r install_id assignment registry_ready block_ready < <(read_relay_state)
if [[ -z "$install_id" ]]; then
  echo "Relay did not expose an installId." >&2
  exit 1
fi

if [[ "$assignment" != "assigned" ]]; then
  echo "Approving this deployment's Relay installId: $install_id"
  approved=0
  for ((i=1; i<=45; i++)); do
    if "${compose[@]}" exec -T registry python - "$install_id" <<'PY'
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

install_id = sys.argv[1]
key = os.environ["REGISTRY_ADMIN_API_KEY"]
url = (
    "http://127.0.0.1:8080/api/admin/registration-requests/"
    + urllib.parse.quote(install_id, safe="")
    + "/approve"
)
request = urllib.request.Request(
    url,
    data=b"{}",
    method="POST",
    headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    },
)
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.load(response)
        print(payload.get("relayId", "approved"))
except urllib.error.HTTPError as exc:
    if exc.code in (404, 409):
        raise SystemExit(3)
    raise
PY
    then
      approved=1
      break
    fi
    sleep 2
  done

  if [[ "$approved" -ne 1 ]]; then
    "${compose[@]}" logs registry relay
    echo "Relay registration request could not be approved." >&2
    exit 1
  fi
fi

for ((i=1; i<=60; i++)); do
  IFS=$'\t' read -r install_id assignment registry_ready block_ready < <(read_relay_state)
  if [[ "$assignment" == "assigned" && "$registry_ready" == "true" && "$block_ready" == "true" ]]; then
    break
  fi
  sleep 2
done

if [[ "$assignment" != "assigned" || "$registry_ready" != "true" || "$block_ready" != "true" ]]; then
  "${compose[@]}" logs registry relay
  echo "Relay assignment or key bootstrap did not complete." >&2
  exit 1
fi

"${compose[@]}" up -d console gateway
"${compose[@]}" ps

echo
echo "Public app:  https://$APP_DOMAIN"
echo "Public API:  https://$API_DOMAIN (client routes only)"
echo "Public Relay: https://$RELAY_DOMAIN (64-hex token GET/PUT only)"
echo "Console is not public. Use an SSH tunnel:"
echo "  ssh -L 8070:127.0.0.1:8070 <user>@<server>"
echo "  then open http://127.0.0.1:8070"
