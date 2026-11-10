"""의존성 주입과 인증.

인증 순서: ① `Authorization: Bearer <토큰>`(비브라우저 클라이언트 — `POST /v1/auth/token`이 발급) → 유효하면
그 사용자, 아니면 401. 브라우저가 알아서 붙이는 값이 아니므로 CSRF 검사를 하지 않는다. ② 세션 쿠키
`pi_session`(HttpOnly) → 유효하면 그 사용자. 쓰기 요청(POST/PUT/PATCH/DELETE)은 `X-CSRF-Token` 헤더가
세션의 토큰과 같아야 한다(403). ③ 둘 다 없고 `AUTH_ALLOW_HEADER_STUB`이면 `X-User-Id` 헤더를 그대로
사용자 id로 쓴다 — 개발·테스트·평가 스크립트용이며 배포에서는 끈다(운영 모드는 켜진 채로 시작하지 않는다).
CSRF를 건너뛰는 것은 **세션을 Bearer 헤더로 찾았을 때뿐**이다 — Bearer 헤더가 있으면 쿠키는 보지 않는다.
어디서도 요청 본문의 user_id를 신뢰하지 않는다 — 항상 이 의존성에서 받은 값을 쓴다.

저장소는 `DATABASE_URL`이 있으면 PostgreSQL, 비어 있으면 인메모리다 (테스트·A/B 실험용).
검색기: A/B는 없음, C는 벡터 kNN(문체), D는 C + 그래프 사실 맥락(NEO4J_URI가 비면 D = C).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import Depends, Header, HTTPException, Request

from app.ai.embeddings import Embedder, build_embedder
from app.ai.providers import build_provider
from app.ai.retrieval.base import NullRetriever, Retriever
from app.ai.retrieval.composite import CompositeRetriever
from app.ai.retrieval.vector import VectorStyleRetriever
from app.ai.workflow import RewriteWorkflow
from app.core.config import Settings
from app.domain.auth import Session
from app.repositories.auth import AuthRepo, InMemoryAuthRepo
from app.repositories.base import ContactRepo, ConversationRepo, RewriteRepo
from app.repositories.memory import InMemoryContactRepo, InMemoryConversationRepo, InMemoryRewriteRepo
from app.repositories.usage import InMemoryUsageRepo, UsageRepo
from app.services.auth_service import AuthService, ForbiddenError, UnauthorizedError
from app.services.deletion_service import DeletionService
from app.services.import_service import ImportService
from app.services.rewrite_service import RewriteService

SESSION_COOKIE = "pi_session"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
BEARER_SCHEME = "bearer"
AUTH_VIA_BEARER = "bearer"  # request.state.auth_via — 이 요청의 세션을 어디서 찾았는가
AUTH_VIA_COOKIE = "cookie"
MAX_STUB_USER_ID_CHARS = 128  # 개발용 X-User-Id 스텁이 받는 길이 (모든 테이블의 user_id 열 길이)

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
        GraphIngestService(
            store,
            conversations,
            contacts,
            user_message_cap=settings.graph_ingest_user_message_cap,
            daily_global_cap=settings.graph_ingest_daily_global_cap,
            day_offset_hours=settings.usage_day_offset_hours,
        ),
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
        rewrite_service=RewriteService(
            settings,
            contacts,
            rewrites,
            workflow,
            usage,
            conversations=conversations,
            graph_enabled=graph_store is not None,
        ),
        import_service=import_service,
        deletion_service=DeletionService(
            contacts, conversations, rewrites, import_service, usage, auth, graph_store
        ),
        workflow=workflow,
        storage=storage,
        engine=engine,
        graph_store=graph_store,
        graph_ingest=graph_ingest,
    )


def get_state(request: Request) -> AppState:
    return request.app.state.container


def bearer_token(request: Request) -> str | None:
    """`Authorization: Bearer <토큰>`의 토큰. 헤더가 없거나 다른 방식(Basic 등)이면 None."""
    scheme, _, value = (request.headers.get("Authorization") or "").partition(" ")
    token = value.strip()
    return token if scheme.lower() == BEARER_SCHEME and token else None


def _bearer_rejected() -> UnauthorizedError:
    return UnauthorizedError(
        "토큰이 만료되었거나 맞지 않습니다. 다시 로그인하세요.",
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )


async def current_session(request: Request, state: AppState) -> Session:
    """Bearer 토큰 또는 쿠키의 세션. 없거나 만료·폐기면 401. 헤더 스텁(X-User-Id)으로는 통과할 수 없다.

    CSRF는 여기서 보지 않는다 — 쿠키로 들어온 쓰기 요청은 `require_csrf`를 함께 부른다.
    """
    token = bearer_token(request)
    if token is not None:
        session = await state.auth_service.resolve(token)
        if session is None:
            raise _bearer_rejected()
        request.state.auth_via = AUTH_VIA_BEARER
        return session
    session = await state.auth_service.resolve(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise UnauthorizedError("로그인이 필요합니다.")
    request.state.auth_via = AUTH_VIA_COOKIE
    return session


def require_csrf(request: Request, state: AppState, session: Session) -> None:
    """쿠키로 인증한 요청이면 `X-CSRF-Token`이 세션의 토큰과 같아야 한다(403). Bearer 인증은 검사하지 않는다.

    `current_session` 뒤에 부른다. 인증 방식을 알 수 없으면 쿠키로 보고 검사한다(안전한 쪽).
    """
    if getattr(request.state, "auth_via", AUTH_VIA_COOKIE) == AUTH_VIA_BEARER:
        return
    if not state.auth_service.csrf_ok(session, request.headers.get(CSRF_HEADER)):
        raise ForbiddenError("CSRF 토큰이 없거나 맞지 않습니다 (X-CSRF-Token).")


async def current_user_id(
    request: Request, state: State, x_user_id: Annotated[str | None, Header()] = None
) -> str:
    """Bearer 토큰 → 세션 쿠키(+CSRF) → 개발용 헤더 스텁(설정으로 끌 수 있다) 순서."""
    bearer = bearer_token(request)
    if bearer is not None:
        session = await state.auth_service.resolve(bearer)
        if session is None:
            raise _bearer_rejected()
        request.state.auth_via = AUTH_VIA_BEARER
        return str(session.user_id)
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        session = await state.auth_service.resolve(token)
        if session is None:
            raise UnauthorizedError("세션이 만료되었습니다. 다시 로그인하세요.")
        if request.method not in SAFE_METHODS and not state.auth_service.csrf_ok(
            session, request.headers.get(CSRF_HEADER)
        ):
            raise ForbiddenError("CSRF 토큰이 없거나 맞지 않습니다 (X-CSRF-Token).")
        request.state.auth_via = AUTH_VIA_COOKIE
        return str(session.user_id)
    stub = (x_user_id or "").strip()
    # user_id 열은 128자다 — 그보다 긴 값은 DB에 넣다가 500이 되므로 스텁으로 받지 않는다.
    if state.settings.auth_allow_header_stub and stub and len(stub) <= MAX_STUB_USER_ID_CHARS:
        return stub
    raise HTTPException(status_code=401, detail={"code": "unauthorized", "message": "로그인이 필요합니다."})


def app_settings(request: Request) -> Settings:
    """이 앱이 시작할 때 받은 설정 (`create_app`에 넘긴 값 — 전역 `get_settings()`와 다를 수 있다)."""
    return request.app.state.container.settings


State = Annotated[AppState, Depends(get_state)]
UserId = Annotated[str, Depends(current_user_id)]
SettingsDep = Annotated[Settings, Depends(app_settings)]
