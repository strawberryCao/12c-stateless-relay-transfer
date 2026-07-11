from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiosqlite
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from ..config import RegistryServerConfig, load_config
from ..persistence.repository import RegistryRepository
from ..services.registry import RegistryService
from .routes import create_admin_router, create_relay_router


def create_app(config: RegistryServerConfig | None = None) -> FastAPI:
    settings = config or load_config()
    repository = RegistryRepository(settings.database_path, settings)
    service = RegistryService(repository)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await repository.initialize()
        yield

    app = FastAPI(title="12C Registry", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allowed_origins),
        allow_methods=["POST", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    @app.get("/health")
    async def health() -> dict[str, object]:
        db_ready = settings.database_path.is_file()
        if db_ready:
            try:
                async with aiosqlite.connect(settings.database_path) as db:
                    await db.execute("SELECT 1")
            except Exception:
                db_ready = False
        if not db_ready:
            raise HTTPException(status_code=503, detail="database not ready")
        return {"status": "ok", "dbReady": True}

    app.include_router(create_relay_router(service))
    app.include_router(create_admin_router(service))

    app.state.config = settings
    app.state.repository = repository
    app.state.service = service
    return app


app = create_app()
