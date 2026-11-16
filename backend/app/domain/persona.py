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


# 관계 유형별 기본 페르소나 (정본). `GET /v1/persona-defaults`, 상대 생성의 `use_default_persona`,
# 평가 하니스(run_eval)·시연 준비(prepare_demo)가 같은 표를 쓴다 — 값을 바꾸면 평가 결과가 달라진다.
# `app.services.synthetic_loader.DEFAULT_PERSONAS`는 이 표를 다시 내보낸 이름이다.
DEFAULT_PERSONAS: dict[str, PersonaSettings] = {
    "superior": PersonaSettings(formality=5, friendliness=2, directness=3, forbidden_phrases=["ㅋㅋ", "ㅠ"]),
    "colleague": PersonaSettings(formality=3, friendliness=4, directness=4),
    "subordinate": PersonaSettings(formality=3, friendliness=3, directness=4),
    "friend": PersonaSettings(formality=1, friendliness=5, directness=5, emoji="rare"),
    "family": PersonaSettings(formality=1, friendliness=5, directness=5),
    "partner": PersonaSettings(formality=1, friendliness=5, directness=3, emoji="often"),
    "acquaintance": PersonaSettings(formality=4, friendliness=2, directness=3),
}


def default_persona(relationship_type: str) -> PersonaSettings:
    """관계 유형의 기본 설정 사본. 표에 없는 값이면 중립 기본값."""
    return DEFAULT_PERSONAS.get(relationship_type, PersonaSettings()).model_copy(deep=True)


Adjust = Literal["shorter", "longer", "more_formal", "more_casual"]
"""이번 요청만 페르소나 위에 덮어쓰는 조정. 저장된 페르소나는 바꾸지 않는다."""


def apply_adjust(settings: PersonaSettings | None, adjust: str | None) -> PersonaSettings | None:
    """조정을 적용한 사본을 돌려준다 (원본은 그대로). 설정이 없으면 덮어쓸 것도 없다.

    shorter → length=shorter, longer → length=longer, more_formal → formality+1(최대 5),
    more_casual → formality−1(최소 1).
    """
    if settings is None or adjust is None:
        return settings
    if adjust == "shorter":
        return settings.model_copy(update={"length": "shorter"})
    if adjust == "longer":
        return settings.model_copy(update={"length": "longer"})
    if adjust == "more_formal":
        return settings.model_copy(update={"formality": min(settings.formality + 1, 5)})
    if adjust == "more_casual":
        return settings.model_copy(update={"formality": max(settings.formality - 1, 1)})
    raise ValueError(f"알 수 없는 조정: {adjust}")
