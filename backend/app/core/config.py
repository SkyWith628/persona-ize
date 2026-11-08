"""환경변수(.env) 기반 설정. 비밀정보는 여기서만 읽고 프론트·로그에 노출하지 않는다."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"

DEFAULT_MODEL = "claude-opus-5-5"


class Settings(BaseSettings):
    """런타임 설정. 값의 의미는 `.env.example`에 적는다."""

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", Path(".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # fake: 네트워크 없이 동작 (테스트·UI 개발). anthropic: Claude API 실제 호출.
    llm_provider: Literal["fake", "anthropic"] = "fake"
    anthropic_api_key: str = ""
    llm_model: str = DEFAULT_MODEL
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    prompt_version: str = "rewrite-v2"  # v2: 호칭 규칙 명시 (decisions 11-09)

    max_draft_chars: int = Field(2000, gt=0)
    max_situation_chars: int = Field(500, gt=0)
    max_output_tokens: int = Field(4000, gt=0)
    request_timeout_seconds: float = Field(20.0, gt=0)

    database_url: str = ""
    # 문체 예시 검색(C) 임베딩. local=sentence-transformers(기본), fake=테스트용 해시 벡터.
    embedding_provider: Literal["local", "fake"] = "local"
    embedding_model: str = "intfloat/multilingual-e5-small"
    embedding_dim: int = Field(384, gt=0)
    max_import_messages: int = Field(5000, gt=0)
    retrieval_top_k: int = Field(4, ge=1, le=20)
    # 사용량 제어 (사용자·하루, 한국 시간 날짜). 넘으면 429. 0이면 전부 막는다.
    usage_daily_rewrite_limit: int = Field(200, ge=0)
    usage_daily_token_limit: int = Field(400_000, ge=0, description="입력+출력 토큰 합, 캐시 읽기 제외")
    usage_day_offset_hours: int = Field(9, ge=-12, le=14)
    # 인증. 세션 쿠키가 기본. 헤더 스텁(X-User-Id)은 개발·테스트·평가 스크립트용 — 배포에서는 끈다.
    session_ttl_days: int = Field(14, ge=1, le=365)
    session_cookie_secure: bool = Field(False, description="https에서만 쿠키 전송. 로컬 http 개발은 False")
    auth_allow_header_stub: bool = Field(True, description="X-User-Id 헤더로 인증 대체 허용 (개발용)")
    auth_allow_register: bool = True
    # 로그인 시도 제한: 이름·IP별 실패 횟수가 한도에 닿으면 잠금 시간 동안 429 (프로세스 안 메모리)
    auth_max_failures: int = Field(5, ge=1)
    auth_lockout_minutes: int = Field(15, ge=1)
    # D 경로(그래프). NEO4J_URI가 비어 있으면 그래프를 쓰지 않고 D = C로 동작한다.
    neo4j_uri: str = ""
    neo4j_username: str = "neo4j"
    neo4j_password: str = ""
    graph_llm_model: str = "claude-haiku-4-5"  # 5.5 계열은 graphiti 강제 tool_choice에 400 (decisions 11-04)
    graph_search_top_k: int = Field(4, ge=1, le=20)

    @model_validator(mode="after")
    def _check_provider(self) -> Settings:
        # anthropic SDK는 ANTHROPIC_API_KEY 환경변수나 `ant auth login` 프로필도 읽으므로
        # 키가 비어 있어도 막지 않는다. 모델명만 명시를 요구한다 (비용 추적).
        if self.llm_provider == "anthropic" and not self.llm_model:
            raise ValueError("LLM_PROVIDER=anthropic이면 LLM_MODEL을 명시해야 합니다.")
        return self


@lru_cache
def get_settings() -> Settings:
    """프로세스당 한 번만 읽는다. 테스트에서는 `get_settings.cache_clear()` 후 환경변수를 바꾼다."""
    return Settings()
