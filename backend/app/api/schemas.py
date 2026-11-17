"""API 요청/응답 스키마 (project-design §7)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, SerializerFunctionWrapHandler, model_serializer, model_validator

from app.domain.conversation import SPEAKER_NAME_MAX, GraphStatus, ImportStatus, SpeakerRole
from app.domain.persona import Adjust, PersonaSettings, RelationshipType
from app.domain.rewrite import (
    INT4_MAX,
    Channel,
    Condition,
    FeedbackReasonCode,
    RequestedCondition,
    RewriteResult,
)
from app.importers.base import ImportFormat, RequestedFormat, has_unstorable_text, repair_unicode


class StoredBody(BaseModel):
    """저장되는 요청 본문의 공통 검사: NUL·깨진 유니코드가 든 글은 받지 않는다 (422).

    그대로 두면 PostgreSQL이 거부해 500이 났다 — 첨삭이면 모델을 부른 뒤에야 실패했다 (2026-11-11 검토).
    """

    @model_validator(mode="before")
    @classmethod
    def _storable(cls, data: Any) -> Any:
        if has_unstorable_text(data):
            raise ValueError("저장할 수 없는 문자가 들어 있습니다 (NUL 또는 깨진 유니코드).")
        return data


class ContactCreate(StoredBody):
    alias: str = Field(min_length=1, max_length=50)
    relationship_type: RelationshipType
    use_default_persona: bool = Field(
        False, description="true면 관계 유형 기본값으로 페르소나 v1을 함께 만든다 (GET /v1/persona-defaults)"
    )


class ContactUpdate(StoredBody):
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


class PersonaPut(StoredBody):
    settings: PersonaSettings
    expected_version: int | None = Field(
        None, le=INT4_MAX, description="현재 최신 버전. 다르면 409. 처음 저장이면 0 또는 생략"
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
    # 응답의 이름만 `register`다. 파이썬 이름까지 그러면 BaseModel(ABC)의 `register`를 가린다는 경고가 난다.
    speech_register: ContactRegisterOut | None = Field(
        None,
        serialization_alias="register",
        description=(
            "내가 이 상대에게 쓰는 말투 — 화면에는 (나)→(상대) 간선처럼 보인다. 그래프에 저장된 값이 아니라 "
            "메시지에서 읽을 때 계산한다(그래프가 꺼져 있어도 온다). 내 메시지가 없거나 계산이 실패하면, "
            "또는 REGISTER_SUGGESTIONS=false인 서버에서는 응답에 이 항목이 없다 (null과 같다)"
        ),
    )

    # 반환 주석을 달지 않는다 (`PersonaSuggestionOut`과 같은 이유: OpenAPI 응답 스키마가 빈 object가 된다).
    @model_serializer(mode="wrap")
    def _omit_empty_register(self, handler: SerializerFunctionWrapHandler):
        # 말투가 없으면 예전 응답과 글자까지 같게 항목을 뺀다 (예전 클라이언트·테스트가 응답 전체를 비교한다)
        data = handler(self)
        for key in ("register", "speech_register"):  # 별칭으로 내보낼 때와 아닐 때
            if key in data and data[key] is None:
                del data[key]
        return data


class RegisterTriggerOut(BaseModel):
    event: str = Field(description="그래프의 사건 이름 (예: 파트장 발령)")
    at: date | None = Field(description="그 사건을 말한 메시지의 날짜 (한국 시간)")


class RegisterSegmentOut(BaseModel):
    level: Literal["hapsyo", "haeyo", "hae"] = Field(description="합쇼체 / 해요체 / 반말")
    start: date = Field(description="이 말투가 시작된 날 (한국 시간)")
    end: date | None = Field(description="다음 구간이 시작한 날. 현재 구간이면 null")
    count: int = Field(description="이 구간에서 이 말투로 관찰된 내 메시지 수")
    status: Literal["confirmed", "candidate", "previous"] = Field(
        description="마지막 구간은 confirmed 또는 candidate(최근 메시지에 없음), 그 앞의 구간은 previous"
    )
    message_ids: list[str] = Field(description="근거 메시지, 최대 5개 (`c3:m08` — 대화방 키:메시지 키)")
    trigger: RegisterTriggerOut | None = Field(
        None, description="그래프에서 찾은 계기 사건. 말투가 바뀐 확정된 현재 구간에만 붙는다"
    )


class RegisterAddressOut(BaseModel):
    term: str = Field(description="메시지에 나온 호칭 그대로")
    count: int
    status: Literal["confirmed", "candidate"] = Field(description="2회 이상이면 confirmed, 1회면 candidate")


class ContactRegisterOut(BaseModel):
    """(나 → 상대)의 말투 프로필을 간선 하나처럼 보여 주는 값. 규칙 계산이고 저장하지 않는다."""

    segments: list[RegisterSegmentOut] = Field(description="시간 순 말투 구간")
    address: RegisterAddressOut | None = Field(description="현재 호칭. 관찰된 것이 없으면 null")
    based_on_messages: int = Field(description="본 내 메시지 수 (최근 500건까지)")


class RewriteCreate(StoredBody):
    contact_id: UUID
    draft_text: str
    # 정수 열(INTEGER)에 들어가는 값은 범위를 먼저 본다 — 넘치면 모델을 부른 뒤 저장에서 실패했다.
    draft_version: int = Field(1, ge=1, le=INT4_MAX)
    situation: str = ""
    condition: RequestedCondition = Field(
        "B", description="A~D 또는 auto(이 상대에 쓸 수 있는 가장 풍부한 조건을 서버가 고른다)"
    )
    persona_version: int | None = Field(None, ge=-INT4_MAX, le=INT4_MAX)
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


class FeedbackIn(StoredBody):
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
    graph_messages_ingested: int = Field(
        0,
        description="그래프 수집 상한이 보는 값: max(지금 그래프에 든 양, 오늘 넣은 양). "
        "메시지 1건이 1이고 1,000자마다 1을 더 센다",
    )
    graph_message_cap: int = Field(0, description="사용자당 그래프 수집 상한. 0이면 무제한")
    input_tokens: int
    output_tokens: int
    token_limit: int


class ErrorOut(BaseModel):
    code: str
    message: str


class ConversationCreate(StoredBody):
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
    speaker_name: str | None = Field(None, description="내보내기의 화자 이름. 모르면 null")
    sent_at: datetime
    text: str


class SpeakerOut(BaseModel):
    """대화방 안의 화자 한 명. `name`이 null이면 이름 없이 저장된 메시지 묶음(화자별로 고칠 수 없다)."""

    name: str | None
    speaker_role: SpeakerRole
    messages: int


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
    index_done: int | None = Field(
        None,
        description="processing 동안: 벡터가 준비된 말투 묶음 수. 서버가 모르면 null (index_total과 함께)",
    )
    index_total: int | None = Field(None, description="processing 동안: 이번에 만들 말투 묶음 전체 수")


SPEAKER_ROLES_MAX = 200  # 한 번에 고칠 수 있는 화자 수


class SpeakerRolesIn(StoredBody):
    """화자 역할 고치기: "이 메시지는 `speaker_role`이 한 말이다". 지금과 다르면 방 전체의 나↔상대를 맞바꾼다.

    바꾸는 방향이 아니라 맞는 상태를 적으므로 같은 요청을 다시 보내도 도로 뒤집히지 않는다.


    둘 중 하나로 보낸다.
    - `message_id` + `speaker_role`: 방 전체 맞바꾸기 (화자 이름이 없는 예전 대화도 된다).
    - `speakers`: 화자 이름별 지정 `{"이름": "me" | "contact" | "skip"}` — 적지 않은 화자는 그대로 두고,
      `skip`은 그 화자의 메시지를 지운다. 이름은 `GET /v1/conversations/{id}/speakers`의 것.
    """

    message_id: UUID | None = Field(
        None, description="기준 메시지 (`GET /v1/conversations/{id}/messages`의 id)"
    )
    speaker_role: SpeakerRole | None = Field(None, description="그 메시지를 실제로 쓴 쪽")
    speakers: dict[str, Literal["me", "contact", "skip"]] | None = Field(
        None, description="화자 이름 → 맞는 역할. skip은 그 화자의 메시지를 지운다"
    )
    me_name: str | None = Field(
        None,
        max_length=50,
        description="'나'가 바뀔 때 새로 쓸 내 표시 이름. 생략하면 맞바꾸기는 비우고, 화자별 지정은 "
        "'나'가 한 사람이면 그 이름을 쓴다",
    )

    @model_validator(mode="after")
    def _one_mode(self) -> SpeakerRolesIn:
        by_anchor = self.message_id is not None or self.speaker_role is not None
        if self.speakers is not None:
            if by_anchor:
                raise ValueError("speakers와 message_id·speaker_role은 함께 보낼 수 없습니다.")
            if not self.speakers or len(self.speakers) > SPEAKER_ROLES_MAX:
                raise ValueError(f"speakers에는 화자를 1~{SPEAKER_ROLES_MAX}명 적습니다.")
            if any(not name or len(name) > SPEAKER_NAME_MAX for name in self.speakers):
                raise ValueError(f"화자 이름은 1~{SPEAKER_NAME_MAX}자입니다.")
        elif self.message_id is None or self.speaker_role is None:
            raise ValueError("message_id와 speaker_role을 함께 보내거나, speakers를 보냅니다.")
        return self


class SpeakerRolesOut(BaseModel):
    conversation_id: UUID
    changed: bool = Field(description="거짓이면 이미 맞는 상태라 아무것도 하지 않았다")
    messages: int = Field(description="역할이 바뀐 메시지 수")
    messages_removed: int = Field(0, description="제외(skip)로 지운 메시지 수")
    my_messages: int = Field(description="바뀐 뒤 내 메시지 수 (changed가 거짓이면 0)")
    me_name: str | None
    graph_episodes_removed: int = Field(description="그래프에서 지운 에피소드 수 (다시 수집한다)")
    graph_requeued: int = Field(description="다시 수집 대상으로 돌린 메시지 수")
    job: ImportJobOut | None = Field(
        description="묶음을 다시 만드는 작업 — `GET /v1/imports/{id}`로 확인. 바뀐 것이 없으면 null"
    )


# ---- 대화 가져오기: 원문 파싱 (카카오톡·텔레그램·붙여넣기·json) ----


class ImportParseIn(BaseModel):
    """파일 업로드(multipart)는 쓰지 않는다 — 브라우저가 파일을 읽어 `text`로 보낸다."""

    format: RequestedFormat = "auto"
    text: str
    tz_offset_minutes: int = Field(540, ge=-720, le=840, description="시각에 시간대가 없을 때 쓰는 값 (분)")
    chat_id: str | int | None = Field(
        None, description="텔레그램 계정 전체 내보내기에서 고른 대화 id (미리보기의 chats[].id)"
    )

    @model_validator(mode="before")
    @classmethod
    def _readable(cls, data: Any) -> Any:
        """깨진 유니코드(짝 없는 대리 문자): 원문(`text`)은 `?`로 바꿔 읽고, 그 밖의 값에 있으면 422.

        원문의 NUL은 파서가 뺀다. 화자 지정(`speaker_map`)의 이름 등에 NUL·깨진 글자가 있으면 받지 않는다.
        """
        if not isinstance(data, dict):
            return data
        if has_unstorable_text({k: v for k, v in data.items() if k != "text"}):
            raise ValueError("저장할 수 없는 문자가 들어 있습니다 (NUL 또는 깨진 유니코드).")
        text = data.get("text")
        if isinstance(text, str) and repair_unicode(text) is not text:
            return {**data, "text": repair_unicode(text)}
        return data


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
    graph_held: int = Field(
        0, description="거듭 실패해 보류된 메시지 수 (그래프에 없고 다시 시도하지 않는다)"
    )
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
    source: Literal["messages", "register", "graph"] = Field(
        "messages",
        description=(
            "근거의 출처: 내 메시지의 겉모습 / 말투 프로필(문장 단위 분류로 본 말투·호칭의 변화 시점) / "
            "그래프의 관계 사실(말투·호칭이 바뀐 시점)"
        ),
    )
    since: date | None = Field(
        None, description="말투 프로필·그래프 근거일 때 이 값이 처음 확인된 날 (한국 시간)"
    )


class CardSpeechOut(BaseModel):
    level: Literal["hapsyo", "haeyo", "hae"] = Field(description="합쇼체 / 해요체 / 반말")
    count: int = Field(description="이 구간의 내 메시지 수")
    message_ids: list[str] = Field(description="근거 메시지 (`c3:m08` — 대화방 키:메시지 키)")


class CardCurrentSpeechOut(CardSpeechOut):
    since: date = Field(description="이 말투가 시작된 날 (한국 시간)")


class CardAddressOut(BaseModel):
    term: str = Field(description="메시지에 나온 호칭 그대로")
    count: int
    since: date
    status: Literal["confirmed", "candidate"] = Field(description="2회 이상이면 confirmed, 1회면 candidate")
    message_ids: list[str]


class CardTriggerOut(BaseModel):
    event: str = Field(description="그래프의 사건 이름 (예: 파트장 발령)")
    at: date | None = Field(description="그 사건을 말한 메시지의 날짜 (한국 시간)")
    message_ids: list[str]


class CardScopeHintOut(BaseModel):
    text: str = Field(description="변화 시점 앞뒤의 관계 사실 문장 (그래프)")
    message_ids: list[str]


class CardProposedOut(BaseModel):
    formality: int | None = Field(None, description="카드를 승인하면 넣을 격식. 제안이 없으면 null")
    address_term: str | None = Field(None, description="카드를 승인하면 넣을 호칭. 제안이 없으면 null")


class SuggestionCardOut(BaseModel):
    """제안 카드 — 말투가 바뀐 것으로 보일 때 근거와 함께 보여 준다. 저장하지 않는다."""

    contact_alias: str
    speech_previous: CardSpeechOut | None
    speech_current: CardCurrentSpeechOut | None
    two_valued: dict[str, int] | None = Field(
        description="최근 메시지에 두 말투가 섞여 있으면 단계별 건수. 이때는 값을 정해 주지 않는다"
    )
    address: CardAddressOut | None
    trigger: CardTriggerOut | None = Field(description="그래프에서 찾은 계기 사건. 그래프가 없으면 null")
    scope_hints: list[CardScopeHintOut]
    ask: bool = Field(description="참이면 값을 정해 주지 않고 사용자에게 묻는다")
    ask_reason: str | None
    proposed: CardProposedOut


class PersonaSuggestionOut(BaseModel):
    based_on_messages: int
    settings: PersonaSettings | None
    signals: list[PersonaSignalOut]
    card: SuggestionCardOut | None = Field(
        None, description="제안 카드. 보여 줄 것이 없으면 응답에 이 항목이 없다 (null과 같다)"
    )

    # 반환 주석을 달지 않는다: 달면 pydantic이 그 타입(dict)으로 직렬화 스키마를 만들어 OpenAPI의 응답
    # 스키마가 빈 object가 된다.
    @model_serializer(mode="wrap")
    def _omit_empty_card(self, handler: SerializerFunctionWrapHandler):
        # 카드가 없으면 예전 응답과 글자까지 같게 항목을 뺀다 (예전 클라이언트·테스트가 응답 전체를 비교한다)
        data = handler(self)
        if data.get("card") is None:
            data.pop("card", None)
        return data


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
    refs_recorded: bool = Field(
        True,
        description="거짓이면 근거의 출처 참조를 남기기 전에 만든 결과라 원문을 찾을 수 없다 (missing은 0)",
    )


class RewritesDeletedOut(BaseModel):
    deleted: int


# 묶음 첨삭 한 번에 받을 상대 수. 상대 1명 = 모델 호출 1번이라 요청 하나가 쓸 비용의 상한이기도 하다.
REWRITE_BATCH_MAX_CONTACTS = 5


class RewriteBatchCreate(StoredBody):
    """같은 초안을 여러 상대에게 — "의미는 한 번, 말투는 상대마다".

    `RewriteCreate`에서 `contact_id`만 `contact_ids`로 바뀐 본문이다. 나머지 필드는 이름·뜻이 같고 모든
    상대에 똑같이 적용한다. `RewriteCreate`에 필드를 더하면 여기에도 더한다 (두 본문의 필드가 같은지는
    `test_rewrites_batch_api.py`가 지킨다).
    """

    contact_ids: list[UUID] = Field(
        min_length=1,
        max_length=REWRITE_BATCH_MAX_CONTACTS,
        description="첨삭할 상대들 (1~5명, 중복 불가). 응답의 items는 이 순서를 따른다",
    )
    draft_text: str
    draft_version: int = Field(1, ge=1, le=INT4_MAX)
    situation: str = ""
    condition: RequestedCondition = Field("B", description="A~D 또는 auto. 상대마다 따로 정해진다")
    persona_version: int | None = Field(None, ge=-INT4_MAX, le=INT4_MAX)
    adjust: Adjust | None = Field(None, description="이번 요청만의 조정. 저장된 페르소나는 바뀌지 않는다")
    channel: Channel = "web"

    @model_validator(mode="after")
    def _distinct_contacts(self) -> RewriteBatchCreate:
        # 같은 상대가 두 번 있으면 한 요청이 한도를 두 번 쓰고 같은 결과를 두 건 남긴다 — 실수로 본다.
        if len(set(self.contact_ids)) != len(self.contact_ids):
            raise ValueError("contact_ids에 같은 상대가 두 번 들어 있습니다.")
        return self


class RewriteBatchErrorOut(BaseModel):
    code: str = Field(description="단건 경로의 오류 코드와 같다 (not_found, usage_exceeded 등)")
    message: str


class RewriteBatchItemOut(BaseModel):
    """상대 한 명의 결과. `rewrite`와 `error` 가운데 하나만 채워진다."""

    contact_id: UUID
    rewrite: RewriteOut | None = None
    error: RewriteBatchErrorOut | None = None


class RewriteBatchOut(BaseModel):
    items: list[RewriteBatchItemOut]
