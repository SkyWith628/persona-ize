"""그래프 이름 대조: 이미 수집된 그래프에서 상대 노드 이름이 어긋난 상대를 찾아 맞춘다.

언제 쓰나 — 맥락 그래프 화면이나 D 조건의 사실 맥락이 비어 나올 때. 앱은 상대 노드를 이름이 정확히 같은
엔티티로 찾는데, 추출 모델이 이름을 다르게 적을 때가 있다(별칭 '김상무님' → 노드 '김상무'). 수집이 끝날
때마다 같은 대조가 돌지만(`GraphIngestService.reconcile_names`), 그 전에 수집된 그래프는 이 스크립트로 맞춘다.
무엇을 하나
  1. 그래프에 메시지가 들어간 상대마다 지금 이름(`contacts.graph_name` 또는 별칭)과 같은 노드가 있는지 본다.
  2. 없으면 그 상대의 대화방 에피소드가 언급한 Person 노드 중 경칭('님'·'씨')·공백만 다른 이름을 찾는다.
  3. 후보가 정확히 하나면 `contacts.graph_name`을 그 이름으로 고친다. 없거나 둘 이상이면 그대로 두고
     알려 준다 — 그때는 노드 이름을 확인해 `graph_name`을 직접 고친다.
모델을 부르지 않고 Neo4j는 읽기만 한다(비용 없음). 바꾸는 것은 PostgreSQL의 `contacts.graph_name`뿐이다.

사용 예 (저장소 루트, .env의 DATABASE_URL·NEO4J_*):
    .venv/Scripts/python.exe scripts/graph_names.py --user demo --dry-run   # 보기만
    .venv/Scripts/python.exe scripts/graph_names.py --user demo            # 맞춘다
--user 는 username 또는 user_id. 끝난 뒤에도 어긋난 상대가 남으면 종료 코드 1.
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
from app.graph.ingest import GraphIngestService  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="수집된 그래프의 상대 노드 이름을 대조해 맞춘다")
    p.add_argument("--user", required=True, help="username 또는 user_id")
    p.add_argument("--dry-run", action="store_true", help="대조 결과만 보여주고 아무것도 바꾸지 않는다")
    return p.parse_args(argv)


async def reconcile(ingest: GraphIngestService, user_id: str, *, apply: bool) -> int:
    """대조 결과를 찍고 `apply`면 맞춘다. 어긋난 채 남는 상대가 있으면 1, 없으면 0."""
    checks = await ingest.check_names(user_id)
    if not checks:
        print(
            f"사용자 {user_id}: 그래프에 메시지가 들어간 상대가 없습니다 (username이면 등록돼 있는지 확인)."
        )
        return 0
    left = 0
    for c in checks:
        if c.found:
            print(f"- {c.alias}: 맞음 (노드 '{c.name}')")
        elif c.match is not None:
            print(f"- {c.alias}: '{c.name}' 노드 없음 → '{c.match}'" + ("로 맞춘다" if apply else ""))
        else:
            left += 1
            shown = ", ".join(f"'{n}'" for n in c.candidates) or "없음"
            print(f"- {c.alias}: '{c.name}' 노드 없음, 후보 {shown} — 그대로 둔다")
    if not apply:
        print("--dry-run: 바꾼 것 없음.")
        return 1 if left else 0
    fixed = await ingest.reconcile_names(user_id)
    print(f"고친 상대 {len(fixed)}명, 어긋난 채 남은 상대 {left}명.")
    return 1 if left else 0


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
        return await reconcile(state.graph_ingest, user_id, apply=not args.dry_run)
    finally:
        await state.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
