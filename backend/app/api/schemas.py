"""API 요청/응답 스키마 (project-design §7)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.conversation import GraphStatus, ImportStatus, SpeakerRole
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


class ContactDeletionOut(BaseModel):
    contact_id: UUID
    conversations: int
    messages: int
    episodes: int
    graph_episodes: int
    rewrites: int
    graph_errors: list[str]


class AccountDeleteIn(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class AccountDeletionOut(BaseModel):
    contacts: int
    conversations: int
    messages: int
    graph_nodes_removed: int
    graph_error: str | None


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


class GraphNodeOut(BaseModel):
    id: str
    name: str
    kind: Literal["person", "event", "other"]
    summary: str
    event_type: str | None = None
    importance: int | None = None
    is_contact: bool = Field(description="이 상대의 노드 (그림의 중심)")


class GraphEdgeOut(BaseModel):
    id: str
    source: str
    target: str
    relation: str = Field(description="관계 유형 (Relationship·Involved·RELATES_TO)")
    fact: str
    valid_at: datetime | None
    invalid_at: datetime | None
    current: bool = Field(description="지금도 유효한 사실인지 (무효화되지 않았거나 무효 시각이 아직 안 옴)")


class ContactGraphOut(BaseModel):
    """상대 주변의 맥락 그래프. D 조건 검색이 이 상대의 사실로 보는 범위와 같다."""

    enabled: bool = Field(description="서버가 그래프를 쓰는지 (NEO4J_URI)")
    center_id: str | None = Field(None, description="상대 별칭과 이름이 같은 노드. 없으면 아직 수집 전")
    nodes: list[GraphNodeOut] = Field(default_factory=list)
    edges: list[GraphEdgeOut] = Field(default_factory=list)


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
    condition: Condition
    created_at: datetime
    result: RewriteResult


class FeedbackIn(BaseModel):
    accepted: bool
    edited_text: str | None = None
    reason: str | None = Field(None, max_length=500)
    consent_to_reuse: bool = False


class UsageOut(BaseModel):
    day: date
    rewrites: int
    rewrite_limit: int
    remaining_rewrites: int
    input_tokens: int
    output_tokens: int
    token_limit: int


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class AuthOut(BaseModel):
    user_id: str
    username: str
    csrf_token: str = Field(description="쓰기 요청의 X-CSRF-Token 헤더 값")
    expires_at: datetime


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


class PasswordChangeOut(BaseModel):
    revoked_sessions: int = Field(description="함께 로그아웃된 다른 세션 수")


class MeOut(BaseModel):
    user_id: str
    username: str
    csrf_token: str


class ErrorOut(BaseModel):
    code: str
    message: str


class ConversationCreate(BaseModel):
    contact_id: UUID
    title: str | None = Field(None, max_length=200)
    source: str = Field("json", max_length=64)
    source_key: str | None = Field(
        None, max_length=64, description="데이터셋의 대화방 id (c1). 생략하면 생성. 사용자 안에서 유일"
    )
    me_name: str | None = Field(
        None, max_length=50, description="그래프 수집에 쓰는 내 표시 이름 (예: 김민수)"
    )


class ConversationOut(BaseModel):
    id: UUID
    contact_id: UUID
    title: str
    source: str
    source_key: str
    me_name: str | None
    created_at: datetime


class MessageOut(BaseModel):
    id: UUID
    source_key: str
    speaker_role: SpeakerRole
    sent_at: datetime
    text: str


class DeletionOut(BaseModel):
    conversation_id: UUID
    messages: int
    episodes: int
    graph_episodes: int
    graph_error: str | None


class ImportJobOut(BaseModel):
    id: UUID
    conversation_id: UUID
    status: ImportStatus
    imported: int
    duplicates: int
    episodes: int
    error_code: str | None
    error_message: str | None
    graph_status: GraphStatus
    graph_episodes: int
    graph_error: str | None
    created_at: datetime
