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
    prompt_version: str = "rewrite-v1"

    max_draft_chars: int = Field(2000, gt=0)
    max_situation_chars: int = Field(500, gt=0)
    max_output_tokens: int = Field(4000, gt=0)
    request_timeout_seconds: float = Field(20.0, gt=0)

    database_url: str = ""
    neo4j_uri: str = ""
    neo4j_username: str = "neo4j"
    neo4j_password: str = ""

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
