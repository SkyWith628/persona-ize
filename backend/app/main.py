"""FastAPI 앱 팩토리. `uvicorn app.main:app --reload`"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.deps import build_state
from app.api.routes import contacts, conversations, health, rewrites
from app.core.config import Settings, get_settings
from app.core.errors import DomainError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    log.info("storage=%s", app.state.container.storage)
    try:
        yield
    finally:
        await app.state.container.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Persona_ize API", version="0.1.0", lifespan=_lifespan)
    app.state.container = build_state(settings)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://localhost:3000"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(DomainError)
    async def _domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code, "message": exc.message})

    app.include_router(health.router)
    app.include_router(contacts.router)
    app.include_router(rewrites.router)
    app.include_router(rewrites.usage_router)
    app.include_router(conversations.router)
    return app


app = create_app()
