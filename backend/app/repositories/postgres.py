"""PostgreSQL 저장소 (SQLAlchemy 2 비동기). `base.py`의 Protocol을 구현한다.

소유권은 모든 쿼리의 WHERE에 user_id를 함께 건다 — 다른 사용자의 id로는 행이 보이지 않으므로
"없음"과 "남의 것"이 같은 NotFoundError가 된다. 메서드 하나가 트랜잭션 하나다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.core.errors import NotFoundError, VersionConflictError
from app.db.models import ContactRow, FeedbackRow, PersonaVersionRow, RewriteRequestRow, RewriteResultRow
from app.db.session import SessionFactory
from app.domain.contact import Contact
from app.domain.persona import PersonaSettings, PersonaVersion, RelationshipType
from app.domain.rewrite import Feedback, RewriteRecord, RewriteResult
from app.repositories.postgres_conversations import PostgresConversationRepo, _gone

__all__ = ["PostgresContactRepo", "PostgresConversationRepo", "PostgresRewriteRepo"]


def _now() -> datetime:
    return datetime.now(UTC)


def _contact(row: ContactRow) -> Contact:
    return Contact(
        id=row.id,
        user_id=row.user_id,
        alias=row.alias,
        relationship_type=row.relationship_type,  # type: ignore[arg-type]
        created_at=row.created_at,
        graph_name=row.graph_name,
    )


def _persona(row: PersonaVersionRow) -> PersonaVersion:
    return PersonaVersion(
        id=row.id,
        user_id=row.user_id,
        contact_id=row.contact_id,
        version=row.version,
        settings=PersonaSettings.from_stored(row.settings),
        created_at=row.created_at,
    )


def _feedback(row: FeedbackRow) -> Feedback:
    return Feedback(
        request_id=row.request_id,
        accepted=row.accepted,
        edited_text=row.edited_text,
        reason=row.reason,
        consent_to_reuse=row.consent_to_reuse,
        reason_code=row.reason_code,  # type: ignore[arg-type]
        created_at=row.created_at,
    )


def _record(row: RewriteRequestRow, feedback: FeedbackRow | None = None) -> RewriteRecord:
    if row.result is None:  # 결과 없는 요청은 저장 경로상 생기지 않는다
        raise NotFoundError("추천 요청을 찾을 수 없습니다.")
    return RewriteRecord(
        id=row.id,
        user_id=row.user_id,
        contact_id=row.contact_id,
        persona_version_id=row.persona_version_id,
        draft_version=row.draft_version,
        condition=row.condition,  # type: ignore[arg-type]
        channel=row.channel or "web",  # type: ignore[arg-type]
        input_hash=row.input_hash,
        idempotency_key=row.idempotency_key,
        created_at=row.created_at,
        result=RewriteResult.model_validate(row.result.payload),
        feedback=_feedback(feedback) if feedback is not None else None,
    )


class PostgresContactRepo:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def create(self, user_id: str, alias: str, relationship_type: RelationshipType) -> Contact:
        row = ContactRow(
            id=uuid4(), user_id=user_id, alias=alias, relationship_type=relationship_type, created_at=_now()
        )
        async with self._sessions.begin() as s:
            s.add(row)
        return _contact(row)

    async def list(self, user_id: str) -> list[Contact]:
        stmt = select(ContactRow).where(ContactRow.user_id == user_id).order_by(ContactRow.created_at)
        async with self._sessions() as s:
            rows = (await s.scalars(stmt)).all()
        return [_contact(r) for r in rows]

    async def get(self, user_id: str, contact_id: UUID) -> Contact:
        async with self._sessions() as s:
            row = await self._owned_contact(s, user_id, contact_id)
        return _contact(row)

    async def latest_persona(self, user_id: str, contact_id: UUID) -> PersonaVersion | None:
        async with self._sessions() as s:
            await self._owned_contact(s, user_id, contact_id)
            stmt = (
                select(PersonaVersionRow)
                .where(PersonaVersionRow.contact_id == contact_id, PersonaVersionRow.user_id == user_id)
                .order_by(PersonaVersionRow.version.desc())
                .limit(1)
            )
            row = await s.scalar(stmt)
        return _persona(row) if row is not None else None

    async def get_persona(self, user_id: str, contact_id: UUID, version: int) -> PersonaVersion:
        async with self._sessions() as s:
            await self._owned_contact(s, user_id, contact_id)
            stmt = select(PersonaVersionRow).where(
                PersonaVersionRow.contact_id == contact_id,
                PersonaVersionRow.user_id == user_id,
                PersonaVersionRow.version == version,
            )
            row = await s.scalar(stmt)
        if row is None:
            raise NotFoundError("페르소나 버전을 찾을 수 없습니다.")
        return _persona(row)

    async def save_persona(
        self, user_id: str, contact_id: UUID, settings: PersonaSettings, expected_version: int | None
    ) -> PersonaVersion:
        """새 버전을 추가한다. expected_version이 현재 최신과 다르면 409.

        같은 상대에 동시에 저장하면 (contact_id, version) 유일 제약이 한쪽을 막는다 → 409.
        """
        try:
            async with self._sessions.begin() as s:
                await self._owned_contact(s, user_id, contact_id)
                current = await s.scalar(
                    select(func.coalesce(func.max(PersonaVersionRow.version), 0)).where(
                        PersonaVersionRow.contact_id == contact_id
                    )
                )
                current = int(current or 0)
                if expected_version is not None and expected_version != current:
                    raise VersionConflictError(f"페르소나 버전 충돌: 현재 {current}, 기대 {expected_version}")
                row = PersonaVersionRow(
                    id=uuid4(),
                    user_id=user_id,
                    contact_id=contact_id,
                    version=current + 1,
                    settings=settings.model_dump(mode="json"),
                    created_at=_now(),
                )
                s.add(row)
        except IntegrityError as exc:
            raise VersionConflictError("페르소나 버전 충돌: 동시에 저장되었습니다. 다시 시도하세요.") from exc
        return _persona(row)

    async def delete(self, user_id: str, contact_id: UUID) -> None:
        async with self._sessions.begin() as s:
            row = await self._owned_contact(s, user_id, contact_id)
            await s.delete(row)  # persona_versions·rewrite_requests(→results·feedback)·conversations CASCADE

    async def update(
        self,
        user_id: str,
        contact_id: UUID,
        *,
        alias: str | None = None,
        relationship_type: RelationshipType | None = None,
    ) -> Contact:
        async with self._sessions.begin() as s:
            row = await self._owned_contact(s, user_id, contact_id)
            if alias is not None:
                row.alias = alias
            if relationship_type is not None:
                row.relationship_type = relationship_type
        return _contact(row)

    async def list_personas(self, user_id: str, contact_id: UUID, limit: int = 50) -> list[PersonaVersion]:
        async with self._sessions() as s:
            await self._owned_contact(s, user_id, contact_id)
            stmt = (
                select(PersonaVersionRow)
                .where(PersonaVersionRow.contact_id == contact_id, PersonaVersionRow.user_id == user_id)
                .order_by(PersonaVersionRow.version.desc())
                .limit(limit)
            )
            rows = (await s.scalars(stmt)).all()
        return [_persona(r) for r in rows]

    async def latest_persona_versions(self, user_id: str) -> dict[UUID, int]:
        stmt = (
            select(PersonaVersionRow.contact_id, func.max(PersonaVersionRow.version))
            .where(PersonaVersionRow.user_id == user_id)
            .group_by(PersonaVersionRow.contact_id)
        )
        async with self._sessions() as s:
            rows = (await s.execute(stmt)).all()
        return {contact_id: int(version) for contact_id, version in rows}

    async def count(self, user_id: str) -> int:
        stmt = select(func.count()).select_from(ContactRow).where(ContactRow.user_id == user_id)
        async with self._sessions() as s:
            return int(await s.scalar(stmt) or 0)

    async def remember_graph_name(self, user_id: str, contact_id: UUID, name: str) -> Contact:
        async with self._sessions.begin() as s:
            row = await self._owned_contact(s, user_id, contact_id)
            if row.graph_name is None:
                row.graph_name = name
        return _contact(row)

    async def set_graph_name(self, user_id: str, contact_id: UUID, name: str) -> Contact:
        async with self._sessions.begin() as s:
            row = await self._owned_contact(s, user_id, contact_id)
            row.graph_name = name
        return _contact(row)

    @staticmethod
    async def _owned_contact(s, user_id: str, contact_id: UUID) -> ContactRow:  # noqa: ANN001
        row = await s.scalar(
            select(ContactRow).where(ContactRow.id == contact_id, ContactRow.user_id == user_id)
        )
        if row is None:
            raise NotFoundError("상대를 찾을 수 없습니다.")
        return row


class PostgresRewriteRepo:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def delete_by_contact(self, user_id: str, contact_id: UUID) -> int:
        async with self._sessions.begin() as s:
            result = await s.execute(
                delete(RewriteRequestRow).where(
                    RewriteRequestRow.user_id == user_id, RewriteRequestRow.contact_id == contact_id
                )
            )  # results·feedback은 CASCADE
        return int(result.rowcount or 0)

    async def find_by_idempotency(self, user_id: str, key: str) -> RewriteRecord | None:
        stmt = select(RewriteRequestRow).where(
            RewriteRequestRow.user_id == user_id, RewriteRequestRow.idempotency_key == key
        )
        async with self._sessions() as s:
            row = await s.scalar(stmt)
            fb = await s.get(FeedbackRow, row.id) if row is not None else None
        return _record(row, fb) if row is not None else None

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
        condition: str = "B",
        channel: str = "web",
    ) -> RewriteRecord:
        req = RewriteRequestRow(
            id=uuid4(),
            user_id=user_id,
            contact_id=contact_id,
            persona_version_id=persona_version_id,
            draft_version=draft_version,
            condition=condition,
            channel=channel,
            input_hash=input_hash,
            idempotency_key=idempotency_key,
            status=result.status,
            created_at=_now(),
        )
        req.result = RewriteResultRow(
            result_text=result.result_text,
            validation_status="passed" if result.validation.passed else "failed",
            model_version=result.model_version,
            prompt_version=result.prompt_version,
            retrieval_version=result.retrieval_version,
            token_usage=dict(result.token_usage),
            latency_ms=result.latency_ms,
            payload=result.model_dump(mode="json"),
        )
        try:
            async with self._sessions.begin() as s:
                s.add(req)
        except IntegrityError as exc:
            if _gone(exc):  # 첨삭하는 사이에 상대(또는 계정)가 지워졌다
                raise NotFoundError("상대를 찾을 수 없습니다.") from exc
            # 서비스가 먼저 조회하지만, 같은 키의 동시 요청은 유일 제약에서 걸린다.
            raise VersionConflictError("같은 Idempotency-Key 요청이 동시에 처리되었습니다.") from exc
        return _record(req)

    async def get(self, user_id: str, request_id: UUID) -> RewriteRecord:
        async with self._sessions() as s:
            row = await self._owned_request(s, user_id, request_id)
            fb = await s.get(FeedbackRow, row.id)
        return _record(row, fb)

    async def list(
        self,
        user_id: str,
        contact_id: UUID | None = None,
        limit: int = 20,
        before: datetime | None = None,
    ) -> list[RewriteRecord]:
        stmt = select(RewriteRequestRow).where(RewriteRequestRow.user_id == user_id)
        if contact_id is not None:
            stmt = stmt.where(RewriteRequestRow.contact_id == contact_id)
        if before is not None:
            stmt = stmt.where(RewriteRequestRow.created_at < before)
        stmt = stmt.order_by(RewriteRequestRow.created_at.desc()).limit(limit)
        async with self._sessions() as s:
            rows = (await s.scalars(stmt)).unique().all()
            feedback: dict[UUID, FeedbackRow] = {}
            if rows:  # 피드백은 한 번에 읽는다 (요청마다 따로 읽지 않는다)
                found = await s.scalars(
                    select(FeedbackRow).where(FeedbackRow.request_id.in_([r.id for r in rows]))
                )
                feedback = {f.request_id: f for f in found.all()}
        return [_record(r, feedback.get(r.id)) for r in rows if r.result is not None]

    async def save_feedback(self, user_id: str, feedback: Feedback) -> Feedback:
        async with self._sessions.begin() as s:
            await self._owned_request(s, user_id, feedback.request_id)
            # 요청당 피드백 하나. 다시 보내면 덮어쓴다.
            await s.merge(
                FeedbackRow(
                    request_id=feedback.request_id,
                    user_id=user_id,
                    accepted=feedback.accepted,
                    edited_text=feedback.edited_text,
                    reason=feedback.reason,
                    reason_code=feedback.reason_code,
                    consent_to_reuse=feedback.consent_to_reuse,
                    created_at=_now(),
                )
            )
        return feedback

    async def delete(self, user_id: str, request_id: UUID) -> None:
        async with self._sessions.begin() as s:
            row = await self._owned_request(s, user_id, request_id)
            await s.delete(row)  # 결과는 ORM cascade, 피드백은 FK ON DELETE CASCADE

    async def stats_by_contact(self, user_id: str) -> dict[UUID, tuple[int, datetime | None]]:
        stmt = (
            select(RewriteRequestRow.contact_id, func.count(), func.max(RewriteRequestRow.created_at))
            .where(RewriteRequestRow.user_id == user_id)
            .group_by(RewriteRequestRow.contact_id)
        )
        async with self._sessions() as s:
            rows = (await s.execute(stmt)).all()
        return {contact_id: (int(count), last) for contact_id, count, last in rows}

    async def purge_before(self, before: datetime) -> int:
        async with self._sessions.begin() as s:
            result = await s.execute(
                delete(RewriteRequestRow).where(RewriteRequestRow.created_at < before)
            )  # results·feedback은 FK ON DELETE CASCADE
        return int(result.rowcount or 0)

    @staticmethod
    async def _owned_request(s, user_id: str, request_id: UUID) -> RewriteRequestRow:  # noqa: ANN001
        row = await s.scalar(
            select(RewriteRequestRow).where(
                RewriteRequestRow.id == request_id, RewriteRequestRow.user_id == user_id
            )
        )
        if row is None:
            raise NotFoundError("추천 요청을 찾을 수 없습니다.")
        return row
