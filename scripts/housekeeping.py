"""정리: 만료·폐기 세션, 오래된 가져오기 작업, 오래된 사용량 카운터, 보존 기간이 지난 첨삭 이력을 지운다.

가져온 대화 원문은 건드리지 않는다. 첨삭 이력은 `.env`의 `REWRITE_RETENTION_DAYS`가 0보다 클 때만 지운다.
서버 안 주기 실행(`HOUSEKEEPING_INTERVAL_HOURS`)과 같은 함수
(`app.services.housekeeping.run_housekeeping`)를 쓴다.

사용 예 (저장소 루트, DATABASE_URL은 .env):
    .venv/Scripts/python.exe scripts/housekeeping.py            # 기본: 세션 7일, 작업 30일, 사용량 90일
    .venv/Scripts/python.exe scripts/housekeeping.py --job-days 7 --usage-days 180
주기적으로 돌리려면 작업 스케줄러/cron에 하루 한 번 걸어 둔다. 지운 수를 출력한다.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.api.deps import build_state  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.services.housekeeping import run_housekeeping  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="세션·가져오기 작업·사용량 카운터·첨삭 이력 정리")
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
        result = await run_housekeeping(
            state,
            session_grace_days=args.session_grace_days,
            job_days=args.job_days,
            usage_days=args.usage_days,
        )
    finally:
        await state.close()
    retention = settings.rewrite_retention_days
    print(
        f"지움: 세션 {result['sessions']}, 가져오기 작업 {result['import_jobs']}, "
        f"사용량 카운터 {result['usage_rows']}, 첨삭 이력 {result['rewrites']}"
        + (f" (보존 {retention}일)" if retention else " (보존 기간 무기한)")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
