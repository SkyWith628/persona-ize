"""인메모리 저장소. 테스트와 DATABASE_URL이 비어 있는 실행에서 쓴다 (기본은 `postgres.py`).

모든 조회는 user_id로 격리한다. 다른 사용자의 id를 알아도 존재 여부조차 알 수 없어야 한다 (NotFoundError).
인터페이스는 `base.py`의 Protocol을 따르므로 메서드가 비동기다 (실제 I/O는 없다).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.core.errors import NotFoundError, VersionConflictError
from app.domain.contact import Contact
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
