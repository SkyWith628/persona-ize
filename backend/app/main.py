"""FastAPI 앱 팩토리. `uvicorn app.main:app --reload`

`create_app` 순서: 로깅 수준 → 운영 모드 검사(`APP_ENV=production`이면 위험한 설정에서 멈춘다) →
미들웨어(`core/middleware.py`) → 저장소·서비스(`api/deps.build_state`) → 오류 모양 통일 → 라우터·화면.
요청 제한과 로그인 실패 제한은 프로세스 메모리다 — 워커 1개로 띄운다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import Scope

from app.api.deps import build_state
from app.api.routes import admin, auth, contacts, conversations, data, health, rewrites
from app.core.config import Settings, get_settings
from app.core.guard import enforce_production_guard
from app.core.http_errors import install_error_handlers
from app.core.logs import configure_logging
from app.core.middleware import build_middleware
from app.core.periodic import housekeeping_loop
from app.core.ratelimit import RateLimiter
from app.core.safe_exc import safe_exception_message
from app.core.text_guard import reject_unstorable_text

log = logging.getLogger(__name__)


WEB_DIR = Path(__file__).resolve().parent / "web"
SHUTDOWN_WAIT_SECONDS = 5.0


class PublicStaticFiles(StaticFiles):
    """화면 정적 파일(`/static`). 화면이 쓰지 않는 것은 내보내지 않는다:
    점으로 시작하는 파일·폴더(편집기·OS가 남긴 것)와 단위 테스트 폴더(`web/tests`)."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        parts = [part for part in path.replace("\\", "/").split("/") if part]
        if any(part.startswith(".") for part in parts) or parts[:1] == ["tests"]:
            raise HTTPException(status_code=404)
        return await super().get_response(path, scope)


async def _warm_up_embedder(container) -> None:  # noqa: ANN001
    """로컬 e5 모델을 미리 올린다. 첫 가져오기·C/D 첨삭이 10초 넘게 걸리던 것을 서버 시작 직후로 옮긴다."""
    try:
        await container.embedder.embed_query("워밍업")
        log.info("embedder warmed up: %s", container.embedder.version)
    except Exception as exc:  # noqa: BLE001 - 워밍업 실패는 서비스 장애가 아니다
        log.warning("embedder warm-up failed: %s", safe_exception_message(exc))


async def _resume_graph(container) -> None:  # noqa: ANN001
    """이전 프로세스가 끝내지 못한 그래프 수집을 이어받는다 (백그라운드, 실패해도 서비스는 뜬다)."""
    try:
        done = await container.graph_ingest.resume_all()
        if done:
            log.info("graph resume: %s", done)
    except Exception as exc:  # noqa: BLE001
        log.warning("graph resume failed: %s", safe_exception_message(exc))


async def _resume_imports(container) -> None:  # noqa: ANN001
    """이전 프로세스가 끝내지 못한 가져오기의 묶음 생성을 이어받는다 (모델 호출 없음)."""
    try:
        started = await container.import_service.resume_indexing()
        if started:
            log.info("import resume: %s", started)
    except Exception as exc:  # noqa: BLE001
        log.warning("import resume failed: %s", safe_exception_message(exc))


async def _warn_missing_admins(container) -> None:  # noqa: ANN001
    """운영 모드: `ADMIN_USERNAMES`의 이름으로 가입된 계정이 없으면 알린다.

    관리자 여부는 이름으로만 정한다. 운영에서는 그 이름을 공개 가입으로 만들 수 없으므로(선점 방지)
    서버에서 스크립트로 만들어야 한다.
    """
    settings = container.settings
    try:
        missing = [
            name
            for name in sorted(settings.admin_username_set)
            if not await container.auth.username_exists(name)
        ]
    except Exception as exc:  # noqa: BLE001 - 확인 실패가 시작을 막지 않는다
        log.warning("admin account check failed: %s", type(exc).__name__)
        return
    if not missing:
        return
    log.warning(
        "운영 설정 경고: 관리자 계정이 아직 없습니다(%s). 운영에서는 이 이름을 화면 가입으로 "
        "만들 수 없습니다 — 서버에서 `scripts/reset_password.py --username <이름> --create`로 만드세요.",
        ", ".join(missing),
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    container = app.state.container
    settings = container.settings
    log.info("env=%s storage=%s graph=%s", settings.app_env, container.storage, container.graph_enabled)
    tasks = [asyncio.create_task(_warm_up_embedder(container), name="warm-up-embedder")]
    tasks.append(asyncio.create_task(_resume_imports(container), name="import-resume"))
    if container.graph_enabled and settings.graph_resume_on_start:
        tasks.append(asyncio.create_task(_resume_graph(container), name="graph-resume"))
    if settings.housekeeping_interval_hours > 0:  # 주기 정리 (실패해도 서버는 산다 — core/periodic.py)
        periodic = housekeeping_loop(container, settings.housekeeping_interval_hours * 3600)
        tasks.append(asyncio.create_task(periodic, name="housekeeping"))
    if settings.is_production and settings.admin_username_set:
        tasks.append(asyncio.create_task(_warn_missing_admins(container), name="admin-check"))
    app.state.background_tasks = tasks
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.wait(tasks, timeout=SHUTDOWN_WAIT_SECONDS)
        await container.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    enforce_production_guard(settings)  # 운영 모드에서 위험한 설정이면 여기서 멈춘다 (저장소를 만들기 전)

    limiter = RateLimiter(settings.rate_limit_per_minute, settings.rate_limit_auth_per_minute)
    # 운영 모드에서는 API 문서(/docs·/redoc·/openapi.json)를 열지 않는다.
    docs = {"docs_url": None, "redoc_url": None, "openapi_url": None} if settings.is_production else {}
    app = FastAPI(
        title="Persona_ize API",
        version="0.1.0",
        lifespan=_lifespan,
        middleware=build_middleware(settings, limiter),
        # 저장할 수 없는 문자(NUL·짝 없는 대리 문자)는 어느 라우트든 저장 계층에 닿기 전에 422
        dependencies=[Depends(reject_unstorable_text)],
        **docs,
    )
    app.state.container = build_state(settings)
    app.state.rate_limiter = limiter  # 테스트가 clock을 바꿔 끼운다
    install_error_handlers(app)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(contacts.router)
    app.include_router(rewrites.router)
    app.include_router(rewrites.usage_router)
    app.include_router(conversations.router)
    app.include_router(data.router)
    app.include_router(admin.router)

    # 데모 화면: 빌드 없는 정적 파일 (app/web). React(apps/web)는 뒤로 미뤘다 (decisions 11-09).
    app.mount("/static", PublicStaticFiles(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        icon = WEB_DIR / "favicon.svg"
        if icon.is_file():
            return FileResponse(icon, media_type="image/svg+xml")
        return Response(status_code=204)  # 아이콘이 없어도 브라우저의 자동 요청이 404 로그를 남기지 않게

    # 화면 주소: /admin(운영 대시보드), /privacy·/terms(안내 문서). 파일이 아직 없으면 404.
    def _page(filename: str):  # noqa: ANN202
        async def page() -> FileResponse:
            path = WEB_DIR / filename
            if not path.is_file():
                raise HTTPException(
                    status_code=404, detail={"code": "not_found", "message": "없는 화면입니다."}
                )
            return FileResponse(path)

        return page

    for route, filename in (("/admin", "admin.html"), ("/privacy", "privacy.html"), ("/terms", "terms.html")):
        app.add_api_route(route, _page(filename), methods=["GET"], include_in_schema=False)

    return app


app = create_app()
