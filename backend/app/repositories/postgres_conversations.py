"""PostgreSQL 대화방·메시지·묶음 저장소. kNN은 pgvector 코사인 거리(`<=>`)로 DB 안에서 한다.

소유권 규칙은 `postgres.py`와 같다: 모든 WHERE에 user_id.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.errors import NotFoundError, VersionConflictError
from app.db.models import ConversationRow, EpisodeMessageRow, EpisodeRow, ImportJobRow, MessageRow
from app.db.session import SessionFactory
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


def _now() -> datetime:
    return datetime.now(UTC)


def _conversation(row: ConversationRow) -> Conversation:
    return Conversation(
        id=row.id,
        user_id=row.user_id,
        contact_id=row.contact_id,
        title=row.title,
        source=row.source,
        source_key=row.source_key,
        me_name=row.me_name,
        created_at=row.created_at,
    )


def _message(row: MessageRow) -> Message:
    return Message(
        id=row.id,
        user_id=row.user_id,
        conversation_id=row.conversation_id,
        source_key=row.source_key,
        speaker_role=row.speaker_role,  # type: ignore[arg-type]
        sent_at=row.sent_at,
        text=row.text,
        graph_ingested_at=row.graph_ingested_at,
    )


def _episode(row: EpisodeRow) -> Episode:
    return Episode(
        id=row.id,
        user_id=row.user_id,
        contact_id=row.contact_id,
        conversation_id=row.conversation_id,
        context_text=row.context_text,
        style_text=row.style_text,
        embedding_version=row.embedding_version,
        start_at=row.start_at,
        end_at=row.end_at,
        source_message_ids=list(row.source_message_ids),
    )


def _job(row: ImportJobRow) -> ImportJob:
    return ImportJob(
        id=row.id,
        user_id=row.user_id,
        conversation_id=row.conversation_id,
        source_hash=row.source_hash,
        status=row.status,  # type: ignore[arg-type]
        imported=row.imported,
        duplicates=row.duplicates,
        episodes=row.episodes,
        error_code=row.error_code,
        error_message=row.error_message,
        graph_status=row.graph_status,  # type: ignore[arg-type]
        graph_episodes=row.graph_episodes,
        graph_error=row.graph_error,
        created_at=row.created_at,
    )


class PostgresConversationRepo:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def create(
        self,
        user_id: str,
        contact_id: UUID,
        title: str,
        source: str,
        source_key: str,
        me_name: str | None = None,
    ) -> Conversation:
        row = ConversationRow(
            id=uuid4(),
            user_id=user_id,
            contact_id=contact_id,
            title=title,
            source=source,
            source_key=source_key,
            me_name=me_name,
            created_at=_now(),
        )
        try:
            async with self._sessions.begin() as s:
                s.add(row)
        except IntegrityError as exc:
            raise VersionConflictError(f"같은 source_key의 대화방이 이미 있습니다: {source_key}") from exc
        return _conversation(row)

    async def list(self, user_id: str, contact_id: UUID | None = None) -> list[Conversation]:
        stmt = select(ConversationRow).where(ConversationRow.user_id == user_id)
        if contact_id is not None:
            stmt = stmt.where(ConversationRow.contact_id == contact_id)
        async with self._sessions() as s:
            rows = (await s.scalars(stmt.order_by(ConversationRow.created_at))).all()
        return [_conversation(r) for r in rows]

    async def get(self, user_id: str, conversation_id: UUID) -> Conversation:
        async with self._sessions() as s:
            row = await self._owned(s, user_id, conversation_id)
        return _conversation(row)

    async def add_messages(
        self, user_id: str, conversation_id: UUID, messages: list[NewMessage]
    ) -> tuple[int, int]:
        async with self._sessions.begin() as s:
            conv = await self._owned(s, user_id, conversation_id)
            existing = set(
                (
                    await s.scalars(
                        select(MessageRow.source_key).where(MessageRow.conversation_id == conv.id)
                    )
                ).all()
            )
            inserted = 0
            now = _now()
            for nm in messages:
                if nm.source_key in existing:
                    continue
                existing.add(nm.source_key)
                s.add(
                    MessageRow(
                        id=uuid4(),
                        user_id=user_id,
                        conversation_id=conv.id,
                        source_key=nm.source_key,
                        speaker_role=nm.speaker_role,
                        sent_at=nm.sent_at,
                        text=nm.text,
                        created_at=now,
                    )
                )
                inserted += 1
        return inserted, len(messages) - inserted

    async def list_messages(self, user_id: str, conversation_id: UUID) -> list[Message]:
        async with self._sessions() as s:
            conv = await self._owned(s, user_id, conversation_id)
            rows = (
                await s.scalars(
                    select(MessageRow)
                    .where(MessageRow.conversation_id == conv.id, MessageRow.user_id == user_id)
                    .order_by(MessageRow.sent_at, MessageRow.created_at)
                )
            ).all()
        return [_message(r) for r in rows]

    async def replace_episodes(
        self,
        user_id: str,
        conversation_id: UUID,
        contact_id: UUID,
        drafts: list[EpisodeDraft],
        vectors: list[list[float]],
        embedding_version: str,
    ) -> int:
        if len(drafts) != len(vectors):
            raise ValueError("묶음 수와 벡터 수가 다릅니다.")
        async with self._sessions.begin() as s:
            conv = await self._owned(s, user_id, conversation_id)
            await s.execute(
                delete(EpisodeRow).where(EpisodeRow.conversation_id == conv.id, EpisodeRow.user_id == user_id)
            )
            now = _now()
            episode_ids = [uuid4() for _ in drafts]
            for ep_id, d, vec in zip(episode_ids, drafts, vectors, strict=True):
                s.add(
                    EpisodeRow(
                        id=ep_id,
                        user_id=user_id,
                        contact_id=contact_id,
                        conversation_id=conv.id,
                        context_text=d.context_text,
                        style_text=d.style_text,
                        embedding=vec,
                        embedding_version=embedding_version,
                        start_at=d.start_at,
                        end_at=d.end_at,
                        source_message_ids=list(d.source_message_ids),
                        created_at=now,
                    )
                )
            # 두 모델 사이에 relationship이 없어 flush 순서가 FK를 따르지 않았다 → 묶음을 먼저 확정한다.
            await s.flush()
            for ep_id, d in zip(episode_ids, drafts, strict=True):
                for pos, mid in enumerate(d.message_ids):
                    s.add(EpisodeMessageRow(episode_id=ep_id, message_id=mid, position=pos))
        return len(drafts)

    async def search_episodes(
        self,
        user_id: str,
        contact_id: UUID,
        query: list[float],
        top_k: int,
        before: datetime | None = None,
    ) -> list[EpisodeHit]:
        distance = EpisodeRow.embedding.cosine_distance(query).label("distance")
        stmt = select(EpisodeRow, distance).where(
            EpisodeRow.user_id == user_id, EpisodeRow.contact_id == contact_id
        )
        if before is not None:
            stmt = stmt.where(EpisodeRow.end_at < before)
        stmt = stmt.order_by(distance, EpisodeRow.end_at).limit(top_k)
        async with self._sessions() as s:
            rows = (await s.execute(stmt)).all()
        return [EpisodeHit(episode=_episode(row), score=1.0 - float(dist)) for row, dist in rows]

    async def find_job(self, user_id: str, conversation_id: UUID, source_hash: str) -> ImportJob | None:
        stmt = select(ImportJobRow).where(
            ImportJobRow.user_id == user_id,
            ImportJobRow.conversation_id == conversation_id,
            ImportJobRow.source_hash == source_hash,
        )
        async with self._sessions() as s:
            row = await s.scalar(stmt)
        return _job(row) if row is not None else None

    async def save_job(self, job: ImportJob) -> ImportJob:
        async with self._sessions.begin() as s:
            await s.merge(
                ImportJobRow(
                    id=job.id,
                    user_id=job.user_id,
                    conversation_id=job.conversation_id,
                    source_hash=job.source_hash,
                    status=job.status,
                    imported=job.imported,
                    duplicates=job.duplicates,
                    episodes=job.episodes,
                    error_code=job.error_code,
                    error_message=job.error_message,
                    graph_status=job.graph_status,
                    graph_episodes=job.graph_episodes,
                    graph_error=job.graph_error,
                    created_at=job.created_at,
                )
            )
        return job

    async def get_job(self, user_id: str, job_id: UUID) -> ImportJob:
        async with self._sessions() as s:
            row = await s.scalar(
                select(ImportJobRow).where(ImportJobRow.id == job_id, ImportJobRow.user_id == user_id)
            )
        if row is None:
            raise NotFoundError("가져오기 작업을 찾을 수 없습니다.")
        return _job(row)

    @staticmethod
    async def _owned(s, user_id: str, conversation_id: UUID) -> ConversationRow:  # noqa: ANN001
        row = await s.scalar(
            select(ConversationRow).where(
                ConversationRow.id == conversation_id, ConversationRow.user_id == user_id
            )
        )
        if row is None:
            raise NotFoundError("대화방을 찾을 수 없습니다.")
        return row

    async def mark_graph_ingested(self, user_id: str, message_ids: list[UUID], at: datetime) -> None:
        if not message_ids:
            return
        async with self._sessions.begin() as s:
            await s.execute(
                update(MessageRow)
                .where(MessageRow.user_id == user_id, MessageRow.id.in_(message_ids))
                .values(graph_ingested_at=at)
            )

    async def update_job_graph(
        self, user_id: str, job_id: UUID, status: GraphStatus, episodes: int, error: str | None
    ) -> ImportJob:
        async with self._sessions.begin() as s:
            row = await s.scalar(
                select(ImportJobRow).where(ImportJobRow.id == job_id, ImportJobRow.user_id == user_id)
            )
            if row is None:
                raise NotFoundError("가져오기 작업을 찾을 수 없습니다.")
            row.graph_status = status
            row.graph_episodes = episodes
            row.graph_error = error
        return _job(row)

    async def set_me_name(self, user_id: str, conversation_id: UUID, me_name: str | None) -> Conversation:
        async with self._sessions.begin() as s:
            row = await self._owned(s, user_id, conversation_id)
            row.me_name = me_name
        return _conversation(row)

    async def delete_conversation(self, user_id: str, conversation_id: UUID) -> tuple[int, int]:
        async with self._sessions.begin() as s:
            conv = await self._owned(s, user_id, conversation_id)
            messages = int(
                await s.scalar(
                    select(func.count()).select_from(MessageRow).where(MessageRow.conversation_id == conv.id)
                )
                or 0
            )
            episodes = int(
                await s.scalar(
                    select(func.count()).select_from(EpisodeRow).where(EpisodeRow.conversation_id == conv.id)
                )
                or 0
            )
            # messages·episodes·episode_messages·import_jobs는 FK ON DELETE CASCADE
            await s.delete(conv)
        return messages, episodes

    async def users_with_pending_graph(self) -> list[str]:
        async with self._sessions() as s:
            rows = (
                await s.scalars(
                    select(MessageRow.user_id).where(MessageRow.graph_ingested_at.is_(None)).distinct()
                )
            ).all()
        return sorted(rows)

    async def close_stale_graph_jobs(self, user_id: str) -> int:
        pending_convs = (
            select(MessageRow.conversation_id)
            .where(MessageRow.user_id == user_id, MessageRow.graph_ingested_at.is_(None))
            .distinct()
        )
        async with self._sessions.begin() as s:
            result = await s.execute(
                update(ImportJobRow)
                .where(
                    ImportJobRow.user_id == user_id,
                    ImportJobRow.graph_status.in_(("pending", "running")),
                    ImportJobRow.conversation_id.not_in(pending_convs),
                )
                .values(graph_status="completed")
            )
        return int(result.rowcount or 0)

    async def reset_graph_ingested(self, user_id: str, conversation_id: UUID | None = None) -> int:
        stmt = (
            update(MessageRow)
            .where(MessageRow.user_id == user_id, MessageRow.graph_ingested_at.is_not(None))
            .values(graph_ingested_at=None)
        )
        if conversation_id is not None:
            stmt = stmt.where(MessageRow.conversation_id == conversation_id)
        async with self._sessions.begin() as s:
            result = await s.execute(stmt)
        return int(result.rowcount or 0)
