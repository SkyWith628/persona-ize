"""정리: 만료·폐기 세션, 오래된 가져오기 작업, 오래된 사용량 카운터를 지운다. 대화·첨삭 데이터는 안 건드린다.

사용 예 (저장소 루트, DATABASE_URL은 .env):
    .venv/Scripts/python.exe scripts/housekeeping.py            # 기본: 세션 7일, 작업 30일, 사용량 90일
    .venv/Scripts/python.exe scripts/housekeeping.py --job-days 7 --usage-days 180
주기적으로 돌리려면 작업 스케줄러/cron에 하루 한 번 걸어 둔다. 지운 수를 출력한다.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.api.deps import build_state  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.services.housekeeping import Housekeeping  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="세션·가져오기 작업·사용량 카운터 정리")
    p.add_argument(
        "--session-grace-days", type=int, default=7, help="만료·폐기 뒤 이 일수가 지난 세션 (기본 7)"
    )
    p.add_argument("--job-days", type=int, default=30, help="이보다 오래된 가져오기 작업 (기본 30)")
    p.add_argument("--usage-days", type=int, default=90, help="이보다 오래된 사용량 카운터 (기본 90)")
    return p.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    if not settings.database_url:
        print("DATABASE_URL이 비어 있습니다. 인메모리 저장소는 정리할 것이 없습니다.")
        return 2
    state = build_state(settings)
    try:
        result = await Housekeeping(state.auth, state.conversations, state.usage).run(
            session_grace=timedelta(days=args.session_grace_days),
            job_age=timedelta(days=args.job_days),
            usage_age=timedelta(days=args.usage_days),
            day_offset_hours=settings.usage_day_offset_hours,
        )
    finally:
        await state.close()
    print(
        f"지움: 세션 {result.sessions}, 가져오기 작업 {result.import_jobs}, 사용량 카운터 {result.usage_rows}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
