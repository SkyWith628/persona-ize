"""첨삭 요청·결과 (project-design §5.3, §6).

상태 전이: pending → recommended | fallback_original
- recommended: 검증 통과한 추천이 있다.
- fallback_original: 재생성까지 실패했거나 모델 장애. 원문을 유지하고 추천 실패를 표시한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

Condition = Literal["A", "B", "C", "D"]
"""실험 조건. A 일반 첨삭 / B +페르소나 / C +본인 과거 표현 / D +그래프 맥락."""

Intent = Literal["request", "refusal", "apology", "schedule", "report", "other"]

RewriteStatus = Literal["pending", "recommended", "fallback_original"]


class PreservedItem(BaseModel):
    """원문에서 반드시 보존해야 하는 항목 하나."""

    kind: Literal["datetime", "quantity", "person", "possibility", "promise", "condition", "negation"]
    text: str
    normalized: str


class RetrievedExample(BaseModel):
    """검색된 과거 자료 한 건. purpose로 문체 자료와 사실 맥락을 구분한다 (design.md §3-2)."""

    id: str
    purpose: Literal["style", "context"]
    text: str
    speaker_role: Literal["me", "contact", "graph"]  # graph: 그래프에서 뽑은 사실 문장 (D)
    sent_at: datetime | None = None
    source_message_ids: list[str] = Field(default_factory=list)
    score: float | None = Field(None, description="검색기 내부 점수 (분석용, 프롬프트에는 안 들어간다)")


class ModelOutput(BaseModel):
    """모델의 구조화 출력. 서버가 evidence_ids와 사실 보존을 다시 검증한다."""

    rewritten_text: str = Field(description="첨삭된 메시지 전문")
    change_reasons: list[str] = Field(default_factory=list, description="바꾼 이유, 한 줄씩")
    evidence_ids: list[str] = Field(default_factory=list, description="참고한 문체 예시 id")
    warnings: list[str] = Field(default_factory=list, description="사용자에게 알릴 주의점")


class Violation(BaseModel):
    """검증 실패 항목. 재생성 시 피드백으로 그대로 전달한다."""

    check: Literal[
        "preserve_missing",
        "fact_added",
        "negation_changed",
        "forbidden_phrase",
        "evidence_unknown",
        "empty",
        "refusal",
    ]
    detail: str


class ValidationReport(BaseModel):
    passed: bool
    violations: list[Violation] = Field(default_factory=list)


class AttemptRecord(BaseModel):
    """시도 한 번의 기록. 재생성 효과를 세려면 최종 검증만으로는 부족하다 (첫 실측 메모 11-08)."""

    attempt: int = Field(ge=1)
    passed: bool
    violations: list[Violation] = Field(default_factory=list)
    rewritten_text: str | None = Field(None, description="모델 출력. 모델 장애면 None")


class RewriteInput(BaseModel):
    """워크플로 입력. user_id는 인증에서 결정되며 본문에서 신뢰하지 않는다."""

    user_id: str
    contact_id: UUID
    draft_text: str
    draft_version: int = Field(ge=1)
    situation: str = ""
    condition: Condition = "B"
    persona_version: int | None = None
    # 검색 기준 시각. 이 시각 이전 대화만 검색한다 (평가 사례의 as_of). None이면 지금.
    as_of: datetime | None = None


class RewriteResult(BaseModel):
    status: RewriteStatus
    result_text: str
    change_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    intent: Intent
    preserved_items: list[PreservedItem]
    validation: ValidationReport
    attempts: int
    attempt_log: list[AttemptRecord] = Field(default_factory=list, description="시도별 검증 기록 (시간 순)")
    model_version: str
    prompt_version: str
    retrieval_version: str
    token_usage: dict[str, int] = Field(default_factory=dict)
    latency_ms: int


class RewriteRecord(BaseModel):
    """저장되는 요청+결과. 2주차에 rewrite_requests/rewrite_results 테이블로 나눈다."""

    id: UUID
    user_id: str
    contact_id: UUID
    persona_version_id: UUID | None
    draft_version: int
    condition: Condition = "B"  # 요청한 실험 조건 (0007 이전 행은 B로 채워짐)
    input_hash: str
    idempotency_key: str | None
    created_at: datetime
    result: RewriteResult


class Feedback(BaseModel):
    request_id: UUID
    accepted: bool
    edited_text: str | None = None
    reason: str | None = Field(None, max_length=500)
    consent_to_reuse: bool = False
