"""의존성 주입과 인증.

회원 상태: 승인 대기(pending) 계정은 `/v1/auth/*`만 쓸 수 있다 — `current_user_id`가 403 `account_pending`.

인증 순서: ① 세션 쿠키 `pi_session`(HttpOnly) → 유효하면 그 사용자. 쓰기 요청(POST/PUT/PATCH/DELETE)은
`X-CSRF-Token` 헤더가 세션의 토큰과 같아야 한다(403). ② 쿠키가 없고 `AUTH_ALLOW_HEADER_STUB`이면
`X-User-Id` 헤더를 그대로 사용자 id로 쓴다 — 개발·테스트·평가 스크립트용이며 배포에서는 끈다.
어디서도 요청 본문의 user_id를 신뢰하지 않는다 — 항상 이 의존성에서 받은 값을 쓴다.

저장소는 `DATABASE_URL`이 있으면 PostgreSQL, 비어 있으면 인메모리다 (테스트·A/B 실험용).
검색기: A/B는 없음, C는 벡터 kNN(문체), D는 C + 그래프 사실 맥락(NEO4J_URI가 비면 D = C).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from fastapi import Depends, Header, HTTPException, Request

from app.ai.embeddings import Embedder, build_embedder
from app.ai.providers import build_provider
from app.ai.retrieval.base import NullRetriever, Retriever
from app.ai.retrieval.composite import CompositeRetriever
from app.ai.retrieval.vector import VectorStyleRetriever
from app.ai.workflow import RewriteWorkflow
from app.core.config import Settings, get_settings
from app.core.errors import AccountPendingError, AccountSuspendedError
from app.domain.auth import Session
from app.repositories.auth import AuthRepo, InMemoryAuthRepo
from app.repositories.base import ContactRepo, ConversationRepo, RewriteRepo
from app.repositories.memory import InMemoryContactRepo, InMemoryConversationRepo, InMemoryRewriteRepo
from app.repositories.usage import InMemoryUsageRepo, UsageRepo
from app.services.auth_service import AuthService, ForbiddenError, UnauthorizedError
from app.services.deletion_service import DeletionService
from app.services.import_service import ImportService
from app.services.oauth import OAuthProvider, build_oauth_providers
from app.services.rewrite_service import RewriteService

SESSION_COOKIE = "pi_session"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

Storage = Literal["memory", "postgres"]


@dataclass
class AppState:
    settings: Settings
    contacts: ContactRepo
    rewrites: RewriteRepo
    conversations: ConversationRepo
    embedder: Embedder
    usage: UsageRepo
    auth: AuthRepo
    auth_service: AuthService
    rewrite_service: RewriteService
    import_service: ImportService
    deletion_service: DeletionService
    workflow: RewriteWorkflow  # 평가 하니스가 서비스 계층 없이 직접 돌린다
    storage: Storage = "memory"
    engine: Any = None  # sqlalchemy AsyncEngine | None — db extra 없이도 import되게 느슨히 둔다
    graph_store: Any = None  # app.graph.store.GraphStore | None — NEO4J_URI가 있을 때만
    graph_ingest: Any = None  # app.graph.ingest.GraphIngestService | None
    # 설정된 소셜 로그인 제공자 (google·kakao). 테스트는 여기에 가짜 제공자를 끼운다.
    oauth_providers: dict[str, OAuthProvider] = field(default_factory=dict)

    @property
    def graph_enabled(self) -> bool:
        return self.graph_store is not None

    async def close(self) -> None:
        await self.import_service.wait_graph_tasks()
        if self.graph_store is not None:
            await self.graph_store.close()
        if self.engine is not None:
            await self.engine.dispose()


def _build_repos(
    settings: Settings,
) -> tuple[ContactRepo, RewriteRepo, ConversationRepo, UsageRepo, AuthRepo, Storage, Any]:
    if not settings.database_url:
        return (
            InMemoryContactRepo(),
            InMemoryRewriteRepo(),
            InMemoryConversationRepo(),
            InMemoryUsageRepo(),
            InMemoryAuthRepo(),
            "memory",
            None,
        )
    # db extra(sqlalchemy, asyncpg)가 없는 설치에서도 인메모리 경로는 돌아가야 하므로 여기서만 import.
    from app.db.session import create_engine, create_session_factory
    from app.repositories.auth import PostgresAuthRepo
    from app.repositories.postgres import PostgresContactRepo, PostgresConversationRepo, PostgresRewriteRepo
    from app.repositories.usage import PostgresUsageRepo

    engine = create_engine(settings.database_url)
    sessions = create_session_factory(engine)
    return (
        PostgresContactRepo(sessions),
        PostgresRewriteRepo(sessions),
        PostgresConversationRepo(sessions),
        PostgresUsageRepo(sessions),
        PostgresAuthRepo(sessions),
        "postgres",
        engine,
    )


def _build_graph(settings: Settings, embedder: Any, contacts: ContactRepo, conversations: ConversationRepo):  # noqa: ANN202
    """NEO4J_URI가 있으면 (store, ingest, context_retriever), 없으면 (None, None, None).

    graph extra(graphiti-core)는 여기서만 import한다.
    """
    if not settings.neo4j_uri:
        return None, None, None
    # graphiti_core는 import 시점에 load_dotenv()를 불러 .env 전체를 os.environ에 올린다. 그대로 두면
    # 테스트의 Settings(_env_file=None)이 환경변수로 DATABASE_URL 등을 읽어 Postgres로 붙는다 (11-08).
    before = set(os.environ)
    try:
        from app.graph.client import build_graphiti
        from app.graph.ingest import GraphIngestService
        from app.graph.retriever import GraphContextRetriever
        from app.graph.store import GraphitiStore
    finally:
        for key in set(os.environ) - before:
            del os.environ[key]

    store = GraphitiStore(build_graphiti(settings, embedder))
    return (
        store,
        GraphIngestService(store, conversations, contacts),
        GraphContextRetriever(store, contacts, top_k=settings.graph_search_top_k, embedder=embedder),
    )


def build_state(settings: Settings) -> AppState:
    """앱 시작 시 한 번 만든다. 테스트는 이 함수를 직접 호출해 격리된 상태를 만든다."""
    contacts, rewrites, conversations, usage, auth, storage, engine = _build_repos(settings)
    embedder = build_embedder(settings)
    graph_store, graph_ingest, graph_retriever = _build_graph(settings, embedder, contacts, conversations)
    null = NullRetriever()
    vector = VectorStyleRetriever(conversations, embedder)
    # D = C(문체 예시) + 그래프 사실 맥락. 그래프가 없으면 C와 같다 (그때 D 수치는 비교에 쓰지 않는다).
    d_retriever: Retriever = CompositeRetriever([vector, graph_retriever]) if graph_retriever else vector
    workflow = RewriteWorkflow(
        provider=build_provider(settings),
        retrievers={"A": null, "B": null, "C": vector, "D": d_retriever},
        prompt_version=settings.prompt_version,
    )
    import_service = ImportService(
        contacts,
        conversations,
        embedder,
        max_messages=settings.max_import_messages,
        graph_ingest=graph_ingest,
        graph_mode="background",
    )
    return AppState(
        settings=settings,
        contacts=contacts,
        rewrites=rewrites,
        conversations=conversations,
        embedder=embedder,
        usage=usage,
        auth=auth,
        auth_service=AuthService(settings, auth),
        rewrite_service=RewriteService(settings, contacts, rewrites, workflow, usage),
        import_service=import_service,
        deletion_service=DeletionService(
            contacts, conversations, rewrites, import_service, usage, auth, graph_store
        ),
        workflow=workflow,
        storage=storage,
        engine=engine,
        graph_store=graph_store,
        graph_ingest=graph_ingest,
        oauth_providers=build_oauth_providers(settings),
    )


def get_state(request: Request) -> AppState:
    return request.app.state.container


async def current_session(request: Request, state: AppState) -> Session:
    """쿠키의 세션. 없거나 만료·폐기면 401."""
    session = await state.auth_service.resolve(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise UnauthorizedError("로그인이 필요합니다.")
    return session


def require_csrf(request: Request, state: AppState, session: Session) -> None:
    """쿠키 세션의 쓰기 요청은 `X-CSRF-Token`이 세션의 토큰과 같아야 한다 (403)."""
    if not state.auth_service.csrf_ok(session, request.headers.get(CSRF_HEADER)):
        raise ForbiddenError("CSRF 토큰이 없거나 맞지 않습니다 (X-CSRF-Token).")


async def current_user_id(
    request: Request, state: State, x_user_id: Annotated[str | None, Header()] = None
) -> str:
    """세션 쿠키(+CSRF) 우선, 없으면 개발용 헤더 스텁 (설정으로 끌 수 있다).

    `/v1/auth/*` 밖의 모든 라우트가 이 의존성을 거친다 — 승인 대기(pending) 계정은 여기서 403
    `account_pending`으로 막는다. 헤더 스텁 사용자는 계정이 없으므로 상태 검사가 없다.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        session = await state.auth_service.resolve(token)
        if session is None:
            raise UnauthorizedError("세션이 만료되었습니다. 다시 로그인하세요.")
        if request.method not in SAFE_METHODS and not state.auth_service.csrf_ok(
            session, request.headers.get(CSRF_HEADER)
        ):
            raise ForbiddenError("CSRF 토큰이 없거나 맞지 않습니다 (X-CSRF-Token).")
        user = await state.auth_service.me(session)
        if user.status == "pending":
            raise AccountPendingError("관리자 승인을 기다리는 계정입니다.")
        if user.status == "suspended":  # 정지 때 세션을 폐기하지만 한 번 더 막는다
            raise AccountSuspendedError("정지된 계정입니다. 관리자에게 문의하세요.")
        return str(session.user_id)
    if state.settings.auth_allow_header_stub and x_user_id and x_user_id.strip():
        return x_user_id.strip()
    raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "로그인이 필요합니다."})


State = Annotated[AppState, Depends(get_state)]
UserId = Annotated[str, Depends(current_user_id)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
