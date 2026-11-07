"""로컬 sentence-transformers 임베더 (Graphiti EmbedderClient 구현).

Anthropic에는 임베딩 API가 없어 그래프 검색용 벡터는 로컬 오픈 모델로 만든다.
기본 모델 intfloat/multilingual-e5-small(384차원)은 한국어를 지원하고 CPU에서도 돌아간다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Any

from graphiti_core.embedder.client import EmbedderClient
from pydantic import BaseModel, Field


class LocalEmbedderConfig(BaseModel):
    embedding_model: str = Field(default="intfloat/multilingual-e5-small")
    embedding_dim: int = Field(default=384, gt=0)


class LocalEmbedder(EmbedderClient):
    """sentence-transformers 모델을 지연 로딩해 쓴다. 첫 호출에 모델을 내려받는다."""

    def __init__(self, config: LocalEmbedderConfig | None = None) -> None:
        self.config = config or LocalEmbedderConfig()
        self._model: Any | None = None

    def _load(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - 설치 안내
                raise RuntimeError(
                    "sentence-transformers가 없습니다: pip install sentence-transformers (또는 EMBEDDING_PROVIDER=voyage)"
                ) from exc
            self._model = SentenceTransformer(self.config.embedding_model)
        return self._model

    def _encode(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vectors = model.encode(texts, normalize_embeddings=True)
        return [[float(x) for x in vec[: self.config.embedding_dim]] for vec in vectors]

    async def create(
        self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]
    ) -> list[float]:
        text = input_data if isinstance(input_data, str) else str(list(input_data)[0])  # type: ignore[index]
        return (await asyncio.to_thread(self._encode, [text]))[0]

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._encode, list(input_data_list))
