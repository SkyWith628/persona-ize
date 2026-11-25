"""관리자 화면용 집계 저장소 (PostgreSQL 전용).

다른 저장소와 달리 user_id로 격리하지 않는다 — 전체 사용자를 가로지르는 운영 집계다. 그래서 여기 쿼리는
**개인정보 원칙**을 SELECT 목록에서부터 지킨다: 대화 원문(`messages.text`), 첨삭 문장
(`rewrite_results.result_text`와 payload 안의 문장), 자유 서술 사유(`feedback.reason`), 편집본
(`feedback.edited_text`)은 꺼내지 않는다. payload에서는 검증 위반의 종류(`check`)만, 편집본은 있는지만 센다.
`import_jobs.graph_error`는 원문 그대로 읽지만 서비스(`admin_service.error_kind`)가 오류 종류로 줄인 뒤에만
응답에 나간다.

- 집계는 DB 안에서 한다(GROUP BY, `percentile_cont`, JSONB 풀기). 파이썬은 합쳐서 모양만 만든다.
- `rewrite_requests.user_id`는 문자열이다. 실사용자는 users.id(UUID 문자열), 개발용 헤더 스텁 사용자는
  임의 문자열이라 users와 조인되지 않는다. 요청·토큰 합계는 users와 조인하지 않고 세고, 사용자 목록만
  users에서 출발해 `users.id::text = user_id`로 붙인다.
- 날짜 경계는 호출자가 준 `[start, end)`와 `offset_hours`(사용량과 같은 하루 경계)를 그대로 쓴다.
- 개요(overview)의 쿼리들은 한 트랜잭션(REPEATABLE READ)에서 돌려 수치가 서로 어긋나지 않게 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import uuid4

from sqlalchemy import text

from app.db.models import AdminAccessLogRow
from app.db.session import SessionFactory
from app.domain.conversation import import_job_kind

# ---- 개요 ----

_USERS = text(
    """
    SELECT count(*) AS total,
           count(*) FILTER (WHERE created_at >= :start AND created_at < :end) AS new
    FROM users
    """
)

# 요청을 보낸 사용자 수. users에 없는 id(헤더 스텁)도 활동으로 세되 몇 명인지 따로 알려 준다.
_ACTIVE_USERS = text(
    """
    SELECT count(DISTINCT rr.user_id) AS active,
           count(DISTINCT rr.user_id) FILTER (WHERE u.id IS NULL) AS unregistered
    FROM rewrite_requests rr
    LEFT JOIN users u ON u.id::text = rr.user_id
    WHERE rr.created_at >= :start AND rr.created_at < :end
    """
)

# 날짜(하루 경계 적용)·조건·채널·상태별 요청 수. 합계·분포·일별 막대를 모두 이 한 결과에서 만든다.
_REWRITE_GROUPS = text(
    """
    SELECT ((rr.created_at AT TIME ZONE 'UTC') + make_interval(hours => CAST(:offset AS integer)))::date
               AS day,
           rr.condition AS condition,
           rr.channel AS channel,
           rr.status AS status,
           count(*) AS n
    FROM rewrite_requests rr
    WHERE rr.created_at >= :start AND rr.created_at < :end
    GROUP BY 1, 2, 3, 4
    """
)

# 응답 시간 백분위와 캐시 토큰. 캐시 토큰은 사용량 카운터에 없어 저장된 결과에서만 알 수 있다.
_LATENCY = text(
    """
    SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY res.latency_ms) AS p50,
           percentile_cont(0.95) WITHIN GROUP (ORDER BY res.latency_ms) AS p95,
           coalesce(sum(CASE WHEN jsonb_typeof(res.token_usage->'cache_read_input_tokens') = 'number'
                             THEN (res.token_usage->>'cache_read_input_tokens')::numeric ELSE 0 END), 0)
               AS cache_read,
           coalesce(sum(CASE WHEN jsonb_typeof(res.token_usage->'cache_creation_input_tokens') = 'number'
                             THEN (res.token_usage->>'cache_creation_input_tokens')::numeric ELSE 0 END), 0)
               AS cache_creation
    FROM rewrite_requests rr
    JOIN rewrite_results res ON res.request_id = rr.id
    WHERE rr.created_at >= :start AND rr.created_at < :end
    """
)

# 원문 유지 결과의 최종 검증 위반 종류. 한 요청에 같은 종류가 여러 번 있어도 요청 1건으로 센다.
# 위반 목록이 비어 있는 원문 유지는 'unknown'. detail(원문 조각이 들어 있다)은 읽지 않는다.
_FALLBACK_REASONS = text(
    """
    SELECT coalesce(v.item->>'check', 'unknown') AS reason, count(DISTINCT rr.id) AS n
    FROM rewrite_requests rr
    JOIN rewrite_results res ON res.request_id = rr.id
    LEFT JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(res.payload->'validation'->'violations') = 'array'
             THEN res.payload->'validation'->'violations' ELSE '[]'::jsonb END
    ) AS v(item) ON true
    WHERE rr.status = 'fallback_original' AND rr.created_at >= :start AND rr.created_at < :end
    GROUP BY 1
    ORDER BY n DESC, reason
    """
)

_FEEDBACK = text(
    """
    SELECT count(*) AS total,
           count(*) FILTER (WHERE accepted) AS accepted,
           count(*) FILTER (WHERE NOT accepted) AS rejected,
           count(*) FILTER (WHERE edited_text IS NOT NULL AND btrim(edited_text) <> '') AS edited
    FROM feedback
    WHERE created_at >= :start AND created_at < :end
    """
)

# 거절 피드백의 선택형 사유. 사유를 고르지 않은 거절은 NULL로 묶인다.
_FEEDBACK_REASONS = text(
    """
    SELECT reason_code AS code, count(*) AS n
    FROM feedback
    WHERE NOT accepted AND created_at >= :start AND created_at < :end
    GROUP BY 1
    ORDER BY n DESC, code
    """
)

# 토큰은 사용량 카운터(사용자·일)에서 읽는다 — 사용자가 첨삭 이력을 지워도 쓴 양은 남기 때문이다.
_TOKENS = text(
    """
    SELECT coalesce(sum(input_tokens), 0) AS input, coalesce(sum(output_tokens), 0) AS output
    FROM usage_counters
    WHERE day >= :first_day AND day <= :last_day
    """
)

_USERS_AT_LIMIT = text(
    """
    SELECT count(DISTINCT user_id) AS n
    FROM usage_counters
    WHERE day >= :first_day AND day <= :last_day
      AND user_id <> '(deleted)'
      AND (rewrites >= CAST(:rewrite_limit AS bigint)
           OR input_tokens + output_tokens >= CAST(:token_limit AS bigint))
    """
)

_TODAY_USAGE = text(
    """
    SELECT coalesce(sum(rewrites), 0) AS rewrites, coalesce(sum(input_tokens + output_tokens), 0) AS tokens
    FROM usage_counters
    WHERE day = :today
    """
)

# 수집한 메시지는 기간 안의 것, 미수집 메시지는 지금 남아 있는 전부.
_GRAPH_MESSAGES = text(
    """
    SELECT count(*) FILTER (WHERE graph_ingested_at >= :start AND graph_ingested_at < :end) AS ingested,
           count(*) FILTER (WHERE graph_ingested_at IS NULL AND graph_held_at IS NULL) AS pending,
           count(*) FILTER (WHERE graph_ingested_at IS NULL AND graph_held_at IS NOT NULL) AS held
    FROM messages
    """
)

# 지금 손이 가야 하는 작업 수 (기간과 무관한 현재 상태).
_GRAPH_JOBS = text(
    """
    SELECT graph_status AS status, count(*) AS n
    FROM import_jobs
    WHERE graph_status IN ('pending', 'running', 'failed', 'limited')
    GROUP BY 1
    """
)

# ---- 목록 ----

_USER_COUNT = text("SELECT count(*) FROM users")

_USER_LIST = text(
    """
    SELECT u.id::text AS user_id, u.username, u.created_at, u.consent_version,
           (SELECT count(*) FROM contacts c WHERE c.user_id = u.id::text) AS contacts,
           (SELECT count(*) FROM messages m WHERE m.user_id = u.id::text) AS messages,
           r.rewrites, r.last_rewrite_at
    FROM users u
    LEFT JOIN LATERAL (
        SELECT count(*) AS rewrites, max(rr.created_at) AS last_rewrite_at
        FROM rewrite_requests rr
        WHERE rr.user_id = u.id::text
    ) r ON true
    ORDER BY u.created_at DESC, u.id
    LIMIT :limit OFFSET :offset
    """
)

_JOB_LIST = text(
    """
    SELECT id::text AS id, user_id, status, imported, duplicates, episodes, error_code,
           graph_status, graph_episodes, graph_error, created_at
    FROM import_jobs
    ORDER BY created_at DESC, id
    LIMIT :limit
    """
)

# 거절 피드백. 자유 서술 사유(reason)와 편집본(edited_text)은 SELECT하지 않는다.
_REJECTED_FEEDBACK = text(
    """
    SELECT f.user_id, c.relationship_type, rr.channel, rr.condition, f.reason_code, f.created_at
    FROM feedback f
    JOIN rewrite_requests rr ON rr.id = f.request_id
    LEFT JOIN contacts c ON c.id = rr.contact_id
    WHERE NOT f.accepted
    ORDER BY f.created_at DESC, f.request_id
    LIMIT :limit
    """
)

_ACCESS_LOG = text(
    """
    SELECT admin_username, action, detail, created_at
    FROM admin_access_log
    ORDER BY created_at DESC, id
    LIMIT :limit
    """
)


@dataclass(frozen=True)
class RewriteGroup:
    day: date
    condition: str
    channel: str
    status: str
    count: int


@dataclass(frozen=True)
class OverviewRaw:
    """개요 집계의 원자료. 비율·환산·이름 줄이기는 서비스가 한다."""

    users_total: int
    users_new: int
    active_users: int
    active_unregistered: int
    rewrite_groups: list[RewriteGroup]
    latency_p50: float | None
    latency_p95: float | None
    cache_read_tokens: int
    cache_creation_tokens: int
    fallback_reasons: list[tuple[str, int]]
    feedback_total: int
    feedback_accepted: int
    feedback_rejected: int
    feedback_edited: int
    feedback_reasons: list[tuple[str | None, int]]
    input_tokens: int
    output_tokens: int
    users_at_limit: int
    today_rewrites: int
    today_tokens: int
    graph_ingested: int
    graph_pending: int
    graph_jobs: dict[str, int]
    graph_held: int = 0


@dataclass(frozen=True)
class UserLine:
    user_id: str
    username: str
    created_at: datetime
    consent_version: str | None
    contacts: int
    messages: int
    rewrites: int
    last_rewrite_at: datetime | None


@dataclass(frozen=True)
class JobLine:
    id: str
    user_id: str
    status: str
    kind: str  # import | speaker_roles (`import_job_kind`)
    imported: int
    episodes: int
    error_code: str | None
    graph_status: str
    graph_episodes: int
    graph_error: str | None  # 원문 그대로 — 서비스가 오류 종류로 줄인다
    created_at: datetime


@dataclass(frozen=True)
class RejectedFeedbackLine:
    user_id: str
    relationship_type: str | None
    channel: str
    condition: str
    reason_code: str | None
    created_at: datetime


@dataclass(frozen=True)
class AccessLogLine:
    admin_username: str
    action: str
    detail: str | None
    created_at: datetime


class AdminRepo:
    def __init__(self, sessions: SessionFactory) -> None:
        self._sessions = sessions

    async def ping(self) -> None:
        """연결 확인. 실패하면 예외."""
        async with self._sessions() as s:
            await s.execute(text("SELECT 1"))

    async def log_access(
        self, admin_user_id: str, admin_username: str, action: str, detail: str | None
    ) -> None:
        """관리자 호출 한 건을 남긴다. 열 길이를 넘지 않게 자른다."""
        row = AdminAccessLogRow(
            id=uuid4(),
            admin_user_id=admin_user_id[:128],
            admin_username=admin_username[:32],
            action=action[:64],
            detail=detail[:200] if detail else None,
            created_at=datetime.now(UTC),
        )
        async with self._sessions.begin() as s:
            s.add(row)

    async def overview(
        self,
        *,
        start: datetime,
        end: datetime,
        first_day: date,
        last_day: date,
        today: date,
        offset_hours: int,
        rewrite_limit: int,
        token_limit: int,
    ) -> OverviewRaw:
        """`[start, end)` 기간의 집계. first_day~last_day는 같은 기간의 사용량 날짜(둘 다 포함)."""
        span = {"start": start, "end": end}
        days = {"first_day": first_day, "last_day": last_day}
        async with self._sessions() as s:
            # 쿼리 사이에 새 요청이 들어와도 합계와 분포가 어긋나지 않게 한 스냅샷에서 읽는다.
            await s.connection(execution_options={"isolation_level": "REPEATABLE READ"})
            users = (await s.execute(_USERS, span)).one()
            active = (await s.execute(_ACTIVE_USERS, span)).one()
            groups = (await s.execute(_REWRITE_GROUPS, {**span, "offset": offset_hours})).all()
            latency = (await s.execute(_LATENCY, span)).one()
            reasons = (await s.execute(_FALLBACK_REASONS, span)).all()
            feedback = (await s.execute(_FEEDBACK, span)).one()
            feedback_reasons = (await s.execute(_FEEDBACK_REASONS, span)).all()
            tokens = (await s.execute(_TOKENS, days)).one()
            at_limit = await s.scalar(
                _USERS_AT_LIMIT, {**days, "rewrite_limit": rewrite_limit, "token_limit": token_limit}
            )
            today_usage = (await s.execute(_TODAY_USAGE, {"today": today})).one()
            graph_messages = (await s.execute(_GRAPH_MESSAGES, span)).one()
            graph_jobs = (await s.execute(_GRAPH_JOBS)).all()
        return OverviewRaw(
            users_total=int(users.total),
            users_new=int(users.new),
            active_users=int(active.active),
            active_unregistered=int(active.unregistered),
            rewrite_groups=[
                RewriteGroup(
                    day=g.day, condition=g.condition, channel=g.channel, status=g.status, count=int(g.n)
                )
                for g in groups
            ],
            latency_p50=float(latency.p50) if latency.p50 is not None else None,
            latency_p95=float(latency.p95) if latency.p95 is not None else None,
            cache_read_tokens=int(latency.cache_read),
            cache_creation_tokens=int(latency.cache_creation),
            fallback_reasons=[(str(r.reason), int(r.n)) for r in reasons],
            feedback_total=int(feedback.total),
            feedback_accepted=int(feedback.accepted),
            feedback_rejected=int(feedback.rejected),
            feedback_edited=int(feedback.edited),
            feedback_reasons=[(r.code, int(r.n)) for r in feedback_reasons],
            input_tokens=int(tokens.input),
            output_tokens=int(tokens.output),
            users_at_limit=int(at_limit or 0),
            today_rewrites=int(today_usage.rewrites),
            today_tokens=int(today_usage.tokens),
            graph_ingested=int(graph_messages.ingested),
            graph_pending=int(graph_messages.pending),
            graph_held=int(graph_messages.held),
            graph_jobs={str(j.status): int(j.n) for j in graph_jobs},
        )

    async def list_users(self, limit: int, offset: int) -> tuple[int, list[UserLine]]:
        """(전체 수, 최근 가입 순 한 쪽)."""
        async with self._sessions() as s:
            total = int(await s.scalar(_USER_COUNT) or 0)
            rows = (await s.execute(_USER_LIST, {"limit": limit, "offset": offset})).all()
        return total, [
            UserLine(
                user_id=r.user_id,
                username=r.username,
                created_at=r.created_at,
                consent_version=r.consent_version,
                contacts=int(r.contacts),
                messages=int(r.messages),
                rewrites=int(r.rewrites or 0),
                last_rewrite_at=r.last_rewrite_at,
            )
            for r in rows
        ]

    async def list_jobs(self, limit: int) -> list[JobLine]:
        async with self._sessions() as s:
            rows = (await s.execute(_JOB_LIST, {"limit": limit})).all()
        return [
            JobLine(
                id=r.id,
                user_id=r.user_id,
                status=r.status,
                kind=import_job_kind(int(r.imported), int(r.duplicates)),
                imported=int(r.imported),
                episodes=int(r.episodes),
                error_code=r.error_code,
                graph_status=r.graph_status,
                graph_episodes=int(r.graph_episodes),
                graph_error=r.graph_error,
                created_at=r.created_at,
            )
            for r in rows
        ]

    async def list_rejected_feedback(self, limit: int) -> list[RejectedFeedbackLine]:
        async with self._sessions() as s:
            rows = (await s.execute(_REJECTED_FEEDBACK, {"limit": limit})).all()
        return [
            RejectedFeedbackLine(
                user_id=r.user_id,
                relationship_type=r.relationship_type,
                channel=r.channel,
                condition=r.condition,
                reason_code=r.reason_code,
                created_at=r.created_at,
            )
            for r in rows
        ]

    async def list_access_log(self, limit: int) -> list[AccessLogLine]:
        async with self._sessions() as s:
            rows = (await s.execute(_ACCESS_LOG, {"limit": limit})).all()
        return [
            AccessLogLine(
                admin_username=r.admin_username, action=r.action, detail=r.detail, created_at=r.created_at
            )
            for r in rows
        ]
