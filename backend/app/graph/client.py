"""Settings로 Graphiti 클라이언트를 만든다 (스파이크 `p0/graph.py`의 백엔드판).

- LLM: Claude `GRAPH_LLM_MODEL`(기본 haiku-4-5). graphiti 0.31.2의 AnthropicClient는 강제 tool_choice를 쓰고
  5.5 계열이 이를 400으로 거부한다 (decisions 11-04).
- 임베딩: 백엔드의 `Embedder`(e5-small)를 graphiti `EmbedderClient`로 감싼다. 노드 이름·검색 질의가 모두
  같은 인코딩(passage 접두)을 타므로 대칭이다.
- 리랭커: 패스스루 (기본 리랭커는 OpenAI 호출).
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from graphiti_core import Graphiti
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.llm_client.anthropic_client import AnthropicClient
from graphiti_core.llm_client.config import LLMConfig

from app.ai.embeddings import Embedder
from app.core.config import Settings


class PassthroughReranker(CrossEncoderClient):
    """리랭킹 없이 입력 순서를 유지한다."""

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        total = len(passages)
        return [(passage, float(total - i)) for i, passage in enumerate(passages)]


class _MessagesWithoutTemperature:
    """anthropic SDK 2.x의 `messages.create`에는 temperature가 없는데 graphiti 0.31.2가 넘긴다 → 떼어낸다."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def create(self, **kwargs: Any) -> Any:
        kwargs.pop("temperature", None)
        return await self._inner.create(**kwargs)


class GraphAnthropicClient(AnthropicClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.client.messages = _MessagesWithoutTemperature(self.client.messages)


class EmbedderAdapter(EmbedderClient):
    """백엔드 `Embedder` → graphiti `EmbedderClient`."""

    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder

    async def create(
        self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]
    ) -> list[float]:
        text = input_data if isinstance(input_data, str) else str(list(input_data)[0])  # type: ignore[arg-type]
        return (await self._embedder.embed_passages([text]))[0]

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        return await self._embedder.embed_passages(list(input_data_list))


def ensure_ca_bundle() -> None:
    """Aura(`neo4j+s://`)용. SSL_CERT_FILE이 비어 있으면 certifi 번들을 지정한다 (스파이크 11-08 발견)."""
    if os.getenv("SSL_CERT_FILE", "").strip():
        return
    try:
        import certifi
    except ImportError:  # pragma: no cover
        return
    os.environ["SSL_CERT_FILE"] = certifi.where()


def build_graphiti(settings: Settings, embedder: Embedder) -> Graphiti:
    if not settings.neo4j_uri:
        raise ValueError("NEO4J_URI가 비어 있어 그래프를 쓸 수 없습니다.")
    ensure_ca_bundle()
    llm_config = LLMConfig(
        api_key=settings.anthropic_api_key or None,
        model=settings.graph_llm_model,
        small_model=settings.graph_llm_model,  # 보조 작업도 같은 모델로 고정
    )
    return Graphiti(
        settings.neo4j_uri,
        settings.neo4j_username,
        settings.neo4j_password,
        llm_client=GraphAnthropicClient(config=llm_config),
        embedder=EmbedderAdapter(embedder),
        cross_encoder=PassthroughReranker(),
    )
