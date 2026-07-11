from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from ..identity import RelayIdentityManager
from ..registry.block_auth_key_manager import BlockAuthKeyManager
from ..config import RelayServerConfig
from ..persistence.repository import BlockRepository, compute_block_hash
from ..persistence.disk_store import DiskBlockStore
from ..registry.client import RegistryClient


class BlockStoreError(Exception):
    def __init__(self, message: str, *, status_code: int = 500) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class StorageStats:
    stored_blocks: int
    max_blocks: int
    storage_rate: float


@dataclass(frozen=True)
class BlockSweepResult:
    expired_from_db: int
    orphan_files: int

    @property
    def total_removed(self) -> int:
        return self.expired_from_db + self.orphan_files


class BlockService:
    """按键值存取数据块；重复写入需经注册服务器验证。"""

    def __init__(
        self,
        *,
        config: RelayServerConfig,
        identity: RelayIdentityManager,
        repository: BlockRepository,
        disk_store: DiskBlockStore,
        registry: RegistryClient,
        block_auth_keys: BlockAuthKeyManager,
    ) -> None:
        self._config = config
        self._identity = identity
        self._repository = repository
        self._disk_store = disk_store
        self._registry = registry
        self._block_auth_keys = block_auth_keys

    async def initialize(self) -> None:
        self._disk_store.initialize()
        await self._repository.initialize()

    async def stats(self) -> StorageStats:
        stored = await self._repository.count()
        max_blocks = self._config.max_blocks
        rate = stored / max_blocks if max_blocks > 0 else 0.0
        return StorageStats(
            stored_blocks=stored,
            max_blocks=max_blocks,
            storage_rate=rate,
        )

    async def sweep_expired_blocks(self) -> BlockSweepResult:
        """按 blockMaxAgeSeconds 清理过期 DB 行与孤儿磁盘文件（与 Registry TTL 解耦）。"""
        max_age = self._config.block_max_age_seconds
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age)
        cutoff_iso = cutoff.isoformat()

        stale = await self._repository.list_stale_blocks(cutoff_iso)
        expired_count = 0
        for token, disk_path in stale:
            await self._disk_store.remove(disk_path)
            await self._repository.delete(token)
            expired_count += 1

        active_paths = set(await self._repository.list_disk_paths())
        orphan_count = await self._disk_store.sweep_orphan_files(
            active_relative_paths=active_paths,
            max_age_seconds=max_age,
        )
        await self._repository.record_block_sweep_run(
            expired_from_db=expired_count,
            orphan_files=orphan_count,
        )
        return BlockSweepResult(
            expired_from_db=expired_count,
            orphan_files=orphan_count,
        )

    async def get_block(self, token: str) -> bytes:
        record = await self._repository.get(token)
        if record is None:
            await self._repository.record_block_access(
                token=token,
                action="get",
                status="missing",
            )
            raise BlockStoreError("block not found", status_code=404)

        data = await self._disk_store.read(str(record["disk_path"]))
        if data is None:
            await self._repository.delete(token)
            await self._repository.record_block_access(
                token=token,
                action="get",
                status="missing_file",
                detail="database row existed but disk file was missing",
            )
            raise BlockStoreError("block not found", status_code=404)
        await self._repository.record_block_access(
            token=token,
            action="get",
            status="ok",
            size_bytes=len(data),
        )
        return data

    async def put_block(self, token: str, data: bytes) -> None:
        if len(data) == 0:
            await self._repository.record_block_access(
                token=token,
                action="put",
                status="rejected",
                size_bytes=0,
                detail="empty body",
            )
            raise BlockStoreError("empty body", status_code=400)
        if len(data) > self._config.max_body_bytes:
            await self._repository.record_block_access(
                token=token,
                action="put",
                status="rejected",
                size_bytes=len(data),
                detail="body exceeds maxBodyBytes",
            )
            raise BlockStoreError(
                f"body exceeds maxBodyBytes ({self._config.max_body_bytes})",
                status_code=413,
            )

        block_hash = compute_block_hash(data)
        existing = await self._repository.get(token)

        if existing is not None:
            try:
                await self._verify_overwrite(token=token, block_hash=block_hash)
            except BlockStoreError as exc:
                await self._repository.record_block_access(
                    token=token,
                    action="put_overwrite",
                    status="rejected",
                    size_bytes=len(data),
                    detail=str(exc),
                )
                raise
            disk_path = str(existing["disk_path"])
            try:
                await self._disk_store.write(token, data)
                await self._repository.update(
                    token=token,
                    block_hash=block_hash,
                    size_bytes=len(data),
                )
                await self._registry.register_block(token=token, block_hash=block_hash)
            except httpx.HTTPError as exc:
                if disk_path:
                    previous = await self._disk_store.read(disk_path)
                    if previous is not None:
                        await self._disk_store.write(token, previous)
                await self._repository.record_block_access(
                    token=token,
                    action="put_overwrite",
                    status="registry_failed",
                    size_bytes=len(data),
                    detail=str(exc),
                )
                raise BlockStoreError(
                    f"registry register failed: {exc}",
                    status_code=503,
                ) from exc
            except Exception:
                if disk_path:
                    previous = await self._disk_store.read(disk_path)
                    if previous is not None:
                        await self._disk_store.write(token, previous)
                await self._repository.record_block_access(
                    token=token,
                    action="put_overwrite",
                    status="failed",
                    size_bytes=len(data),
                )
                raise
            await self._repository.record_block_access(
                token=token,
                action="put_overwrite",
                status="ok",
                size_bytes=len(data),
            )
            return

        stored = await self._repository.count()
        if stored >= self._config.max_blocks:
            await self._repository.record_block_access(
                token=token,
                action="put_create",
                status="rejected",
                size_bytes=len(data),
                detail="relay storage capacity reached",
            )
            raise BlockStoreError("relay storage capacity reached", status_code=507)

        relative_path = self._disk_store.relative_disk_path(token)
        try:
            await self._disk_store.write(token, data)
            await self._repository.insert(
                token=token,
                disk_path=relative_path,
                block_hash=block_hash,
                size_bytes=len(data),
            )
            await self._registry.register_block(token=token, block_hash=block_hash)
        except httpx.HTTPError as exc:
            await self._disk_store.remove(relative_path)
            await self._repository.delete(token)
            await self._repository.record_block_access(
                token=token,
                action="put_create",
                status="registry_failed",
                size_bytes=len(data),
                detail=str(exc),
            )
            raise BlockStoreError(
                f"registry register failed: {exc}",
                status_code=503,
            ) from exc
        except Exception:
            await self._disk_store.remove(relative_path)
            await self._repository.delete(token)
            await self._repository.record_block_access(
                token=token,
                action="put_create",
                status="failed",
                size_bytes=len(data),
            )
            raise
        await self._repository.record_block_access(
            token=token,
            action="put_create",
            status="ok",
            size_bytes=len(data),
        )

    async def _verify_overwrite(self, *, token: str, block_hash: str) -> None:
        try:
            result = await self._registry.verify_overwrite(
                token=token,
                block_hash=block_hash,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {403, 404, 409}:
                raise BlockStoreError(
                    "overwrite verification rejected by registry",
                    status_code=409,
                ) from exc
            raise BlockStoreError(
                f"registry verify-overwrite failed: {exc}",
                status_code=503,
            ) from exc
        except Exception as exc:
            raise BlockStoreError(
                f"registry verify-overwrite failed: {exc}",
                status_code=503,
            ) from exc

        if result.block_hash != block_hash:
            raise BlockStoreError(
                "overwrite verification failed: block hash mismatch",
                status_code=409,
            )

        if result.block_auth_algorithm != "HMAC-SHA256-v1":
            raise BlockStoreError(
                "overwrite verification failed: unsupported block auth algorithm",
                status_code=409,
            )

        try:
            credentials = await self._block_auth_keys.credentials()
        except RuntimeError as exc:
            raise BlockStoreError(
                "blockAuthKey not ready for overwrite verification",
                status_code=503,
            ) from exc

        if credentials.block_auth_key_id != result.block_auth_key_id:
            raise BlockStoreError(
                "overwrite verification failed: block auth key mismatch",
                status_code=409,
            )

        assigned_relay_id = self._identity.relay_id
        if not assigned_relay_id:
            raise BlockStoreError(
                "relay id not assigned by registry",
                status_code=503,
            )
        if not verify_block_auth_mac(
            block_auth_key=credentials.block_auth_key,
            block_auth_key_id=result.block_auth_key_id,
            token=token,
            relay_id=assigned_relay_id,
            relay_base_url=self._config.public_base_url,
            block_hash=block_hash,
            expiry_at=result.expiry_at,
            mac_hex=result.block_auth_mac,
        ):
            raise BlockStoreError(
                "overwrite verification failed: block auth mac invalid or expired",
                status_code=409,
            )
