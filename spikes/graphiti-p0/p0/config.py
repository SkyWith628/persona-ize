"""스파이크 실행 설정을 환경변수(.env)에서 읽는다."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_GROUP_ID = "p0-sample"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_EMBEDDING_DIM = 1536


class ConfigError(RuntimeError):
    """필수 설정이 없거나 형식이 잘못되었을 때 발생한다."""


@dataclass(frozen=True)
class Settings:
    """Neo4j·OpenAI 접속 정보와 실험 범위(group_id)."""

    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    openai_api_key: str
    llm_model: str | None
    llm_small_model: str | None
    embedding_model: str
    embedding_dim: int
    group_id: str


def _required(name: str, *fallbacks: str) -> str:
    """이름 순서대로 환경변수를 찾아 첫 번째 값을 반환한다."""
    for key in (name, *fallbacks):
        value = os.getenv(key, "").strip()
        if value:
            return value
    raise ConfigError(f"환경변수 {name}이(가) 비어 있습니다. .env.example을 참고하세요.")


def load_settings(env_file: str | None = None) -> Settings:
    """`.env`를 읽어 Settings를 만든다.

    Neo4j Aura 접속 파일은 `NEO4J_USERNAME`을 쓰므로 `NEO4J_USER`의 대체로 허용한다.
    """
    load_dotenv(env_file)

    raw_dim = os.getenv("EMBEDDING_DIM", str(DEFAULT_EMBEDDING_DIM)).strip()
    try:
        embedding_dim = int(raw_dim)
    except ValueError as exc:
        raise ConfigError(f"EMBEDDING_DIM은 정수여야 합니다: {raw_dim!r}") from exc
    if embedding_dim <= 0:
        raise ConfigError(f"EMBEDDING_DIM은 양수여야 합니다: {embedding_dim}")

    return Settings(
        neo4j_uri=_required("NEO4J_URI"),
        neo4j_user=_required("NEO4J_USER", "NEO4J_USERNAME"),
        neo4j_password=_required("NEO4J_PASSWORD"),
        openai_api_key=_required("OPENAI_API_KEY"),
        llm_model=os.getenv("LLM_MODEL", "").strip() or None,
        llm_small_model=os.getenv("LLM_SMALL_MODEL", "").strip() or None,
        embedding_model=os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip(),
        embedding_dim=embedding_dim,
        group_id=os.getenv("GROUP_ID", DEFAULT_GROUP_ID).strip() or DEFAULT_GROUP_ID,
    )
