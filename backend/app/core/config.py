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
    prompt_version: str = "rewrite-v3"  # v3: 오늘 날짜 + 날짜 대조 (decisions 11-10). v2: 호칭 규칙

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
    import_index_wait_seconds: float = Field(
        2.0,
        ge=0,
        le=600,
        description="가져오기 요청이 묶음 생성을 기다리는 초. 넘으면 processing으로 답하고 뒤에서 끝낸다",
    )
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
    # 로그인 시도 제한 (프로세스 안 메모리). 같은 (이름, IP)에서 실패가 한도에 닿으면 잠금 시간 동안 429.
    # 한 IP의 실패 합계·한 이름의 실패 합계는 그 배수에서 잠근다 — 남의 계정을 5번 틀려 잠그거나, 같은
    # 공유기(NAT) 뒤의 한 사람이 틀려 모두가 막히는 일이 없게 (2026-11-11 검토). 배수 0은 그 범위를 끈다.
    auth_max_failures: int = Field(5, ge=1)
    auth_lockout_minutes: int = Field(15, ge=1)
    auth_ip_failure_factor: int = Field(6, ge=0, description="IP 하나의 실패 합계 한도 = 한도 × 이 값")
    auth_name_failure_factor: int = Field(4, ge=0, description="이름 하나의 실패 합계 한도 = 한도 × 이 값")
    # D 경로(그래프). NEO4J_URI가 비어 있으면 그래프를 쓰지 않고 D = C로 동작한다.
    neo4j_uri: str = ""
    neo4j_username: str = "neo4j"
    neo4j_password: str = ""
    graph_llm_model: str = "claude-haiku-4-5"  # 5.5 계열은 graphiti 강제 tool_choice에 400 (decisions 11-04)
    graph_search_top_k: int = Field(4, ge=1, le=20)
    graph_resume_on_start: bool = Field(
        True, description="서버 시작 때 미수집 메시지를 이어서 그래프에 넣는다"
    )

    # ---- 배포·운영 (2026-11-11). 기본값은 개발 동작을 바꾸지 않는다. 운영 값은 .env.production.example ----
    # production이면 시작 때 위험한 설정을 거부하고 /docs를 끈다 (core/guard).
    app_env: Literal["development", "production"] = "development"
    log_level: str = "INFO"
    # 쉼표 구분. 비면 CORS 미들웨어를 달지 않는다(같은 출처 화면만). 기본은 React 개발 서버.
    cors_allow_origins: str = "http://localhost:5173,http://localhost:3000"
    trusted_hosts: str = Field("", description="쉼표 구분 Host 허용 목록. 비면 검사하지 않는다")
    # 가입 제어·동의
    register_invite_code: str = Field("", description="비어 있지 않으면 가입 때 같은 값을 요구한다")
    require_consent: bool = Field(False, description="true면 가입 요청에 consent=true가 있어야 한다")
    consent_version: str = Field("2026-11-11", description="약관·개인정보 안내 버전. 동의 기록에 남긴다")
    admin_usernames: str = Field("", description="쉼표 구분. 이 이름의 사용자만 /v1/admin/* 접근")
    # 요청 제한 (IP 기준, 프로세스 메모리). 0이면 끈다.
    rate_limit_per_minute: int = Field(0, ge=0, description="/v1/* 전체")
    rate_limit_auth_per_minute: int = Field(0, ge=0, description="로그인·가입·토큰 발급")
    # 한글은 글자당 3바이트다 — MAX_IMPORT_CHARS(200만 자)의 원문이 본문 상한에 먼저 걸리지 않게 8MB.
    max_body_bytes: int = Field(8_000_000, gt=0, description="요청 본문 상한. 넘으면 413")
    # 전체 사용자 합계 차단기 (하루, 사용량 날짜 기준). 0이면 끈다.
    usage_global_daily_rewrite_limit: int = Field(0, ge=0)
    usage_global_daily_token_limit: int = Field(0, ge=0)
    # 가져오기
    max_import_chars: int = Field(2_000_000, gt=0, description="원문 파싱(parse/raw) 본문 글자 수 상한")
    import_mask_pii: bool = Field(True, description="API 가져오기에서 전화번호·이메일·주민번호 등을 가린다")
    # 그래프 수집 비용 상한 (메시지 1건 = haiku 에피소드 1건). 0이면 무제한.
    graph_ingest_user_message_cap: int = Field(0, ge=0, description="사용자당 그래프에 넣는 메시지 총수")
    graph_ingest_daily_global_cap: int = Field(0, ge=0, description="하루 전체 그래프 수집 메시지 수")
    # 주기 작업·보존
    housekeeping_interval_hours: int = Field(0, ge=0, description="서버 안에서 정리 작업을 도는 주기. 0=끔")
    rewrite_retention_days: int = Field(0, ge=0, description="첨삭 이력 보존 일수. 0=무기한")
    # 사용자별 저장 한도 (2026-11-11 보안 검토): 한 계정이 디스크·임베딩 CPU·메모리를 끝없이 쓰지 못하게.
    max_contacts_per_user: int = Field(200, ge=1, description="상대 수")
    max_conversations_per_user: int = Field(500, ge=1, description="대화방 수")
    max_messages_per_user: int = Field(50_000, ge=1, description="가져온 메시지 총수")
    # 운영 모드는 비용 상한이 꺼진 설정(그래프 수집 상한 0, 초대 코드 없는 공개 가입 + 전체 차단기 0)에서
    # 시작을 거부한다. 알고도 그렇게 띄우려면 이것을 true로 둔다.
    allow_uncapped_costs: bool = Field(False, description="운영 모드에서 비용 상한 없는 설정을 허용")
    # 관리자 화면의 비용 환산 단가(USD). 0이면 환산하지 않는다 — 값은 Console 요금표에서 채운다.
    cost_rewrite_input_per_mtok: float = Field(0.0, ge=0)
    cost_rewrite_output_per_mtok: float = Field(0.0, ge=0)
    cost_graph_per_episode: float = Field(0.0, ge=0)

    @staticmethod
    def _split(value: str) -> list[str]:
        return [part.strip() for part in value.split(",") if part.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return self._split(self.cors_allow_origins)

    @property
    def trusted_host_list(self) -> list[str]:
        return self._split(self.trusted_hosts)

    @property
    def admin_username_set(self) -> frozenset[str]:
        """관리자 이름의 소문자 집합 — 이름 선점 차단·경고처럼 '비슷한 이름'을 볼 때 쓴다."""
        return frozenset(name.lower() for name in self._split(self.admin_usernames))

    def is_admin_name(self, username: str) -> bool:
        """관리자인지 — `ADMIN_USERNAMES`에 적힌 이름과 **대소문자까지** 같아야 한다.

        판정은 여기 한 곳이다(`AuthService.is_admin`, 관리자 API가 함께 쓴다). 대소문자만 다른 이름은
        가입 때 중복으로 막지만, 그 규칙 이전에 만들어진 계정이 있어도 관리자가 되지 않게 좁은 쪽으로 본다.
        """
        return bool(username) and username in self._split(self.admin_usernames)

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

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
