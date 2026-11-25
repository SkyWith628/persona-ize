"""스파이크 실행 설정을 환경변수(.env)에서 읽는다.

LLM은 Claude(anthropic), 임베딩은 로컬 sentence-transformers 또는 Voyage AI를 쓴다.
Anthropic은 임베딩 API가 없으므로 임베딩 제공자를 따로 고른다 (docs/project/decisions.md 2026-11-08).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

DEFAULT_GROUP_ID = "p0-sample"
# graphiti AnthropicClient는 강제 tool_choice로 구조화 출력을 받는데, 5.5 계열(Opus/Sonnet/Haiku)은
# 강제 tool_choice를 400으로 거부한다 (docs/project/decisions.md 2026-11-04).
# 그래프 추출은 4.5 Haiku로 시작하고,
# 다른 모델은 GRAPH_LLM_MODEL로 지정하기 전에 --limit 5로 검증한다.
DEFAULT_GRAPH_LLM_MODEL = "claude-haiku-4-5"
DEFAULT_EMBEDDING_PROVIDER = "local"
DEFAULT_LOCAL_EMBEDDING_MODEL = "intfloat/multilingual-e5-small"
DEFAULT_LOCAL_EMBEDDING_DIM = 384
DEFAULT_VOYAGE_EMBEDDING_MODEL = "voyage-3.5"  # 미검증: 첫 호출에서 모델명 오류가 나면 Voyage 문서 확인
DEFAULT_VOYAGE_EMBEDDING_DIM = 1024

EmbeddingProvider = Literal["local", "voyage"]


class ConfigError(RuntimeError):
    """필수 설정이 없거나 형식이 잘못되었을 때 발생한다."""


@dataclass(frozen=True)
class Settings:
    """Neo4j·Claude·임베딩 접속 정보와 실험 범위(group_id)."""

    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str = field(repr=False)  # 로그·테스트 출력에 비밀값이 찍히지 않게 repr에서 제외
    anthropic_api_key: str | None = field(repr=False)
    llm_model: str
    embedding_provider: EmbeddingProvider
    embedding_model: str
    embedding_dim: int
    voyage_api_key: str | None = field(repr=False)
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


def _find_env_upwards(env_file: Path) -> Path | None:
    """`env_file`이 있으면 그대로, 없으면 같은 이름의 파일을 상위 폴더에서 차례로 찾는다."""
    env_file = env_file.resolve()
    for folder in (env_file.parent, *env_file.parent.parents):
        candidate = folder / env_file.name
        if candidate.is_file():
            return candidate
    return None


def ensure_ca_bundle() -> str | None:
    """SSL_CERT_FILE이 비어 있으면 certifi 번들을 지정한다. 지정한 경로를 돌려준다(없으면 None).

    Aura(`neo4j+s://`)의 인증서는 SSL.com 루트로 서명돼 있는데, Windows는 루트 인증서를 처음 쓸 때
    내려받으므로 새 PC의 로컬 저장소에 없을 수 있다. Python의 neo4j 드라이버는 로컬 저장소만 읽어
    "self-signed certificate in certificate chain"으로 실패한다 (2026-11-08). certifi 번들을 함께 신뢰하면
    해결되며, Python은 Windows 저장소와 SSL_CERT_FILE을 둘 다 읽는다.
    사용자가 이미 지정했으면 건드리지 않는다.
    """
    if os.getenv("SSL_CERT_FILE", "").strip():
        return None
    try:
        import certifi
    except ImportError:  # 선택 의존성. 없으면 시스템 저장소만 쓴다.
        return None
    path = certifi.where()
    os.environ["SSL_CERT_FILE"] = path
    return path


def load_settings(env_file: str | None = None) -> Settings:
    """`.env`를 읽어 Settings를 만든다.

    - ANTHROPIC_API_KEY가 비어 있어도 막지 않는다 (SDK가 `ant auth login` 프로필을 읽을 수 있다).
    - EMBEDDING_PROVIDER=voyage면 VOYAGE_API_KEY가 필수다.
    - TLS 검증에 certifi 번들을 추가한다 (`ensure_ca_bundle`). 드라이버를 만들기 전에 호출해야 효과가 있다.
    - `env_file`을 넘겼는데 그 파일이 없으면 상위 폴더를 거슬러 가장 가까운 `.env`(보통 저장소 루트)를 쓴다.
      스파이크 폴더에 `.env`를 따로 두지 않고 루트 `.env` 하나만 관리하기 위해서다.
    """
    if env_file is None:
        load_dotenv()
    else:
        found = _find_env_upwards(Path(env_file))
        if found is not None:
            load_dotenv(found)
    ensure_ca_bundle()

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
