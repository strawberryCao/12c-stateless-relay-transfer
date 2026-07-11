from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import uuid

import aiosqlite

from ..config import AllowlistEntry, RegistryServerConfig, generate_registry_api_key
from ..crypto.block_auth import derive_block_auth_key
from ..crypto.keys import encrypt_registry_api_key_for_relay, encrypt_secret_bytes_for_relay, hash_registry_api_key
from ..scheduling.placement import HealthyRelay
from ..scheduling.placement_ttl import (
    DEFAULT_BLOCK_MAX_AGE_SECONDS,
    DEFAULT_BLOCK_SWEEP_INTERVAL_SECONDS,
    InsufficientRelayCapacityError,
    resolve_placement_with_ttl,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class RelayTarget:
    role: str
    relay_id: str
    relay_base_url: str


@dataclass(frozen=True)
class TokenReserveResult:
    token: str
    targets: tuple[RelayTarget, ...]


@dataclass(frozen=True)
class LockTokensOutcome:
    routes: tuple[TokenReserveResult, ...]
    granted_ttl_seconds: int
    requested_ttl_seconds: int
    degraded: bool
    stripe_count: int
    replica_factor: int
    ideal_relay_count: int
    actual_relay_count: int


@dataclass(frozen=True)
class TokenPlacement:
    token: str
    relay_id: str
    relay_base_url: str
    role: str
    block_hash: str
    expiry_at: str


@dataclass(frozen=True)
class AllowlistRecord:
    relay_id: str
    relay_base_url: str | None
    added_at: str
    enabled: bool


@dataclass(frozen=True)
class RelayState:
    relay_id: str
    relay_base_url: str
    status: str
    stored_blocks: int
    max_blocks: int
    storage_rate: float
    last_heartbeat_at: str
    block_max_age_seconds: int
    block_sweep_interval_seconds: int


@dataclass(frozen=True)
class RelayOverview:
    relay_id: str
    relay_base_url: str | None
    enabled: bool
    added_at: str
    health_status: str
    heartbeat_status: str | None
    stored_blocks: int | None
    max_blocks: int | None
    storage_rate: float | None
    last_heartbeat_at: str | None
    block_max_age_seconds: int | None = None
    block_sweep_interval_seconds: int | None = None


@dataclass(frozen=True)
class RegistrationRequestRecord:
    install_id: str
    relay_id: str | None
    relay_base_url: str
    relay_public_key_pem: str | None
    status: str
    requested_at: str
    last_seen_at: str


ADMIN_DB_TABLES: tuple[str, ...] = (
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
)

ADMIN_DB_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
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

ADMIN_DB_ROW_LIMIT = 500


@dataclass(frozen=True)
class RegistryApiKeyRecord:
    relay_id: str
    key_id: str
    remaining_uses: int
    relay_public_key_pem: str | None


@dataclass(frozen=True)
class NextRegistryApiKey:
    key_id: str
    encrypted_key: str
    algorithm: str = "RSA-OAEP-SHA256"


@dataclass(frozen=True)
class BlockAuthKeyRecord:
    relay_id: str
    key_id: str
    status: str
    created_at: str


@dataclass(frozen=True)
class NextBlockAuthKey:
    key_id: str
    encrypted_key: str
    algorithm: str = "RSA-OAEP-SHA256"


class TokenOccupiedError(Exception):
    def __init__(self, occupied_tokens: list[dict[str, str | None]]) -> None:
        self.occupied_tokens = occupied_tokens
        super().__init__("one or more tokens are occupied")

_PATCH_UNSET = object()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token_prefix(token: str) -> str:
    return token[:8]


class RegistryRepository:
    def __init__(self, database_path: Path, config: RegistryServerConfig) -> None:
        self._database_path = database_path
        self._config = config

    async def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS token_relay_placements (
                    token TEXT NOT NULL,
                    relay_id TEXT NOT NULL,
                    relay_base_url TEXT NOT NULL,
                    role TEXT NOT NULL,
                    block_hash TEXT NOT NULL,
                    expiry_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (token, relay_id)
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS relay_states (
                    relay_id TEXT PRIMARY KEY,
                    relay_base_url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stored_blocks INTEGER NOT NULL,
                    max_blocks INTEGER NOT NULL,
                    storage_rate REAL NOT NULL,
                    last_heartbeat_at TEXT NOT NULL,
                    block_max_age_seconds INTEGER NOT NULL DEFAULT 86400,
                    block_sweep_interval_seconds INTEGER NOT NULL DEFAULT 3600
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS relay_registry_keys (
                    relay_id TEXT NOT NULL,
                    key_id TEXT NOT NULL,
                    key_secret_hash TEXT NOT NULL,
                    remaining_uses INTEGER NOT NULL,
                    relay_public_key_pem TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (relay_id, key_id)
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS registry_allowlist (
                    relay_id TEXT PRIMARY KEY,
                    relay_base_url TEXT,
                    added_at TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS relay_registration_requests (
                    install_id TEXT PRIMARY KEY,
                    relay_id TEXT UNIQUE,
                    relay_base_url TEXT NOT NULL,
                    relay_public_key_pem TEXT,
                    status TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                )
                """,
            )
            await self._migrate_registration_requests_schema(db)
            await self._migrate_relay_states_ttl_columns(db)
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS relay_block_auth_keys (
                    relay_id TEXT NOT NULL,
                    key_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (relay_id, key_id)
                )
                """,
            )
            # 运维审计表只保存状态、统计和哈希摘要，不保存文件内容。
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS relay_heartbeat_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    relay_id TEXT NOT NULL,
                    relay_base_url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stored_blocks INTEGER NOT NULL,
                    max_blocks INTEGER NOT NULL,
                    storage_rate REAL NOT NULL,
                    reported_at TEXT NOT NULL
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS token_reservation_batches (
                    batch_id TEXT PRIMARY KEY,
                    token_count INTEGER NOT NULL,
                    ttl_seconds INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS token_reservation_items (
                    batch_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    token_prefix TEXT NOT NULL,
                    block_hash_prefix TEXT NOT NULL,
                    target_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (batch_id, token_hash)
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS token_resolution_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_count INTEGER NOT NULL,
                    resolved_count INTEGER NOT NULL,
                    requested_at TEXT NOT NULL
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS replica_abandon_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_hash TEXT NOT NULL,
                    token_prefix TEXT NOT NULL,
                    relay_id TEXT NOT NULL,
                    removed INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS registry_admin_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    target_table TEXT,
                    target_key TEXT,
                    created_at TEXT NOT NULL
                )
                """,
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_relay_heartbeat_events_relay_time
                ON relay_heartbeat_events(relay_id, reported_at)
                """,
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_block_resolution_events_time
                ON token_resolution_events(requested_at)
                """,
            )
            await db.commit()

        await self._seed_allowlist()
        await self.purge_expired_tokens()

    async def _migrate_registration_requests_schema(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(relay_registration_requests)")
        rows = await cursor.fetchall()
        if not rows:
            return
        columns = {row[1] for row in rows}
        if "install_id" in columns:
            return
        await db.execute("DROP TABLE relay_registration_requests")
        await db.execute(
            """
            CREATE TABLE relay_registration_requests (
                install_id TEXT PRIMARY KEY,
                relay_id TEXT UNIQUE,
                relay_base_url TEXT NOT NULL,
                relay_public_key_pem TEXT,
                status TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            )
            """,
        )
        await db.commit()

    async def _migrate_relay_states_ttl_columns(self, db: aiosqlite.Connection) -> None:
        cursor = await db.execute("PRAGMA table_info(relay_states)")
        rows = await cursor.fetchall()
        if not rows:
            return
        columns = {row[1] for row in rows}
        if "block_max_age_seconds" not in columns:
            await db.execute(
                """
                ALTER TABLE relay_states
                ADD COLUMN block_max_age_seconds INTEGER NOT NULL DEFAULT 86400
                """,
            )
        if "block_sweep_interval_seconds" not in columns:
            await db.execute(
                """
                ALTER TABLE relay_states
                ADD COLUMN block_sweep_interval_seconds INTEGER NOT NULL DEFAULT 3600
                """,
            )
        await db.commit()

    async def _seed_allowlist(self) -> None:
        if not self._config.allowlist:
            return
        now = utc_now_iso()
        async with aiosqlite.connect(self._database_path) as db:
            for entry in self._config.allowlist:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO registry_allowlist (
                        relay_id, relay_base_url, added_at, enabled
                    ) VALUES (?, ?, ?, 1)
                    """,
                    (entry.relay_id, entry.relay_base_url, now),
                )
            await db.commit()

    async def is_allowlisted(self, relay_id: str) -> bool:
        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                """
                SELECT enabled FROM registry_allowlist
                WHERE relay_id = ? AND enabled = 1
                """,
                (relay_id,),
            )
            row = await cursor.fetchone()
            return row is not None

    async def get_allowlist_entry(self, relay_id: str) -> dict[str, str] | None:
        record = await self.get_allowlist_record(relay_id)
        if record is None or not record.enabled:
            return None
        return {
            "relay_id": record.relay_id,
            "relay_base_url": record.relay_base_url or "",
            "added_at": record.added_at,
            "enabled": "1" if record.enabled else "0",
        }

    def _row_to_allowlist_record(self, row: aiosqlite.Row) -> AllowlistRecord:
        relay_base_url = row["relay_base_url"]
        return AllowlistRecord(
            relay_id=str(row["relay_id"]),
            relay_base_url=str(relay_base_url).rstrip("/") if relay_base_url else None,
            added_at=str(row["added_at"]),
            enabled=bool(row["enabled"]),
        )

    async def list_allowlist_entries(self) -> list[AllowlistRecord]:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT relay_id, relay_base_url, added_at, enabled
                FROM registry_allowlist
                ORDER BY relay_id ASC
                """,
            )
            rows = await cursor.fetchall()
            return [self._row_to_allowlist_record(row) for row in rows]

    async def get_allowlist_record(self, relay_id: str) -> AllowlistRecord | None:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT relay_id, relay_base_url, added_at, enabled
                FROM registry_allowlist
                WHERE relay_id = ?
                """,
                (relay_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_allowlist_record(row)

    async def upsert_allowlist_entry(
        self,
        *,
        relay_id: str,
        relay_base_url: str | None,
        enabled: bool = True,
    ) -> AllowlistRecord:
        now = utc_now_iso()
        normalized_url = relay_base_url.rstrip("/") if relay_base_url else None
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO registry_allowlist (
                    relay_id, relay_base_url, added_at, enabled
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(relay_id) DO UPDATE SET
                    relay_base_url = excluded.relay_base_url,
                    enabled = excluded.enabled
                """,
                (relay_id, normalized_url, now, 1 if enabled else 0),
            )
            await db.commit()

        record = await self.get_allowlist_record(relay_id)
        if record is None:
            raise RuntimeError(f"failed to upsert allowlist entry: {relay_id}")
        return record

    async def patch_allowlist_entry(
        self,
        relay_id: str,
        *,
        relay_base_url: str | None | object = _PATCH_UNSET,
        enabled: bool | None = None,
    ) -> AllowlistRecord | None:
        existing = await self.get_allowlist_record(relay_id)
        if existing is None:
            return None

        next_url = existing.relay_base_url
        if relay_base_url is not _PATCH_UNSET:
            if relay_base_url is None:
                next_url = None
            else:
                next_url = str(relay_base_url).rstrip("/")

        next_enabled = existing.enabled if enabled is None else enabled

        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                UPDATE registry_allowlist
                SET relay_base_url = ?, enabled = ?
                WHERE relay_id = ?
                """,
                (next_url, 1 if next_enabled else 0, relay_id),
            )
            await db.commit()

        return await self.get_allowlist_record(relay_id)

    async def delete_allowlist_entry(self, relay_id: str) -> bool:
        existing = await self.get_allowlist_record(relay_id)
        if existing is None:
            return False

        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                "DELETE FROM token_relay_placements WHERE relay_id = ?",
                (relay_id,),
            )
            await db.execute(
                "DELETE FROM relay_states WHERE relay_id = ?",
                (relay_id,),
            )
            await db.execute(
                "DELETE FROM relay_registry_keys WHERE relay_id = ?",
                (relay_id,),
            )
            await db.execute(
                "DELETE FROM relay_block_auth_keys WHERE relay_id = ?",
                (relay_id,),
            )
            cursor = await db.execute(
                "DELETE FROM registry_allowlist WHERE relay_id = ?",
                (relay_id,),
            )
            await db.commit()
            return cursor.rowcount > 0

    def _row_to_registration_request(
        self,
        row: aiosqlite.Row,
    ) -> RegistrationRequestRecord:
        pem = row["relay_public_key_pem"]
        assigned = row["relay_id"]
        return RegistrationRequestRecord(
            install_id=str(row["install_id"]),
            relay_id=str(assigned) if assigned else None,
            relay_base_url=str(row["relay_base_url"]).rstrip("/"),
            relay_public_key_pem=str(pem) if pem else None,
            status=str(row["status"]),
            requested_at=str(row["requested_at"]),
            last_seen_at=str(row["last_seen_at"]),
        )

    async def upsert_registration_request(
        self,
        *,
        install_id: str,
        relay_base_url: str,
        relay_public_key_pem: str | None,
    ) -> RegistrationRequestRecord:
        now = utc_now_iso()
        normalized_url = relay_base_url.rstrip("/")
        existing = await self.get_registration_request_by_install_id(install_id)
        if existing is not None and existing.status == "approved" and existing.relay_id:
            if await self.is_allowlisted(existing.relay_id):
                return existing
        next_status = "pending"
        if existing is not None and existing.status == "ignored":
            next_status = "pending"
        requested_at = existing.requested_at if existing is not None else now
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO relay_registration_requests (
                    install_id, relay_id, relay_base_url, relay_public_key_pem,
                    status, requested_at, last_seen_at
                ) VALUES (?, NULL, ?, ?, ?, ?, ?)
                ON CONFLICT(install_id) DO UPDATE SET
                    relay_base_url = excluded.relay_base_url,
                    relay_public_key_pem = COALESCE(
                        excluded.relay_public_key_pem,
                        relay_registration_requests.relay_public_key_pem
                    ),
                    status = excluded.status,
                    relay_id = excluded.relay_id,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    install_id,
                    normalized_url,
                    relay_public_key_pem,
                    next_status,
                    requested_at,
                    now,
                ),
            )
            await db.commit()
        record = await self.get_registration_request_by_install_id(install_id)
        if record is None:
            raise RuntimeError(f"failed to upsert registration request: {install_id}")
        return record

    async def get_registration_request_by_install_id(
        self,
        install_id: str,
    ) -> RegistrationRequestRecord | None:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT install_id, relay_id, relay_base_url, relay_public_key_pem,
                       status, requested_at, last_seen_at
                FROM relay_registration_requests
                WHERE install_id = ?
                """,
                (install_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_registration_request(row)

    async def get_registration_request(
        self,
        relay_id: str,
    ) -> RegistrationRequestRecord | None:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT install_id, relay_id, relay_base_url, relay_public_key_pem,
                       status, requested_at, last_seen_at
                FROM relay_registration_requests
                WHERE relay_id = ?
                """,
                (relay_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_registration_request(row)

    async def list_registration_requests(
        self,
        *,
        status: str | None = "pending",
    ) -> list[RegistrationRequestRecord]:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            if status is None:
                cursor = await db.execute(
                    """
                    SELECT install_id, relay_id, relay_base_url, relay_public_key_pem,
                           status, requested_at, last_seen_at
                    FROM relay_registration_requests
                    ORDER BY last_seen_at DESC, install_id ASC
                    """,
                )
            else:
                cursor = await db.execute(
                    """
                    SELECT install_id, relay_id, relay_base_url, relay_public_key_pem,
                           status, requested_at, last_seen_at
                    FROM relay_registration_requests
                    WHERE status = ?
                    ORDER BY last_seen_at DESC, install_id ASC
                    """,
                    (status,),
                )
            rows = await cursor.fetchall()
            return [self._row_to_registration_request(row) for row in rows]

    async def count_registration_requests(self, *, status: str = "pending") -> int:
        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                """
                SELECT COUNT(*) FROM relay_registration_requests
                WHERE status = ?
                """,
                (status,),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row is not None else 0

    async def set_registration_request_status(
        self,
        install_id: str,
        *,
        status: str,
    ) -> RegistrationRequestRecord | None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                UPDATE relay_registration_requests
                SET status = ?
                WHERE install_id = ?
                """,
                (status, install_id),
            )
            await db.commit()
        return await self.get_registration_request_by_install_id(install_id)

    async def assign_registration_request(
        self,
        install_id: str,
        *,
        relay_id: str,
    ) -> RegistrationRequestRecord | None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                UPDATE relay_registration_requests
                SET status = 'approved', relay_id = ?
                WHERE install_id = ?
                """,
                (relay_id, install_id),
            )
            await db.commit()
        return await self.get_registration_request_by_install_id(install_id)

    async def list_relay_states(self) -> list[RelayState]:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT relay_id, relay_base_url, status, stored_blocks,
                       max_blocks, storage_rate, last_heartbeat_at,
                       block_max_age_seconds, block_sweep_interval_seconds
                FROM relay_states
                ORDER BY relay_id ASC
                """,
            )
            rows = await cursor.fetchall()
            return [
                RelayState(
                    relay_id=str(row["relay_id"]),
                    relay_base_url=str(row["relay_base_url"]).rstrip("/"),
                    status=str(row["status"]),
                    stored_blocks=int(row["stored_blocks"]),
                    max_blocks=int(row["max_blocks"]),
                    storage_rate=float(row["storage_rate"]),
                    last_heartbeat_at=str(row["last_heartbeat_at"]),
                    block_max_age_seconds=int(row["block_max_age_seconds"]),
                    block_sweep_interval_seconds=int(row["block_sweep_interval_seconds"]),
                )
                for row in rows
            ]

    def _relay_health_status(
        self,
        *,
        enabled: bool,
        last_heartbeat_at: str | None,
    ) -> str:
        if not enabled:
            return "disabled"
        if last_heartbeat_at is None:
            return "never_seen"
        stale_before = (
            utc_now() - timedelta(seconds=self._config.relay_heartbeat_stale_seconds)
        ).isoformat()
        if last_heartbeat_at <= stale_before:
            return "stale"
        return "online"

    async def list_relay_overviews(self) -> list[RelayOverview]:
        allowlist = await self.list_allowlist_entries()
        states = {item.relay_id: item for item in await self.list_relay_states()}
        overviews: list[RelayOverview] = []
        for entry in allowlist:
            state = states.get(entry.relay_id)
            last_heartbeat = state.last_heartbeat_at if state is not None else None
            overviews.append(
                RelayOverview(
                    relay_id=entry.relay_id,
                    relay_base_url=entry.relay_base_url,
                    enabled=entry.enabled,
                    added_at=entry.added_at,
                    health_status=self._relay_health_status(
                        enabled=entry.enabled,
                        last_heartbeat_at=last_heartbeat,
                    ),
                    heartbeat_status=state.status if state is not None else None,
                    stored_blocks=state.stored_blocks if state is not None else None,
                    max_blocks=state.max_blocks if state is not None else None,
                    storage_rate=state.storage_rate if state is not None else None,
                    last_heartbeat_at=last_heartbeat,
                    block_max_age_seconds=(
                        state.block_max_age_seconds if state is not None else None
                    ),
                    block_sweep_interval_seconds=(
                        state.block_sweep_interval_seconds if state is not None else None
                    ),
                ),
            )
        return overviews

    async def record_heartbeat_event(
        self,
        *,
        relay_id: str,
        relay_base_url: str,
        status: str,
        stored_blocks: int,
        max_blocks: int,
        storage_rate: float,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO relay_heartbeat_events (
                    relay_id, relay_base_url, status, stored_blocks,
                    max_blocks, storage_rate, reported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    relay_id,
                    relay_base_url.rstrip("/"),
                    status,
                    stored_blocks,
                    max_blocks,
                    storage_rate,
                    utc_now_iso(),
                ),
            )
            await db.commit()

    async def record_token_reservation_batch(
        self,
        *,
        entries: list[tuple[str, str]],
        results: list[TokenReserveResult],
        ttl_seconds: int,
    ) -> None:
        batch_id = uuid.uuid4().hex
        now = utc_now_iso()
        target_counts = {item.token: len(item.targets) for item in results}
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO token_reservation_batches (
                    batch_id, token_count, ttl_seconds, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (batch_id, len(entries), ttl_seconds, now),
            )
            for token, block_hash in entries:
                await db.execute(
                    """
                    INSERT INTO token_reservation_items (
                        batch_id, token_hash, token_prefix, block_hash_prefix,
                        target_count, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        _hash_token(token),
                        _token_prefix(token),
                        block_hash[:12],
                        target_counts.get(token, 0),
                        now,
                    ),
                )
            await db.commit()

    async def record_token_resolution_event(
        self,
        *,
        token_count: int,
        resolved_count: int,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO token_resolution_events (
                    token_count, resolved_count, requested_at
                ) VALUES (?, ?, ?)
                """,
                (token_count, resolved_count, utc_now_iso()),
            )
            await db.commit()

    async def record_admin_event(
        self,
        *,
        action: str,
        target_table: str | None = None,
        keys: dict[str, object] | None = None,
    ) -> None:
        target_key = (
            json.dumps(keys, ensure_ascii=False, sort_keys=True, default=str)
            if keys is not None
            else None
        )
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO registry_admin_events (
                    action, target_table, target_key, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (action, target_table, target_key, utc_now_iso()),
            )
            await db.commit()

    def _mask_admin_row(self, table: str, row: dict[str, object]) -> dict[str, object]:
        masked = dict(row)
        if table == "relay_registry_keys" and masked.get("key_secret_hash"):
            value = str(masked["key_secret_hash"])
            masked["key_secret_hash"] = f"{value[:8]}…" if len(value) > 8 else "…"
        if table == "relay_registry_keys" and masked.get("relay_public_key_pem"):
            pem = str(masked["relay_public_key_pem"])
            masked["relay_public_key_pem"] = f"{pem[:40]}…" if len(pem) > 40 else pem
        if table == "relay_registration_requests" and masked.get("relay_public_key_pem"):
            pem = str(masked["relay_public_key_pem"])
            masked["relay_public_key_pem"] = f"{pem[:40]}…" if len(pem) > 40 else pem
        return masked

    async def export_admin_database(
        self,
        *,
        row_limit: int = ADMIN_DB_ROW_LIMIT,
    ) -> dict[str, object]:
        tables: dict[str, object] = {}
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            for table in ADMIN_DB_TABLES:
                cursor = await db.execute(
                    f"SELECT COUNT(*) FROM {table}",
                )
                count_row = await cursor.fetchone()
                total = int(count_row[0]) if count_row is not None else 0
                cursor = await db.execute(
                    f"SELECT * FROM {table} LIMIT ?",
                    (row_limit,),
                )
                rows = await cursor.fetchall()
                tables[table] = {
                    "totalRows": total,
                    "truncated": total > len(rows),
                    "primaryKey": list(ADMIN_DB_PRIMARY_KEYS.get(table, ())),
                    "rows": [
                        self._mask_admin_row(table, {key: row[key] for key in row.keys()})
                        for row in rows
                    ],
                }
        return {"tables": tables, "rowLimit": row_limit}

    async def delete_admin_db_row(
        self,
        *,
        table: str,
        keys: dict[str, object],
    ) -> bool:
        if table not in ADMIN_DB_PRIMARY_KEYS:
            raise ValueError(f"table not deletable: {table}")
        pk_columns = ADMIN_DB_PRIMARY_KEYS[table]
        missing = [column for column in pk_columns if column not in keys]
        if missing:
            raise ValueError(f"missing primary key columns: {', '.join(missing)}")
        where = " AND ".join(f"{column} = ?" for column in pk_columns)
        values = [keys[column] for column in pk_columns]
        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                f"DELETE FROM {table} WHERE {where}",
                values,
            )
            await db.commit()
            return cursor.rowcount > 0


    async def purge_expired_tokens(self) -> int:
        now = utc_now_iso()
        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                "DELETE FROM token_relay_placements WHERE expiry_at <= ?",
                (now,),
            )
            await db.commit()
            return cursor.rowcount

    async def list_healthy_relays(self) -> list[HealthyRelay]:
        stale_before = (
            utc_now() - timedelta(seconds=self._config.relay_heartbeat_stale_seconds)
        ).isoformat()
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT rs.relay_id, rs.relay_base_url, rs.storage_rate,
                       rs.block_max_age_seconds, rs.block_sweep_interval_seconds
                FROM relay_states rs
                INNER JOIN registry_allowlist al ON al.relay_id = rs.relay_id
                WHERE al.enabled = 1 AND rs.last_heartbeat_at > ?
                ORDER BY rs.storage_rate ASC, rs.last_heartbeat_at DESC, rs.relay_id ASC
                """,
                (stale_before,),
            )
            rows = await cursor.fetchall()
            if rows:
                return [
                    HealthyRelay(
                        relay_id=str(row["relay_id"]),
                        relay_base_url=str(row["relay_base_url"]).rstrip("/"),
                        storage_rate=float(row["storage_rate"]),
                        block_max_age_seconds=int(row["block_max_age_seconds"]),
                        block_sweep_interval_seconds=int(row["block_sweep_interval_seconds"]),
                    )
                    for row in rows
                ]

            cursor = await db.execute(
                """
                SELECT relay_id, relay_base_url
                FROM registry_allowlist
                WHERE enabled = 1 AND relay_base_url IS NOT NULL
                ORDER BY relay_id ASC
                """,
            )
            rows = await cursor.fetchall()
            return [
                HealthyRelay(
                    relay_id=str(row["relay_id"]),
                    relay_base_url=str(row["relay_base_url"]).rstrip("/"),
                    storage_rate=0.0,
                    block_max_age_seconds=DEFAULT_BLOCK_MAX_AGE_SECONDS,
                    block_sweep_interval_seconds=DEFAULT_BLOCK_SWEEP_INTERVAL_SECONDS,
                )
                for row in rows
            ]

    def _row_to_placement(self, row: aiosqlite.Row) -> TokenPlacement:
        return TokenPlacement(
            token=str(row["token"]),
            relay_id=str(row["relay_id"]),
            relay_base_url=str(row["relay_base_url"]).rstrip("/"),
            role=str(row["role"]),
            block_hash=str(row["block_hash"]),
            expiry_at=str(row["expiry_at"]),
        )

    async def get_token_placements(self, token: str) -> list[TokenPlacement]:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT token, relay_id, relay_base_url, role, block_hash, expiry_at
                FROM token_relay_placements
                WHERE token = ?
                ORDER BY CASE role WHEN 'primary' THEN 0 ELSE 1 END, relay_id ASC
                """,
                (token,),
            )
            rows = await cursor.fetchall()
            return [self._row_to_placement(row) for row in rows]

    async def get_token_placement(
        self,
        token: str,
        relay_id: str,
    ) -> TokenPlacement | None:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT token, relay_id, relay_base_url, role, block_hash, expiry_at
                FROM token_relay_placements
                WHERE token = ? AND relay_id = ?
                """,
                (token, relay_id),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return self._row_to_placement(row)

    async def get_primary_placement(self, token: str) -> TokenPlacement | None:
        placements = await self.get_token_placements(token)
        return next((item for item in placements if item.role == "primary"), None)

    def _is_expired(self, expiry_at: str) -> bool:
        return parse_iso(expiry_at) <= utc_now()

    def is_placement_live(self, placement: TokenPlacement) -> bool:
        return not self._is_expired(placement.expiry_at)

    async def update_token_block_hash(self, token: str, block_hash: str) -> None:
        now = utc_now_iso()
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                UPDATE token_relay_placements
                SET block_hash = ?, updated_at = ?
                WHERE token = ?
                """,
                (block_hash, now, token),
            )
            await db.commit()

    def _placements_to_reserve_result(
        self,
        token: str,
        placements: list[TokenPlacement],
    ) -> TokenReserveResult:
        return TokenReserveResult(
            token=token,
            targets=tuple(
                RelayTarget(
                    role=item.role,
                    relay_id=item.relay_id,
                    relay_base_url=item.relay_base_url,
                )
                for item in placements
            ),
        )

    async def lock_tokens_with_block_hashes(
        self,
        entries: list[tuple[str, str]],
        *,
        ttl_seconds: int | None = None,
    ) -> LockTokensOutcome:
        await self.purge_expired_tokens()
        if not entries:
            return LockTokensOutcome(
                routes=(),
                granted_ttl_seconds=self._config.token_ttl_seconds,
                requested_ttl_seconds=self._config.token_ttl_seconds,
                degraded=False,
                stripe_count=0,
                replica_factor=0,
                ideal_relay_count=0,
                actual_relay_count=0,
            )

        unique: list[tuple[str, str]] = []
        seen: set[str] = set()
        for token, block_hash in entries:
            if token in seen:
                raise ValueError(f"duplicate token in reserve request: {token}")
            seen.add(token)
            unique.append((token, block_hash))

        occupied: list[dict[str, str | None]] = []
        for token, _block_hash in unique:
            primary = await self.get_primary_placement(token)
            if primary is not None and self.is_placement_live(primary):
                occupied.append(
                    {
                        "token": token,
                        "expiryAt": primary.expiry_at,
                        "blockHash": primary.block_hash,
                    },
                )

        if occupied:
            raise TokenOccupiedError(occupied)

        healthy = await self.list_healthy_relays()
        if not healthy:
            raise RuntimeError("no healthy relay available for upload assignment")

        requested_ttl = (
            ttl_seconds if ttl_seconds is not None else self._config.token_ttl_seconds
        )
        try:
            resolution = resolve_placement_with_ttl(
                unique,
                healthy,
                requested_ttl=requested_ttl,
                policy=self._config.placement_policy,
            )
        except InsufficientRelayCapacityError as exc:
            raise RuntimeError(str(exc)) from exc

        planned = resolution.placements
        now = utc_now()
        expiry = (now + timedelta(seconds=resolution.granted_ttl_seconds)).isoformat()
        now_iso = now.isoformat()

        async with aiosqlite.connect(self._database_path) as db:
            for planned_item in planned:
                await db.execute(
                    """
                    INSERT INTO token_relay_placements (
                        token, relay_id, relay_base_url, role,
                        block_hash, expiry_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(token, relay_id) DO UPDATE SET
                        relay_base_url = excluded.relay_base_url,
                        role = excluded.role,
                        block_hash = excluded.block_hash,
                        expiry_at = excluded.expiry_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        planned_item.token,
                        planned_item.relay_id,
                        planned_item.relay_base_url,
                        planned_item.role,
                        planned_item.block_hash,
                        expiry,
                        now_iso,
                    ),
                )
            await db.commit()

        locked: list[TokenReserveResult] = []
        for token, _block_hash in unique:
            placements = await self.get_token_placements(token)
            if not placements:
                raise RuntimeError(f"failed to lock token placements: {token}")
            locked.append(self._placements_to_reserve_result(token, placements))

        await self.record_token_reservation_batch(
            entries=unique,
            results=locked,
            ttl_seconds=resolution.granted_ttl_seconds,
        )
        return LockTokensOutcome(
            routes=tuple(locked),
            granted_ttl_seconds=resolution.granted_ttl_seconds,
            requested_ttl_seconds=resolution.requested_ttl_seconds,
            degraded=resolution.degraded,
            stripe_count=resolution.placement_plan.stripe_count,
            replica_factor=resolution.placement_plan.replica_factor,
            ideal_relay_count=resolution.ideal_relay_count,
            actual_relay_count=resolution.actual_relay_count,
        )

    async def get_resolve_targets(self, token: str) -> TokenReserveResult | None:
        placements = await self.get_token_placements(token)
        if not placements:
            return None

        primary = next((item for item in placements if item.role == "primary"), None)
        if primary is None or not self.is_placement_live(primary):
            return None

        healthy = await self.list_healthy_relays()
        healthy_rates = {item.relay_id: item.storage_rate for item in healthy}
        healthy_ids = set(healthy_rates)

        live_targets: list[RelayTarget] = []
        for item in placements:
            if item.relay_id not in healthy_ids:
                continue
            if not self.is_placement_live(item):
                continue
            live_targets.append(
                RelayTarget(
                    role=item.role,
                    relay_id=item.relay_id,
                    relay_base_url=item.relay_base_url,
                ),
            )

        if not any(target.role == "primary" for target in live_targets):
            return None

        live_targets.sort(
            key=lambda target: (
                0 if target.role == "primary" else 1,
                healthy_rates.get(target.relay_id, 1.0),
                target.relay_id,
            ),
        )
        return TokenReserveResult(
            token=token,
            targets=tuple(live_targets),
        )

    async def abandon_replica_placements(
        self,
        failures: list[tuple[str, str]],
    ) -> list[tuple[str, str]]:
        """Remove live replica placements; primary rows are never deleted."""
        if not failures:
            return []

        unique: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for token, relay_id in failures:
            if (token, relay_id) in seen:
                continue
            seen.add((token, relay_id))
            unique.append((token, relay_id))

        removed: list[tuple[str, str]] = []
        async with aiosqlite.connect(self._database_path) as db:
            for token, relay_id in unique:
                cursor = await db.execute(
                    """
                    DELETE FROM token_relay_placements
                    WHERE token = ? AND relay_id = ? AND role = 'replica'
                    """,
                    (token, relay_id),
                )
                if cursor.rowcount > 0:
                    removed.append((token, relay_id))
            removed_set = set(removed)
            now = utc_now_iso()
            for token, relay_id in unique:
                await db.execute(
                    """
                    INSERT INTO replica_abandon_events (
                        token_hash, token_prefix, relay_id, removed, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        _hash_token(token),
                        _token_prefix(token),
                        relay_id,
                        1 if (token, relay_id) in removed_set else 0,
                        now,
                    ),
                )
            await db.commit()

        return removed

    async def upsert_relay_state(
        self,
        *,
        relay_id: str,
        relay_base_url: str,
        status: str,
        stored_blocks: int,
        max_blocks: int,
        storage_rate: float,
        block_max_age_seconds: int = DEFAULT_BLOCK_MAX_AGE_SECONDS,
        block_sweep_interval_seconds: int = DEFAULT_BLOCK_SWEEP_INTERVAL_SECONDS,
    ) -> None:
        now = utc_now_iso()
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO relay_states (
                    relay_id, relay_base_url, status, stored_blocks,
                    max_blocks, storage_rate, last_heartbeat_at,
                    block_max_age_seconds, block_sweep_interval_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(relay_id) DO UPDATE SET
                    relay_base_url = excluded.relay_base_url,
                    status = excluded.status,
                    stored_blocks = excluded.stored_blocks,
                    max_blocks = excluded.max_blocks,
                    storage_rate = excluded.storage_rate,
                    last_heartbeat_at = excluded.last_heartbeat_at,
                    block_max_age_seconds = excluded.block_max_age_seconds,
                    block_sweep_interval_seconds = excluded.block_sweep_interval_seconds
                """,
                (
                    relay_id,
                    relay_base_url.rstrip("/"),
                    status,
                    stored_blocks,
                    max_blocks,
                    storage_rate,
                    now,
                    block_max_age_seconds,
                    block_sweep_interval_seconds,
                ),
            )
            await db.commit()

    async def get_active_registry_api_key(self, relay_id: str) -> RegistryApiKeyRecord | None:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT relay_id, key_id, remaining_uses, relay_public_key_pem
                FROM relay_registry_keys
                WHERE relay_id = ? AND remaining_uses > 0
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (relay_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return RegistryApiKeyRecord(
                relay_id=str(row["relay_id"]),
                key_id=str(row["key_id"]),
                remaining_uses=int(row["remaining_uses"]),
                relay_public_key_pem=str(row["relay_public_key_pem"])
                if row["relay_public_key_pem"]
                else None,
            )

    async def verify_registry_api_key(
        self,
        *,
        relay_id: str,
        key_id: str,
        registry_api_key: str,
        require_remaining: bool = True,
    ) -> RegistryApiKeyRecord | None:
        key_hash = hash_registry_api_key(registry_api_key)
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT relay_id, key_id, remaining_uses, relay_public_key_pem
                FROM relay_registry_keys
                WHERE relay_id = ? AND key_id = ? AND key_secret_hash = ?
                """,
                (relay_id, key_id, key_hash),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            remaining = int(row["remaining_uses"])
            if require_remaining and remaining <= 0:
                return None
            return RegistryApiKeyRecord(
                relay_id=str(row["relay_id"]),
                key_id=str(row["key_id"]),
                remaining_uses=remaining,
                relay_public_key_pem=str(row["relay_public_key_pem"])
                if row["relay_public_key_pem"]
                else None,
            )

    async def consume_registry_api_key_use(
        self,
        *,
        relay_id: str,
        key_id: str,
    ) -> int:
        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute(
                """
                UPDATE relay_registry_keys
                SET remaining_uses = remaining_uses - 1
                WHERE relay_id = ? AND key_id = ? AND remaining_uses > 0
                """,
                (relay_id, key_id),
            )
            await db.commit()
            if cursor.rowcount == 0:
                return 0
            cursor = await db.execute(
                """
                SELECT remaining_uses FROM relay_registry_keys
                WHERE relay_id = ? AND key_id = ?
                """,
                (relay_id, key_id),
            )
            row = await cursor.fetchone()
            return int(row[0]) if row is not None else 0

    async def create_registry_api_key(
        self,
        *,
        relay_id: str,
        relay_public_key_pem: str | None,
        remaining_uses: int | None = None,
    ) -> tuple[str, str, RegistryApiKeyRecord]:
        key_id = generate_registry_api_key()[:16]
        registry_api_key = generate_registry_api_key()
        uses = remaining_uses or self._config.registry_api_key_initial_uses
        now = utc_now_iso()
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO relay_registry_keys (
                    relay_id, key_id, key_secret_hash, remaining_uses,
                    relay_public_key_pem, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    relay_id,
                    key_id,
                    hash_registry_api_key(registry_api_key),
                    uses,
                    relay_public_key_pem,
                    now,
                ),
            )
            await db.commit()
        record = RegistryApiKeyRecord(
            relay_id=relay_id,
            key_id=key_id,
            remaining_uses=uses,
            relay_public_key_pem=relay_public_key_pem,
        )
        return key_id, registry_api_key, record

    async def rotate_registry_api_key(
        self,
        *,
        relay_id: str,
        relay_public_key_pem: str,
    ) -> tuple[str, str, NextRegistryApiKey]:
        new_key_id, new_key, _ = await self.create_registry_api_key(
            relay_id=relay_id,
            relay_public_key_pem=relay_public_key_pem,
        )
        encrypted = encrypt_registry_api_key_for_relay(relay_public_key_pem, new_key)
        return new_key_id, new_key, NextRegistryApiKey(
            key_id=new_key_id,
            encrypted_key=encrypted,
        )

    async def update_relay_public_key(self, relay_id: str, public_key_pem: str) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                UPDATE relay_registry_keys
                SET relay_public_key_pem = ?
                WHERE relay_id = ?
                """,
                (public_key_pem, relay_id),
            )
            await db.commit()

    async def get_active_block_auth_key(self, relay_id: str) -> BlockAuthKeyRecord | None:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT relay_id, key_id, status, created_at
                FROM relay_block_auth_keys
                WHERE relay_id = ? AND status = 'active'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (relay_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return BlockAuthKeyRecord(
                relay_id=str(row["relay_id"]),
                key_id=str(row["key_id"]),
                status=str(row["status"]),
                created_at=str(row["created_at"]),
            )

    async def create_block_auth_key(self, relay_id: str) -> tuple[str, bytes]:
        key_id = generate_registry_api_key()[:16]
        key_bytes = derive_block_auth_key(
            self._config.block_auth_master_key,
            relay_id=relay_id,
            key_id=key_id,
        )
        now = utc_now_iso()
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO relay_block_auth_keys (
                    relay_id, key_id, status, created_at
                ) VALUES (?, ?, 'active', ?)
                """,
                (relay_id, key_id, now),
            )
            await db.commit()
        return key_id, key_bytes

    async def rotate_block_auth_key(
        self,
        *,
        relay_id: str,
        relay_public_key_pem: str,
    ) -> NextBlockAuthKey:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                "DELETE FROM relay_block_auth_keys WHERE relay_id = ?",
                (relay_id,),
            )
            await db.commit()
        key_id, key_bytes = await self.create_block_auth_key(relay_id)
        encrypted = encrypt_secret_bytes_for_relay(relay_public_key_pem, key_bytes)
        return NextBlockAuthKey(key_id=key_id, encrypted_key=encrypted)
