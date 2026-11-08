"""저장소 인터페이스. 인메모리(테스트·A/B 실험)와 PostgreSQL(기본) 구현이 같은 모양을 지킨다.

모든 메서드는 user_id로 격리한다. 다른 사용자의 자원은 존재 여부조차 드러내지 않는다 (NotFoundError).
구현은 비동기다 — asyncpg 위에서 돌아가는 PostgreSQL 구현이 기준이고, 인메모리는 그 모양을 따른다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.domain.contact import Contact
from app.domain.conversation import (
    Conversation,
    EpisodeDraft,
    EpisodeHit,
    GraphStatus,
    ImportJob,
    Message,
    NewMessage,
)
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

    async def delete(self, user_id: str, contact_id: UUID) -> None:
        """상대·페르소나 버전·첨삭 이력을 지운다 (대화방은 먼저 지워야 한다). 없으면 NotFoundError."""
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
        condition: str = "B",
    ) -> RewriteRecord: ...

    async def get(self, user_id: str, request_id: UUID) -> RewriteRecord: ...

    async def list(
        self, user_id: str, contact_id: UUID | None = None, limit: int = 20
    ) -> list[RewriteRecord]:
        """최근 순. contact_id가 있으면 그 상대 것만."""
        ...

    async def save_feedback(self, user_id: str, feedback: Feedback) -> Feedback: ...

    async def delete_by_contact(self, user_id: str, contact_id: UUID) -> int:
        """그 상대의 첨삭 요청·결과·피드백을 지운다 (상대 삭제). 지운 요청 수."""
        ...


class ConversationRepo(Protocol):
    """대화방·메시지·검색 단위(episode)·가져오기 작업. 임베딩 벡터는 저장소가 들고 kNN도 저장소가 한다."""

    async def create(
        self,
        user_id: str,
        contact_id: UUID,
        title: str,
        source: str,
        source_key: str,
        me_name: str | None = None,
    ) -> Conversation:
        """(user_id, source_key)가 이미 있으면 VersionConflictError."""
        ...

    async def list(self, user_id: str, contact_id: UUID | None = None) -> list[Conversation]: ...

    async def get(self, user_id: str, conversation_id: UUID) -> Conversation: ...

    async def add_messages(
        self, user_id: str, conversation_id: UUID, messages: list[NewMessage]
    ) -> tuple[int, int]:
        """(새로 넣은 수, source_key가 겹쳐 건너뛴 수)."""
        ...

    async def list_messages(self, user_id: str, conversation_id: UUID) -> list[Message]:
        """sent_at 순."""
        ...

    async def replace_episodes(
        self,
        user_id: str,
        conversation_id: UUID,
        contact_id: UUID,
        drafts: list[EpisodeDraft],
        vectors: list[list[float]],
        embedding_version: str,
    ) -> int:
        """이 대화방의 기존 묶음을 지우고 새로 넣는다. 넣은 수를 돌려준다."""
        ...

    async def search_episodes(
        self,
        user_id: str,
        contact_id: UUID,
        query: list[float],
        top_k: int,
        before: datetime | None = None,
    ) -> list[EpisodeHit]:
        """같은 사용자·같은 상대, end_at < before 인 묶음을 코사인 유사도 순으로."""
        ...

    async def find_job(self, user_id: str, conversation_id: UUID, source_hash: str) -> ImportJob | None: ...

    async def save_job(self, job: ImportJob) -> ImportJob: ...

    async def get_job(self, user_id: str, job_id: UUID) -> ImportJob: ...

    async def mark_graph_ingested(self, user_id: str, message_ids: list[UUID], at: datetime) -> None:
        """그래프에 넣은 메시지를 표시한다 (재실행 멱등)."""
        ...

    async def update_job_graph(
        self, user_id: str, job_id: UUID, status: GraphStatus, episodes: int, error: str | None
    ) -> ImportJob: ...

    async def set_me_name(self, user_id: str, conversation_id: UUID, me_name: str | None) -> Conversation:
        """그래프 에피소드용 사용자 표시 이름을 바꾼다. 이미 수집된 에피소드는 바뀌지 않는다."""
        ...

    async def delete_conversation(self, user_id: str, conversation_id: UUID) -> tuple[int, int]:
        """대화방과 메시지·묶음·가져오기 작업을 지운다.

        (지운 메시지 수, 지운 묶음 수). 없으면 NotFoundError.
        """
        ...

    async def users_with_pending_graph(self) -> list[str]:
        """그래프 미수집 메시지가 있는 사용자 id들 (서버 시작 시 이어받기)."""
        ...

    async def close_stale_graph_jobs(self, user_id: str) -> int:
        """미수집 메시지가 남지 않은 대화방의 pending/running 작업을 completed로. 바꾼 수."""
        ...
