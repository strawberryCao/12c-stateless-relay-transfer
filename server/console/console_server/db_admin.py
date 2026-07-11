from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi import HTTPException

REGISTRY_DB_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "registry_allowlist": ("relay_id",),
    "relay_registration_requests": ("install_id",),
    "relay_states": ("relay_id",),
    "token_relay_placements": ("token", "relay_id"),
    "relay_registry_keys": ("relay_id", "key_id"),
    "relay_block_auth_keys": ("relay_id", "key_id"),
    "relay_heartbeat_events": ("event_id",),
    "token_reservation_batches": ("batch_id",),
    "token_reservation_items": ("batch_id", "token_hash"),
    "token_resolution_events": ("event_id",),
    "replica_abandon_events": ("event_id",),
    "registry_admin_events": ("event_id",),
}

RELAY_DB_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "blocks": ("token",),
    "block_access_events": ("event_id",),
    "block_sweep_runs": ("run_id",),
}


def resolve_database_path(service_config_path: Path, field: str = "databasePath") -> Path:
    if not service_config_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"service config not found: {service_config_path}",
        )
    with service_config_path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="service config must be a JSON object")
    value = data.get(field, "./data/registry.db" if field == "databasePath" else "./data/relay.db")
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=500, detail=f"{field} must be a non-empty string")
    path = Path(value)
    if path.is_absolute():
        return path
    return (service_config_path.parent / path).resolve()


def enrich_admin_db_payload(
    payload: dict[str, object],
    primary_keys: dict[str, tuple[str, ...]],
) -> dict[str, object]:
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        return payload
    for name, table in tables.items():
        if not isinstance(table, dict):
            continue
        if table.get("primaryKey"):
            continue
        pk = primary_keys.get(name)
        if pk:
            table["primaryKey"] = list(pk)
    return payload


def delete_sqlite_row(
    database_path: Path,
    *,
    table: str,
    keys: dict[str, object],
    primary_keys: dict[str, tuple[str, ...]],
) -> bool:
    if table not in primary_keys:
        raise ValueError(f"table not deletable: {table}")
    pk_columns = primary_keys[table]
    missing = [column for column in pk_columns if column not in keys]
    if missing:
        raise ValueError(f"missing primary key columns: {', '.join(missing)}")
    if not database_path.is_file():
        raise HTTPException(status_code=404, detail=f"database not found: {database_path}")
    where = " AND ".join(f"{column} = ?" for column in pk_columns)
    values = [keys[column] for column in pk_columns]
    with sqlite3.connect(database_path) as db:
        cursor = db.execute(f"DELETE FROM {table} WHERE {where}", values)
        db.commit()
        return cursor.rowcount > 0


def is_missing_upstream_route(response_detail: object) -> bool:
    return response_detail == "Not Found"
