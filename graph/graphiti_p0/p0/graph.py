"""Settings로 Graphiti 클라이언트를 만든다 (LLM: Claude, 임베딩: 로컬 또는 Voyage)."""

from __future__ import annotations

from graphiti_core import Graphiti
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.llm_client.anthropic_client import AnthropicClient
from graphiti_core.llm_client.config import LLMConfig

from p0.config import Settings


def build_embedder(settings: Settings) -> EmbedderClient:
    """임베딩 제공자를 고른다. 모델·차원은 보고서에 기록해 재색인 시 혼합을 막는다."""
    if settings.embedding_provider == "voyage":
        from graphiti_core.embedder.voyage import VoyageAIEmbedder, VoyageAIEmbedderConfig

        return VoyageAIEmbedder(
            config=VoyageAIEmbedderConfig(
                api_key=settings.voyage_api_key,
                embedding_model=settings.embedding_model,
                embedding_dim=settings.embedding_dim,
            )
        )
    from p0.embedder_local import LocalEmbedder, LocalEmbedderConfig

    return LocalEmbedder(
        LocalEmbedderConfig(embedding_model=settings.embedding_model, embedding_dim=settings.embedding_dim)
    )


def build_graphiti(settings: Settings) -> Graphiti:
    """Claude LLM·선택한 임베더·Neo4j로 Graphiti를 구성한다.

    AnthropicClient는 ANTHROPIC_API_KEY 환경변수도 읽지만, 설정에서 명시적으로 넘겨 출처를 분명히 한다.
    """
    llm_config = LLMConfig(api_key=settings.anthropic_api_key, model=settings.llm_model)
    return Graphiti(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password,
        llm_client=AnthropicClient(config=llm_config),
        embedder=build_embedder(settings),
    )
