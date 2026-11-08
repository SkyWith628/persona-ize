"""그래프 수집: 아직 안 들어간 메시지를 발화 시각 순으로 `add_episode` 한다.

- group_id = user_id. 사용자 그래프는 서로 섞이지 않는다.
- Graphiti는 넣는 순서대로 이전 사실을 무효화하므로 시간 순이 필수다. 대화방 여러 개를 한 번에 넣을 때는
  전체를 시각 순으로 섞어 넣는다 (`ingest_pending`). 가져오기 직후 백그라운드 수집은 그 대화방만 넣는다.
- 메시지마다 `graph_ingested_at`을 찍어 재실행이 멱등하다. 에피소드 하나가 haiku로 10~15초 걸린다.
- 실패하면 그 메시지부터 다시 시도할 수 있게 앞선 메시지의 표시는 남긴다.
"""

from __future__ import annotations

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
