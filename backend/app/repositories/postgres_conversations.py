"""PostgreSQL 대화방·메시지·묶음 저장소. kNN은 pgvector 코사인 거리(`<=>`)로 DB 안에서 한다.

소유권 규칙은 `postgres.py`와 같다: 모든 WHERE에 user_id.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from app.core.errors import NotFoundError, VersionConflictError
from app.db.models import (
    ConversationRow,
    EpisodeMessageRow,
    EpisodeRow,
    ImportJobRow,
    MessageRow,
    UsageCounterRow,
)
from app.db.session import SessionFactory
from app.domain.conversation import (
    ContactDataStats,
    Conversation,
    ConversationStats,
    Episode,
    EpisodeDraft,
    EpisodeHit,
    GraphStatus,
    ImportJob,
    Message,
    NewMessage,
)

# 그래프 수집 상한에서 메시지 한 건을 세는 양 (domain.conversation.graph_units와 같은 식): 1 + 글자 수 // 1000
_GRAPH_UNITS = 1 + func.char_length(MessageRow.text) // 1000


def _now() -> datetime:
    return datetime.now(UTC)


def _gone(exc: IntegrityError) -> bool:
    """외래 키 위반(23503)인가 — 쓰는 사이에 대화방(또는 상대·계정)이 지워졌다."""
    code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    return code == "23503"


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


# 그래프 수집을 기다리는 메시지: 아직 안 들어갔고, 거듭 실패해 보류되지도 않았다.
_GRAPH_PENDING = (MessageRow.graph_ingested_at.is_(None), MessageRow.graph_held_at.is_(None))


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
        graph_attempts=row.graph_attempts,
        graph_held_at=row.graph_held_at,
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
        try:
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
        except IntegrityError as exc:
            if _gone(exc):
                raise NotFoundError("대화방을 찾을 수 없습니다.") from exc
            # 같은 메시지를 다른 요청이 방금 넣었다
            # (다른 프로세스와의 경합 — 한 프로세스 안은 서비스가 직렬화한다).
            raise VersionConflictError(
                "같은 대화방에 다른 가져오기가 진행 중입니다. 잠시 뒤 다시 시도하세요."
            ) from exc
        return inserted, len(messages) - inserted

    async def list_messages(
        self, user_id: str, conversation_id: UUID, limit: int | None = None
    ) -> list[Message]:
        async with self._sessions() as s:
            conv = await self._owned(s, user_id, conversation_id)
            stmt = select(MessageRow).where(
                MessageRow.conversation_id == conv.id, MessageRow.user_id == user_id
            )
            if limit is None:
                stmt = stmt.order_by(MessageRow.sent_at, MessageRow.created_at)
            else:  # 가장 최근 limit건을 고른 뒤 아래에서 시각 순으로 뒤집는다
                stmt = stmt.order_by(MessageRow.sent_at.desc(), MessageRow.created_at.desc()).limit(limit)
            rows = (await s.scalars(stmt)).all()
        if limit is not None:
            rows = list(reversed(rows))
        return [_message(r) for r in rows]

    async def get_message(self, user_id: str, conversation_id: UUID, message_id: UUID) -> Message:
        async with self._sessions() as s:
            conv = await self._owned(s, user_id, conversation_id)
            row = await s.scalar(
                select(MessageRow).where(
                    MessageRow.id == message_id,
                    MessageRow.conversation_id == conv.id,
                    MessageRow.user_id == user_id,
                )
            )
        if row is None:
            raise NotFoundError("메시지를 찾을 수 없습니다.")
        return _message(row)

    async def swap_speaker_roles(self, user_id: str, conversation_id: UUID) -> int:
        async with self._sessions.begin() as s:
            conv = await self._owned(s, user_id, conversation_id)
            result = await s.execute(
                update(MessageRow)
                .where(MessageRow.conversation_id == conv.id, MessageRow.user_id == user_id)
                .values(speaker_role=case((MessageRow.speaker_role == "me", "contact"), else_="me"))
            )
        return int(result.rowcount or 0)

    async def message_keys(self, user_id: str, conversation_id: UUID) -> set[str]:
        async with self._sessions() as s:
            conv = await self._owned(s, user_id, conversation_id)
            keys = await s.scalars(select(MessageRow.source_key).where(MessageRow.conversation_id == conv.id))
            return set(keys.all())

    async def count_owned(self, user_id: str) -> tuple[int, int]:
        async with self._sessions() as s:
            convs = await s.scalar(
                select(func.count()).select_from(ConversationRow).where(ConversationRow.user_id == user_id)
            )
            msgs = await s.scalar(
                select(func.count()).select_from(MessageRow).where(MessageRow.user_id == user_id)
            )
        return int(convs or 0), int(msgs or 0)

    async def episode_vectors(self, user_id: str, conversation_id: UUID) -> dict[str, list[float]]:
        async with self._sessions() as s:
            conv = await self._owned(s, user_id, conversation_id)
            rows = (
                await s.execute(
                    select(EpisodeRow.embedding_version, EpisodeRow.context_text, EpisodeRow.embedding).where(
                        EpisodeRow.conversation_id == conv.id, EpisodeRow.user_id == user_id
                    )
                )
            ).all()
        return {f"{version}\n{text}": [float(x) for x in vector] for version, text, vector in rows}

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
        try:
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
        except IntegrityError as exc:
            if _gone(exc):
                raise NotFoundError("대화방을 찾을 수 없습니다.") from exc
            # (사용자, 대화방, 내용 해시)가 같은 작업이 이미 있다 — 같은 파일을 동시에 올렸다.
            raise VersionConflictError("같은 내용의 가져오기 작업이 이미 있습니다.") from exc
        return job

    async def processing_jobs(self) -> list[ImportJob]:
        async with self._sessions() as s:
            rows = (
                await s.execute(
                    select(ImportJobRow)
                    .where(ImportJobRow.status == "processing")
                    .order_by(ImportJobRow.created_at, ImportJobRow.id)
                )
            ).scalars()
            return [_job(row) for row in rows]

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
            rows = (await s.scalars(select(MessageRow.user_id).where(*_GRAPH_PENDING).distinct())).all()
        return sorted(rows)

    async def record_graph_failure(self, user_id: str, message_id: UUID, hold: bool, at: datetime) -> None:
        values: dict[str, Any] = {"graph_attempts": MessageRow.graph_attempts + 1}
        if hold:
            values["graph_held_at"] = at
        async with self._sessions.begin() as s:
            await s.execute(
                update(MessageRow)
                .where(MessageRow.id == message_id, MessageRow.user_id == user_id)
                .values(**values)
            )

    async def release_held_graph(self, user_id: str | None = None) -> dict[str, int]:
        stmt = (
            update(MessageRow)
            .where(MessageRow.graph_ingested_at.is_(None), MessageRow.graph_held_at.is_not(None))
            .values(graph_attempts=0, graph_held_at=None)
            .returning(MessageRow.user_id)
        )
        if user_id is not None:
            stmt = stmt.where(MessageRow.user_id == user_id)
        released: dict[str, int] = {}
        async with self._sessions.begin() as s:
            for owner in (await s.scalars(stmt)).all():
                released[owner] = released.get(owner, 0) + 1
        return released

    async def close_stale_graph_jobs(self, user_id: str) -> int:
        pending_convs = (
            select(MessageRow.conversation_id)
            .where(MessageRow.user_id == user_id, *_GRAPH_PENDING)
            .distinct()
        )
        async with self._sessions.begin() as s:
            result = await s.execute(
                update(ImportJobRow)
                .where(
                    ImportJobRow.user_id == user_id,
                    ImportJobRow.graph_status.in_(("pending", "running", "limited")),
                    ImportJobRow.conversation_id.not_in(pending_convs),
                )
                .values(graph_status="completed", graph_error=None)
            )
        return int(result.rowcount or 0)

    async def reset_graph_ingested(self, user_id: str, conversation_id: UUID | None = None) -> int:
        stmt = (
            update(MessageRow)
            .where(
                MessageRow.user_id == user_id,
                or_(MessageRow.graph_ingested_at.is_not(None), MessageRow.graph_attempts > 0),
            )
            .values(graph_ingested_at=None, graph_attempts=0, graph_held_at=None)
        )
        if conversation_id is not None:
            stmt = stmt.where(MessageRow.conversation_id == conversation_id)
        async with self._sessions.begin() as s:
            result = await s.execute(stmt)
        return int(result.rowcount or 0)

    async def purge_jobs(self, before: datetime) -> int:
        async with self._sessions.begin() as s:
            result = await s.execute(
                delete(ImportJobRow).where(
                    ImportJobRow.created_at < before,
                    ImportJobRow.status != "processing",
                    ImportJobRow.graph_status.not_in(("pending", "running")),
                )
            )
        return int(result.rowcount or 0)

    async def conversation_stats(self, user_id: str, conversation_id: UUID) -> ConversationStats:
        async with self._sessions() as s:
            conv = await self._owned(s, user_id, conversation_id)
            total, mine, ingested, held, first, last = (
                await s.execute(
                    select(
                        func.count(MessageRow.id),
                        func.count(MessageRow.id).filter(MessageRow.speaker_role == "me"),
                        func.count(MessageRow.graph_ingested_at),
                        func.count(MessageRow.id).filter(
                            MessageRow.graph_ingested_at.is_(None), MessageRow.graph_held_at.is_not(None)
                        ),
                        func.min(MessageRow.sent_at),
                        func.max(MessageRow.sent_at),
                    ).where(MessageRow.conversation_id == conv.id, MessageRow.user_id == user_id)
                )
            ).one()
            episodes = await s.scalar(
                select(func.count()).select_from(EpisodeRow).where(EpisodeRow.conversation_id == conv.id)
            )
        return ConversationStats(
            conversation_id=conv.id,
            messages=int(total or 0),
            my_messages=int(mine or 0),
            episodes=int(episodes or 0),
            graph_ingested=int(ingested or 0),
            graph_held=int(held or 0),
            first_message_at=first,
            last_message_at=last,
        )

    async def latest_job(self, user_id: str, conversation_id: UUID) -> ImportJob | None:
        stmt = (
            select(ImportJobRow)
            .where(ImportJobRow.user_id == user_id, ImportJobRow.conversation_id == conversation_id)
            .order_by(ImportJobRow.created_at.desc())
            .limit(1)
        )
        async with self._sessions() as s:
            row = await s.scalar(stmt)
        return _job(row) if row is not None else None

    async def contact_stats(
        self, user_id: str, contact_id: UUID | None = None
    ) -> dict[UUID, ContactDataStats]:
        """상대가 많아도 쿼리 두 번: 대화방⟕메시지 집계 + 묶음 집계."""
        messages = (
            select(
                ConversationRow.contact_id,
                func.count(func.distinct(ConversationRow.id)),
                func.count(MessageRow.id),
                func.count(MessageRow.id).filter(MessageRow.speaker_role == "me"),
                func.count(MessageRow.graph_ingested_at),
            )
            .select_from(ConversationRow)
            .outerjoin(MessageRow, MessageRow.conversation_id == ConversationRow.id)
            .where(ConversationRow.user_id == user_id)
            .group_by(ConversationRow.contact_id)
        )
        episodes = (
            select(EpisodeRow.contact_id, func.count())
            .where(EpisodeRow.user_id == user_id)
            .group_by(EpisodeRow.contact_id)
        )
        if contact_id is not None:
            messages = messages.where(ConversationRow.contact_id == contact_id)
            episodes = episodes.where(EpisodeRow.contact_id == contact_id)
        async with self._sessions() as s:
            message_rows = (await s.execute(messages)).all()
            episode_rows = dict((await s.execute(episodes)).all())
        return {
            cid: ContactDataStats(
                contact_id=cid,
                conversations=int(convs),
                messages=int(total),
                my_messages=int(mine),
                episodes=int(episode_rows.get(cid, 0)),
                graph_ingested=int(ingested),
            )
            for cid, convs, total, mine, ingested in message_rows
        }

    async def count_graph_ingested(self, user_id: str | None = None, since: datetime | None = None) -> int:
        stmt = select(func.count()).select_from(MessageRow).where(MessageRow.graph_ingested_at.is_not(None))
        if user_id is not None:
            stmt = stmt.where(MessageRow.user_id == user_id)
        if since is not None:
            stmt = stmt.where(MessageRow.graph_ingested_at >= since)
        async with self._sessions() as s:
            return int(await s.scalar(stmt) or 0)

    async def find_messages(self, user_id: str, refs: list[str]) -> dict[str, Message]:
        if not refs:
            return {}
        async with self._sessions() as s:
            convs = (
                await s.execute(
                    select(ConversationRow.id, ConversationRow.source_key).where(
                        ConversationRow.user_id == user_id
                    )
                )
            ).all()
            # 참조는 `대화방키:메시지키`. 대화방 키에 ':'이 들어 있어도 되도록 접두로 맞춘다.
            wanted: dict[UUID, dict[str, str]] = {}  # conversation_id → {메시지키: 참조}
            for conv_id, conv_key in convs:
                prefix = f"{conv_key}:"
                for ref in refs:
                    if ref.startswith(prefix):
                        wanted.setdefault(conv_id, {})[ref[len(prefix) :]] = ref
            if not wanted:
                return {}
            rows = (
                await s.scalars(
                    select(MessageRow).where(
                        MessageRow.user_id == user_id,
                        or_(
                            *(
                                and_(
                                    MessageRow.conversation_id == conv_id,
                                    MessageRow.source_key.in_(list(keys)),
                                )
                                for conv_id, keys in wanted.items()
                            )
                        ),
                    )
                )
            ).all()
        return {wanted[r.conversation_id][r.source_key]: _message(r) for r in rows}

    async def graph_units_in_graph(self, user_id: str) -> int:
        stmt = select(func.coalesce(func.sum(_GRAPH_UNITS), 0)).where(
            MessageRow.user_id == user_id, MessageRow.graph_ingested_at.is_not(None)
        )
        async with self._sessions() as s:
            return int(await s.scalar(stmt) or 0)

    async def add_graph_usage(self, user_id: str, day: date, units: int) -> None:
        if units == 0:
            return
        now = _now()
        stmt = (
            pg_insert(UsageCounterRow)
            .values(
                user_id=user_id,
                day=day,
                rewrites=0,
                input_tokens=0,
                output_tokens=0,
                graph_episodes=max(units, 0),
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[UsageCounterRow.user_id, UsageCounterRow.day],
                set_={
                    "graph_episodes": func.greatest(UsageCounterRow.graph_episodes + units, 0),
                    "updated_at": now,
                },
            )
        )
        async with self._sessions.begin() as s:
            await s.execute(stmt)

    async def graph_usage(self, day: date, user_id: str | None = None) -> int:
        stmt = select(func.coalesce(func.sum(UsageCounterRow.graph_episodes), 0)).where(
            UsageCounterRow.day == day
        )
        if user_id is not None:
            stmt = stmt.where(UsageCounterRow.user_id == user_id)
        async with self._sessions() as s:
            return int(await s.scalar(stmt) or 0)

    async def limit_stale_graph_jobs(self, user_id: str, reason: str) -> int:
        pending_convs = (
            select(MessageRow.conversation_id)
            .where(MessageRow.user_id == user_id, *_GRAPH_PENDING)
            .distinct()
        )
        async with self._sessions.begin() as s:
            result = await s.execute(
                update(ImportJobRow)
                .where(
                    ImportJobRow.user_id == user_id,
                    ImportJobRow.graph_status.in_(("pending", "running")),
                    ImportJobRow.conversation_id.in_(pending_convs),
                )
                .values(graph_status="limited", graph_error=reason)
            )
        return int(result.rowcount or 0)
