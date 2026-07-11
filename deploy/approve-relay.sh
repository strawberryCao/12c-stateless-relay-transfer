#!/usr/bin/env bash
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${DEPLOY_DIR}"

docker compose exec -T relay python - <<'PY'
import json
import os
import time
import urllib.error
import urllib.request

admin_key = os.environ["REGISTRY_ADMIN_API_KEY"]

def request(url: str, *, method: str = "GET", payload: dict | None = None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Authorization": f"Bearer {admin_key}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)

for attempt in range(30):
    health = request("http://127.0.0.1:9090/health")
    if health.get("relayId"):
        print(f"Relay already assigned: {health['relayId']}")
        raise SystemExit(0)
    install_id = health["installId"]
    try:
        result = request(
            f"http://registry:8080/api/admin/registration-requests/{install_id}/approve",
            method="POST",
            payload={},
        )
        print(f"Relay approved: {result['relayId']}")
        raise SystemExit(0)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    time.sleep(2)

raise SystemExit("Relay registration request did not appear within 60 seconds")
PY
