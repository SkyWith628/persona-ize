"""인메모리 저장소. 테스트와 DATABASE_URL이 비어 있는 실행에서 쓴다 (기본은 `postgres.py`).

모든 조회는 user_id로 격리한다. 다른 사용자의 id를 알아도 존재 여부조차 알 수 없어야 한다 (NotFoundError).
인터페이스는 `base.py`의 Protocol을 따르므로 메서드가 비동기다 (실제 I/O는 없다).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.ai.embeddings import cosine
from app.core.errors import NotFoundError, VersionConflictError
from app.domain.contact import Contact
from app.domain.conversation import (
    Conversation,
    Episode,
    EpisodeDraft,
    EpisodeHit,
    GraphStatus,
    ImportJob,
    Message,
    NewMessage,
)
from app.domain.persona import PersonaSettings, PersonaVersion, RelationshipType
from app.domain.rewrite import Feedback, RewriteRecord, RewriteResult


def _now() -> datetime:
    return datetime.now(UTC)


class InMemoryContactRepo:
    def __init__(self) -> None:
        self._contacts: dict[UUID, Contact] = {}
        self._personas: dict[UUID, list[PersonaVersion]] = {}

    async def create(self, user_id: str, alias: str, relationship_type: RelationshipType) -> Contact:
        c = Contact(
            id=uuid4(), user_id=user_id, alias=alias, relationship_type=relationship_type, created_at=_now()
        )
        self._contacts[c.id] = c
        self._personas[c.id] = []
        return c

    async def list(self, user_id: str) -> list[Contact]:
        return sorted(
            (c for c in self._contacts.values() if c.user_id == user_id), key=lambda c: c.created_at
        )

    async def get(self, user_id: str, contact_id: UUID) -> Contact:
        c = self._contacts.get(contact_id)
        if c is None or c.user_id != user_id:
            raise NotFoundError("상대를 찾을 수 없습니다.")
        return c

    async def latest_persona(self, user_id: str, contact_id: UUID) -> PersonaVersion | None:
        await self.get(user_id, contact_id)
        versions = self._personas.get(contact_id, [])
        return versions[-1] if versions else None

    async def get_persona(self, user_id: str, contact_id: UUID, version: int) -> PersonaVersion:
        await self.get(user_id, contact_id)
        for v in self._personas.get(contact_id, []):
            if v.version == version:
                return v
        raise NotFoundError("페르소나 버전을 찾을 수 없습니다.")

    async def save_persona(
        self, user_id: str, contact_id: UUID, settings: PersonaSettings, expected_version: int | None
    ) -> PersonaVersion:
        """새 버전을 추가한다. expected_version이 현재 최신과 다르면 409."""
        await self.get(user_id, contact_id)
        versions = self._personas[contact_id]
        current = versions[-1].version if versions else 0
        if expected_version is not None and expected_version != current:
            raise VersionConflictError(f"페르소나 버전 충돌: 현재 {current}, 기대 {expected_version}")
        pv = PersonaVersion(
            id=uuid4(),
            user_id=user_id,
            contact_id=contact_id,
            version=current + 1,
            settings=settings,
            created_at=_now(),
        )
        versions.append(pv)
        return pv


class InMemoryRewriteRepo:
    def __init__(self) -> None:
        self._records: dict[UUID, RewriteRecord] = {}
        self._by_idem: dict[tuple[str, str], UUID] = {}
        self._feedback: dict[UUID, Feedback] = {}

    async def find_by_idempotency(self, user_id: str, key: str) -> RewriteRecord | None:
        rid = self._by_idem.get((user_id, key))
        return self._records.get(rid) if rid else None

    async def save(
        self,
        *,
        user_id: str,
        contact_id: UUID,
        persona_version_id: UUID | None,
        draft_version: int,
        input_hash: str,
        idempotency_key: str | None,
        result: RewriteResult,
    ) -> RewriteRecord:
        rec = RewriteRecord(
            id=uuid4(),
            user_id=user_id,
            contact_id=contact_id,
            persona_version_id=persona_version_id,
            draft_version=draft_version,
            input_hash=input_hash,
            idempotency_key=idempotency_key,
            created_at=_now(),
            result=result,
        )
        self._records[rec.id] = rec
        if idempotency_key:
            self._by_idem[(user_id, idempotency_key)] = rec.id
        return rec

    async def get(self, user_id: str, request_id: UUID) -> RewriteRecord:
        rec = self._records.get(request_id)
        if rec is None or rec.user_id != user_id:
            raise NotFoundError("추천 요청을 찾을 수 없습니다.")
        return rec

    async def save_feedback(self, user_id: str, feedback: Feedback) -> Feedback:
        await self.get(user_id, feedback.request_id)
        self._feedback[feedback.request_id] = feedback
        return feedback


class InMemoryConversationRepo:
    """대화방·메시지·묶음을 메모리에 둔다. kNN은 전수 코사인 (테스트·소규모 실험용)."""

    def __init__(self) -> None:
        self._conversations: dict[UUID, Conversation] = {}
        self._messages: dict[UUID, list[Message]] = {}
        self._episodes: dict[UUID, list[tuple[Episode, list[float]]]] = {}  # conversation_id → (episode, vec)
        self._jobs: dict[UUID, ImportJob] = {}

    async def create(
        self,
        user_id: str,
        contact_id: UUID,
        title: str,
        source: str,
        source_key: str,
        me_name: str | None = None,
    ) -> Conversation:
        for c in self._conversations.values():
            if c.user_id == user_id and c.source_key == source_key:
                raise VersionConflictError(f"같은 source_key의 대화방이 이미 있습니다: {source_key}")
        conv = Conversation(
            id=uuid4(),
            user_id=user_id,
            contact_id=contact_id,
            title=title,
            source=source,
            source_key=source_key,
            me_name=me_name,
            created_at=_now(),
        )
        self._conversations[conv.id] = conv
        self._messages[conv.id] = []
        self._episodes[conv.id] = []
        return conv

    async def list(self, user_id: str, contact_id: UUID | None = None) -> list[Conversation]:
        return sorted(
            (
                c
                for c in self._conversations.values()
                if c.user_id == user_id and (contact_id is None or c.contact_id == contact_id)
            ),
            key=lambda c: c.created_at,
        )

    async def get(self, user_id: str, conversation_id: UUID) -> Conversation:
        c = self._conversations.get(conversation_id)
        if c is None or c.user_id != user_id:
            raise NotFoundError("대화방을 찾을 수 없습니다.")
        return c

    async def add_messages(
        self, user_id: str, conversation_id: UUID, messages: list[NewMessage]
    ) -> tuple[int, int]:
        conv = await self.get(user_id, conversation_id)
        existing = {m.source_key for m in self._messages[conv.id]}
        inserted = 0
        for nm in messages:
            if nm.source_key in existing:
                continue
            existing.add(nm.source_key)
            self._messages[conv.id].append(
                Message(
                    id=uuid4(),
                    user_id=user_id,
                    conversation_id=conv.id,
                    source_key=nm.source_key,
                    speaker_role=nm.speaker_role,
                    sent_at=nm.sent_at,
                    text=nm.text,
                )
            )
            inserted += 1
        return inserted, len(messages) - inserted

    async def list_messages(self, user_id: str, conversation_id: UUID) -> list[Message]:
        conv = await self.get(user_id, conversation_id)
        return sorted(self._messages[conv.id], key=lambda m: m.sent_at)

    async def replace_episodes(
        self,
        user_id: str,
        conversation_id: UUID,
        contact_id: UUID,
        drafts: list[EpisodeDraft],
        vectors: list[list[float]],
        embedding_version: str,
    ) -> int:
        conv = await self.get(user_id, conversation_id)
        if len(drafts) != len(vectors):
            raise ValueError("묶음 수와 벡터 수가 다릅니다.")
        self._episodes[conv.id] = [
            (
                Episode(
                    id=uuid4(),
                    user_id=user_id,
                    contact_id=contact_id,
                    conversation_id=conv.id,
                    context_text=d.context_text,
                    style_text=d.style_text,
                    embedding_version=embedding_version,
                    start_at=d.start_at,
                    end_at=d.end_at,
                    source_message_ids=list(d.source_message_ids),
                ),
                vec,
            )
            for d, vec in zip(drafts, vectors, strict=True)
        ]
        return len(drafts)

    async def search_episodes(
        self,
        user_id: str,
        contact_id: UUID,
        query: list[float],
        top_k: int,
        before: datetime | None = None,
    ) -> list[EpisodeHit]:
        hits: list[EpisodeHit] = []
        for conv_id, pairs in self._episodes.items():
            conv = self._conversations[conv_id]
            if conv.user_id != user_id or conv.contact_id != contact_id:
                continue
            for ep, vec in pairs:
                if before is not None and ep.end_at >= before:
                    continue
                hits.append(EpisodeHit(episode=ep, score=cosine(query, vec)))
        hits.sort(key=lambda h: (-h.score, h.episode.end_at))
        return hits[:top_k]

    async def find_job(self, user_id: str, conversation_id: UUID, source_hash: str) -> ImportJob | None:
        for job in self._jobs.values():
            if (
                job.user_id == user_id
                and job.conversation_id == conversation_id
                and job.source_hash == source_hash
            ):
                return job
        return None

    async def save_job(self, job: ImportJob) -> ImportJob:
        self._jobs[job.id] = job
        return job

    async def get_job(self, user_id: str, job_id: UUID) -> ImportJob:
        job = self._jobs.get(job_id)
        if job is None or job.user_id != user_id:
            raise NotFoundError("가져오기 작업을 찾을 수 없습니다.")
        return job

    async def mark_graph_ingested(self, user_id: str, message_ids: list[UUID], at: datetime) -> None:
        wanted = set(message_ids)
        for conv_id, msgs in self._messages.items():
            if self._conversations[conv_id].user_id != user_id:
                continue
            self._messages[conv_id] = [
                m.model_copy(update={"graph_ingested_at": at}) if m.id in wanted else m for m in msgs
            ]

    async def update_job_graph(
        self, user_id: str, job_id: UUID, status: GraphStatus, episodes: int, error: str | None
    ) -> ImportJob:
        job = await self.get_job(user_id, job_id)
        job = job.model_copy(
            update={"graph_status": status, "graph_episodes": episodes, "graph_error": error}
        )
        self._jobs[job_id] = job
        return job
