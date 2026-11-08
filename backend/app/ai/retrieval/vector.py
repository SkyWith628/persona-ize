"""C 경로: 같은 상대에게 쓴 본인 발화 묶음을 kNN으로 고른다 (design.md §3-2, KATE).

- 질의 = 상황 + 초안. 임베딩은 묶음의 `context_text`(앞 맥락 + 본인 발화)와 비교한다.
- 결과의 `text`는 본인 발화(`style_text`)만이다. 상대 말투가 문체 예시에 섞이면 안 된다.
- 범위는 같은 사용자·같은 상대, `as_of` 이전 메시지만 (현재 요청 이전 데이터만, project-design §5.3).
- 저장소가 벡터 검색을 맡으므로 pgvector든 인메모리든 같은 코드다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.ai.embeddings import Embedder
from app.domain.rewrite import RetrievedExample
from app.repositories.base import ConversationRepo


class VectorStyleRetriever:
    def __init__(self, conversations: ConversationRepo, embedder: Embedder) -> None:
        self._conversations = conversations
        self._embedder = embedder
        self.version = f"knn-style-v1/{embedder.version}"

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
        query_text = "\n".join(part for part in (situation.strip(), draft_text.strip()) if part)
        if not query_text:
            return []
        query = await self._embedder.embed_query(query_text)
        hits = await self._conversations.search_episodes(
            user_id, contact_id, query, top_k, before=as_of or datetime.now(UTC)
        )
        return [
            RetrievedExample(
                id=str(h.episode.id),
                purpose="style",
                text=h.episode.style_text,
                speaker_role="me",
                sent_at=h.episode.end_at,
                source_message_ids=list(h.episode.source_message_ids),
            )
            for h in hits
        ]
