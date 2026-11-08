"""합성 대화(data/synthetic/*.json)를 DB에 적재한다. 여러 번 돌려도 같은 결과 (중복은 건너뛴다).

사용 예 (저장소 루트):
    .venv/Scripts/python.exe scripts/import_synthetic.py                 # .env의 DATABASE_URL, user=demo
    .venv/Scripts/python.exe scripts/import_synthetic.py --user eval --dataset data/synthetic/x.json

DATABASE_URL이 비어 있으면 인메모리에 넣고 끝나므로 의미가 없다 — 그 경우 경고하고 종료한다.
임베딩은 .env의 EMBEDDING_PROVIDER(local이면 첫 실행에 모델 다운로드).
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
from app.services.synthetic_loader import load_synthetic  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="합성 대화 적재")
    p.add_argument("--dataset", type=Path, default=ROOT / "data" / "synthetic" / "sample_conversations.json")
    p.add_argument("--characters", type=Path, default=ROOT / "evals" / "characters.csv")
    p.add_argument("--user", default="demo", help="적재할 사용자 id (X-User-Id와 같은 값)")
    return p.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    if not settings.database_url:
        print(
            "DATABASE_URL이 비어 있습니다. 인메모리 적재는 프로세스가 끝나면 사라집니다. .env를 확인하세요."
        )
        return 2
    state = build_state(settings)
    try:
        report = await load_synthetic(state, args.dataset, args.user, args.characters)
    finally:
        await state.close()
    print(f"user={report.user_id} storage={state.storage} embedder={state.embedder.version}")
    print(f"상대 {len(report.contacts)}명, 대화방 {len(report.conversations)}개")
    print(f"메시지 새로 {report.imported}건, 중복 건너뜀 {report.duplicates}건, 묶음 {report.episodes}개")
    for line in report.failed:
        print("실패:", line)
    return 1 if report.failed else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
