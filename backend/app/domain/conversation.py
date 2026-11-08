"""과거 대화 가져오기 (project-design §5.1, §6).

대화방(conversation) → 메시지(message) → 검색 단위(episode). 원본 메시지 id는 `source_key`로 보존해
다른 파일에서 `대화방키:메시지키`(예 `c1:m03`)로 가리킬 수 있게 한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

SpeakerRole = Literal["me", "contact"]
ImportStatus = Literal["completed", "failed"]
GraphStatus = Literal["skipped", "pending", "running", "completed", "failed"]


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
    graph_error: str | None = None
    created_at: datetime
