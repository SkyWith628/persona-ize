"""그래프 수집: 아직 안 들어간 메시지를 발화 시각 순으로 `add_episode` 한다.

- group_id = user_id. 사용자 그래프는 서로 섞이지 않는다.
- Graphiti는 넣는 순서대로 이전 사실을 무효화하므로 시간 순이 필수다. 그래서 수집은 **사용자 단위로 직렬**이다
  (`ingest_user`: 사용자별 asyncio.Lock, 미수집 메시지를 대화방 가리지 않고 전체 시각 순으로). 가져오기 직후
  백그라운드 수집도 이 경로를 탄다 — 다른 방의 가져오기가 겹치면 뒤 작업이 잠금을 기다렸다가 남은 것을 넣는다
  (11-09 백로그 A3; 그 전엔 대화방별 태스크라 방 사이 순서가 어긋날 수 있었다).
- 메시지마다 `graph_ingested_at`을 찍어 재실행이 멱등하다. 에피소드 하나가 haiku로 10~15초 걸린다.
- 실패하면 그 메시지부터 다시 시도할 수 있게 앞선 메시지의 표시는 남긴다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from uuid import UUID

from app.domain.conversation import Conversation, Message
from app.graph.episodes import build_graph_episodes
from app.graph.store import GraphStore
from app.repositories.base import ContactRepo, ConversationRepo

log = logging.getLogger(__name__)


class GraphIngestService:
    def __init__(self, store: GraphStore, conversations: ConversationRepo, contacts: ContactRepo) -> None:
        self._store = store
        self._conversations = conversations
        self._contacts = contacts
        self._prepared = False
        self._locks: dict[str, asyncio.Lock] = {}  # user_id → 수집 잠금 (프로세스 안에서만 유효)

    async def ingest_user(self, user_id: str, job_id: UUID | None = None) -> int:
        """이 사용자의 미수집 메시지를 전체 시각 순으로 넣는다. 같은 사용자의 수집은 한 번에 하나만.

        잠금을 기다리는 동안 다른 작업이 먼저 넣은 메시지는 pending에서 빠지므로 중복 수집은 없다.
        job_id를 주면 그 작업의 graph_* 상태를 이번 실행 결과로 갱신한다
        (다른 방의 메시지가 섞여 들어갈 수 있다).
        """
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        if lock.locked() and job_id is not None:
            await self._conversations.update_job_graph(user_id, job_id, "pending", 0, None)
        async with lock:
            return await self.ingest_pending(user_id, None, job_id)

    async def ingest_conversation(
        self, user_id: str, conversation_id: UUID, job_id: UUID | None = None
    ) -> int:
        return await self.ingest_pending(user_id, [conversation_id], job_id)

    async def ingest_pending(
        self, user_id: str, conversation_ids: list[UUID] | None = None, job_id: UUID | None = None
    ) -> int:
        """대화방들의 미수집 메시지를 시각 순으로 넣는다. 넣은 수를 돌려준다."""
        convs = (
            [await self._conversations.get(user_id, cid) for cid in conversation_ids]
            if conversation_ids is not None
            else await self._conversations.list(user_id)
        )
        pending: list[tuple[Message, Conversation, str]] = []  # (message, conversation, episode body)
        for conv in convs:
            contact = await self._contacts.get(user_id, conv.contact_id)
            messages = await self._conversations.list_messages(user_id, conv.id)
            episodes = {
                e.message_id: e
                for e in build_graph_episodes(messages, conv.source_key, conv.me_name, contact.alias)
            }
            pending.extend((m, conv, episodes[m.id].body) for m in messages if m.graph_ingested_at is None)
        pending.sort(key=lambda item: item[0].sent_at)

        if job_id is not None:
            await self._conversations.update_job_graph(user_id, job_id, "running", 0, None)
        if not pending:
            if job_id is not None:
                await self._conversations.update_job_graph(user_id, job_id, "completed", 0, None)
            return 0

        if not self._prepared:
            await self._store.prepare()
            self._prepared = True

        done = 0
        try:
            for message, conv, body in pending:
                await self._store.add_episode(
                    name=f"{conv.source_key}:{message.source_key}",
                    body=body,
                    reference_time=message.sent_at,
                    group_id=user_id,
                    source_description=f"과거 대화 ({conv.title})",
                )
                await self._conversations.mark_graph_ingested(user_id, [message.id], datetime.now(UTC))
                done += 1
                if job_id is not None:
                    await self._conversations.update_job_graph(user_id, job_id, "running", done, None)
        except Exception as exc:
            log.warning("graph ingest failed user=%s after %d episodes: %s", user_id, done, exc)
            if job_id is not None:
                await self._conversations.update_job_graph(user_id, job_id, "failed", done, str(exc)[:500])
            raise
        if job_id is not None:
            await self._conversations.update_job_graph(user_id, job_id, "completed", done, None)
        return done

    async def remove_episodes(self, user_id: str, names: list[str]) -> int:
        """대화방 삭제 때 그 메시지들의 에피소드를 그래프에서 지운다."""
        return await self._store.remove_episodes(user_id, names)
