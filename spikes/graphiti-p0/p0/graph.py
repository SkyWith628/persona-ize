"""Settings로 Graphiti 클라이언트를 만든다."""

from __future__ import annotations

from graphiti_core import Graphiti
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.embedder.voyage import VoyageAIEmbedder, VoyageAIEmbedderConfig
from graphiti_core.llm_client.anthropic_client import AnthropicClient
from graphiti_core.llm_client.config import LLMConfig

from p0.config import Settings


class PassthroughReranker(CrossEncoderClient):
    """리랭킹을 하지 않고 입력 순서를 유지한다.

    graphiti 기본 리랭커는 OpenAI를 호출하므로, Claude·Voyage만 쓰는 스파이크에서는 대체한다.
    """

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        total = len(passages)
        return [(passage, float(total - i)) for i, passage in enumerate(passages)]


def build_graphiti(settings: Settings) -> Graphiti:
    """Claude LLM·Voyage 임베딩과 Neo4j로 Graphiti를 구성한다.

    비용 측정 시에는 모델명을 .env에서 명시하는 편이 좋다.
    """
    llm_config = LLMConfig(
        api_key=settings.anthropic_api_key,
        model=settings.llm_model,
        small_model=settings.llm_small_model or settings.llm_model,
    )
    embedder = VoyageAIEmbedder(
        config=VoyageAIEmbedderConfig(
            api_key=settings.voyage_api_key,
            embedding_model=settings.embedding_model,
            embedding_dim=settings.embedding_dim,
        )
    )
    return Graphiti(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password,
        llm_client=AnthropicClient(config=llm_config),
        embedder=embedder,
        cross_encoder=PassthroughReranker(),
    )
