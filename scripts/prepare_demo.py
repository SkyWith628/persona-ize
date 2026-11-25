"""시연용 계정·데이터를 준비한다 (여러 번 돌려도 같은 결과).

1. `demo` 사용자를 만든다 (있으면 그대로). 비밀번호는 `--password` 또는 DEMO_PASSWORD, 기본 persona-demo-2026.
2. 그 사용자 id로 합성 대화(data/synthetic/sample_conversations.json)를 적재한다 — 상대 4명, 대화방 4개, 36건.
3. 상대마다 페르소나 v1을 만든다 (characters.csv의 관계 유형별 기본값 + 첫 호칭). 이미 있으면 건너뛴다.
4. `--graph`면 그래프(Neo4j)에도 넣는다 (haiku 호출, 메시지당 10~15초 → 36건 약 7분).

사용 예 (저장소 루트):
    .venv/Scripts/python.exe scripts/prepare_demo.py --graph
화면에서 demo / <비밀번호> 로 로그인하면 바로 시연할 수 있다. 대본: docs/guides/demo-script.md
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.api.deps import build_state  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.core.errors import VersionConflictError  # noqa: E402
from app.services.synthetic_loader import DEFAULT_PERSONAS, load_synthetic, persona_for  # noqa: E402

DEFAULT_PASSWORD = "persona-demo-2026"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="시연 계정·데이터 준비")
    p.add_argument("--username", default="demo")
    p.add_argument("--password", default=os.environ.get("DEMO_PASSWORD", DEFAULT_PASSWORD))
    p.add_argument("--dataset", type=Path, default=ROOT / "data" / "synthetic" / "sample_conversations.json")
    p.add_argument("--characters", type=Path, default=ROOT / "evals" / "characters.csv")
    p.add_argument("--graph", action="store_true", help="그래프(Neo4j)에도 수집 (haiku 호출)")
    return p.parse_args(argv)


async def run(args: argparse.Namespace) -> int:
    settings = Settings()
    if not settings.database_url:
        print("DATABASE_URL이 비어 있습니다. 시연 데이터는 PostgreSQL에 있어야 서버 재시작 뒤에도 남습니다.")
        return 2
    state = build_state(settings)
    graph_failed = False
    try:
        found = await state.auth.find_user_by_username(args.username)
        if found is None:
            result = await state.auth_service.register(args.username, args.password)
            user = result.user
            print(f"사용자 생성: {user.username} ({user.id})")
        else:
            user = found[0]
            print(f"사용자 있음: {user.username} ({user.id})")
        user_id = str(user.id)

        if args.graph and not state.graph_enabled:
            print("--graph에는 .env의 NEO4J_URI가 필요합니다.")
            return 2
        # 대화 적재 (그래프는 뒤에서 따로 — 모델 호출이 실패해도 페르소나까지는 만들어 둔다)
        report = await load_synthetic(state, args.dataset, user_id, args.characters, graph=False)
        print(
            f"상대 {len(report.contacts)}명, 대화방 {len(report.conversations)}개, "
            f"메시지 새로 {report.imported}건(중복 {report.duplicates}), 묶음 {report.episodes}개"
            + (f", 그래프 에피소드 새로 {report.graph_episodes}개" if args.graph else "")
        )
        for line in report.failed:
            print("실패:", line)

        made = 0
        for contact in await state.contacts.list(user_id):
            settings_ = persona_for(contact.relationship_type, contact.alias, args.characters)
            current = await state.contacts.latest_persona(user_id, contact.id)
            if current is not None:
                # 호칭 규칙이 바뀌어(11-09) 기존 페르소나에 호칭이 비어 있으면 새 버전으로 보강한다.
                if settings_.address_term and not current.settings.address_term:
                    await state.contacts.save_persona(user_id, contact.id, settings_, current.version)
                    made += 1
                continue
            try:
                await state.contacts.save_persona(user_id, contact.id, settings_, 0)
                made += 1
            except VersionConflictError:
                pass
        print(f"페르소나 생성·보강 {made}명 (기본값 표: {sorted(DEFAULT_PERSONAS)})")

        # 그래프 (haiku 호출). 실패하면 넣은 데까지 남고, 다시 돌리면 남은 메시지부터 이어서 넣는다.
        if args.graph:
            try:
                n = await state.graph_ingest.ingest_pending(user_id)
                print(f"그래프 에피소드 새로 {n}개")
            except Exception as exc:  # noqa: BLE001 - 보고하고 종료 코드로 알린다
                graph_failed = True
                print(f"그래프 수집 중단: {str(exc)[:200]}")
                print("→ API 크레딧·키·Neo4j를 확인한 뒤 같은 명령을 다시 돌리면 이어서 넣는다.")
        print(f"\n로그인: {args.username} / (지정한 비밀번호)  →  http://localhost:8000/")
    finally:
        await state.close()
    return 1 if (report.failed or graph_failed) else 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
