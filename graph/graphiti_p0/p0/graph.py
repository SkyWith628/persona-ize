"""Settings로 Graphiti 클라이언트를 만든다 (LLM: Claude, 임베딩: 로컬 또는 Voyage)."""

from __future__ import annotations

from graphiti_core import Graphiti
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.llm_client.anthropic_client import AnthropicClient
from graphiti_core.llm_client.config import LLMConfig

from p0.config import Settings


class PassthroughReranker(CrossEncoderClient):
    """리랭킹을 하지 않고 입력 순서를 유지한다.

    graphiti 기본 리랭커(OpenAIRerankerClient)는 OpenAI 키를 요구한다. Claude만 쓰는 이 프로젝트에서는
    검색 순서를 그대로 두고, 리랭킹이 필요해지면 로컬 cross-encoder로 교체한다 (docs/decisions.md 2026-11-04).
    """

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        total = len(passages)
        return [(passage, float(total - i)) for i, passage in enumerate(passages)]


def build_embedder(settings: Settings) -> EmbedderClient:
    """임베딩 제공자를 고른다. 모델·차원은 보고서에 기록해 재색인 시 혼합을 막는다."""
    if settings.embedding_provider == "voyage":
        from graphiti_core.embedder.voyage import (
            VoyageAIEmbedder,
            VoyageAIEmbedderConfig,
        )

        return VoyageAIEmbedder(
            config=VoyageAIEmbedderConfig(
                api_key=settings.voyage_api_key,
                embedding_model=settings.embedding_model,
                embedding_dim=settings.embedding_dim,
            )
        )
    from p0.embedder_local import LocalEmbedder, LocalEmbedderConfig

    return LocalEmbedder(
        LocalEmbedderConfig(
            embedding_model=settings.embedding_model,
            embedding_dim=settings.embedding_dim,
        )
    )


def build_graphiti(settings: Settings) -> Graphiti:
    """Claude LLM·선택한 임베더·Neo4j로 Graphiti를 구성한다.

    AnthropicClient는 ANTHROPIC_API_KEY 환경변수도 읽지만, 설정에서 명시적으로 넘겨 출처를 분명히 한다.
    """
    llm_config = LLMConfig(
        api_key=settings.anthropic_api_key,
        model=settings.llm_model,
        small_model=settings.llm_model,  # graphiti가 보조 작업에 small_model을 쓴다. 같은 모델로 고정.
    )
    return Graphiti(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password,
        llm_client=AnthropicClient(config=llm_config),
        embedder=build_embedder(settings),
        cross_encoder=PassthroughReranker(),
    )
