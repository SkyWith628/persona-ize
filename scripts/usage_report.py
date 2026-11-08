"""사용량 보고: 사용자·일(KST)·조건별 첨삭 호출 수와 토큰 (A1 비용 점검의 DB 쪽 근거).

사용 예 (저장소 루트, DATABASE_URL은 .env):
    .venv/Scripts/python.exe scripts/usage_report.py                 # 오늘 포함 최근 7일
    .venv/Scripts/python.exe scripts/usage_report.py --days 30 --user <user_id>
    .venv/Scripts/python.exe scripts/usage_report.py --since 2026-11-01 --until 2026-11-09 --csv out.csv
출력은 마크다운 표(stdout). --csv를 주면 조건별 표를 CSV로도 쓴다. 사용자 id는 앞 8자만(--full-ids로 전체).
Anthropic Console의 청구 금액과 맞춰 볼 때 쓴다 — 토큰 수는 모델 응답의 usage를 그대로 더한 값.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import Settings  # noqa: E402
from app.db.session import create_engine  # noqa: E402
from app.domain.usage import usage_day  # noqa: E402
from app.tools.usage_report import format_report, load_report, to_rows  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="사용자·일·조건별 사용량 보고")
    p.add_argument("--days", type=int, default=7, help="오늘 포함 최근 N일 (기본 7)")
    p.add_argument("--since", type=date.fromisoformat, help="시작일 (YYYY-MM-DD, --days보다 우선)")
    p.add_argument("--until", type=date.fromisoformat, help="끝일 (기본 오늘)")
    p.add_argument("--user", help="이 사용자 id만")
    p.add_argument("--csv", type=Path, help="조건별 표를 CSV로 저장")
    p.add_argument("--full-ids", action="store_true", help="사용자 id를 줄이지 않는다")
    return p.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    if not settings.database_url:
        print("DATABASE_URL이 비어 있습니다. 사용량은 PostgreSQL에만 쌓입니다.")
        return 2
    until = args.until or usage_day(offset_hours=settings.usage_day_offset_hours)
    since = args.since or until - timedelta(days=max(args.days, 1) - 1)
    engine = create_engine(settings.database_url)
    try:
        report = await load_report(engine, since, until, settings.usage_day_offset_hours, args.user)
    finally:
        await engine.dispose()
    sys.stdout.write(format_report(report, full_ids=args.full_ids))
    if args.csv:
        rows = to_rows(report)
        with args.csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["day"])
            w.writeheader()
            w.writerows(rows)
        print(f"\nCSV: {args.csv} ({len(rows)}행)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
