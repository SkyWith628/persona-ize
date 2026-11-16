"""FastAPI 앱 팩토리. `uvicorn app.main:app --reload`"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.deps import build_state
from app.api.routes import admin, auth, contacts, conversations, health, rewrites
from app.core.config import Settings, get_settings
from app.core.errors import DomainError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


WEB_DIR = Path(__file__).resolve().parent / "web"


async def _warm_up_embedder(container) -> None:  # noqa: ANN001
    """로컬 e5 모델을 미리 올린다. 첫 가져오기·C/D 첨삭이 10초 넘게 걸리던 것을 서버 시작 직후로 옮긴다."""
    try:
        await container.embedder.embed_query("워밍업")
        log.info("embedder warmed up: %s", container.embedder.version)
    except Exception as exc:  # noqa: BLE001 - 워밍업 실패는 서비스 장애가 아니다
        log.warning("embedder warm-up failed: %s", exc)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    container = app.state.container
    log.info("storage=%s graph=%s", container.storage, container.graph_enabled)
    tasks = [asyncio.create_task(_warm_up_embedder(container))]
    if container.graph_enabled and container.settings.graph_resume_on_start:
        tasks.append(asyncio.create_task(_resume_graph(container)))
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await container.close()


async def _resume_graph(container) -> None:  # noqa: ANN001
    """이전 프로세스가 끝내지 못한 그래프 수집을 이어받는다 (백그라운드, 실패해도 서비스는 뜬다)."""
    try:
        done = await container.graph_ingest.resume_all()
        if done:
            log.info("graph resume: %s", done)
    except Exception as exc:  # noqa: BLE001
        log.warning("graph resume failed: %s", exc)


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
    app.include_router(auth.router)
    app.include_router(contacts.router)
    app.include_router(rewrites.router)
    app.include_router(rewrites.usage_router)
    app.include_router(conversations.router)
    app.include_router(admin.router)

    # 화면: 빌드 없는 정적 파일 (app/web). `/`는 와이어프레임 v2 기준 모바일 웹, `/manage`는 실험용 관리 화면
    # (조건 선택·근거 id), `/admin`은 운영 대시보드. HTML은 누구나 받지만 API는 로그인·권한이 있어야 열린다.
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    pages = {"/": "mobile.html", "/manage": "index.html", "/admin": "admin.html"}

    def _page(name: str):  # noqa: ANN202
        async def page() -> FileResponse:
            return FileResponse(WEB_DIR / name)

        return page

    for path, name in pages.items():
        app.add_api_route(path, _page(name), include_in_schema=False)

    return app


app = create_app()
