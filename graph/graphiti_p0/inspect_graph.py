"""수집된 그래프를 조회해 한국어 추출 품질을 점검한다.

사용 예:
    python inspect_graph.py                         # 요약 + 점검표
    python inspect_graph.py --search "팀장님 일정"    # 하이브리드 검색 결과
    python inspect_graph.py --reset --yes           # 이 group_id의 데이터 삭제
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase

from p0.config import ConfigError, Settings, load_settings
from p0.episodes import load_dataset

ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = ROOT.parents[1] / "data" / "synthetic" / "sample_conversations.json"

# 모든 쿼리는 group_id를 매개변수로 받아 다른 실험 데이터와 섞이지 않게 한다.
Q_EPISODE_COUNT = "MATCH (e:Episodic {group_id: $group_id}) RETURN count(e) AS cnt"
Q_LABEL_COUNTS = """
MATCH (n:Entity {group_id: $group_id})
UNWIND labels(n) AS label
WITH label WHERE label <> 'Entity'
RETURN label, count(*) AS cnt ORDER BY cnt DESC
"""
Q_ENTITIES = """
MATCH (n:Entity {group_id: $group_id})
RETURN n.name AS name, [l IN labels(n) WHERE l <> 'Entity'] AS types, n.summary AS summary
ORDER BY name
"""
Q_FACTS = """
MATCH (a:Entity {group_id: $group_id})-[r:RELATES_TO]->(b:Entity)
RETURN a.name AS source, r.name AS edge, b.name AS target, r.fact AS fact,
       r.valid_at AS valid_at, r.invalid_at AS invalid_at, r.expired_at AS expired_at,
       properties(r) AS props
ORDER BY r.created_at
"""
Q_DELETE_GROUP = "MATCH (n {group_id: $group_id}) DETACH DELETE n"

CUSTOM_EDGE_FIELDS = (
    "relation_type",
    "speech_style",
    "address_term",
    "closeness",
    "participant_role",
    "feeling",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI 인자를 해석한다."""
    parser = argparse.ArgumentParser(description="Graphiti P0 그래프 점검")
    parser.add_argument(
        "--data",
        type=Path,
        default=DEFAULT_DATA,
        help="점검 기준(checks)이 든 데이터셋",
    )
    parser.add_argument("--search", type=str, default=None, help="하이브리드 검색 질의")
    parser.add_argument("--reset", action="store_true", help="이 group_id의 노드·엣지를 모두 삭제")
    parser.add_argument("--yes", action="store_true", help="--reset 확인")
    return parser.parse_args(argv)


def _short(value: Any) -> str:
    return "-" if value is None else str(value)[:19]


def print_summary(session: Any, group_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """에피소드·엔티티·사실 목록을 출력하고 점검에 쓸 레코드를 반환한다."""
    episodes = session.run(Q_EPISODE_COUNT, group_id=group_id).single()["cnt"]
    print(f"== group_id={group_id} | 에피소드 {episodes}개")

    print("\n== 엔티티 타입별 개수")
    for rec in session.run(Q_LABEL_COUNTS, group_id=group_id):
        print(f"  {rec['label']:<10} {rec['cnt']}")

    entities = [rec.data() for rec in session.run(Q_ENTITIES, group_id=group_id)]
    print(f"\n== 엔티티 ({len(entities)}개)")
    for ent in entities:
        print(f"  [{','.join(ent['types']) or '-'}] {ent['name']} — {(ent['summary'] or '')[:60]}")

    facts = [rec.data() for rec in session.run(Q_FACTS, group_id=group_id)]
    print(f"\n== 사실 엣지 ({len(facts)}개)  ✗ = 무효화됨")
    for fact in facts:
        mark = "✗" if fact["invalid_at"] or fact["expired_at"] else " "
        attrs = {k: fact["props"][k] for k in CUSTOM_EDGE_FIELDS if fact["props"].get(k) is not None}
        print(f" {mark} {fact['source']} -[{fact['edge']}]-> {fact['target']}")
        print(f"     {fact['fact']}")
        print(f"     valid={_short(fact['valid_at'])} invalid={_short(fact['invalid_at'])} {attrs or ''}")
    return entities, facts


def run_checks(checks: dict[str, Any], entities: list[dict[str, Any]], facts: list[dict[str, Any]]) -> bool:
    """데이터셋의 checks 기준으로 PASS/WARN을 출력한다. 모두 통과하면 True."""
    ok = True
    print("\n== 점검표")
    for person in checks.get("people", []):
        matches = [e["name"] for e in entities if person["match"] in (e["name"] or "")]
        passed = len(matches) == 1
        ok &= passed
        detail = matches or "없음"
        print(f"  {'PASS' if passed else 'WARN'}  '{person['canonical']}' 엔티티가 하나로 병합됨 → {detail}")

    if checks.get("expect_invalidated_edges"):
        invalidated = [f for f in facts if f["invalid_at"] or f["expired_at"]]
        passed = bool(invalidated)
        ok &= passed
        print(f"  {'PASS' if passed else 'WARN'}  관계 변화로 무효화된 엣지 존재 → {len(invalidated)}개")

    typed = [f for f in facts if any(f["props"].get(k) is not None for k in CUSTOM_EDGE_FIELDS)]
    passed = bool(typed)
    ok &= passed
    mark = "PASS" if passed else "WARN"
    print(f"  {mark}  커스텀 속성(말투·호칭 등)이 채워진 엣지 → {len(typed)}/{len(facts)}개")
    return ok


async def run_search(settings: Settings, query: str) -> None:
    """Graphiti 하이브리드 검색(BM25 + 임베딩, RRF) 결과를 출력한다."""
    # main()에서 .env를 먼저 읽은 뒤에 graphiti_core를 import해야 SEMAPHORE_LIMIT가 반영된다.
    from p0.graph import build_graphiti

    graphiti = build_graphiti(settings)
    try:
        results = await graphiti.search(query, group_ids=[settings.group_id], num_results=5)
    finally:
        await graphiti.close()
    print(f"\n== 검색: {query!r} (상위 {len(results)}개)")
    for edge in results:
        print(f"  - {edge.fact}  (valid={_short(edge.valid_at)}, invalid={_short(edge.invalid_at)})")


def main(argv: list[str] | None = None) -> int:
    """진입점."""
    args = parse_args(argv)
    try:
        settings = load_settings(str(ROOT / ".env"))
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2

    with GraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    ) as driver:
        with driver.session() as session:
            if args.reset:
                if not args.yes:
                    print(
                        "--reset은 데이터를 삭제합니다. 확인하려면 --yes를 함께 주세요.",
                        file=sys.stderr,
                    )
                    return 2
                session.run(Q_DELETE_GROUP, group_id=settings.group_id).consume()
                print(f"group_id={settings.group_id} 데이터를 삭제했습니다.")
                return 0
            entities, facts = print_summary(session, settings.group_id)

    all_passed = run_checks(load_dataset(args.data).get("checks", {}), entities, facts)
    if args.search:
        asyncio.run(run_search(settings, args.search))
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
