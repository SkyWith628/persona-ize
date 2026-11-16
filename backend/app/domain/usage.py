"""사용량 제어 (project-design §7 429, §9 운영).

하루(한국 시간 기준) 단위로 사용자별 첨삭 요청 수와 모델 토큰을 센다. 한도를 넘으면 429.
- 요청 수는 모델을 부르기 **전에** 예약한다 (한도를 넘긴 호출이 과금되지 않게).
- 토큰은 호출이 끝난 뒤 더한다. 토큰 한도는 다음 요청부터 막는다 (한 번의 초과는 허용).
- 멱등 재요청(같은 Idempotency-Key)은 세지 않는다 — 모델을 부르지 않기 때문이다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

from pydantic import BaseModel

from app.core.errors import InvalidInputError, UsageLimitError

KST = timezone(timedelta(hours=9))


class BudgetExceededError(UsageLimitError):
    """서비스 전체(모든 사용자 합계)의 하루 한도에 닿았다 (`USAGE_GLOBAL_DAILY_*`).

    사용자 한도(`usage_exceeded`)와 구분해 `budget_exceeded`로 내보낸다. 둘 다 429.
    """

    code = "budget_exceeded"


def usage_day(now: datetime | None = None, offset_hours: int = 9) -> date:
    """사용량 집계 날짜. 기본은 한국 시간의 날짜."""
    tz = timezone(timedelta(hours=offset_hours))
    return (now or datetime.now(UTC)).astimezone(tz).date()


class QuotaExceededError(InvalidInputError):
    """사용자별 저장 한도(상대 수·대화방 수·메시지 총수)를 넘는다. 아무것도 저장하지 않는다."""

    code = "quota_exceeded"


def estimate_tokens(chars: int) -> int:
    """글자 수로 어림한 토큰 수 (한글 약 2자/토큰 — 가짜 프로바이더와 같은 기준).

    모델 호출이 실패·시간 초과로 끝나 실제 토큰 수를 모를 때, 한도 계산에만 쓴다.
    """
    return max(chars // 2, 1)


class UsageCounter(BaseModel):
    user_id: str
    day: date
    rewrites: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    graph_episodes: int = 0  # 그날 그래프에 넣은 양 (메시지 1건 = 1, 긴 메시지는 가중)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class UsageStatus(BaseModel):
    """`GET /v1/usage` 응답. 오늘 쓴 양과 한도."""

    day: date
    rewrites: int
    rewrite_limit: int
    input_tokens: int
    output_tokens: int
    token_limit: int
    # 그래프 수집 (D 조건 재료). 상한 0은 무제한.
    # graph_messages_ingested는 상한이 보는 값이다: max(지금 그래프에 든 양, 오늘 넣은 양).
    # 메시지 1건이 1이고 1,000자마다 1을 더 센다 — 보통 길이의 메시지만 있으면 건수와 같다.
    graph_messages_ingested: int = 0
    graph_message_cap: int = 0

    @property
    def remaining_rewrites(self) -> int:
        return max(self.rewrite_limit - self.rewrites, 0)
