"""D 경로 사실 맥락 검색기 (design.md §3-2 `purpose=context`).

- 질의 = 상황 + 초안. Graphiti 하이브리드 검색(BM25 + 임베딩, RRF)으로 엣지(사실 문장)를 받는다.
- 범위: 사용자 그래프(group_id=user_id) 안에서 상대 노드를 중심으로 거리 재정렬하고, **양 끝 중 하나가 상대
  노드이거나 상대와 연결된 Event 노드인 엣지만** 남긴다. 거리 재정렬만으로는 다른 방의 사실이 섞였다(11-08
  첫 실측: 팀장 질의에 친구 방 홈커밍, 동기 질의에 동생 방 케이크). Person 이웃(사용자 자신, 다른 상대)은
  기준으로 삼지 않는다 — 사용자 노드는 모든 방에 걸쳐 있어서 걸러지는 게 없기 때문이다.
  상대 이름과 정확히 일치하는 노드가 없으면(호칭으로만 병합된 경우) 필터 없이 거리 재정렬만 한다.
- 요청 이전 데이터만: 출처 에피소드 중 하나라도 `as_of` 이후면 그 사실을 버린다 (평가에서 미래 유출 방지).
  이미 무효화된(invalid_at ≤ as_of) 사실도 버린다 — 지금 어조를 정하는 데는 현재 관계가 맞다.
- 결과 `text`는 사실 문장 그대로. 프롬프트의 [사실 맥락] 블록에 들어가며 "어조 조정에만 사용"한다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.domain.rewrite import RetrievedExample
from app.graph.store import GraphStore
from app.repositories.base import ContactRepo


class GraphContextRetriever:
    version = "graph-context-v1"

    def __init__(self, store: GraphStore, contacts: ContactRepo, top_k: int = 4, oversample: int = 4) -> None:
        self._store = store
        self._contacts = contacts
        self._top_k = top_k
        self._oversample = oversample

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
        query = "\n".join(part for part in (situation.strip(), draft_text.strip()) if part)
        if not query:
            return []
        limit = (self._top_k or top_k) * self._oversample
        contact = await self._contacts.get(user_id, contact_id)
        center = await self._store.find_entity_uuid(user_id, contact.alias)
        hits = await self._store.search(query, user_id, center, limit)
        allowed: set[str] | None = None
        if center is not None:
            allowed = {center} | await self._store.event_neighbors(user_id, center)
        refs = await self._store.episode_refs({u for h in hits for u in h.episode_uuids})
        cutoff = as_of or datetime.now(UTC)

        out: list[RetrievedExample] = []
        seen_sources: set[frozenset[str]] = set()
        for h in hits:
            if allowed is not None and not ({h.source_node_uuid, h.target_node_uuid} & allowed):
                continue
            eps = [refs[u] for u in h.episode_uuids if u in refs]
            # 같은 메시지에서 나온 사실이 둘이면 하나만 (2차 실측: c1:m01 "주간보고" 사실이 두 번 들어감)
            source_key = frozenset(e.name for e in eps)
            if source_key and source_key in seen_sources:
                continue
            seen_sources.add(source_key)
            times = [e.valid_at for e in eps if e.valid_at is not None]
            if any(t >= cutoff for t in times):
                continue
            if h.invalid_at is not None and h.invalid_at <= cutoff:
                continue
            when = h.valid_at or (max(times) if times else None)
            out.append(
                RetrievedExample(
                    id=h.uuid,
                    purpose="context",
                    text=h.fact,
                    speaker_role="graph",
                    sent_at=when,
                    source_message_ids=sorted(e.name for e in eps),
                )
            )
            if len(out) >= (self._top_k or top_k):
                break
        return out
