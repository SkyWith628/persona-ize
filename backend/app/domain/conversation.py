"""과거 대화 가져오기 (project-design §5.1, §6).

대화방(conversation) → 메시지(message) → 검색 단위(episode). 원본 메시지 id는 `source_key`로 보존해
다른 파일에서 `대화방키:메시지키`(예 `c1:m03`)로 가리킬 수 있게 한다.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

KST = timezone(timedelta(hours=9))
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")


def exception_kind(exc: BaseException) -> str:
    """예외의 **종류만**: 클래스 이름과, 있으면 형태가 정해진 코드(HTTP 상태, SQLSTATE, Neo4j 코드).

    예외 문구(`str(exc)`)는 쓰지 않는다. DB·그래프·모델 클라이언트의 문구에는 SQL 매개변수나 프롬프트
    조각 — 사용자의 대화 원문 — 이 들어 있을 수 있다. 작업 기록(`import_jobs.error_message`·
    `graph_error`), 삭제 응답의 `graph_error`, 로그 줄에는 이것만 남긴다 (2026-11-11 검토).
    """
    name = type(exc).__name__
    for source in (exc, getattr(exc, "orig", None)):
        for attr in ("status_code", "sqlstate", "pgcode", "code"):
            value = getattr(source, attr, None)
            if isinstance(value, bool):
                continue
            if isinstance(value, int) or (isinstance(value, str) and _SAFE_CODE.match(value)):
                return f"{name}({value})"
    return name


def local_time(moment: datetime) -> datetime:
    """메시지 시각을 **날짜·요일·시:분으로 읽을 때** 쓰는 현지 시각(한국 시간).

    저장소마다 돌려주는 시간대가 다르다 — 인메모리는 가져온 그대로(+09:00), PostgreSQL(timestamptz)은 UTC.
    그 datetime의 자기 시간대로 날짜를 읽으면 화요일 08:01(KST)이 월요일 23:01이 된다. 날짜·요일을 글자로
    만드는 곳(그래프 에피소드 본문, 상대 날짜 풀이, 프롬프트 예시의 날짜)은 반드시 이것을 거친다.
    시간대가 없는 값은 UTC로 본다 (`ai.prompt.today_kst`와 같은 기준).
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(KST)


GRAPH_UNIT_CHARS = 1000


def graph_units(text: str) -> int:
    """그래프 수집 상한에서 메시지 한 건을 세는 양: 기본 1, 1,000자마다 1씩 더한다.

    에피소드 하나의 모델 비용은 본문 길이에 따라 는다 — 4,000자 메시지를 1건으로 세면 상한이 헐거워진다.
    """
    return 1 + len(text) // GRAPH_UNIT_CHARS


SpeakerRole = Literal["me", "contact"]
ImportStatus = Literal["processing", "completed", "failed"]
# limited: 그래프 수집 상한(GRAPH_INGEST_*_CAP)에 닿아 멈췄다. 미수집 메시지는 남아 있다 (DB 열 16자).
GraphStatus = Literal["skipped", "pending", "running", "completed", "failed", "limited"]


class Conversation(BaseModel):
    id: UUID
    user_id: str
    contact_id: UUID
    title: str
    source: str = Field(description="synthetic-json 등 원본 종류")
    source_key: str = Field(description="데이터셋의 대화방 id (c1). 메시지 참조 `c1:m03`의 앞부분")
    me_name: str | None = Field(None, description="그래프 에피소드에 쓰는 사용자 표시 이름 (없으면 '사용자')")
    created_at: datetime


class NewMessage(BaseModel):
    """가져오기 입력을 정규화한 한 건. 화자는 이미 me/contact로 지정된 상태."""

    source_key: str = Field(min_length=1, max_length=64)
    speaker_role: SpeakerRole
    sent_at: datetime
    text: str = Field(min_length=1, max_length=4000)


class Message(BaseModel):
    id: UUID
    user_id: str
    conversation_id: UUID
    source_key: str
    speaker_role: SpeakerRole
    sent_at: datetime
    text: str
    graph_ingested_at: datetime | None = Field(None, description="그래프(D)에 넣은 시각. None이면 미수집")
    graph_attempts: int = Field(0, description="그래프에 넣다 실패한 횟수 (일시 장애는 세지 않는다)")
    graph_held_at: datetime | None = Field(
        None, description="거듭 실패해 더 시도하지 않기로 한 시각. 운영자가 graph_reset으로 푼다"
    )

    @property
    def graph_pending(self) -> bool:
        """그래프 수집을 기다리는가 — 아직 안 들어갔고 보류되지도 않았다."""
        return self.graph_ingested_at is None and self.graph_held_at is None


class EpisodeDraft(BaseModel):
    """메시지 묶음 하나. 저장 전(임베딩 전) 상태."""

    context_text: str = Field(description="임베딩에 쓰는 묶음 전문 (상대/나 표기)")
    style_text: str = Field(description="문체 예시로 쓰는 본인 발화만")
    start_at: datetime
    end_at: datetime
    message_ids: list[UUID]
    source_message_ids: list[str] = Field(description="`대화방키:메시지키` 목록 (묶음에 든 모든 메시지)")


class Episode(BaseModel):
    id: UUID
    user_id: str
    contact_id: UUID
    conversation_id: UUID
    context_text: str
    style_text: str
    embedding_version: str
    start_at: datetime
    end_at: datetime
    source_message_ids: list[str]


class EpisodeHit(BaseModel):
    episode: Episode
    score: float = Field(description="코사인 유사도 (높을수록 가까움)")


class DeletionResult(BaseModel):
    """대화방 삭제 결과. 원문·묶음·그래프 에피소드까지 추적한다 (project-design §6 삭제)."""

    conversation_id: UUID
    messages: int
    episodes: int
    graph_episodes: int = Field(0, description="그래프에서 지운 에피소드 수 (그래프를 안 쓰면 0)")
    graph_error: str | None = Field(None, description="그래프 삭제 실패 사유. DB 삭제는 그래도 진행한다")


class ImportJob(BaseModel):
    id: UUID
    user_id: str
    conversation_id: UUID
    source_hash: str
    status: ImportStatus
    imported: int = 0
    duplicates: int = 0
    episodes: int = 0
    error_code: str | None = None
    error_message: str | None = None
    graph_status: GraphStatus = Field("skipped", description="그래프 수집 단계. 그래프를 안 쓰면 skipped")
    graph_episodes: int = 0
    graph_error: str | None = Field(
        None, description="failed면 오류 문장, limited면 닿은 상한(user_cap / daily_cap)"
    )
    created_at: datetime


class SpeakerRoleChange(BaseModel):
    """화자 역할 고치기의 결과 (`PUT /v1/conversations/{id}/speaker-roles`).

    메시지에는 원래 화자 이름이 없고 역할(me/contact)만 남아 있어, 고칠 수 있는 것은 방 전체의
    나↔상대 맞바꾸기뿐이다.
    """

    conversation_id: UUID
    changed: bool = Field(description="거짓이면 이미 그 역할이라 아무것도 하지 않았다")
    messages: int = Field(0, description="역할이 바뀐 메시지 수")
    my_messages: int = Field(0, description="바뀐 뒤 내 메시지 수")
    me_name: str | None = None
    graph_removed: int = Field(0, description="그래프에서 지운 에피소드 수 (잘못된 역할로 들어간 것)")
    graph_requeued: int = Field(0, description="다시 수집 대상으로 돌린 메시지 수")
    job: ImportJob | None = Field(None, description="묶음을 다시 만드는 작업. 바뀐 것이 없으면 None")


class ConversationStats(BaseModel):
    """대화방 하나의 집계 (`GET /v1/conversations/{id}/stats`)."""

    conversation_id: UUID
    messages: int = 0
    my_messages: int = 0
    episodes: int = 0
    graph_ingested: int = 0
    graph_held: int = 0  # 거듭 실패해 보류된 메시지 — 그래프에 없고, 다시 시도하지도 않는다
    first_message_at: datetime | None = None
    last_message_at: datetime | None = None

    @property
    def graph_pending(self) -> int:
        """앞으로 넣을 메시지 수. 보류된 것은 세지 않는다."""
        return max(self.messages - self.graph_ingested - self.graph_held, 0)


class ContactDataStats(BaseModel):
    """상대 한 명에게 쌓인 대화 자료의 집계 (`GET /v1/data/summary`, 조건 auto 판단)."""

    contact_id: UUID
    conversations: int = 0
    messages: int = 0
    my_messages: int = 0
    episodes: int = 0
    graph_ingested: int = 0
