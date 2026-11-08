"""문체 예시 검색용 임베딩 (C 경로). Anthropic에는 임베딩 API가 없어 로컬 오픈 모델을 기본으로 한다.

- local: sentence-transformers `intfloat/multilingual-e5-small`(384차원). e5 계열은 비대칭 모델이라
  질의에 `query: `, 저장 문서에 `passage: ` 접두를 붙여야 한다. 첫 호출에 모델을 내려받는다.
- fake: 글자 n-gram 해시 기반 결정적 벡터. 네트워크·torch 없이 테스트와 CI에서 쓴다.
  비슷한 글자가 많으면 가까운 벡터가 나와 kNN 순서 테스트가 가능하다.

모델·차원·전처리 버전은 `version`으로 기록한다. 바꾸면 기존 벡터와 섞지 않고 재색인한다 (project-design §5.1).
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from typing import Any, Protocol

from app.core.config import Settings


class Embedder(Protocol):
    version: str
    dim: int

    async def embed_passages(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class LocalE5Embedder:
    """sentence-transformers 지연 로딩. 인코딩은 스레드로 보내 이벤트 루프를 막지 않는다."""

    def __init__(self, model_name: str = "intfloat/multilingual-e5-small", dim: int = 384) -> None:
        self.model_name = model_name
        self.dim = dim
        self.version = f"st:{model_name}:{dim}:v1"
        self._model: Any | None = None

    def _load(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:  # pragma: no cover - 설치 안내
                raise RuntimeError(
                    "sentence-transformers가 없습니다: pip install -e 'backend[embed]' "
                    "(또는 EMBEDDING_PROVIDER=fake)"
                ) from exc
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def _encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self._load().encode(texts, normalize_embeddings=True)
        out = [[float(x) for x in vec[: self.dim]] for vec in vectors]
        for vec in out:
            if len(vec) != self.dim:
                raise RuntimeError(f"임베딩 차원 불일치: 모델 {len(vec)} vs 설정 {self.dim}")
        return out

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await asyncio.to_thread(self._encode, [f"passage: {t}" for t in texts])

    async def embed_query(self, text: str) -> list[float]:
        return (await asyncio.to_thread(self._encode, [f"query: {text}"]))[0]


class FakeEmbedder:
    """결정적 해시 임베딩. 의미는 모르지만 글자 2-gram이 겹칠수록 코사인 유사도가 높다."""

    version = "fake:char-2gram:v1"

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def _encode_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        norm = "".join(text.lower().split())
        grams = [norm[i : i + 2] for i in range(len(norm) - 1)] or [norm]
        for g in grams:
            h = int.from_bytes(hashlib.blake2b(g.encode("utf-8"), digest_size=8).digest(), "big")
            vec[h % self.dim] += 1.0 if (h >> 63) else -1.0
        length = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / length for x in vec]

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [self._encode_one(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._encode_one(text)


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "fake":
        return FakeEmbedder(dim=settings.embedding_dim)
    return LocalE5Embedder(model_name=settings.embedding_model, dim=settings.embedding_dim)


def cosine(a: list[float], b: list[float]) -> float:
    """정규화 여부와 무관하게 쓰는 코사인 유사도 (인메모리 검색용)."""
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)
