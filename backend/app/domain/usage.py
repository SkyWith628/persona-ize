"""사용량 제어 (project-design §7 429, §9 운영).

하루(한국 시간 기준) 단위로 사용자별 첨삭 요청 수와 모델 토큰을 센다. 한도를 넘으면 429.
- 요청 수는 모델을 부르기 **전에** 예약한다 (한도를 넘긴 호출이 과금되지 않게).
- 토큰은 호출이 끝난 뒤 더한다. 토큰 한도는 다음 요청부터 막는다 (한 번의 초과는 허용).
- 멱등 재요청(같은 Idempotency-Key)은 세지 않는다 — 모델을 부르지 않기 때문이다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

from pydantic import BaseModel

KST = timezone(timedelta(hours=9))


def usage_day(now: datetime | None = None, offset_hours: int = 9) -> date:
    """사용량 집계 날짜. 기본은 한국 시간의 날짜."""
    tz = timezone(timedelta(hours=offset_hours))
    return (now or datetime.now(UTC)).astimezone(tz).date()


class UsageCounter(BaseModel):
    user_id: str
    day: date
    rewrites: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

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

    @property
    def remaining_rewrites(self) -> int:
        return max(self.rewrite_limit - self.rewrites, 0)
