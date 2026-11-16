"""운영 대시보드 집계 (와이어프레임 A1).

품질(추천 성공률·원문 유지 사유)·지연·토큰·수집 대기를 먼저 보인다. 대화 원문·첨삭 문장·피드백 사유 글은
다루지 않는다 — 저장소에서 꺼내는 값(`RewriteStat`)에 애초에 넣지 않는다.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field

from app.domain.admin import RewriteStat
from app.domain.usage import usage_day
from app.repositories.base import ConversationRepo, RewriteRepo
from app.repositories.usage import UsageRepo

MAX_ROWS = 20_000  # 한 번에 집계하는 첨삭 수 상한. 넘으면 `truncated`로 알린다.
RECENT_REJECTIONS = 10


class ReasonCount(BaseModel):
    check: str
    count: int


class ConditionStat(BaseModel):
    condition: str
    requests: int
    kept: int


class Rejection(BaseModel):
    user: str = Field(description="사용자 id의 해시 앞 6자 (원래 id는 내보내지 않는다)")
    condition: str
    at: datetime
    has_reason: bool


class AdminOverview(BaseModel):
    days: int
    generated_at: datetime
    truncated: bool
    requests: int
    active_users: int
    recommended: int
    kept: int
    success_rate: float | None
    latency_p50_ms: int | None
    latency_p95_ms: int | None
    feedback_total: int
    feedback_accepted: int
    accept_rate: float | None
    input_tokens: int
    output_tokens: int
    fallback_reasons: list[ReasonCount]
    by_condition: list[ConditionStat]
    recent_rejections: list[Rejection]
    users_at_limit_today: int
    daily_rewrite_limit: int
    graph_enabled: bool
    graph_pending_messages: int
    graph_failed_jobs: int


def percentile(values: list[int], q: float) -> int | None:
    """최근접 순위 백분위수. 값이 없으면 None."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def mask_user(user_id: str) -> str:
    return "u_" + hashlib.sha256(user_id.encode()).hexdigest()[:6]


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def summarize(stats: list[RewriteStat]) -> dict:
    """첨삭 요약 목록을 대시보드 수치로. 저장소와 무관한 순수 계산."""
    done = [s for s in stats if s.status in ("recommended", "fallback_original")]
    kept = [s for s in done if s.status == "fallback_original"]
    feedback = [s for s in stats if s.feedback_accepted is not None]
    reasons: Counter[str] = Counter()
    for s in kept:
        # 사유가 기록되지 않은 원문 유지(모델 장애 등)는 따로 센다.
        reasons.update(sorted(set(s.violation_checks)) or ["unknown"])
    conditions = sorted({s.condition for s in stats})
    rejected = sorted((s for s in feedback if s.feedback_accepted is False), key=lambda s: s.created_at)
    return {
        "requests": len(stats),
        "active_users": len({s.user_id for s in stats}),
        "recommended": len(done) - len(kept),
        "kept": len(kept),
        "success_rate": _rate(len(done) - len(kept), len(done)),
        "latency_p50_ms": percentile([s.latency_ms for s in done], 0.5),
        "latency_p95_ms": percentile([s.latency_ms for s in done], 0.95),
        "feedback_total": len(feedback),
        "feedback_accepted": sum(1 for s in feedback if s.feedback_accepted),
        "accept_rate": _rate(sum(1 for s in feedback if s.feedback_accepted), len(feedback)),
        "input_tokens": sum(s.input_tokens for s in stats),
        "output_tokens": sum(s.output_tokens for s in stats),
        "fallback_reasons": [ReasonCount(check=c, count=n) for c, n in reasons.most_common()],
        "by_condition": [
            ConditionStat(
                condition=c,
                requests=sum(1 for s in stats if s.condition == c),
                kept=sum(1 for s in kept if s.condition == c),
            )
            for c in conditions
        ],
        "recent_rejections": [
            Rejection(
                user=mask_user(s.user_id),
                condition=s.condition,
                at=s.created_at,
                has_reason=s.feedback_has_reason,
            )
            for s in reversed(rejected[-RECENT_REJECTIONS:])
        ],
    }


class AdminService:
    def __init__(
        self,
        rewrites: RewriteRepo,
        conversations: ConversationRepo,
        usage: UsageRepo,
        *,
        daily_rewrite_limit: int,
        graph_enabled: bool,
    ) -> None:
        self._rewrites = rewrites
        self._conversations = conversations
        self._usage = usage
        self._limit = daily_rewrite_limit
        self._graph_enabled = graph_enabled

    async def overview(self, days: int, now: datetime | None = None) -> AdminOverview:
        now = now or datetime.now(UTC)
        stats = await self._rewrites.stats_since(now - timedelta(days=days), MAX_ROWS + 1)
        pending, failed = await self._conversations.graph_backlog()
        at_limit = await self._usage.count_at_limit(usage_day(now), self._limit) if self._limit else 0
        return AdminOverview(
            days=days,
            generated_at=now,
            truncated=len(stats) > MAX_ROWS,
            users_at_limit_today=at_limit,
            daily_rewrite_limit=self._limit,
            graph_enabled=self._graph_enabled,
            graph_pending_messages=pending,
            graph_failed_jobs=failed,
            **summarize(stats[:MAX_ROWS]),
        )
