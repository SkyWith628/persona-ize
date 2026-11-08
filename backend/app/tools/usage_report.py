"""사용량 보고 (운영): 사용자·일(KST)·조건별 첨삭 호출 수와 토큰.

두 출처를 함께 본다.
- `usage_counters`: 한도 집행에 쓰는 카운터(요청 수·토큰, 사용자·일). 모델 호출 전 예약이라 실패도 포함.
- `rewrite_requests` + `rewrite_results`: 실제 저장된 결과(조건 A/B/C/D, token_usage JSON, 지연, 검증 상태).

하루 경계는 설정의 `usage_day_offset_hours`(기본 9 = KST)로 맞춘다. 사용자 id는 기본으로 앞 8자만 보인다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.models import RewriteRequestRow, RewriteResultRow, UsageCounterRow
from app.domain.usage import usage_day


@dataclass(frozen=True)
class CounterLine:
    user_id: str
    day: date
    rewrites: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ConditionLine:
    user_id: str
    day: date
    condition: str
    requests: int
    input_tokens: int
    output_tokens: int
    avg_latency_ms: int
    failed: int  # validation_status == failed (원문 유지)


@dataclass(frozen=True)
class UsageReport:
    since: date
    until: date
    counters: list[CounterLine]
    conditions: list[ConditionLine]

    @property
    def total_rewrites(self) -> int:
        return sum(c.rewrites for c in self.counters)

    @property
    def total_tokens(self) -> tuple[int, int]:
        return sum(c.input_tokens for c in self.counters), sum(c.output_tokens for c in self.counters)


def local_day(at: datetime, day_offset_hours: int) -> date:
    """UTC 시각을 하루 경계(기본 KST)의 날짜로 — 한도 카운터와 같은 규칙."""
    return usage_day(at, offset_hours=day_offset_hours)


def group_conditions(
    rows: list[tuple[str, datetime, str, int, int, int, str]], day_offset_hours: int
) -> list[ConditionLine]:
    """(user_id, created_at, condition, input_tokens, output_tokens, latency_ms, validation_status) 행 묶기.

    날짜 경계 계산을 DB가 아닌 파이썬에서 해 어느 DB에서도 같은 결과가 나오고 단위 테스트가 된다.
    """
    acc: dict[tuple[str, date, str], list[int]] = {}
    for user_id, created_at, condition, in_tok, out_tok, latency, status in rows:
        key = (user_id, local_day(created_at, day_offset_hours), condition)
        cur = acc.setdefault(key, [0, 0, 0, 0, 0])  # n, in, out, latency_sum, failed
        cur[0] += 1
        cur[1] += in_tok
        cur[2] += out_tok
        cur[3] += latency
        cur[4] += 1 if status == "failed" else 0
    return [
        ConditionLine(u, d, c, n, i, o, round(lat / n) if n else 0, f)
        for (u, d, c), (n, i, o, lat, f) in sorted(acc.items(), key=lambda kv: (kv[0][1], kv[0][0], kv[0][2]))
    ]


async def load_report(
    engine: AsyncEngine, since: date, until: date, day_offset_hours: int = 9, user_id: str | None = None
) -> UsageReport:
    """[since, until] 날짜 범위(둘 다 포함)의 집계를 읽는다."""
    tz = timezone(timedelta(hours=day_offset_hours))
    start = datetime.combine(since, datetime.min.time(), tzinfo=tz).astimezone(UTC)
    end = datetime.combine(until + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(UTC)

    counter_q = (
        select(
            UsageCounterRow.user_id,
            UsageCounterRow.day,
            UsageCounterRow.rewrites,
            UsageCounterRow.input_tokens,
            UsageCounterRow.output_tokens,
        )
        .where(UsageCounterRow.day >= since, UsageCounterRow.day <= until)
        .order_by(UsageCounterRow.day, UsageCounterRow.user_id)
    )
    in_tok = func.coalesce(cast(RewriteResultRow.token_usage["input_tokens"].astext, Integer), 0)
    out_tok = func.coalesce(cast(RewriteResultRow.token_usage["output_tokens"].astext, Integer), 0)
    rows_q = (
        select(
            RewriteRequestRow.user_id,
            RewriteRequestRow.created_at,
            RewriteRequestRow.condition,
            in_tok,
            out_tok,
            func.coalesce(RewriteResultRow.latency_ms, 0),
            func.coalesce(RewriteResultRow.validation_status, "none"),
        )
        .join(RewriteResultRow, RewriteResultRow.request_id == RewriteRequestRow.id, isouter=True)
        .where(RewriteRequestRow.created_at >= start, RewriteRequestRow.created_at < end)
    )
    if user_id:
        counter_q = counter_q.where(UsageCounterRow.user_id == user_id)
        rows_q = rows_q.where(RewriteRequestRow.user_id == user_id)

    async with engine.connect() as conn:
        counters = [
            CounterLine(u, d, int(n), int(i), int(o))
            for u, d, n, i, o in (await conn.execute(counter_q)).all()
        ]
        raw = [
            (u, at if at.tzinfo else at.replace(tzinfo=UTC), c, int(i), int(o), int(lat), str(st))
            for u, at, c, i, o, lat, st in (await conn.execute(rows_q)).all()
        ]
    return UsageReport(
        since=since, until=until, counters=counters, conditions=group_conditions(raw, day_offset_hours)
    )


def _short(user_id: str, full: bool) -> str:
    return user_id if full or len(user_id) <= 12 else user_id[:8] + "…"


def format_report(report: UsageReport, full_ids: bool = False) -> str:
    lines = [f"# 사용량 {report.since} ~ {report.until}"]
    i, o = report.total_tokens
    lines.append(f"합계: 첨삭 {report.total_rewrites}회, 입력 {i:,} / 출력 {o:,} 토큰 (usage_counters 기준)")
    lines += [
        "",
        "## 사용자·일 (한도 카운터)",
        "| 일 | 사용자 | 첨삭 | 입력 토큰 | 출력 토큰 |",
        "|---|---|---:|---:|---:|",
    ]
    for c in report.counters:
        who = _short(c.user_id, full_ids)
        lines.append(f"| {c.day} | {who} | {c.rewrites} | {c.input_tokens:,} | {c.output_tokens:,} |")
    if not report.counters:
        lines.append("| (없음) | | | | |")
    lines += [
        "",
        "## 사용자·일·조건 (저장된 결과)",
        "| 일 | 사용자 | 조건 | 요청 | 입력 토큰 | 출력 토큰 | 평균 지연(ms) | 원문 유지 |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for c in report.conditions:
        lines.append(
            f"| {c.day} | {_short(c.user_id, full_ids)} | {c.condition} | {c.requests} | {c.input_tokens:,} |"
            f" {c.output_tokens:,} | {c.avg_latency_ms} | {c.failed} |"
        )
    if not report.conditions:
        lines.append("| (없음) | | | | | | | |")
    return "\n".join(lines) + "\n"


def to_rows(report: UsageReport) -> list[dict[str, Any]]:
    """CSV용 평면 행 (조건별 표)."""
    return [
        {
            "day": c.day.isoformat(),
            "user_id": c.user_id,
            "condition": c.condition,
            "requests": c.requests,
            "input_tokens": c.input_tokens,
            "output_tokens": c.output_tokens,
            "avg_latency_ms": c.avg_latency_ms,
            "failed": c.failed,
        }
        for c in report.conditions
    ]
