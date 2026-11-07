"""환경변수 설정 로딩 테스트."""

from __future__ import annotations

import pytest

from p0.config import (
    DEFAULT_GRAPH_LLM_MODEL,
    DEFAULT_GROUP_ID,
    DEFAULT_LOCAL_EMBEDDING_DIM,
    DEFAULT_LOCAL_EMBEDDING_MODEL,
    DEFAULT_VOYAGE_EMBEDDING_DIM,
    ConfigError,
    ensure_ca_bundle,
    load_settings,
)

KEYS = (
    "SSL_CERT_FILE",
    "NEO4J_URI",
    "NEO4J_USER",
    "NEO4J_USERNAME",
    "NEO4J_PASSWORD",
    "ANTHROPIC_API_KEY",
    "GRAPH_LLM_MODEL",
    "EMBEDDING_PROVIDER",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
    "VOYAGE_API_KEY",
    "GROUP_ID",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """환경변수를 비우고, 상위 폴더의 실제 `.env`가 잡히지 않도록 빈 임시 폴더에서 실행한다."""
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def _set_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("NEO4J_USERNAME", "neo4j")
    monkeypatch.setenv("NEO4J_PASSWORD", "pw")


def test_defaults_local_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    s = load_settings(env_file="/nonexistent/.env")
    assert s.neo4j_user == "neo4j"
    assert s.anthropic_api_key is None  # SDK가 프로필에서 읽을 수 있으므로 필수 아님
    assert s.llm_model == DEFAULT_GRAPH_LLM_MODEL
    assert s.embedding_provider == "local"
    assert s.embedding_model == DEFAULT_LOCAL_EMBEDDING_MODEL
    assert s.embedding_dim == DEFAULT_LOCAL_EMBEDDING_DIM
    assert s.group_id == DEFAULT_GROUP_ID


def test_voyage_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "voyage")
    with pytest.raises(ConfigError, match="VOYAGE_API_KEY"):
        load_settings(env_file="/nonexistent/.env")
    monkeypatch.setenv("VOYAGE_API_KEY", "v")
    s = load_settings(env_file="/nonexistent/.env")
    assert s.embedding_dim == DEFAULT_VOYAGE_EMBEDDING_DIM


def test_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    with pytest.raises(ConfigError, match="EMBEDDING_PROVIDER"):
        load_settings(env_file="/nonexistent/.env")


def test_neo4j_user_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.delenv("NEO4J_USERNAME")
    monkeypatch.setenv("NEO4J_USER", "alt")
    assert load_settings(env_file="/nonexistent/.env").neo4j_user == "alt"


@pytest.mark.parametrize("missing", ["NEO4J_URI", "NEO4J_PASSWORD"])
def test_missing_required(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    _set_required(monkeypatch)
    monkeypatch.delenv(missing)
    with pytest.raises(ConfigError, match=missing):
        load_settings(env_file="/nonexistent/.env")


@pytest.mark.parametrize("bad", ["abc", "0", "-5"])
def test_bad_dim(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("EMBEDDING_DIM", bad)
    with pytest.raises(ConfigError, match="EMBEDDING_DIM"):
        load_settings(env_file="/nonexistent/.env")


def test_ca_bundle_set_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    certifi = pytest.importorskip("certifi")
    assert ensure_ca_bundle() == certifi.where()
    assert os.environ["SSL_CERT_FILE"] == certifi.where()
    assert os.path.isfile(os.environ["SSL_CERT_FILE"])


def test_ca_bundle_respects_user_value(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.setenv("SSL_CERT_FILE", "/custom/ca.pem")
    assert ensure_ca_bundle() is None
    assert os.environ["SSL_CERT_FILE"] == "/custom/ca.pem"


def test_load_settings_sets_ca_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    pytest.importorskip("certifi")
    _set_required(monkeypatch)
    load_settings(env_file="/nonexistent/.env")
    assert os.environ.get("SSL_CERT_FILE")


def test_missing_env_file_falls_back_to_parent(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    root = tmp_path
    (root / ".env").write_text(
        "NEO4J_URI=bolt://from-root:7687\nNEO4J_USERNAME=neo4j\nNEO4J_PASSWORD=pw\n", encoding="utf-8"
    )
    sub = root / "graph" / "spike"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    s = load_settings(env_file=str(sub / ".env"))  # 없는 파일 → 상위 .env
    assert s.neo4j_uri == "bolt://from-root:7687"


def test_repr_hides_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    text = repr(load_settings(env_file="/nonexistent/.env"))
    assert "pw" not in text.split("neo4j_user")[1] and "sk-ant-secret" not in text
    assert "neo4j_uri='bolt://localhost:7687'" in text
