"""합성 대화 적재(`load_synthetic`)는 묶음 생성이 끝날 때까지 기다린다 (2026-11-16).

`build_state`는 API용 대기 시간(`IMPORT_INDEX_WAIT_SECONDS`)을 넣는다. 임베딩이 그보다 오래 걸리면
(모델을 처음 올리는 실행) 가져오기가 `processing`으로 돌아와 스크립트가 실패로 보고하고, 닫히면서 묶음
생성까지 취소했다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.ai.embeddings import FakeEmbedder
from app.api.deps import build_state
from app.core.config import Settings
from app.services.synthetic_loader import load_synthetic

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "data" / "synthetic" / "sample_conversations.json"
CHARACTERS = ROOT / "evals" / "characters.csv"


class SlowEmbedder(FakeEmbedder):
    def __init__(self, dim: int, delay: float = 0.05) -> None:
        super().__init__(dim=dim)
        self.delay = delay

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        await asyncio.sleep(self.delay)
        return await super().embed_passages(texts)


async def test_load_waits_for_indexing_even_when_the_api_wait_is_short() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, llm_provider="fake", embedding_provider="fake", import_index_wait_seconds=0.0
    )
    state = build_state(settings)
    try:
        state.import_service._embedder = SlowEmbedder(dim=settings.embedding_dim)
        report = await load_synthetic(state, DATASET, "u", CHARACTERS)
        assert report.failed == []
        assert report.imported > 0 and report.episodes > 0
        for conversation_id in report.conversations.values():
            stats = await state.conversations.conversation_stats("u", conversation_id)
            assert stats.episodes > 0
        # API용 대기 시간은 적재가 끝나면 되돌린다
        assert state.import_service.index_wait == 0.0
        assert state.import_service.graph_mode == "off"  # 그래프 없는 구성의 원래 값
    finally:
        await state.close()


async def test_load_restores_the_wait_when_an_import_raises() -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, llm_provider="fake", embedding_provider="fake", import_index_wait_seconds=1.5
    )
    state = build_state(settings)
    try:

        async def boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("가져오기 실패")

        state.import_service.import_messages = boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            await load_synthetic(state, DATASET, "u", CHARACTERS)
        assert state.import_service.index_wait == 1.5
    finally:
        await state.close()
