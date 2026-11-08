"""저장소 인터페이스. 인메모리(테스트·A/B 실험)와 PostgreSQL(기본) 구현이 같은 모양을 지킨다.

모든 메서드는 user_id로 격리한다. 다른 사용자의 자원은 존재 여부조차 드러내지 않는다 (NotFoundError).
구현은 비동기다 — asyncpg 위에서 돌아가는 PostgreSQL 구현이 기준이고, 인메모리는 그 모양을 따른다.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.domain.contact import Contact
from app.domain.persona import PersonaSettings, PersonaVersion, RelationshipType
from app.domain.rewrite import Feedback, RewriteRecord, RewriteResult


class ContactRepo(Protocol):
    async def create(self, user_id: str, alias: str, relationship_type: RelationshipType) -> Contact: ...

    async def list(self, user_id: str) -> list[Contact]: ...

    async def get(self, user_id: str, contact_id: UUID) -> Contact: ...

    async def latest_persona(self, user_id: str, contact_id: UUID) -> PersonaVersion | None: ...

    async def get_persona(self, user_id: str, contact_id: UUID, version: int) -> PersonaVersion: ...

    async def save_persona(
        self, user_id: str, contact_id: UUID, settings: PersonaSettings, expected_version: int | None
    ) -> PersonaVersion:
        """새 버전을 추가한다. expected_version이 현재 최신과 다르면 VersionConflictError."""
        ...


class RewriteRepo(Protocol):
    async def find_by_idempotency(self, user_id: str, key: str) -> RewriteRecord | None: ...

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
    ) -> RewriteRecord: ...

    async def get(self, user_id: str, request_id: UUID) -> RewriteRecord: ...

    async def save_feedback(self, user_id: str, feedback: Feedback) -> Feedback: ...
