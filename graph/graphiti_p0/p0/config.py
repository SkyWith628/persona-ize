"""스파이크 실행 설정을 환경변수(.env)에서 읽는다.

LLM은 Claude(anthropic), 임베딩은 로컬 sentence-transformers 또는 Voyage AI를 쓴다.
Anthropic은 임베딩 API가 없으므로 임베딩 제공자를 따로 고른다 (docs/decisions.md 2026-11-08).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from dotenv import load_dotenv

DEFAULT_GROUP_ID = "p0-sample"
DEFAULT_GRAPH_LLM_MODEL = "claude-haiku-5-5"
DEFAULT_EMBEDDING_PROVIDER = "local"
DEFAULT_LOCAL_EMBEDDING_MODEL = "intfloat/multilingual-e5-small"
DEFAULT_LOCAL_EMBEDDING_DIM = 384
DEFAULT_VOYAGE_EMBEDDING_MODEL = "voyage-3"
DEFAULT_VOYAGE_EMBEDDING_DIM = 1024

EmbeddingProvider = Literal["local", "voyage"]


class ConfigError(RuntimeError):
    """필수 설정이 없거나 형식이 잘못되었을 때 발생한다."""


@dataclass(frozen=True)
class Settings:
    """Neo4j·Claude·임베딩 접속 정보와 실험 범위(group_id)."""

    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    anthropic_api_key: str | None
    llm_model: str
    embedding_provider: EmbeddingProvider
    embedding_model: str
    embedding_dim: int
    voyage_api_key: str | None
    group_id: str


def _required(name: str, *fallbacks: str) -> str:
    """이름 순서대로 환경변수를 찾아 첫 번째 값을 반환한다."""
    for key in (name, *fallbacks):
        value = os.getenv(key, "").strip()
        if value:
            return value
    raise ConfigError(f"환경변수 {name}이(가) 비어 있습니다. .env.example을 참고하세요.")


def _optional(name: str) -> str | None:
    return os.getenv(name, "").strip() or None


def load_settings(env_file: str | None = None) -> Settings:
    """`.env`를 읽어 Settings를 만든다.

    - ANTHROPIC_API_KEY가 비어 있어도 막지 않는다 (SDK가 `ant auth login` 프로필을 읽을 수 있다).
    - EMBEDDING_PROVIDER=voyage면 VOYAGE_API_KEY가 필수다.
    """
    load_dotenv(env_file)

    provider_raw = (
        os.getenv("EMBEDDING_PROVIDER", DEFAULT_EMBEDDING_PROVIDER).strip() or DEFAULT_EMBEDDING_PROVIDER
    )
    if provider_raw not in ("local", "voyage"):
        raise ConfigError(f"EMBEDDING_PROVIDER는 local 또는 voyage여야 합니다: {provider_raw!r}")
    provider: EmbeddingProvider = provider_raw  # type: ignore[assignment]

    default_model = DEFAULT_LOCAL_EMBEDDING_MODEL if provider == "local" else DEFAULT_VOYAGE_EMBEDDING_MODEL
    default_dim = DEFAULT_LOCAL_EMBEDDING_DIM if provider == "local" else DEFAULT_VOYAGE_EMBEDDING_DIM

    raw_dim = os.getenv("EMBEDDING_DIM", str(default_dim)).strip() or str(default_dim)
    try:
        embedding_dim = int(raw_dim)
    except ValueError as exc:
        raise ConfigError(f"EMBEDDING_DIM은 정수여야 합니다: {raw_dim!r}") from exc
    if embedding_dim <= 0:
        raise ConfigError(f"EMBEDDING_DIM은 양수여야 합니다: {embedding_dim}")

    voyage_key = _optional("VOYAGE_API_KEY")
    if provider == "voyage" and not voyage_key:
        raise ConfigError("EMBEDDING_PROVIDER=voyage이면 VOYAGE_API_KEY가 필요합니다.")

    return Settings(
        neo4j_uri=_required("NEO4J_URI"),
        neo4j_user=_required("NEO4J_USER", "NEO4J_USERNAME"),
        neo4j_password=_required("NEO4J_PASSWORD"),
        anthropic_api_key=_optional("ANTHROPIC_API_KEY"),
        llm_model=os.getenv("GRAPH_LLM_MODEL", DEFAULT_GRAPH_LLM_MODEL).strip() or DEFAULT_GRAPH_LLM_MODEL,
        embedding_provider=provider,
        embedding_model=os.getenv("EMBEDDING_MODEL", default_model).strip() or default_model,
        embedding_dim=embedding_dim,
        voyage_api_key=voyage_key,
        group_id=os.getenv("GROUP_ID", DEFAULT_GROUP_ID).strip() or DEFAULT_GROUP_ID,
    )
