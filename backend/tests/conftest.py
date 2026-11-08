from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


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


@pytest.fixture
def anyio_backend() -> str:
    # 비동기 테스트(pytest.mark.anyio)는 asyncio로만 돈다 (trio 미설치).
    return "asyncio"
