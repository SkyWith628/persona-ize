"""테이블 정의 (project-design §6). 도메인 모델(`app/domain`)과는 분리하고 저장소가 변환한다.

- UUID 기본키, 시각은 timestamptz(UTC).
- 모든 사용자 데이터 테이블은 user_id 열을 직접 가진다 — 조인 없이 소유권 조건을 걸기 위해서다.
- 설정·결과처럼 모양이 바뀌는 값은 JSONB에 통째로 두고, 집계에 쓰는 열만 따로 뺀다.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# episodes.embedding의 차원. 마이그레이션 0002와 같아야 한다. 모델을 바꾸면 새 마이그레이션으로 재색인.
EMBEDDING_DIM = 384


class Base(DeclarativeBase):
    pass


class ContactRow(Base):
    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    alias: Mapped[str] = mapped_column(String(50), nullable=False)
    relationship_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # 이 상대의 메시지를 처음 그래프에 넣을 때의 이름 (0009). 이름을 바꿔도 그래프 노드는 이 이름이다.
    graph_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_contacts_user_created", "user_id", "created_at"),)


class PersonaVersionRow(Base):
    __tablename__ = "persona_versions"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("contact_id", "version", name="uq_persona_versions_contact_version"),
        Index("ix_persona_versions_user_contact", "user_id", "contact_id"),
    )


class RewriteRequestRow(Base):
    __tablename__ = "rewrite_requests"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    persona_version_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("persona_versions.id", ondelete="SET NULL"), nullable=True
    )
    draft_version: Mapped[int] = mapped_column(Integer, nullable=False)
    condition: Mapped[str] = mapped_column(String(1), nullable=False, default="B")  # A/B/C/D (0007)
    # 요청이 온 곳: web(PC 웹) | mobile | desktop | api (0008). 관리자 화면의 채널 집계에 쓴다.
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="web", server_default="web")
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # 결과는 요청과 1:1. 항상 함께 읽으므로 조인 로딩으로 고정한다 (비동기에서 지연 로딩 금지).
    result: Mapped[RewriteResultRow | None] = relationship(
        back_populates="request", uselist=False, lazy="joined", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # NULL 키는 유일성 검사에서 빠지므로 키 없는 요청은 몇 번이든 저장된다.
        UniqueConstraint("user_id", "idempotency_key", name="uq_rewrite_requests_user_idem"),
        Index("ix_rewrite_requests_user_created", "user_id", "created_at"),
    )


class RewriteResultRow(Base):
    __tablename__ = "rewrite_results"

    request_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("rewrite_requests.id", ondelete="CASCADE"), primary_key=True
    )
    result_text: Mapped[str] = mapped_column(Text, nullable=False)
    validation_status: Mapped[str] = mapped_column(String(16), nullable=False)  # passed | failed
    # 검색기 버전은 "knn-style-v1/st:intfloat/multilingual-e5-small:384:v1+graph-context-v1"처럼 길다 (0004)
    model_version: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(200), nullable=False)
    retrieval_version: Mapped[str] = mapped_column(String(200), nullable=False)
    token_usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    # RewriteResult 전체 (보존 항목·검증 보고·근거 id 포함). 도메인 모델이 바뀌어도 열을 늘리지 않기 위해.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    request: Mapped[RewriteRequestRow] = relationship(back_populates="result")


class FeedbackRow(Base):
    __tablename__ = "feedback"

    request_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("rewrite_requests.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    edited_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 선택형 사유 (0008). 값 목록은 app/domain/rewrite.py의 FeedbackReasonCode
    reason_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    consent_to_reuse: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConversationRow(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    me_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "source_key", name="uq_conversations_user_source_key"),
        Index("ix_conversations_user_contact", "user_id", "contact_id"),
    )


class ImportJobRow(Base):
    __tablename__ = "import_jobs"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # processing | completed | failed
    imported: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicates: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    episodes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    graph_status: Mapped[str] = mapped_column(String(16), nullable=False, default="skipped")
    graph_episodes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    graph_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "conversation_id", "source_hash", name="uq_import_jobs_user_conv_hash"),
    )


class MessageRow(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    source_key: Mapped[str] = mapped_column(String(64), nullable=False)
    speaker_role: Mapped[str] = mapped_column(String(8), nullable=False)  # me | contact
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    graph_ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("conversation_id", "source_key", name="uq_messages_conversation_source_key"),
        Index("ix_messages_user_conversation_sent", "user_id", "conversation_id", "sent_at"),
    )


class EpisodeRow(Base):
    """검색 단위. 벡터 차원은 마이그레이션(0002)에 384로 고정.

    임베딩 모델을 바꾸면 재색인 + 새 마이그레이션.
    """

    __tablename__ = "episodes"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    contact_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    context_text: Mapped[str] = mapped_column(Text, nullable=False)
    style_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    embedding_version: Mapped[str] = mapped_column(String(128), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_message_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_episodes_user_contact_end", "user_id", "contact_id", "end_at"),)


class EpisodeMessageRow(Base):
    """묶음 ↔ 메시지 연결. 원문 삭제 시 파생 묶음을 찾는 데 쓴다."""

    __tablename__ = "episode_messages"

    episode_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("episodes.id", ondelete="CASCADE"), primary_key=True
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class UsageCounterRow(Base):
    """사용자·일(KST) 단위 사용량. 한도 검사는 `repositories/usage.py`의 원자적 UPSERT로."""

    __tablename__ = "usage_counters"

    user_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    rewrites: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 그날 그래프에 넣은 양 (0009). 메시지 1건 = 1, 긴 메시지는 가중. 대화를 지워도 줄지 않는다.
    graph_episodes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    username: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 약관·개인정보 안내 동의 (0008). 동의한 적 없으면 둘 다 NULL.
    consented_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consent_version: Mapped[str | None] = mapped_column(String(32), nullable=True)


class AdminAccessLogRow(Base):
    """관리자 화면·API 접근 기록 (0008). 대화 원문은 관리자 화면에 나오지 않지만 누가 언제 봤는지는 남긴다."""

    __tablename__ = "admin_access_log"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    admin_user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    admin_username: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # overview | users | jobs | feedback | ...
    detail: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_admin_access_log_created", "created_at"),)


class SessionRow(Base):
    """세션. token_hash만 저장 (쿠키의 토큰 원문은 DB에 없다)."""

    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_sessions_user", "user_id"),)
