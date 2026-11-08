"""비동기 엔진·세션 팩토리. 앱 시작 시 한 번 만들고 종료 시 `engine.dispose()`."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

SessionFactory = async_sessionmaker[AsyncSession]


def create_engine(database_url: str) -> AsyncEngine:
    if not database_url.startswith("postgresql+asyncpg://"):
        raise ValueError("DATABASE_URL은 postgresql+asyncpg:// 로 시작해야 합니다 (.env.example 참고).")
    return create_async_engine(database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> SessionFactory:
    # expire_on_commit=False: 커밋 뒤에도 ORM 객체 속성을 추가 쿼리 없이 읽는다 (비동기에서 필수).
    return async_sessionmaker(engine, expire_on_commit=False)
