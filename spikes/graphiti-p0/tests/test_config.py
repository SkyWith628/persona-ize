"""환경변수 설정 로딩 테스트."""

from __future__ import annotations

import pytest

from p0.config import DEFAULT_GROUP_ID, ConfigError, load_settings

KEYS = (
    "NEO4J_URI", "NEO4J_USER", "NEO4J_USERNAME", "NEO4J_PASSWORD", "OPENAI_API_KEY",
    "LLM_MODEL", "LLM_SMALL_MODEL", "EMBEDDING_MODEL", "EMBEDDING_DIM", "GROUP_ID",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)


def _set_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEO4J_URI", "neo4j+s://example.databases.neo4j.io")
    monkeypatch.setenv("NEO4J_PASSWORD", "pw")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


def test_aura_username_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("NEO4J_USERNAME", "neo4j")
    settings = load_settings("nonexistent.env")
    assert settings.neo4j_user == "neo4j"
    assert settings.group_id == DEFAULT_GROUP_ID
    assert settings.llm_model is None


def test_missing_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    with pytest.raises(ConfigError, match="NEO4J_USER"):
        load_settings("nonexistent.env")


@pytest.mark.parametrize("raw", ["abc", "0", "-5"])
def test_invalid_embedding_dim(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("NEO4J_USER", "neo4j")
    monkeypatch.setenv("EMBEDDING_DIM", raw)
    with pytest.raises(ConfigError, match="EMBEDDING_DIM"):
        load_settings("nonexistent.env")
