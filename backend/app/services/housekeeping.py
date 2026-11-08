"""정리 작업 (운영): 만료·폐기된 세션, 오래된 가져오기 작업, 오래된 사용량 카운터를 지운다.

- 세션: 만료(expires_at) 또는 폐기(revoked_at)된 뒤 `session_grace`가 지난 행. 활성 세션은 건드리지 않는다.
- 가져오기 작업: `job_age`보다 오래된 작업. pending/running(그래프 수집 중)은 남긴다. 메시지 중복 방지는
  source_key로 하므로 작업 행을 지워도 다시 올린 파일이 중복 저장되지 않는다.
- 사용량 카운터: `usage_age`보다 오래된 날. 보고(`scripts/usage_report.py`)를 그 뒤로는 못 보니 기본 90일.
로그인 제한(LoginThrottle)은 프로세스 메모리라 서비스 안에서 스스로 비운다(`prune_all`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.domain.usage import usage_day
from app.repositories.auth import AuthRepo
from app.repositories.base import ConversationRepo
from app.repositories.usage import UsageRepo


@dataclass(frozen=True)
class HousekeepingResult:
    sessions: int
    import_jobs: int
    usage_rows: int


class Housekeeping:
    def __init__(self, auth: AuthRepo, conversations: ConversationRepo, usage: UsageRepo) -> None:
        self._auth = auth
        self._conversations = conversations
        self._usage = usage

    async def run(
        self,
        now: datetime | None = None,
        session_grace: timedelta = timedelta(days=7),
        job_age: timedelta = timedelta(days=30),
        usage_age: timedelta = timedelta(days=90),
        day_offset_hours: int = 9,
    ) -> HousekeepingResult:
        now = now or datetime.now(UTC)
        sessions = await self._auth.purge_sessions(now - session_grace)
        jobs = await self._conversations.purge_jobs(now - job_age)
        usage = await self._usage.purge_before(usage_day(now - usage_age, offset_hours=day_offset_hours))
        return HousekeepingResult(sessions=sessions, import_jobs=jobs, usage_rows=usage)
