"""API 요청/응답 스키마 (project-design §7)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.conversation import GraphStatus, ImportStatus, SpeakerRole
from app.domain.persona import Adjust, PersonaSettings, RelationshipType
from app.domain.rewrite import Channel, Condition, FeedbackReasonCode, RequestedCondition, RewriteResult
from app.importers.base import ImportFormat, RequestedFormat


class ContactCreate(BaseModel):
    alias: str = Field(min_length=1, max_length=50)
    relationship_type: RelationshipType
    use_default_persona: bool = Field(
        False, description="true면 관계 유형 기본값으로 페르소나 v1을 함께 만든다 (GET /v1/persona-defaults)"
    )


class ContactUpdate(BaseModel):
    """바꿀 것만 보낸다. 생략하거나 null이면 그대로."""

    alias: str | None = Field(None, min_length=1, max_length=50)
    relationship_type: RelationshipType | None = None


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
    condition: RequestedCondition = Field(
        "B", description="A~D 또는 auto(이 상대에 쓸 수 있는 가장 풍부한 조건을 서버가 고른다)"
    )
    persona_version: int | None = None
    adjust: Adjust | None = Field(None, description="이번 요청만의 조정. 저장된 페르소나는 바뀌지 않는다")
    channel: Channel = "web"


class FeedbackOut(BaseModel):
    accepted: bool
    edited_text: str | None
    reason: str | None
    reason_code: FeedbackReasonCode | None
    consent_to_reuse: bool
    created_at: datetime | None


class RewriteOut(BaseModel):
    id: UUID
    contact_id: UUID
    draft_version: int
    condition: Condition = Field(description="실제 적용된 조건 (auto 요청이면 서버가 고른 A~D)")
    channel: Channel = "web"
    created_at: datetime
    result: RewriteResult
    feedback: FeedbackOut | None = None


class FeedbackIn(BaseModel):
    accepted: bool
    edited_text: str | None = None
    reason: str | None = Field(None, max_length=500)
    reason_code: FeedbackReasonCode | None = None
    consent_to_reuse: bool = False


class UsageOut(BaseModel):
    day: date
    rewrites: int
    rewrite_limit: int
    remaining_rewrites: int
    graph_messages_ingested: int = Field(0, description="그래프(D)에 들어가 있는 내 메시지 수")
    graph_message_cap: int = Field(0, description="사용자당 그래프 수집 상한. 0이면 무제한")
    input_tokens: int
    output_tokens: int
    token_limit: int


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


# ---- 대화 가져오기: 원문 파싱 (카카오톡·텔레그램·붙여넣기·json) ----


class ImportParseIn(BaseModel):
    """파일 업로드(multipart)는 쓰지 않는다 — 브라우저가 파일을 읽어 `text`로 보낸다."""

    format: RequestedFormat = "auto"
    text: str
    tz_offset_minutes: int = Field(540, ge=-720, le=840, description="시각에 시간대가 없을 때 쓰는 값 (분)")
    chat_id: str | int | None = Field(
        None, description="텔레그램 계정 전체 내보내기에서 고른 대화 id (미리보기의 chats[].id)"
    )


class ImportRawIn(ImportParseIn):
    speaker_map: dict[str, Literal["me", "contact", "skip"]] = Field(
        default_factory=dict, description="화자 이름 → me / contact / skip(그 화자 메시지를 버림)"
    )
    keep_recent: int | None = Field(None, ge=1, description="최근 N건만 가져온다")


class ImportSpeakerOut(BaseModel):
    name: str
    count: int
    suggested_role: Literal["me", "contact"] | None


class ImportSampleOut(BaseModel):
    id: str
    speaker: str
    sent_at: datetime
    text: str


class ImportChatOut(BaseModel):
    id: str
    name: str
    type: str | None
    messages: int


class ImportPreviewOut(BaseModel):
    format: ImportFormat
    variant: str | None = Field(description="카카오톡: android | ios | pc | csv. 그 밖에는 null")
    title_hint: str | None
    messages_total: int
    date_from: datetime | None
    date_to: datetime | None
    speakers: list[ImportSpeakerOut]
    sample: list[ImportSampleOut] = Field(description="앞 20건")
    skipped_lines: int = Field(description="읽지 못해 가져오지 않는 줄(행·항목) 수")
    system_lines: int = Field(description="날짜 구분선·입퇴장·사진/이모티콘 자리표시 등")
    masked_pii: int = Field(description="가린 개인정보 패턴 수 (IMPORT_MASK_PII가 꺼져 있으면 0)")
    problems: list[str]
    max_messages: int = Field(
        0, description="한 번에 가져올 수 있는 메시지 수 (MAX_IMPORT_MESSAGES). 넘으면 keep_recent로 줄인다"
    )
    chats: list[ImportChatOut] | None = Field(
        None, description="텔레그램 계정 전체 내보내기: 값이 있으면 하나를 골라 chat_id로 다시 요청한다"
    )


class ConversationStatsOut(BaseModel):
    conversation_id: UUID
    messages: int
    my_messages: int
    episodes: int
    graph_enabled: bool
    graph_ingested: int
    graph_pending: int
    graph_limited: bool
    first_message_at: datetime | None
    last_message_at: datetime | None
    latest_job: ImportJobOut | None


# ---- 내 데이터: 요약·페르소나 추정·근거 ----


class ContactSummaryOut(BaseModel):
    contact_id: UUID
    alias: str
    relationship_type: RelationshipType
    persona_version: int | None
    conversations: int
    messages: int
    my_messages: int
    graph_ingested: int
    rewrites: int
    last_rewrite_at: datetime | None


class DataTotalsOut(BaseModel):
    contacts: int
    conversations: int
    messages: int
    rewrites: int


class DataSummaryOut(BaseModel):
    contacts: list[ContactSummaryOut]
    totals: DataTotalsOut
    graph_enabled: bool
    retention_days: int = Field(description="첨삭 이력 보존 일수. 0이면 무기한")


class PersonaSignalOut(BaseModel):
    field: str
    value: Any
    reason: str


class PersonaSuggestionOut(BaseModel):
    based_on_messages: int
    settings: PersonaSettings | None
    signals: list[PersonaSignalOut]


class EvidenceMessageOut(BaseModel):
    speaker_role: SpeakerRole
    sent_at: datetime
    text: str


class EvidenceItemOut(BaseModel):
    id: str
    source_message_ids: list[str]
    messages: list[EvidenceMessageOut]


class EvidenceOut(BaseModel):
    items: list[EvidenceItemOut]
    missing: int = Field(description="원문이 모두 지워져 보여 줄 수 없는 근거 수")


class RewritesDeletedOut(BaseModel):
    deleted: int
