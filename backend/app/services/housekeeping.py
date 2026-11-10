"""정리 작업 (운영): 만료·폐기된 세션, 오래된 가져오기 작업, 오래된 사용량 카운터, 보존 기간이 지난 첨삭 이력.

- 세션: 만료(expires_at) 또는 폐기(revoked_at)된 뒤 `session_grace`가 지난 행. 활성 세션은 건드리지 않는다.
- 가져오기 작업: `job_age`보다 오래된 작업. pending/running(그래프 수집 중)은 남긴다. 메시지 중복 방지는
  source_key로 하므로 작업 행을 지워도 다시 올린 파일이 중복 저장되지 않는다.
- 사용량 카운터: `usage_age`보다 오래된 날. 보고(`scripts/usage_report.py`)를 그 뒤로는 못 보니 기본 90일.
- 첨삭 이력: `REWRITE_RETENTION_DAYS`가 0보다 크면 그보다 오래된 요청·결과·피드백을 지운다 (0이면 무기한).
  가져온 대화 원문은 여기서 지우지 않는다 — 사용자가 상대별·대화방별로 지운다.
로그인 제한(LoginThrottle)은 프로세스 메모리라 서비스 안에서 스스로 비운다(`prune_all`).

서버 안 주기 실행(`HOUSEKEEPING_INTERVAL_HOURS`)과 `scripts/housekeeping.py`는
`run_housekeeping(state)`를 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from app.domain.usage import usage_day
from app.repositories.auth import AuthRepo
from app.repositories.base import ConversationRepo, RewriteRepo
from app.repositories.usage import UsageRepo

if TYPE_CHECKING:
    from app.api.deps import AppState

SESSION_GRACE_DAYS = 7
JOB_DAYS = 30
USAGE_DAYS = 90


@dataclass(frozen=True)
class HousekeepingResult:
    sessions: int
    import_jobs: int
    usage_rows: int
    rewrites: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "sessions": self.sessions,
            "import_jobs": self.import_jobs,
            "usage_rows": self.usage_rows,
            "rewrites": self.rewrites,
        }


class Housekeeping:
    def __init__(
        self,
        auth: AuthRepo,
        conversations: ConversationRepo,
        usage: UsageRepo,
        rewrites: RewriteRepo | None = None,
    ) -> None:
        self._auth = auth
        self._conversations = conversations
        self._usage = usage
        self._rewrites = rewrites

    async def run(
        self,
        now: datetime | None = None,
        session_grace: timedelta = timedelta(days=SESSION_GRACE_DAYS),
        job_age: timedelta = timedelta(days=JOB_DAYS),
        usage_age: timedelta = timedelta(days=USAGE_DAYS),
        day_offset_hours: int = 9,
        rewrite_age: timedelta | None = None,
    ) -> HousekeepingResult:
        """`rewrite_age`가 None이면 첨삭 이력은 지우지 않는다 (보존 기간 무기한)."""
        now = now or datetime.now(UTC)
        sessions = await self._auth.purge_sessions(now - session_grace)
        jobs = await self._conversations.purge_jobs(now - job_age)
        usage = await self._usage.purge_before(usage_day(now - usage_age, offset_hours=day_offset_hours))
        rewrites = 0
        if rewrite_age is not None and self._rewrites is not None:
            rewrites = await self._rewrites.purge_before(now - rewrite_age)
        return HousekeepingResult(sessions=sessions, import_jobs=jobs, usage_rows=usage, rewrites=rewrites)


async def run_housekeeping(
    state: AppState,
    *,
    now: datetime | None = None,
    session_grace_days: int = SESSION_GRACE_DAYS,
    job_days: int = JOB_DAYS,
    usage_days: int = USAGE_DAYS,
) -> dict[str, int]:
    """정리를 한 번 돌고 지운 수를 돌려준다: `{"sessions", "import_jobs", "usage_rows", "rewrites"}`.

    첨삭 이력 보존 기간은 설정(`REWRITE_RETENTION_DAYS`, 0=무기한)에서 읽는다.
    """
    settings = state.settings
    retention = settings.rewrite_retention_days
    result = await Housekeeping(state.auth, state.conversations, state.usage, state.rewrites).run(
        now=now,
        session_grace=timedelta(days=session_grace_days),
        job_age=timedelta(days=job_days),
        usage_age=timedelta(days=usage_days),
        day_offset_hours=settings.usage_day_offset_hours,
        rewrite_age=timedelta(days=retention) if retention > 0 else None,
    )
    return result.as_dict()
