from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from relay_server.config import RegistryConfig, RelayServerConfig
from relay_server.domain.blocks import BlockService, BlockStoreError
from relay_server.persistence.disk_store import DiskBlockStore
from relay_server.persistence.repository import BlockRepository


TOKEN = "d" * 64
MISSING_TOKEN = "e" * 64


class BlockAuditTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.repository = BlockRepository(self.root / "relay.db")
        self.disk_store = DiskBlockStore(self.root / "blocks")
        self.registry = MagicMock()
        self.registry.register_block = AsyncMock()
        self.identity = MagicMock()
        self.identity.relay_id = "relay-a"
        self.config = RelayServerConfig(
            host="127.0.0.1",
            port=9090,
            public_base_url="http://relay.test",
            max_body_bytes=1024,
            max_blocks=10,
            data_dir=self.root / "blocks",
            database_path=self.root / "relay.db",
            heartbeat_interval_seconds=30,
            registry=RegistryConfig(url="http://registry.test"),
            secrets_dir=self.root / "secrets",
            relay_rsa_key_path=self.root / "secrets" / "relay_rsa.pem",
            registry_api_key_store_path=self.root / "secrets" / "registry_api_key.json",
            registry_api_key_initial_uses=10,
            block_auth_key_store_path=self.root / "secrets" / "block_auth_key.json",
            block_max_age_seconds=60,
            block_sweep_interval_seconds=3600,
        )
        self.service = BlockService(
            config=self.config,
            identity=self.identity,
            repository=self.repository,
            disk_store=self.disk_store,
            registry=self.registry,
            block_auth_keys=MagicMock(),
        )
        await self.service.initialize()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def _count(self, table: str) -> int:
        with sqlite3.connect(self.root / "relay.db") as db:
            row = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row is not None else 0

    async def test_relay_initializes_three_project_tables(self) -> None:
        with sqlite3.connect(self.root / "relay.db") as db:
            names = {
                str(row[0])
                for row in db.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    """,
                ).fetchall()
            }

        self.assertTrue(
            {"blocks", "block_access_events", "block_sweep_runs"}.issubset(names),
        )

    async def test_put_get_and_missing_get_write_access_events(self) -> None:
        await self.service.put_block(TOKEN, b"payload")
        self.assertEqual(await self.service.get_block(TOKEN), b"payload")

        with self.assertRaises(BlockStoreError):
            await self.service.get_block(MISSING_TOKEN)

        with sqlite3.connect(self.root / "relay.db") as db:
            rows = db.execute(
                """
                SELECT action, status, size_bytes
                FROM block_access_events
                ORDER BY event_id ASC
                """,
            ).fetchall()

        self.assertEqual(
            [(row[0], row[1]) for row in rows],
            [("put_create", "ok"), ("get", "ok"), ("get", "missing")],
        )
        self.assertEqual(rows[0][2], 7)
        self.assertEqual(rows[1][2], 7)

    async def test_sweep_records_run_summary(self) -> None:
        result = await self.service.sweep_expired_blocks()

        self.assertEqual(result.total_removed, 0)
        self.assertEqual(self._count("block_sweep_runs"), 1)
