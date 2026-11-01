"""Graphiti 커스텀 엔티티·엣지 타입.

클래스 docstring과 Field description이 그대로 추출 프롬프트에 들어가므로 한국어로 구체적으로 쓴다.
필드 이름은 Graphiti 예약 필드(uuid, name, summary, fact, valid_at 등)와 겹치지 않게 한다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Person(BaseModel):
    """대화에 등장하는 사람. 서비스 사용자 본인과 대화 상대를 모두 포함한다."""

    role: str | None = Field(None, description="직업·직책·가족 관계 등 사회적 역할 (예: 팀장, 동생)")


class Event(BaseModel):
    """시점이 있는 사건·약속·상태 변화 (예: 일정 조율, 사과, 발표 약속, 발령)."""

    event_type: (
        Literal["request", "refusal", "apology", "schedule", "promise", "conflict", "status", "other"]
        | None
    ) = Field(None, description="사건 유형")
    status: Literal["open", "resolved"] | None = Field(
        None, description="아직 진행 중이면 open, 마무리되었으면 resolved"
    )
    importance: int | None = Field(
        None, ge=1, le=10, description="사용자 관계에 미치는 중요도 1~10 (일상 대화 1, 갈등·약속 7 이상)"
    )


class Relationship(BaseModel):
    """한 사람이 다른 사람을 대하는 관계. 방향이 있다 (A가 B를 어떻게 대하는가)."""

    relation_type: (
        Literal["family", "friend", "colleague", "superior", "subordinate", "partner", "acquaintance"]
        | None
    ) = Field(None, description="target이 source에게 어떤 사람인지 (superior = target이 source의 상사)")
    speech_style: Literal["반말", "존댓말", "혼용"] | None = Field(
        None, description="source가 target에게 쓰는 말투"
    )
    address_term: str | None = Field(None, description="source가 target을 부르는 호칭 (예: 팀장님, 형)")
    closeness: int | None = Field(None, ge=1, le=5, description="친밀도 1(거리감)~5(매우 가까움)")


class Involved(BaseModel):
    """사람이 사건에 관여함."""

    participant_role: str | None = Field(None, description="당사자 / 상대 / 요청자 / 목격자 등")
    feeling: str | None = Field(None, description="이 사람이 사건에 대해 드러낸 감정 (드러나지 않으면 비움)")


ENTITY_TYPES: dict[str, type[BaseModel]] = {"Person": Person, "Event": Event}
EDGE_TYPES: dict[str, type[BaseModel]] = {"Relationship": Relationship, "Involved": Involved}
EDGE_TYPE_MAP: dict[tuple[str, str], list[str]] = {
    ("Person", "Person"): ["Relationship"],
    ("Person", "Event"): ["Involved"],
}

EXTRACTION_INSTRUCTIONS = """\
한국어 메신저 대화다. 다음 규칙을 따른다.
- 같은 사람을 가리키는 호칭(예: '박지현', '지현 팀장님', '팀장님')은 하나의 Person으로 본다.
  엔티티 이름은 성과 이름이 드러난 가장 완전한 실명을 쓴다.
- 존댓말/반말, 호칭은 실제 발화에서 확인될 때만 기록하고 추측하지 않는다.
- '어제', '다음 주 금요일' 같은 상대 시간은 메시지 시각을 기준으로 해석한다.
- 관계가 바뀌면(예: 동기 → 상사) 새 사실로 기록한다. 이전 관계는 더 이상 유효하지 않다.
- 성격·건강·정치 성향 등 민감한 특성은 추정하지 않는다.
"""
