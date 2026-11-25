"""관리자 화면 서비스: 기간 계산 → 집계(저장소) → 비율·비용 환산 → 개인정보를 줄인 응답 모양.

이 서비스가 지키는 것 (계약 §4.6):
- 모든 호출을 `admin_access_log`에 먼저 남긴다. 기록에 실패하면 자료도 주지 않는다.
- 대화 원문·첨삭 문장·자유 서술 사유·편집본은 응답에 없다(저장소가 애초에 읽지 않는다). 사용자 id는
  `u_8f2…a1`처럼 줄이고, 작업 오류 문자열은 오류 종류만 남긴다(`error_kind`).
- 표본이 없으면 비율·백분위는 0이 아니라 None, 단가가 없으면 비용도 None이다 — 숫자를 지어내지 않는다.
- 날짜 경계는 사용량 한도와 같은 `usage_day_offset_hours`(기본 한국 시간).

sqlalchemy는 저장소(`repositories/admin.py`)만 import한다 — 이 모듈은 db extra 없이도 import된다.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.config import Settings
from app.core.errors import DomainError, InvalidInputError

if TYPE_CHECKING:
    from app.repositories.admin import AdminRepo, OverviewRaw
    from app.repositories.base import ConversationRepo

ALLOWED_DAYS = (1, 7, 30)
CONDITIONS = ("A", "B", "C", "D")
CHANNELS = ("web", "mobile", "desktop", "api")
GRAPH_JOB_STATES = ("pending", "running", "failed", "limited")
CHECK_TIMEOUT_SECONDS = 3.0
MAX_ERROR_KIND_CHARS = 80


class AdminForbiddenError(DomainError):
    """로그인은 했지만 관리자가 아니다. 응답은 다른 403과 같은 `forbidden`이다."""

    status_code = 403
    code = "forbidden"


class AdminUnavailableError(DomainError):
    """집계는 PostgreSQL에서만 한다. 인메모리 저장소로 뜬 서버에서는 관리자 화면을 쓸 수 없다."""

    status_code = 503
    code = "admin_unavailable"


class GraphDisabledError(DomainError):
    """그래프(D)가 꺼진 서버다 — 보류를 풀어도 다시 넣을 곳이 없으므로 아무것도 바꾸지 않는다."""

    status_code = 409
    code = "graph_disabled"


# ---------- 순수 함수 (DB 없이 테스트한다) ----------


def is_admin_username(username: str, settings: Settings) -> bool:
    """이 이름이 **설정으로** 관리자인지 (`ADMIN_USERNAMES`, 대소문자까지 같아야 한다).

    2026-11-16부터 권한(`users.role == "admin"`)으로도 관리자가 된다 — 사용자를 놓고 판정할 때는
    `AuthService.is_admin(user)`를 쓴다(관리자 API의 접근 검사가 그것을 쓴다).
    """
    return settings.is_admin_name(username)


def short_user_id(user_id: str) -> str:
    """사용자 id를 `u_` + 앞 3자 + `…` + 뒤 2자로 줄인다.

    users.id(UUID 문자열)를 그대로 보여 주지 않기 위한 것이다. 개발용 헤더 스텁의 짧은 id(8자 미만)는
    앞뒤를 다 보이면 전부 드러나므로 앞부분 일부만 남긴다.
    """
    raw = (user_id or "").strip()
    if len(raw) >= 8:
        return f"u_{raw[:3]}…{raw[-2:]}"
    if not raw:
        return "u_…"
    return f"u_{raw[: max(1, len(raw) // 3)]}…"


def estimate_token_cost(
    input_tokens: int, output_tokens: int, input_per_mtok: float, output_per_mtok: float
) -> float | None:
    """첨삭 토큰 비용(USD). 입력·출력 단가 중 하나라도 0이면 환산하지 않는다(None)."""
    if input_per_mtok <= 0 or output_per_mtok <= 0:
        return None
    return round((input_tokens * input_per_mtok + output_tokens * output_per_mtok) / 1_000_000, 6)


def estimate_graph_cost(episodes: int, per_episode: float) -> float | None:
    """그래프 수집 비용(USD) = 수집한 메시지 수 × 건당 단가. 단가가 0이면 None."""
    if per_episode <= 0:
        return None
    return round(episodes * per_episode, 6)


@dataclass(frozen=True)
class Period:
    """집계 기간. `[start, end)`이고 end는 마지막 날 다음 날 0시(하루 경계 시간대)."""

    days: int
    start: datetime
    end: datetime
    first_day: date
    last_day: date
    offset_hours: int

    def each_day(self) -> list[date]:
        return [self.first_day + timedelta(days=i) for i in range(self.days)]


def period_bounds(days: int, now: datetime, offset_hours: int = 9) -> Period:
    """오늘(하루 경계 시간대의 날짜)을 포함한 최근 `days`일. 1이면 오늘 하루."""
    if days not in ALLOWED_DAYS:
        raise InvalidInputError("days는 1, 7, 30 중 하나여야 합니다.")
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    tz = timezone(timedelta(hours=offset_hours))
    last = now.astimezone(tz).date()
    first = last - timedelta(days=days - 1)
    return Period(
        days=days,
        start=datetime.combine(first, time.min, tzinfo=tz),
        end=datetime.combine(last + timedelta(days=1), time.min, tzinfo=tz),
        first_day=first,
        last_day=last,
        offset_hours=offset_hours,
    )


_CODE = re.compile(r"[A-Za-z0-9_\-]{1,40}")


def safe_code(value: str | None, default: str = "other") -> str:
    """짧은 코드 값(조건·채널·사유 코드·상태)만 통과시킨다. 문장처럼 보이는 값은 default로 바꾼다.

    DB의 이 열들은 서버가 정한 값만 들어가지만, 관리자 응답에 자유 문장이 섞이지 않게 한 번 더 거른다.
    """
    if value is None:
        return default
    return value if _CODE.fullmatch(value) else default


_EXC_NAME = re.compile(
    r"\b([A-Z][A-Za-z0-9]{2,60}(?:Error|Exception|Timeout|Unavailable|Refused|Failure|Exceeded))\b"
)
_NEO_CODE = re.compile(r"\b(Neo\.[A-Za-z]{1,30}\.[A-Za-z]{1,30}\.[A-Za-z]{1,40})\b")
_SNAKE_ERROR = re.compile(r"\b([a-z]{2,20}(?:_[a-z]{2,20}){0,3}_error)\b")
_HTTP_STATUS = re.compile(r"(?:error code|status code|status|http)\D{0,3}([45]\d\d)\b", re.IGNORECASE)
_KEYWORDS = (
    ("timeout", ("timed out", "timeout", "시간 초과")),
    ("rate_limit", ("rate limit", "too many requests", "한도 초과")),
    ("connection", ("connect", "connection", "연결")),
    ("auth", ("unauthorized", "authentication", "forbidden")),
)


def error_kind(message: str | None) -> str | None:
    """오류 문자열에서 **종류만** 뽑는다. 못 알아보면 "other".

    `import_jobs.graph_error`는 `str(exc)[:500]`이라 모델·그래프 라이브러리가 되돌려 준 본문(대화 내용)이
    섞일 수 있다. 그래서 문자열을 잘라 보여 주지 않고, 예외 이름·오류 코드·HTTP 상태처럼 형태가 정해진
    토큰만 골라낸다. 자세한 내용은 서버 로그에서 본다.
    """
    if message is None or not message.strip():
        return None
    found: list[str] = []
    for pattern in (_NEO_CODE, _EXC_NAME, _SNAKE_ERROR):
        m = pattern.search(message)
        # Neo4j 코드(Neo.ClientError.…) 안의 조각(ClientError)을 한 번 더 적지 않는다.
        if m and not any(m.group(1) in earlier for earlier in found):
            found.append(m.group(1))
    status = _HTTP_STATUS.search(message)
    if status:
        found.append(f"HTTP {status.group(1)}")
    if not found:
        lowered = message.lower()
        found = [name for name, needles in _KEYWORDS if any(n in lowered for n in needles)][:1]
    return " · ".join(found)[:MAX_ERROR_KIND_CHARS] if found else "other"


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole > 0 else None


# ---------- 응답 모양 (계약 §4.6) ----------


class RangeOut(BaseModel):
    days: int
    # `from`은 파이썬 예약어라 필드 이름은 from_, 응답 JSON에서는 "from"으로 나간다.
    from_: datetime = Field(serialization_alias="from", description="기간 시작 (포함, 하루 경계 시간대)")
    to: datetime = Field(description="기간 끝 (미포함 — 마지막 날 다음 날 0시)")


class UsersSummary(BaseModel):
    total: int
    new: int
    active: int = Field(description="기간 안에 첨삭을 요청한 사용자 수 (등록 안 된 id 포함)")
    active_unregistered: int = Field(0, description="그중 users에 없는 id (개발용 헤더 스텁)")


class DayCount(BaseModel):
    day: date
    total: int
    fallback: int


class RewritesSummary(BaseModel):
    total: int
    recommended: int
    fallback: int
    success_rate: float | None = Field(description="recommended / total. 요청이 없으면 null")
    by_condition: dict[str, int]
    by_channel: dict[str, int]
    by_day: list[DayCount]


class LatencySummary(BaseModel):
    p50: int | None
    p95: int | None


class ReasonCount(BaseModel):
    check: str
    count: int = Field(description="이 종류의 위반으로 원문을 유지한 요청 수")


class CodeCount(BaseModel):
    code: str
    count: int


class FeedbackSummary(BaseModel):
    total: int
    accepted: int
    rejected: int
    edited: int
    acceptance_rate: float | None = Field(description="accepted / total. 피드백이 없으면 null")
    by_reason_code: list[CodeCount] = Field(description="거절 피드백의 선택형 사유. 안 고른 것은 unspecified")


class TokensSummary(BaseModel):
    input: int
    output: int
    cache_read: int = Field(0, description="캐시 읽기 토큰 (저장된 결과 기준, 비용 환산에 넣지 않는다)")
    cache_creation: int = Field(0, description="캐시 쓰기 토큰 (저장된 결과 기준, 비용 환산에 넣지 않는다)")
    estimated_cost_usd: float | None


class GraphJobs(BaseModel):
    pending: int = 0
    running: int = 0
    failed: int = 0
    limited: int = 0


class GraphSummary(BaseModel):
    enabled: bool
    ingested_messages: int = Field(description="기간 안에 그래프에 넣은 메시지 수")
    pending_messages: int = Field(description="지금 그래프에 들어가지 않은 메시지 수 (보류된 것은 빼고)")
    held_messages: int = Field(
        0, description="거듭 실패해 보류된 메시지 수 — 목록 `GET /v1/admin/graph/held`, 풀기 `.../retry-held`"
    )
    jobs: GraphJobs
    estimated_cost_usd: float | None


class LimitsSummary(BaseModel):
    users_at_daily_limit: int = Field(description="기간 중 하루 요청 수 또는 토큰 한도에 닿은 사용자 수")
    daily_rewrite_limit: int
    daily_token_limit: int
    today_rewrites: int = Field(description="오늘 전체 사용자의 첨삭 요청 수 (사용량 카운터)")
    today_tokens: int
    global_daily_rewrite_limit: int = Field(description="전체 합계 차단기. 0이면 꺼짐")
    global_daily_token_limit: int


class HealthSummary(BaseModel):
    postgres: str
    neo4j: str


class OverviewOut(BaseModel):
    range: RangeOut
    users: UsersSummary
    rewrites: RewritesSummary
    latency_ms: LatencySummary
    fallback_reasons: list[ReasonCount]
    feedback: FeedbackSummary
    tokens: TokensSummary
    graph: GraphSummary
    limits: LimitsSummary
    health: HealthSummary


class UserOut(BaseModel):
    user_id: str
    username: str
    created_at: datetime
    consent_version: str | None
    contacts: int
    messages: int
    rewrites: int
    last_rewrite_at: datetime | None


class UsersPage(BaseModel):
    total: int
    items: list[UserOut]


class JobOut(BaseModel):
    id: str
    user_id: str
    status: str
    kind: str = Field(description="import(가져오기) | speaker_roles(화자 역할 고치기 — 넣은 메시지 없음)")
    imported: int
    episodes: int
    error_code: str | None
    graph_status: str
    graph_episodes: int
    graph_error: str | None = Field(description="오류 종류만 (원래 문자열은 서버 로그에서 본다)")
    created_at: datetime


class RejectedFeedbackOut(BaseModel):
    user_id: str
    relationship_type: str | None
    channel: str
    condition: str
    reason_code: str | None
    created_at: datetime


class HeldGroupOut(BaseModel):
    """보류된 메시지를 대화방별로 묶은 한 줄. 본문·화자 이름·방 제목은 없다."""

    user_id: str = Field(description="줄인 사용자 id (`u_8f2…a1`) — 다시 시도의 범위로는 쓸 수 없다")
    conversation_id: UUID = Field(description="대화방 id — `retry-held`의 `conversation_id`로 그대로 쓴다")
    messages: int = Field(description="이 대화방에서 보류된 메시지 수")
    attempts: int = Field(description="그 메시지들의 실패 횟수 중 가장 큰 값")
    oldest_held_at: datetime
    latest_held_at: datetime


class RetryHeldIn(BaseModel):
    """다시 시도할 범위. 둘 다 비우면(또는 본문이 없으면) 모든 사용자의 보류를 푼다."""

    user_id: str | None = Field(
        None, min_length=1, max_length=128, description="사용자 id 전체 (회원관리 `/members`의 `user_id`)"
    )
    conversation_id: UUID | None = Field(None, description="보류 목록(`/graph/held`)의 `conversation_id`")


class RetryHeldOut(BaseModel):
    released: int = Field(description="보류를 풀어 다시 수집 대상으로 만든 메시지 수")
    users: int = Field(description="그 메시지들의 사용자 수")
    started: bool = Field(description="백그라운드 수집을 시작했는가 (푼 것이 없으면 false)")


class AccessLogOut(BaseModel):
    admin_username: str
    action: str
    detail: str | None
    created_at: datetime


@dataclass(frozen=True)
class AdminIdentity:
    """접근 검사를 통과한 관리자. 접근 기록에 남긴다."""

    user_id: str
    username: str


def _tally(base: tuple[str, ...], pairs: list[tuple[str, int]]) -> dict[str, int]:
    """정해진 키를 0으로 깔고 센다. 예상 밖의 값도 버리지 않아 합계가 맞는다."""
    out = dict.fromkeys(base, 0)
    for key, n in pairs:
        key = safe_code(key)
        out[key] = out.get(key, 0) + n
    return out


def _merge_counts(pairs: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """같은 코드로 모인 것을 합치고 많은 순으로."""
    merged: dict[str, int] = {}
    for key, n in pairs:
        merged[key] = merged.get(key, 0) + n
    return sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))


def build_overview(
    period: Period, raw: OverviewRaw, settings: Settings, graph_enabled: bool, health: HealthSummary
) -> OverviewOut:
    """집계 원자료 → 응답. DB를 쓰지 않는 순수 조립이다."""
    groups = raw.rewrite_groups
    total = sum(g.count for g in groups)
    recommended = sum(g.count for g in groups if g.status == "recommended")
    fallback = sum(g.count for g in groups if g.status == "fallback_original")
    per_day: dict[date, list[int]] = {d: [0, 0] for d in period.each_day()}
    for g in groups:
        bucket = per_day.setdefault(g.day, [0, 0])
        bucket[0] += g.count
        bucket[1] += g.count if g.status == "fallback_original" else 0

    return OverviewOut(
        range=RangeOut(days=period.days, from_=period.start, to=period.end),
        users=UsersSummary(
            total=raw.users_total,
            new=raw.users_new,
            active=raw.active_users,
            active_unregistered=raw.active_unregistered,
        ),
        rewrites=RewritesSummary(
            total=total,
            recommended=recommended,
            fallback=fallback,
            success_rate=_rate(recommended, total),
            by_condition=_tally(CONDITIONS, [(g.condition, g.count) for g in groups]),
            by_channel=_tally(CHANNELS, [(g.channel, g.count) for g in groups]),
            by_day=[DayCount(day=d, total=n[0], fallback=n[1]) for d, n in sorted(per_day.items())],
        ),
        latency_ms=LatencySummary(
            p50=round(raw.latency_p50) if raw.latency_p50 is not None else None,
            p95=round(raw.latency_p95) if raw.latency_p95 is not None else None,
        ),
        fallback_reasons=[
            ReasonCount(check=check, count=n)
            for check, n in _merge_counts([(safe_code(c), n) for c, n in raw.fallback_reasons])
        ],
        feedback=FeedbackSummary(
            total=raw.feedback_total,
            accepted=raw.feedback_accepted,
            rejected=raw.feedback_rejected,
            edited=raw.feedback_edited,
            acceptance_rate=_rate(raw.feedback_accepted, raw.feedback_total),
            by_reason_code=[
                CodeCount(code=code, count=n)
                for code, n in _merge_counts(
                    [("unspecified" if c is None else safe_code(c), n) for c, n in raw.feedback_reasons]
                )
            ],
        ),
        tokens=TokensSummary(
            input=raw.input_tokens,
            output=raw.output_tokens,
            cache_read=raw.cache_read_tokens,
            cache_creation=raw.cache_creation_tokens,
            estimated_cost_usd=estimate_token_cost(
                raw.input_tokens,
                raw.output_tokens,
                settings.cost_rewrite_input_per_mtok,
                settings.cost_rewrite_output_per_mtok,
            ),
        ),
        graph=GraphSummary(
            enabled=graph_enabled,
            ingested_messages=raw.graph_ingested,
            pending_messages=raw.graph_pending,
            held_messages=raw.graph_held,
            jobs=GraphJobs(**{k: raw.graph_jobs.get(k, 0) for k in GRAPH_JOB_STATES}),
            estimated_cost_usd=estimate_graph_cost(raw.graph_ingested, settings.cost_graph_per_episode),
        ),
        limits=LimitsSummary(
            users_at_daily_limit=raw.users_at_limit,
            daily_rewrite_limit=settings.usage_daily_rewrite_limit,
            daily_token_limit=settings.usage_daily_token_limit,
            today_rewrites=raw.today_rewrites,
            today_tokens=raw.today_tokens,
            global_daily_rewrite_limit=settings.usage_global_daily_rewrite_limit,
            global_daily_token_limit=settings.usage_global_daily_token_limit,
        ),
        health=health,
    )


async def _probe(coro: Any) -> str:
    """연결 점검 결과를 상태 문자열로 (`/healthz?deep=true`와 같은 표기)."""
    try:
        await asyncio.wait_for(coro, CHECK_TIMEOUT_SECONDS)
        return "ok"
    except TimeoutError:
        return "error: timeout"
    except Exception as exc:  # noqa: BLE001 - 상태 문자열로 보고
        return f"error: {type(exc).__name__}"


class AdminService:
    def __init__(
        self,
        settings: Settings,
        repo: AdminRepo,
        graph_store: Any = None,
        graph_ingest: Any = None,
        conversations: ConversationRepo | None = None,
    ) -> None:
        self._settings = settings
        self._repo = repo
        self._graph_store = graph_store
        self._graph_ingest = graph_ingest  # GraphIngestService | None — 보류 다시 시도에만 쓴다
        self._conversations = conversations  # 보류 목록에만 쓴다 (그래프가 꺼진 서버에서도 읽는다)

    async def overview(self, admin: AdminIdentity, days: int, now: datetime | None = None) -> OverviewOut:
        offset = self._settings.usage_day_offset_hours
        period = period_bounds(days, now or datetime.now(UTC), offset)
        await self._log(admin, "overview", f"days={days}")
        raw = await self._repo.overview(
            start=period.start,
            end=period.end,
            first_day=period.first_day,
            last_day=period.last_day,
            today=period.last_day,
            offset_hours=offset,
            rewrite_limit=self._settings.usage_daily_rewrite_limit,
            token_limit=self._settings.usage_daily_token_limit,
        )
        health = HealthSummary(
            postgres=await _probe(self._repo.ping()),
            neo4j=await _probe(self._graph_store.ping()) if self._graph_store is not None else "skipped",
        )
        return build_overview(period, raw, self._settings, self._graph_store is not None, health)

    async def users(self, admin: AdminIdentity, limit: int, offset: int) -> UsersPage:
        await self._log(admin, "users", f"limit={limit} offset={offset}")
        total, rows = await self._repo.list_users(limit, offset)
        return UsersPage(
            total=total,
            items=[
                UserOut(
                    user_id=short_user_id(r.user_id),
                    username=r.username,
                    created_at=r.created_at,
                    consent_version=r.consent_version,
                    contacts=r.contacts,
                    messages=r.messages,
                    rewrites=r.rewrites,
                    last_rewrite_at=r.last_rewrite_at,
                )
                for r in rows
            ],
        )

    async def jobs(self, admin: AdminIdentity, limit: int) -> list[JobOut]:
        await self._log(admin, "jobs", f"limit={limit}")
        return [
            JobOut(
                id=r.id,
                user_id=short_user_id(r.user_id),
                status=safe_code(r.status),
                kind=safe_code(r.kind),
                imported=r.imported,
                episodes=r.episodes,
                error_code=safe_code(r.error_code) if r.error_code else None,
                graph_status=safe_code(r.graph_status),
                graph_episodes=r.graph_episodes,
                graph_error=error_kind(r.graph_error),
                created_at=r.created_at,
            )
            for r in await self._repo.list_jobs(limit)
        ]

    async def rejected_feedback(self, admin: AdminIdentity, limit: int) -> list[RejectedFeedbackOut]:
        await self._log(admin, "feedback", f"limit={limit}")
        return [
            RejectedFeedbackOut(
                user_id=short_user_id(r.user_id),
                relationship_type=safe_code(r.relationship_type) if r.relationship_type else None,
                channel=safe_code(r.channel),
                condition=safe_code(r.condition),
                reason_code=safe_code(r.reason_code) if r.reason_code else None,
                created_at=r.created_at,
            )
            for r in await self._repo.list_rejected_feedback(limit)
        ]

    async def access_log(self, admin: AdminIdentity, limit: int) -> list[AccessLogOut]:
        await self._log(admin, "access_log", f"limit={limit}")
        return [
            AccessLogOut(
                admin_username=r.admin_username, action=r.action, detail=r.detail, created_at=r.created_at
            )
            for r in await self._repo.list_access_log(limit)
        ]

    async def held(self, admin: AdminIdentity, limit: int) -> list[HeldGroupOut]:
        """보류된 그래프 메시지를 사용자·대화방별로 묶은 목록 (최근에 보류된 방부터).

        저장소가 건수·횟수·시각만 읽는다. 사용자 id는 줄이고, 다시 시도에 쓸 식별자로는 대화방 id를 준다.
        """
        await self._log(admin, "graph_held", f"limit={limit}")
        if self._conversations is None:
            return []
        return [
            HeldGroupOut(
                user_id=short_user_id(g.user_id),
                conversation_id=g.conversation_id,
                messages=g.messages,
                attempts=g.attempts,
                oldest_held_at=g.oldest_held_at,
                latest_held_at=g.latest_held_at,
            )
            for g in await self._conversations.held_graph_groups(limit)
        ]

    async def retry_held(
        self, admin: AdminIdentity, user_id: str | None = None, conversation_id: UUID | None = None
    ) -> RetryHeldOut:
        """보류된 그래프 메시지를 풀고, 그 사용자들의 수집을 백그라운드로 이어받게 한다.

        `user_id`·`conversation_id`를 주면 그 범위의 보류만 푼다(없으면 전체). 접근 기록에는 범위를
        남기되 사용자 id는 줄여 적는다. 수집은 풀린 사용자의 미수집 전체를 잇는다(보류는 고른 것만 풀린다).

        그래프는 지우지 않는다(`scripts/graph_reset.py`와 다른 점). 수집은 API 경로라 상한을 따른다 —
        예산이 없는 사용자의 메시지는 미수집으로 남아 예산이 생기면 주기 이어받기가 넣는다.
        요청은 기다리지 않는다(메시지당 모델 호출 10~15초). 결과는 접근 기록에 한 줄 더 남긴다.
        """
        scope = []
        if user_id is not None:
            scope.append(f"user={short_user_id(user_id)}")
        if conversation_id is not None:
            scope.append(f"conversation={conversation_id}")
        await self._log(admin, "graph_retry_held", " ".join(scope))
        if self._graph_ingest is None:
            raise GraphDisabledError("사건 정리(그래프)가 꺼진 서버라 다시 시도할 수 없습니다.")
        released = await self._graph_ingest.release_held(user_id, conversation_id)
        started = self._graph_ingest.start_resume(released)
        out = RetryHeldOut(released=sum(released.values()), users=len(released), started=started)
        await self._log(admin, "graph_retry_held_done", f"released={out.released} users={out.users}")
        return out

    async def record(self, admin: AdminIdentity, action: str, detail: str) -> None:
        """접근 기록 한 줄 (회원관리 서비스가 같은 `admin_access_log`에 남길 때 쓴다)."""
        await self._log(admin, action, detail)

    async def _log(self, admin: AdminIdentity, action: str, detail: str) -> None:
        await self._repo.log_access(admin.user_id, admin.username, action, detail)
