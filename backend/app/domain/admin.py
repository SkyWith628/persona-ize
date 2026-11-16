"""운영 집계에 쓰는 값. 저장소가 채우고 `services/admin_service.py`가 합친다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RewriteStat:
    """첨삭 한 건의 집계용 요약. 본문은 없다."""

    user_id: str
    condition: str
    created_at: datetime
    status: str  # recommended | fallback_original | pending
    latency_ms: int
    input_tokens: int
    output_tokens: int
    violation_checks: tuple[str, ...] = ()
    feedback_accepted: bool | None = None  # 피드백이 없으면 None
    feedback_has_reason: bool = False
