"""API 요청/응답 스키마 (project-design §7)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.persona import PersonaSettings, RelationshipType
from app.domain.rewrite import Condition, RewriteResult


class ContactCreate(BaseModel):
    alias: str = Field(min_length=1, max_length=50)
    relationship_type: RelationshipType


class ContactOut(BaseModel):
    id: UUID
    alias: str
    relationship_type: RelationshipType
    persona_version: int | None
    created_at: datetime


class PersonaPut(BaseModel):
    settings: PersonaSettings
    expected_version: int | None = Field(
        None, description="현재 최신 버전. 다르면 409. 처음 저장이면 0 또는 생략"
    )


class PersonaOut(BaseModel):
    contact_id: UUID
    version: int
    settings: PersonaSettings
    created_at: datetime


class RewriteCreate(BaseModel):
    contact_id: UUID
    draft_text: str
    draft_version: int = Field(1, ge=1)
    situation: str = ""
    condition: Condition = "B"
    persona_version: int | None = None


class RewriteOut(BaseModel):
    id: UUID
    contact_id: UUID
    draft_version: int
    created_at: datetime
    result: RewriteResult


class FeedbackIn(BaseModel):
    accepted: bool
    edited_text: str | None = None
    reason: str | None = Field(None, max_length=500)
    consent_to_reuse: bool = False


class ErrorOut(BaseModel):
    code: str
    message: str
