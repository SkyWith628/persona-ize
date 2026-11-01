"""Settings로 Graphiti 클라이언트를 만든다."""

from __future__ import annotations

from graphiti_core import Graphiti
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_client import OpenAIClient

from p0.config import Settings


def build_graphiti(settings: Settings) -> Graphiti:
    """OpenAI LLM·임베딩과 Neo4j로 Graphiti를 구성한다.

    모델명을 비워 두면 graphiti-core 기본 모델을 쓴다. 비용 측정 시에는 명시하는 편이 좋다.
    """
    llm_config = LLMConfig(
        api_key=settings.openai_api_key,
        model=settings.llm_model,
        small_model=settings.llm_small_model,
    )
    embedder = OpenAIEmbedder(
        config=OpenAIEmbedderConfig(
            api_key=settings.openai_api_key,
            embedding_model=settings.embedding_model,
            embedding_dim=settings.embedding_dim,
        )
    )
    return Graphiti(
        settings.neo4j_uri,
        settings.neo4j_user,
        settings.neo4j_password,
        llm_client=OpenAIClient(config=llm_config),
        embedder=embedder,
    )
