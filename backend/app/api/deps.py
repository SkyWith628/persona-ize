"""의존성 주입과 개발용 인증.

인증은 MVP에서 `X-User-Id` 헤더 스텁이다 (3주차에 세션 쿠키 + CSRF로 교체).
어디서도 요청 본문의 user_id를 신뢰하지 않는다 — 항상 이 의존성에서 받은 값을 쓴다.

저장소는 `DATABASE_URL`이 있으면 PostgreSQL, 비어 있으면 인메모리다 (테스트·A/B 실험용).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import Depends, Header, HTTPException, Request

from app.ai.providers import build_provider
from app.ai.retrieval.base import NullRetriever
from app.ai.workflow import RewriteWorkflow
from app.core.config import Settings, get_settings
from app.repositories.base import ContactRepo, RewriteRepo
from app.repositories.memory import InMemoryContactRepo, InMemoryRewriteRepo
from app.services.rewrite_service import RewriteService

Storage = Literal["memory", "postgres"]


@dataclass
class AppState:
    settings: Settings
    contacts: ContactRepo
    rewrites: RewriteRepo
    rewrite_service: RewriteService
    storage: Storage = "memory"
    engine: Any = None  # sqlalchemy AsyncEngine | None — db extra 없이도 import되게 느슨히 둔다

    async def close(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()


def _build_repos(settings: Settings) -> tuple[ContactRepo, RewriteRepo, Storage, Any]:
    if not settings.database_url:
        return InMemoryContactRepo(), InMemoryRewriteRepo(), "memory", None
    # db extra(sqlalchemy, asyncpg)가 없는 설치에서도 인메모리 경로는 돌아가야 하므로 여기서만 import.
    from app.db.session import create_engine, create_session_factory
    from app.repositories.postgres import PostgresContactRepo, PostgresRewriteRepo

    engine = create_engine(settings.database_url)
    sessions = create_session_factory(engine)
    return PostgresContactRepo(sessions), PostgresRewriteRepo(sessions), "postgres", engine


def build_state(settings: Settings) -> AppState:
    """앱 시작 시 한 번 만든다. 테스트는 이 함수를 직접 호출해 격리된 상태를 만든다."""
    contacts, rewrites, storage, engine = _build_repos(settings)
    null = NullRetriever()
    workflow = RewriteWorkflow(
        provider=build_provider(settings),
        # C/D는 2~3주차에 교체. 지금은 검색 없이 동작해 A/B 실험만 가능하다.
        retrievers={"A": null, "B": null, "C": null, "D": null},
        prompt_version=settings.prompt_version,
    )
    return AppState(
        settings=settings,
        contacts=contacts,
        rewrites=rewrites,
        rewrite_service=RewriteService(settings, contacts, rewrites, workflow),
        storage=storage,
        engine=engine,
    )


def get_state(request: Request) -> AppState:
    return request.app.state.container


def current_user_id(x_user_id: Annotated[str | None, Header()] = None) -> str:
    if not x_user_id or not x_user_id.strip():
        raise HTTPException(
            status_code=401, detail={"code": "unauthorized", "message": "X-User-Id 헤더가 필요합니다."}
        )
    return x_user_id.strip()


State = Annotated[AppState, Depends(get_state)]
UserId = Annotated[str, Depends(current_user_id)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
