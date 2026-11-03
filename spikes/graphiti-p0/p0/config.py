"""스파이크 실행 설정을 환경변수(.env)에서 읽는다."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_GROUP_ID = "p0-sample"
# graphiti AnthropicClient가 강제 tool_choice로 구조화 출력을 받는다. 5.5 계열은 강제 tool_choice가 400이라
# 기본은 4.5 Haiku를 쓴다. 다른 모델은 LLM_MODEL로 지정하기 전에 소규모로 검증한다.
DEFAULT_LLM_MODEL = "claude-haiku-4-5"
DEFAULT_EMBEDDING_MODEL = "voyage-3.5"  # 미검증: 첫 호출에서 모델명 오류가 나면 Voyage 문서 확인
DEFAULT_EMBEDDING_DIM = 1024


class ConfigError(RuntimeError):
    """필수 설정이 없거나 형식이 잘못되었을 때 발생한다."""


@dataclass(frozen=True)
class Settings:
    """Neo4j·Anthropic·Voyage 접속 정보와 실험 범위(group_id)."""

    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    anthropic_api_key: str
    voyage_api_key: str
    llm_model: str
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
        anthropic_api_key=_required("ANTHROPIC_API_KEY"),
        voyage_api_key=_required("VOYAGE_API_KEY"),
        llm_model=os.getenv("LLM_MODEL", "").strip() or DEFAULT_LLM_MODEL,
        llm_small_model=os.getenv("LLM_SMALL_MODEL", "").strip() or None,
        embedding_model=os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip(),
        embedding_dim=embedding_dim,
        group_id=os.getenv("GROUP_ID", DEFAULT_GROUP_ID).strip() or DEFAULT_GROUP_ID,
    )
