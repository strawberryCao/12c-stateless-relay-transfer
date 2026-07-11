from __future__ import annotations

import logging

import aiosqlite
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..config import RelayServerConfig, load_config
from ..domain.blocks import BlockService, BlockStoreError
from ..persistence.disk_store import DiskBlockStore
from ..persistence.repository import BlockRepository
from ..registry.api_key_manager import RegistryApiKeyManager
from ..registry.block_auth_key_manager import BlockAuthKeyManager
from ..registry.client import RegistryClient
from ..registry.connectivity import registry_connectivity_snapshot
from ..identity import RelayIdentityManager
from ..runtime.background import (
    run_block_sweep,
    run_startup_auto_registration,
    start_background_tasks,
    stop_background_tasks,
)
from .deps import normalize_token
from .routes import create_admin_router

logger = logging.getLogger(__name__)


def create_app(config: RelayServerConfig | None = None) -> FastAPI:
    settings = config or load_config()
    identity = RelayIdentityManager(settings.secrets_dir)
    identity.load()
    repository = BlockRepository(settings.database_path)
    disk_store = DiskBlockStore(settings.data_dir)
    registry_api_keys = RegistryApiKeyManager(
        relay_rsa_key_path=settings.relay_rsa_key_path,
        registry_api_key_store_path=settings.registry_api_key_store_path,
        initial_remaining_uses=settings.registry_api_key_initial_uses,
    )
    block_auth_keys = BlockAuthKeyManager(
        relay_rsa_key_path=settings.relay_rsa_key_path,
        block_auth_key_store_path=settings.block_auth_key_store_path,
    )
    registry = RegistryClient(
        base_url=settings.registry.url,
        identity=identity,
        relay_base_url=settings.public_base_url,
        registry_api_keys=registry_api_keys,
        block_auth_keys=block_auth_keys,
        http_proxy=settings.registry.http_proxy,
        block_max_age_seconds=settings.block_max_age_seconds,
        block_sweep_interval_seconds=settings.block_sweep_interval_seconds,
    )
    blocks = BlockService(
        config=settings,
        identity=identity,
        repository=repository,
        disk_store=disk_store,
        registry=registry,
        block_auth_keys=block_auth_keys,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await blocks.initialize()
        await run_startup_auto_registration(identity, blocks, registry, settings)
        try:
            await registry.ensure_secrets()
            logger.info(
                "relay secrets ready install=%s relayId=%s registryApiKey=%s blockAuthKey=%s",
                identity.install_id,
                identity.relay_id or "unassigned",
                registry_api_keys.has_registry_api_key,
                block_auth_keys.has_block_auth_key,
            )
        except Exception:
            logger.exception("relay secrets bootstrap failed; will retry after assignment")

        await run_block_sweep(blocks, settings)
        registry_task, sweep_task = start_background_tasks(
            identity,
            blocks,
            registry,
            settings,
        )
        yield
        await stop_background_tasks(registry_task, sweep_task)
        await registry.close()

    app = FastAPI(
        title="12C Stateless Relay",
        description="按 token 存取 opaque blob；Registry 通信使用 registryApiKey",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allowed_origins),
        allow_methods=["GET", "PUT", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    app.include_router(
        create_admin_router(
            config=settings,
            identity=identity,
            repository=repository,
            blocks=blocks,
            registry=registry,
        ),
    )

    @app.get("/health")
    async def health() -> dict[str, object]:
        try:
            stats = await blocks.stats()
            db_ready = settings.database_path.is_file()
            if db_ready:
                async with aiosqlite.connect(settings.database_path) as db:
                    await db.execute("SELECT 1")
            else:
                db_ready = False
        except Exception as exc:
            raise HTTPException(status_code=503, detail="database not ready") from exc
        if not db_ready:
            raise HTTPException(status_code=503, detail="database not ready")
        return {
            "status": "ok",
            "dbReady": True,
            "installId": identity.install_id,
            "relayId": identity.relay_id,
            "assignmentStatus": "assigned" if identity.is_assigned else "unassigned",
            "publicBaseUrl": settings.public_base_url,
            "storedBlocks": stats.stored_blocks,
            "maxBlocks": stats.max_blocks,
            "storageRate": stats.storage_rate,
            "blockMaxAgeSeconds": settings.block_max_age_seconds,
            "blockSweepIntervalSeconds": settings.block_sweep_interval_seconds,
            "heartbeatIntervalSeconds": settings.heartbeat_interval_seconds,
            "registryApiKeyReady": registry_api_keys.has_registry_api_key,
            "blockAuthKeyReady": block_auth_keys.has_block_auth_key,
            **registry_connectivity_snapshot(),
        }

    @app.put("/{token}")
    async def put_block(token: str, request: Request) -> Response:
        normalized = normalize_token(token)
        body = await request.body()
        try:
            await blocks.put_block(normalized, body)
        except BlockStoreError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return Response(status_code=204)

    @app.get("/{token}")
    async def get_block(token: str) -> Response:
        normalized = normalize_token(token)
        try:
            data = await blocks.get_block(normalized)
        except BlockStoreError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return Response(content=data, media_type="application/octet-stream")

    @app.exception_handler(HTTPException)
    async def http_exception_handler(
        _: Request,
        exc: HTTPException,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )

    app.state.config = settings
    app.state.identity = identity
    app.state.blocks = blocks
    app.state.registry_api_keys = registry_api_keys
    app.state.block_auth_keys = block_auth_keys
    return app


app = create_app()
