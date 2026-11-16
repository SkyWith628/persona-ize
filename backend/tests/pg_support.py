"""PostgreSQL 통합 테스트 준비 (conftest의 `pg_url` 픽스처가 쓴다).

같은 서버에 `persona_ize_test` DB를 만들어 public 스키마를 비우고 Alembic head까지 올린다.
`.env`의 DATABASE_URL이 가리키는 운영 DB는 건드리지 않는다. `TEST_DATABASE_URL`을 직접 주면 그 DB를 쓰되,
이름이 `_test`로 끝나야 한다 (매 실행마다 스키마를 지우기 때문).
"""

from __future__ import annotations

import asyncio
import os

import pytest

TEST_DB = "persona_ize_test"


def dsn(url: str) -> str:
    """SQLAlchemy URL → asyncpg가 받는 DSN."""
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _urls() -> tuple[str, str] | None:
    """(관리용 접속 URL, 테스트 DB URL). DATABASE_URL이 없으면 None."""
    from app.core.config import Settings

    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        if not explicit.rpartition("/")[2].split("?")[0].endswith("_test"):
            raise RuntimeError("TEST_DATABASE_URL의 DB 이름은 _test로 끝나야 합니다 (스키마를 지웁니다).")
        return explicit, explicit
    base = Settings().database_url  # .env를 읽는다
    if not base:
        return None
    head, _, _ = base.rpartition("/")
    return base, f"{head}/{TEST_DB}"


async def _prepare(asyncpg, admin_url: str, test_url: str) -> None:  # noqa: ANN001
    if admin_url != test_url:
        conn = await asyncpg.connect(dsn(admin_url), timeout=3)
        try:
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", TEST_DB)
            if not exists:
                await conn.execute(f'CREATE DATABASE "{TEST_DB}"')
        finally:
            await conn.close()
    conn = await asyncpg.connect(dsn(test_url), timeout=3)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    finally:
        await conn.close()


def prepare_test_db() -> str:
    """테스트 DB를 비우고 최신 스키마로 올린 뒤 URL을 돌려준다. 준비할 수 없으면 테스트를 건너뛴다."""
    pytest.importorskip("sqlalchemy")
    asyncpg = pytest.importorskip("asyncpg")
    from app.db.migrate import upgrade_to_head

    urls = _urls()
    if urls is None:
        pytest.skip("DATABASE_URL이 비어 있어 PostgreSQL 테스트를 건너뜁니다.")
    admin_url, test_url = urls
    try:
        asyncio.run(_prepare(asyncpg, admin_url, test_url))
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PostgreSQL 연결 실패({type(exc).__name__}) — `docker compose up -d postgres` 후 다시.")
    upgrade_to_head(test_url)
    return test_url
