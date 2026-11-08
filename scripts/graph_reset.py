"""그래프 재구축: 한 사용자의 Neo4j 그래프를 지우고(또는 한 대화방만) 메시지를 다시 수집한다.

언제 쓰나 — 그래프 스키마·추출 프롬프트·모델(GRAPH_LLM_MODEL)을 바꿨을 때, 수집이 반쯤 깨졌을 때.
무엇을 하나
  1. --conversation <source_key> 를 주면 그 방의 에피소드만, 아니면 사용자 group 전체를 Neo4j에서 지운다.
  2. 그 메시지들의 graph_ingested_at을 비운다 (DB 원문·문체 묶음은 그대로).
  3. --no-reingest 가 아니면 바로 다시 수집한다 (haiku 호출, 메시지당 10~15초, 비용 발생).

사용 예 (저장소 루트, .env의 DATABASE_URL·NEO4J_*·ANTHROPIC_API_KEY):
    .venv/Scripts/python.exe scripts/graph_reset.py --user demo --dry-run          # 지울 것만 보여준다
    .venv/Scripts/python.exe scripts/graph_reset.py --user demo --conversation c1   # 한 방만 다시
    .venv/Scripts/python.exe scripts/graph_reset.py --user demo --no-reingest --yes # 지우기만
--user 는 username 또는 user_id. 되돌릴 수 없으므로 --yes 가 없으면 한 번 묻는다.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.api.deps import build_state  # noqa: E402
from app.core.config import Settings  # noqa: E402

SECONDS_PER_MESSAGE = 12  # 11-08 실측 10~15초


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="사용자 그래프 지우고 다시 수집")
    p.add_argument("--user", required=True, help="username 또는 user_id")
    p.add_argument("--conversation", help="이 대화방(source_key, 예: c1)만")
    p.add_argument("--no-reingest", action="store_true", help="지우기만 하고 재수집은 하지 않는다")
    p.add_argument("--dry-run", action="store_true", help="지울 대상만 보여주고 아무것도 바꾸지 않는다")
    p.add_argument("--yes", action="store_true", help="확인 질문 없이 진행")
    return p.parse_args(argv)


async def _resolve_user(state, raw: str) -> str:  # noqa: ANN001
    found = await state.auth.find_user_by_username(raw)
    return str(found[0].id) if found else raw


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    if not settings.database_url:
        print("DATABASE_URL이 비어 있습니다.")
        return 2
    state = build_state(settings)
    try:
        if not state.graph_enabled:
            print(".env의 NEO4J_URI가 비어 있어 그래프를 쓰지 않는 설정입니다.")
            return 2
        user_id = await _resolve_user(state, args.user)
        convs = await state.conversations.list(user_id)
        if args.conversation:
            convs = [c for c in convs if c.source_key == args.conversation]
            if not convs:
                print(f"대화방 '{args.conversation}'이 없습니다 (사용자 {user_id}).")
                return 2
        if not convs:
            print(f"사용자 {user_id}에게 대화방이 없습니다 (username이면 등록돼 있는지 확인).")
            return 2
        total = ingested = 0
        for c in convs:
            msgs = await state.conversations.list_messages(user_id, c.id)
            done = sum(1 for m in msgs if m.graph_ingested_at is not None)
            total += len(msgs)
            ingested += done
            print(f"- {c.source_key} ({c.title}): 메시지 {len(msgs)}, 그래프 수집됨 {done}")
        scope = f"대화방 {args.conversation}" if args.conversation else "사용자 group 전체"
        print(f"\n대상: 사용자 {user_id}, {scope} — 메시지 {total}건 (수집됨 {ingested})")
        if not args.no_reingest:
            print(
                f"재수집 예상: 약 {total * SECONDS_PER_MESSAGE // 60}분, "
                f"{settings.graph_llm_model} 호출 {total}건 이상"
            )
        if args.dry_run:
            print("--dry-run: 바꾼 것 없음.")
            return 0
        if not args.yes:
            answer = input("그래프를 지웁니다. 되돌릴 수 없습니다. 계속할까요? [y/N] ").strip().lower()
            if answer != "y":
                print("취소.")
                return 1

        conv_id = convs[0].id if args.conversation else None
        removed, reset = await state.graph_ingest.reset(user_id, conv_id)
        what = "에피소드" if args.conversation else "노드"
        print(f"지움: 그래프 {what} {removed}, 되돌린 메시지 {reset}")
        if args.no_reingest:
            print(
                "재수집은 하지 않았습니다. 서버가 뜨면 GRAPH_RESUME_ON_START로 이어받습니다 "
                "(또는 이 스크립트를 다시)."
            )
            return 0
        started = time.monotonic()
        n = await state.graph_ingest.ingest_user(user_id)
        print(f"재수집 완료: 에피소드 {n}개, {time.monotonic() - started:.0f}초")
        return 0
    finally:
        await state.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
