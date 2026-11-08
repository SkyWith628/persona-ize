"""테이블 정의 (project-design §6). 도메인 모델(`app/domain`)과는 분리하고 저장소가 변환한다.

- UUID 기본키, 시각은 timestamptz(UTC).
- 모든 사용자 데이터 테이블은 user_id 열을 직접 가진다 — 조인 없이 소유권 조건을 걸기 위해서다.
- 설정·결과처럼 모양이 바뀌는 값은 JSONB에 통째로 두고, 집계에 쓰는 열만 따로 뺀다.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class ContactRow(Base):
    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False)
    alias: Mapped[str] = mapped_column(String(50), nullable=False)
    relationship_type: Mapped[str] = mapped_column(String(32), nullable=False)
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
    model_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    retrieval_version: Mapped[str] = mapped_column(String(64), nullable=False)
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
    consent_to_reuse: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
