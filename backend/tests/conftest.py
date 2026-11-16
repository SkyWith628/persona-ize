from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

# 테스트가 돌다가 os.environ에 생기면 안 되는 변수. 이 값이 생기면 뒤따르는 테스트의
# `Settings(_env_file=None, ...)`이 환경변수에서 읽어 개발 DB·Neo4j·실제 모델 키로 붙는다
# (graphiti_core가 import될 때 load_dotenv()로 .env 전체를 올린다 — 2026-11-16 실제로 샜다).
# 세션을 시작할 때 이미 있던 변수는 실행한 사람이 넣은 것이므로 문제 삼지 않는다.
_GUARDED_ENV = ("DATABASE_URL", "NEO4J_URI", "ANTHROPIC_API_KEY", "LLM_PROVIDER")
_ENV_AT_START = frozenset(k for k in _GUARDED_ENV if k in os.environ)


@pytest.fixture(autouse=True)
def _no_env_leak() -> Iterator[None]:
    """테스트 하나가 위 변수를 os.environ에 남기면 그 테스트를 실패시키고, 다음 테스트를 위해 지운다."""
    yield
    leaked = [k for k in _GUARDED_ENV if k in os.environ and k not in _ENV_AT_START]
    for key in leaked:
        del os.environ[key]
    if leaked:
        pytest.fail(
            f"이 테스트가 환경변수 {leaked}를 os.environ에 남겼습니다 (.env가 샌 것입니다). "
            "graphiti_core를 import하는 구간을 deps._build_graph처럼 감싸세요."
        )


@pytest.fixture
def settings() -> Settings:
    # .env를 무시하고 fake 프로바이더·fake 임베더로 고정한다 (네트워크·torch 없음).
    return Settings(_env_file=None, llm_provider="fake", embedding_provider="fake")  # type: ignore[call-arg]


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


@pytest.fixture
def user_a() -> dict[str, str]:
    return {"X-User-Id": "user-a"}


@pytest.fixture
def user_b() -> dict[str, str]:
    return {"X-User-Id": "user-b"}


@pytest.fixture(scope="session")
def pg_url() -> str:
    """PostgreSQL 통합 테스트용 DB URL. 세션당 한 번 스키마를 비우고 최신으로 올린다. 없으면 건너뛴다."""
    from tests.pg_support import prepare_test_db

    return prepare_test_db()


@pytest.fixture
def anyio_backend() -> str:
    # 비동기 테스트(pytest.mark.anyio)는 asyncio로만 돈다 (trio 미설치).
    return "asyncio"
