"""검색 인터페이스. 실험 조건별로 구현을 바꿔 끼운다 (design.md §3-2).

- A/B: NullRetriever (검색 없음)
- C: VectorStyleRetriever (`vector.py`) — 같은 상대에게 쓴 본인 발화 묶음 kNN, purpose=style
- D: C + GraphContextRetriever (3주차, Graphiti) — 사건·관계 맥락, purpose=context
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.rewrite import RetrievedExample


class Retriever(Protocol):
    """동일 사용자·동일 상대 범위, 요청 이전 데이터만 대상으로 한다."""

    version: str

    async def retrieve(
        self,
        *,
        user_id: str,
        contact_id: UUID,
        draft_text: str,
        situation: str,
        top_k: int = 4,
        as_of: datetime | None = None,
    ) -> list[RetrievedExample]: ...


class NullRetriever:
    """검색하지 않는다. A/B 조건과 검색 장애 시 축소 동작에 쓴다."""

    version = "none"

    async def retrieve(
        self,
        *,
        user_id: str,
        contact_id: UUID,
        draft_text: str,
        situation: str,
        top_k: int = 4,
        as_of: datetime | None = None,
    ) -> list[RetrievedExample]:
        return []
