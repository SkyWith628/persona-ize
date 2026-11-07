"""상대별 페르소나 설정 (project-design §5.2).

'정중함' 하나로 압축하지 않고 축을 분리한다. 민감한 성격 특성은 저장하지 않는다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

RelationshipType = Literal[
    "superior",  # 상사
    "colleague",  # 동료·동기
    "subordinate",  # 후배·부하
    "friend",
    "family",
    "partner",
    "acquaintance",  # 거래처·지인
]


class PersonaSettings(BaseModel):
    """한 상대에게 적용할 말투 설정. 수치 범위는 UI에서 설명한다."""

    formality: int = Field(3, ge=1, le=5, description="격식 1(반말·캐주얼)~5(공식 문서체)")
    friendliness: int = Field(3, ge=1, le=5, description="친근함 1(거리감)~5(매우 친근)")
    directness: int = Field(3, ge=1, le=5, description="직접성 1(완곡)~5(단도직입)")
    length: Literal["shorter", "same", "longer"] = Field("same", description="원문 대비 선호 길이")
    emoji: Literal["none", "rare", "often"] = Field("none", description="이모지 빈도")
    address_term: str | None = Field(None, max_length=30, description="호칭 (예: 팀장님, 형)")
    preferred_phrases: list[str] = Field(default_factory=list, max_length=20)
    forbidden_phrases: list[str] = Field(default_factory=list, max_length=50)
    notes: str | None = Field(None, max_length=500, description="자유 설명 (예: 업무 얘기는 결론부터)")

    @field_validator("preferred_phrases", "forbidden_phrases")
    @classmethod
    def _strip_and_dedupe(cls, values: list[str]) -> list[str]:
        seen: list[str] = []
        for v in values:
            v = v.strip()
            if v and v not in seen:
                seen.append(v)
        return seen


class PersonaVersion(BaseModel):
    """설정의 불변 스냅샷. 생성 결과는 사용한 버전을 가리킨다."""

    id: UUID
    user_id: str
    contact_id: UUID
    version: int = Field(ge=1)
    settings: PersonaSettings
    created_at: datetime
