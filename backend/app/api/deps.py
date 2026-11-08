"""의존성 주입과 개발용 인증.

인증은 MVP에서 `X-User-Id` 헤더 스텁이다 (3주차에 세션 쿠키 + CSRF로 교체).
어디서도 요청 본문의 user_id를 신뢰하지 않는다 — 항상 이 의존성에서 받은 값을 쓴다.

저장소는 `DATABASE_URL`이 있으면 PostgreSQL, 비어 있으면 인메모리다 (테스트·A/B 실험용).
검색기: A/B는 없음, C는 벡터 kNN(문체), D는 아직 C와 같다 (3주차에 그래프 맥락을 더한다).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import Depends, Header, HTTPException, Request

from app.ai.embeddings import Embedder, build_embedder
from app.ai.providers import build_provider
from app.ai.retrieval.base import NullRetriever
from app.ai.retrieval.vector import VectorStyleRetriever
from app.ai.workflow import RewriteWorkflow
from app.core.config import Settings, get_settings
from app.repositories.base import ContactRepo, ConversationRepo, RewriteRepo
from app.repositories.memory import InMemoryContactRepo, InMemoryConversationRepo, InMemoryRewriteRepo
from app.services.import_service import ImportService
from app.services.rewrite_service import RewriteService

Storage = Literal["memory", "postgres"]


@dataclass
class AppState:
    settings: Settings
    contacts: ContactRepo
    rewrites: RewriteRepo
    conversations: ConversationRepo
    embedder: Embedder
    rewrite_service: RewriteService
    import_service: ImportService
    workflow: RewriteWorkflow  # 평가 하니스가 서비스 계층 없이 직접 돌린다
    storage: Storage = "memory"
    engine: Any = None  # sqlalchemy AsyncEngine | None — db extra 없이도 import되게 느슨히 둔다

    async def close(self) -> None:
        if self.engine is not None:
            await self.engine.dispose()


def _build_repos(settings: Settings) -> tuple[ContactRepo, RewriteRepo, ConversationRepo, Storage, Any]:
    if not settings.database_url:
        return InMemoryContactRepo(), InMemoryRewriteRepo(), InMemoryConversationRepo(), "memory", None
    # db extra(sqlalchemy, asyncpg)가 없는 설치에서도 인메모리 경로는 돌아가야 하므로 여기서만 import.
    from app.db.session import create_engine, create_session_factory
    from app.repositories.postgres import PostgresContactRepo, PostgresConversationRepo, PostgresRewriteRepo

    engine = create_engine(settings.database_url)
    sessions = create_session_factory(engine)
    return (
        PostgresContactRepo(sessions),
        PostgresRewriteRepo(sessions),
        PostgresConversationRepo(sessions),
        "postgres",
        engine,
    )


def build_state(settings: Settings) -> AppState:
    """앱 시작 시 한 번 만든다. 테스트는 이 함수를 직접 호출해 격리된 상태를 만든다."""
    contacts, rewrites, conversations, storage, engine = _build_repos(settings)
    embedder = build_embedder(settings)
    null = NullRetriever()
    vector = VectorStyleRetriever(conversations, embedder)
    workflow = RewriteWorkflow(
        provider=build_provider(settings),
        # D는 3주차에 C + 그래프 맥락으로 바뀐다. 그때까지는 C와 같아 D 실험은 의미가 없다.
        retrievers={"A": null, "B": null, "C": vector, "D": vector},
        prompt_version=settings.prompt_version,
    )
    return AppState(
        settings=settings,
        contacts=contacts,
        rewrites=rewrites,
        conversations=conversations,
        embedder=embedder,
        rewrite_service=RewriteService(settings, contacts, rewrites, workflow),
        import_service=ImportService(
            contacts, conversations, embedder, max_messages=settings.max_import_messages
        ),
        workflow=workflow,
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
