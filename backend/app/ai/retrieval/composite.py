"""여러 검색기를 이어 붙인다 (D = C 문체 예시 + 그래프 사실 맥락).

한 부분이 실패하면 그 부분만 비우고 나머지는 돌려준다 — 그래프가 죽어도 C 수준으로는 동작해야 한다
(project-design §9 축소 동작).
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

from app.ai.retrieval.base import Retriever
from app.domain.rewrite import RetrievedExample

log = logging.getLogger(__name__)


class CompositeRetriever:
    def __init__(self, parts: list[Retriever]) -> None:
        if not parts:
            raise ValueError("검색기가 하나 이상 필요합니다.")
        self._parts = parts
        self.version = "+".join(p.version for p in parts)

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
        out: list[RetrievedExample] = []
        for part in self._parts:
            try:
                out.extend(
                    await part.retrieve(
                        user_id=user_id,
                        contact_id=contact_id,
                        draft_text=draft_text,
                        situation=situation,
                        top_k=top_k,
                        as_of=as_of,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 부분 장애는 축소 동작
                log.warning("retriever %s failed, continuing without it: %s", part.version, exc)
        return out
