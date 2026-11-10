"""D 경로 사실 맥락 검색기 (design.md §3-2 `purpose=context`).

- 질의 = 상황 + 초안. Graphiti 하이브리드 검색(BM25 + 임베딩, RRF)으로 엣지(사실 문장)를 받는다.
- 범위: 사용자 그래프(group_id=user_id) 안에서 상대 노드를 중심으로 거리 재정렬하고, **양 끝 중 하나가 상대
  노드이거나 상대와 연결된 Event 노드인 엣지만** 남긴다. 거리 재정렬만으로는 다른 방의 사실이 섞였다(11-08
  첫 실측: 팀장 질의에 친구 방 홈커밍, 동기 질의에 동생 방 케이크). Person 이웃(사용자 자신, 다른 상대)은
  기준으로 삼지 않는다 — 사용자 노드는 모든 방에 걸쳐 있어서 걸러지는 게 없기 때문이다.
  상대 노드는 그래프에 넣을 때 쓴 이름(`Contact.name_in_graph` — 별칭을 바꿔도 처음 수집한 이름)으로
  찾는다. 그 이름과 정확히 일치하는 노드가 없으면(아직 수집 전, 호칭으로만 병합된 경우) **사실 맥락을
  비운다** — 예전에는 필터 없이 돌려줘 다른 상대의 사실이 섞일 수 있었다.
- 요청 이전 데이터만: 출처 에피소드 중 하나라도 `as_of` 이후면 그 사실을 버린다 (평가에서 미래 유출 방지).
  이미 무효화된(invalid_at ≤ as_of) 사실도 버린다 — 지금 어조를 정하는 데는 현재 관계가 맞다.
- 재정렬(11-09, 백로그 A2): Graphiti의 순위는 BM25 비중이 커서 "주간보고 초안 공유"처럼 질의와 무관한 사실이
  상위에 왔다. 남은 후보를 **질의-사실 임베딩 유사도(로컬 e5) + 최근성**으로 다시 매기고, 1위보다 많이
  떨어지는 사실은 버린다. LLM 호출은 없다.
- 결과 `text`는 사실 문장 그대로. 프롬프트의 [사실 맥락] 블록에 들어가며 "어조 조정에만 사용"한다.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from app.ai.embeddings import Embedder, cosine
from app.domain.rewrite import RetrievedExample
from app.graph.store import EventInfo, GraphEdgeHit, GraphStore
from app.repositories.base import ContactRepo

SIMILARITY_WEIGHT = 0.75
RECENCY_WEIGHT = 0.15
RANK_WEIGHT = 0.10
RECENCY_HALF_LIFE_DAYS = 30.0
RELATIVE_CUTOFF = 0.12  # 1위(가중 전) 점수보다 이만큼 낮으면 버린다 (무관한 사실 억제)
MIN_KEEP = 2  # 임계와 무관하게 남기는 최소 사실 수
# 사건 유형 가중 (schema.Event.event_type). 일상 상태 공유(주간보고·회의록)는 어조 판단에 덜 중요하다.
EVENT_TYPE_WEIGHT = {
    "conflict": 1.0,
    "apology": 1.0,
    "refusal": 1.0,
    "promise": 1.0,
    "request": 0.95,
    "schedule": 0.95,
    "other": 0.85,
    "status": 0.75,
}
DEFAULT_IMPORTANCE = 5  # 모델이 importance를 안 채운 사건


@dataclass
class _Candidate:
    hit: GraphEdgeHit
    when: datetime | None
    source_ids: list[str]
    rank: int  # Graphiti 순위 (0부터)
    score: float = 0.0  # 가중 점수 (정렬용)
    base: float = 0.0  # 가중 전 점수 (임계용)


class GraphContextRetriever:
    version = "graph-context-v2"  # v1: Graphiti 순위 그대로 / v2: 유사도+최근성 재정렬

    def __init__(
        self,
        store: GraphStore,
        contacts: ContactRepo,
        top_k: int = 4,
        oversample: int = 4,
        embedder: Embedder | None = None,
    ) -> None:
        self._store = store
        self._contacts = contacts
        self._top_k = top_k
        self._oversample = oversample
        self._embedder = embedder  # None이면 재정렬 없이 Graphiti 순위를 쓴다 (v1 동작)

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
        k = self._top_k or top_k
        contact = await self._contacts.get(user_id, contact_id)
        # 그래프에 넣을 때 쓴 이름으로 찾는다 (별칭을 바꿔도 노드 이름은 처음 수집한 이름이다).
        center = await self._store.find_entity_uuid(user_id, contact.name_in_graph)
        if center is None:
            # 상대 노드가 없으면 '이 상대와 관련된 사실'을 가려낼 기준이 없다. 필터 없이 돌려주면
            # 다른 상대의 사실이 섞인다 — 사실 맥락 없이 간다 (2026-11-11 검토).
            return []
        hits = await self._store.search(query, user_id, center, k * self._oversample)
        allowed: set[str] | None = {center} | await self._store.event_neighbors(user_id, center)
        refs = await self._store.episode_refs({u for h in hits for u in h.episode_uuids})
        cutoff = as_of or datetime.now(UTC)

        candidates: list[_Candidate] = []
        seen_sources: set[frozenset[str]] = set()
        for rank, h in enumerate(hits):
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
            candidates.append(_Candidate(hit=h, when=when, source_ids=sorted(e.name for e in eps), rank=rank))

        if self._embedder is not None and candidates:
            endpoints = {c.hit.source_node_uuid for c in candidates} | {
                c.hit.target_node_uuid for c in candidates
            }
            events = await self._store.event_info(user_id, endpoints)
            candidates = await self._rerank(query, candidates, cutoff, events)
        chosen = candidates[:k]

        return [
            RetrievedExample(
                id=c.hit.uuid,
                purpose="context",
                text=c.hit.fact,
                speaker_role="graph",
                sent_at=c.when,
                source_message_ids=c.source_ids,
                score=round(c.score, 4) if self._embedder is not None else None,
            )
            for c in chosen
        ]

    async def _rerank(
        self, query: str, candidates: list[_Candidate], cutoff: datetime, events: dict[str, EventInfo]
    ) -> list[_Candidate]:
        """유사도·최근성·원래 순위를 합치고 사건 유형·중요도로 가중한 뒤, 1위에서 떨어지는 사실을 버린다."""
        assert self._embedder is not None
        q = await self._embedder.embed_query(query)
        vecs = await self._embedder.embed_passages([c.hit.fact for c in candidates])
        n = len(candidates)
        for c, v in zip(candidates, vecs, strict=True):
            sim = cosine(q, v)
            if c.when is not None:
                days = max((cutoff - c.when).total_seconds() / 86400.0, 0.0)
                recency = math.exp(-days / RECENCY_HALF_LIFE_DAYS * math.log(2))
            else:
                recency = 0.5
            rank_prior = 1.0 - (c.rank / n if n > 1 else 0.0)
            base = SIMILARITY_WEIGHT * sim + RECENCY_WEIGHT * recency + RANK_WEIGHT * rank_prior
            event = events.get(c.hit.source_node_uuid) or events.get(c.hit.target_node_uuid)
            c.base = base
            c.score = base * _event_weight(event)
        # 임계는 가중 전 점수(질의 관련성)로 자른다. 가중 점수로 자르면 중요도 낮은 사건이 관련성과 무관하게
        # 잘려 나간다 (11-09 repeat3: 최유진 '밥 약속' 사실이 빠져 recall 1.0→0.5). 최소 2건은 남긴다.
        candidates.sort(key=lambda c: -c.base)
        best_base = candidates[0].base
        kept = [c for i, c in enumerate(candidates) if i < MIN_KEEP or best_base - c.base <= RELATIVE_CUTOFF]
        kept.sort(key=lambda c: -c.score)
        return kept


def _event_weight(event: EventInfo | None) -> float:
    """사람↔사람(Relationship) 사실은 1.0. 사건 사실은 유형 × 중요도(1~10 → 0.735~1.05)."""
    if event is None:
        return 1.0
    type_w = EVENT_TYPE_WEIGHT.get(event.event_type or "other", 0.85)
    importance = event.importance if event.importance is not None else DEFAULT_IMPORTANCE
    importance_w = 0.7 + 0.35 * (max(1, min(importance, 10)) / 10)
    return type_w * importance_w
