from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_block_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token_prefix(token: str) -> str:
    return token[:8]


ADMIN_DB_TABLES: tuple[str, ...] = (
    "blocks",
    "block_access_events",
    "block_sweep_runs",
)

ADMIN_DB_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "blocks": ("token",),
    "block_access_events": ("event_id",),
    "block_sweep_runs": ("run_id",),
}


class BlockRepository:
    """token ↔ 磁盘路径 映射（关系型 SQLite）。"""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path

    async def initialize(self) -> None:
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS blocks (
                    token TEXT PRIMARY KEY,
                    disk_path TEXT NOT NULL UNIQUE,
                    block_hash TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
            )
            # 只记录访问结果和 token 摘要，避免把文件内容或文件名写入数据库。
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS block_access_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_hash TEXT NOT NULL,
                    token_prefix TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    size_bytes INTEGER,
                    detail TEXT,
                    created_at TEXT NOT NULL
                )
                """,
            )
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS block_sweep_runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    expired_from_db INTEGER NOT NULL,
                    orphan_files INTEGER NOT NULL,
                    total_removed INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                )
                """,
            )
            await db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_block_access_events_time
                ON block_access_events(created_at)
                """,
            )
            await db.commit()

    async def get(self, token: str) -> dict[str, object] | None:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT token, disk_path, block_hash, size_bytes, created_at, updated_at
                FROM blocks
                WHERE token = ?
                """,
                (token,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return dict(row)

    async def count(self) -> int:
        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM blocks")
            row = await cursor.fetchone()
            return int(row[0]) if row is not None else 0

    async def insert(
        self,
        *,
        token: str,
        disk_path: str,
        block_hash: str,
        size_bytes: int,
    ) -> None:
        now = utc_now_iso()
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO blocks (
                    token, disk_path, block_hash, size_bytes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (token, disk_path, block_hash, size_bytes, now, now),
            )
            await db.commit()

    async def update(
        self,
        *,
        token: str,
        block_hash: str,
        size_bytes: int,
    ) -> None:
        now = utc_now_iso()
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                UPDATE blocks
                SET block_hash = ?, size_bytes = ?, updated_at = ?
                WHERE token = ?
                """,
                (block_hash, size_bytes, now, token),
            )
            await db.commit()

    async def delete(self, token: str) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute("DELETE FROM blocks WHERE token = ?", (token,))
            await db.commit()

    async def list_stale_blocks(self, updated_before: str) -> list[tuple[str, str]]:
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT token, disk_path
                FROM blocks
                WHERE updated_at <= ?
                """,
                (updated_before,),
            )
            rows = await cursor.fetchall()
            return [(str(row["token"]), str(row["disk_path"])) for row in rows]

    async def list_disk_paths(self) -> list[str]:
        async with aiosqlite.connect(self._database_path) as db:
            cursor = await db.execute("SELECT disk_path FROM blocks")
            rows = await cursor.fetchall()
            return [str(row[0]) for row in rows]

    async def record_block_access(
        self,
        *,
        token: str,
        action: str,
        status: str,
        size_bytes: int | None = None,
        detail: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO block_access_events (
                    token_hash, token_prefix, action, status,
                    size_bytes, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _hash_token(token),
                    _token_prefix(token),
                    action,
                    status,
                    size_bytes,
                    detail,
                    utc_now_iso(),
                ),
            )
            await db.commit()

    async def record_block_sweep_run(
        self,
        *,
        expired_from_db: int,
        orphan_files: int,
    ) -> None:
        total = expired_from_db + orphan_files
        async with aiosqlite.connect(self._database_path) as db:
            await db.execute(
                """
                INSERT INTO block_sweep_runs (
                    expired_from_db, orphan_files, total_removed, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (expired_from_db, orphan_files, total, utc_now_iso()),
            )
            await db.commit()

    async def export_admin_database(self, *, row_limit: int = 500) -> dict[str, object]:
        order_by = {
            "blocks": "updated_at DESC",
            "block_access_events": "event_id DESC",
            "block_sweep_runs": "run_id DESC",
        }
        tables: dict[str, object] = {}
        async with aiosqlite.connect(self._database_path) as db:
            db.row_factory = aiosqlite.Row
            for table in ADMIN_DB_TABLES:
                cursor = await db.execute(f"SELECT COUNT(*) FROM {table}")
                count_row = await cursor.fetchone()
                total = int(count_row[0]) if count_row is not None else 0
                cursor = await db.execute(
                    f"SELECT * FROM {table} ORDER BY {order_by[table]} LIMIT ?",
                    (row_limit,),
                )
                rows = await cursor.fetchall()
                table_rows = [{key: row[key] for key in row.keys()} for row in rows]
                tables[table] = {
                    "totalRows": total,
                    "truncated": total > len(table_rows),
                    "primaryKey": list(ADMIN_DB_PRIMARY_KEYS[table]),
                    "rows": table_rows,
                }
        return {"rowLimit": row_limit, "tables": tables}

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
