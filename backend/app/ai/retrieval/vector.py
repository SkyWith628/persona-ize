"""C 경로: 같은 상대에게 쓴 본인 발화 묶음을 kNN으로 고른다 (design.md §3-2, KATE).

- 질의 = 상황 + 초안. 임베딩은 묶음의 `context_text`(앞 맥락 + 본인 발화)와 비교한다.
- 결과의 `text`는 본인 발화(`style_text`)만이다. 상대 말투가 문체 예시에 섞이면 안 된다.
- 범위는 같은 사용자·같은 상대, `as_of` 이전 메시지만 (현재 요청 이전 데이터만, project-design §5.3).
- 저장소가 벡터 검색을 맡으므로 pgvector든 인메모리든 같은 코드다.
- `gate_by_register=True`면 후보를 **현재 말투 구간**의 묶음으로 좁힌다 (graph-redesign 5.4, 경로 X).
  반말을 쓰다가 합쇼체로 바꾼 상대에게 예전 반말 예시가 문체 예시로 들어가는 것을 막으려는 것이다.
  구간은 본인 메시지에서 규칙으로 계산한다(`app.ai.register.build_profile`) — 모델 호출이 없다.
  기본값(False)에서는 검색도 버전 문자열도 예전과 같다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.ai.embeddings import Embedder
from app.ai.register_source import own_messages, recent_before
from app.domain.conversation import EpisodeHit
from app.domain.rewrite import RetrievedExample
from app.repositories.base import ConversationRepo

log = logging.getLogger(__name__)

# 구간으로 좁힌 결과가 이보다 적으면 좁히지 않은 결과로 돌아간다 (설계 5.4 "2개 미만이 남으면").
MIN_GATED_EXAMPLES = 2

# (메시지 (id, sent_at, text) 목록, 상대 별칭, as_of=...) → app.ai.register.RegisterProfile
ProfileFn = Callable[..., Any]
# (사용자 id, 상대 id) → 상대 별칭
AliasFn = Callable[[str, UUID], Awaitable[str]]


def _default_profile_fn() -> ProfileFn:
    # 쓸 때 가져온다 — 걸러내기를 켜지 않은 구성은 register 모듈을 올리지 않는다.
    from app.ai.register import build_profile

    return build_profile


class VectorStyleRetriever:
    def __init__(
        self,
        conversations: ConversationRepo,
        embedder: Embedder,
        *,
        gate_by_register: bool = False,
        profile_fn: ProfileFn | None = None,
        alias_of: AliasFn | None = None,
    ) -> None:
        """`profile_fn`은 테스트에서 프로필 계산을 바꿔 끼우는 자리다. None이면 `register.build_profile`.

        `alias_of`는 상대 별칭을 읽는 함수다 (`build_state`가 상대 저장소로 채운다). 별칭은 호칭뿐 아니라
        말투 라벨에도 쓰인다 — 어미가 안 읽히는 "서연아 ㅇㅋ?"를 이름+아 호격으로 반말로 보는 규칙이 별칭의
        이름을 필요로 한다. 별칭 없이 계산하면 제안 카드·상태 블록과 다른 구간이 나올 수 있다.
        None이면 별칭 없이 계산한다 (그 규칙만 빠진다).
        """
        self._conversations = conversations
        self._embedder = embedder
        self._gate = gate_by_register
        self._profile_fn = profile_fn
        self._alias_of = alias_of
        self.version = f"knn-style-v1/{embedder.version}"
        if gate_by_register:
            # 같은 입력이라도 고르는 예시가 달라지므로 실험 기록에서 구분되어야 한다.
            self.version += "+gate"

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
        before = as_of or datetime.now(UTC)
        hits: list[EpisodeHit] | None = None
        if self._gate:
            hits = await self._gated_hits(user_id, contact_id, query, top_k, before)
        if hits is None:
            hits = await self._conversations.search_episodes(user_id, contact_id, query, top_k, before=before)
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

    async def _gated_hits(
        self, user_id: str, contact_id: UUID, query: list[float], top_k: int, before: datetime
    ) -> list[EpisodeHit] | None:
        """현재 말투 구간으로 좁힌 검색 결과. 좁힐 근거가 없거나 결과가 너무 적으면 None (좁히지 않는다).

        걸러내기는 보조 장치다. 여기서 무엇이 실패해도 첨삭은 예전 방식의 예시로 진행해야 하므로 예외를
        삼키고 None을 돌려준다.
        """
        try:
            after = await self._segment_start(user_id, contact_id, before)
            if after is None:
                return None
            hits = await self._conversations.search_episodes(
                user_id, contact_id, query, top_k, before=before, after=after
            )
        except Exception as exc:  # noqa: BLE001 - 프로필 계산·검색 어디서 실패하든 좁히지 않은 검색으로 간다
            log.debug("말투 구간 걸러내기 실패, 좁히지 않고 검색: %s", type(exc).__name__)
            return None
        # top_k가 1이면 한 건으로도 요청을 다 채운 것이다.
        if len(hits) < min(MIN_GATED_EXAMPLES, top_k):
            log.debug("현재 말투 구간의 예시가 %d건뿐이라 좁히지 않고 검색", len(hits))
            return None
        return hits

    async def _segment_start(self, user_id: str, contact_id: UUID, before: datetime) -> datetime | None:
        """현재 말투 구간이 시작한 시각. 말투가 바뀐 적이 없거나 지금 두 단계를 섞어 쓰면 None.

        - 확정된 현재 구간과 그 직전의 다른 구간이 둘 다 있어야 한다 — 바뀐 적이 없으면 좁힐 이유가 없다.
        - 최근 메시지에 두 단계가 섞여 있으면(two_valued) 상황별 말투일 수 있어 한쪽만 남기지 않는다.
        """
        # 제안·상태 블록과 같은 입력(as_of 이전, 최근 500건)으로 계산해야 같은 구간이 나온다.
        messages = recent_before(await own_messages(self._conversations, user_id, contact_id), before)
        if not messages:
            return None
        build = self._profile_fn or _default_profile_fn()
        alias = await self._alias_of(user_id, contact_id) if self._alias_of is not None else ""
        # 정규식 계산이라 이벤트 루프 밖에서 돈다 (첨삭 요청마다 불린다).
        profile = await asyncio.to_thread(build, messages, alias, as_of=before)
        current = profile.current_speech
        if current is None or current.status != "confirmed":
            return None
        if profile.previous_speech is None or profile.two_valued:
            return None
        start: datetime = current.start
        return start
