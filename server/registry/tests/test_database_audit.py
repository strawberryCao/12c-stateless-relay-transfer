from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from registry_server.api.app import create_app
from registry_server.config import AllowlistEntry, RegistryServerConfig
from tests.registry_fixtures import DEFAULT_TEST_PLACEMENT_POLICY


TOKEN = "a" * 64
BLOCK_HASH = "b" * 64
ADMIN_KEY = "test-admin-key"


def _config(database_path: Path) -> RegistryServerConfig:
    return RegistryServerConfig(
        host="127.0.0.1",
        port=8080,
        database_path=database_path,
        token_ttl_seconds=3600,
        registry_api_key_initial_uses=10,
        block_auth_master_key=base64.urlsafe_b64decode(
            base64.urlsafe_b64encode(b"0" * 32),
        ),
        allowlist=(
            AllowlistEntry("relay-a", "http://a.test"),
            AllowlistEntry("relay-b", "http://b.test"),
            AllowlistEntry("relay-c", "http://c.test"),
            AllowlistEntry("relay-d", "http://d.test"),
        ),
        placement_policy=DEFAULT_TEST_PLACEMENT_POLICY,
        relay_heartbeat_stale_seconds=3600,
        admin_api_key=ADMIN_KEY,
    )


def _table_names(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as db:
        rows = db.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """,
        ).fetchall()
    return {str(row[0]) for row in rows}


def _count(database_path: Path, table: str) -> int:
    with sqlite3.connect(database_path) as db:
        row = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row is not None else 0


def test_registry_initializes_twelve_project_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "registry.db"
    app = create_app(_config(database_path))
    with TestClient(app):
        pass

    assert {
        "registry_allowlist",
        "relay_registration_requests",
        "relay_states",
        "token_relay_placements",
        "relay_registry_keys",
        "relay_block_auth_keys",
        "relay_heartbeat_events",
        "token_reservation_batches",
        "token_reservation_items",
        "token_resolution_events",
        "replica_abandon_events",
        "registry_admin_events",
    }.issubset(_table_names(database_path))


def test_registry_audit_tables_record_core_flows(tmp_path: Path) -> None:
    database_path = tmp_path / "registry.db"
    app = create_app(_config(database_path))
    with TestClient(app) as client:
        reserve = client.post(
            "/api/relay/reserve-tokens",
            json={"blocks": [{"token": TOKEN, "blockHash": BLOCK_HASH}]},
        )
        assert reserve.status_code == 200
        targets = reserve.json()["routes"][0]["targets"]
        replica = next(target for target in targets if target["role"] == "replica")

        resolve = client.post("/api/relay/resolve", json={"tokens": [TOKEN]})
        assert resolve.status_code == 200

        abandon = client.post(
            "/api/relay/abandon-replica-placements",
            json={"failures": [{"token": TOKEN, "relayId": replica["relayId"]}]},
        )
        assert abandon.status_code == 200

        heartbeat = client.post(
            "/api/relay/heartbeat",
            json={
                "relayId": "relay-a",
                "relayBaseUrl": "http://a.test",
                "storedBlocks": 1,
                "maxBlocks": 100,
                "storageRate": 0.01,
                "relayPublicKeyPem": "test-public-key",
            },
        )
        assert heartbeat.status_code == 200

        admin = client.post(
            "/api/admin/allowlist",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"relayId": "relay-x", "relayBaseUrl": "http://x.test"},
        )
        assert admin.status_code == 200

    assert _count(database_path, "token_reservation_batches") == 1
    assert _count(database_path, "token_reservation_items") == 1
    assert _count(database_path, "token_resolution_events") == 1
    assert _count(database_path, "replica_abandon_events") == 1
    assert _count(database_path, "relay_heartbeat_events") == 1
    assert _count(database_path, "registry_admin_events") >= 1
