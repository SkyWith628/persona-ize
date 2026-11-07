"""`graph/graph_queries.csv`의 질의를 그래프에서 검색해 출처 메시지 기준 Recall@k를 잰다.

사용 예:
    python eval_queries.py            # 질의 4개, k=5
    python eval_queries.py --k 10

검색 결과(사실 엣지)가 어느 에피소드(메시지)에서 나왔는지 `episodes`로 추적해 정답 메시지 id와 비교한다.
결과는 `runs/queries-<시각>.json`에 남긴다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from neo4j import AsyncGraphDatabase

from p0.config import ConfigError, Settings, load_settings
from p0.queries import GraphQuery, load_queries, recall_at_k

ROOT = Path(__file__).resolve().parent
DEFAULT_QUERIES = ROOT.parent / "graph_queries.csv"

Q_EPISODE_NAMES = "MATCH (e:Episodic) WHERE e.uuid IN $uuids RETURN e.uuid AS uuid, e.name AS name"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI 인자를 해석한다."""
    parser = argparse.ArgumentParser(description="graph_queries.csv Recall@k")
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES, help="질의 CSV 경로")
    parser.add_argument("--k", type=int, default=5, help="상위 k개 메시지 (기본 5)")
    parser.add_argument("--num-results", type=int, default=10, help="검색에서 받아올 엣지 수 (기본 10)")
    return parser.parse_args(argv)


async def episode_names(settings: Settings, uuids: set[str]) -> dict[str, str]:
    """에피소드 uuid → 이름(`c1:m03`)."""
    if not uuids:
        return {}
    async with AsyncGraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    ) as driver:
        records, _, _ = await driver.execute_query(Q_EPISODE_NAMES, uuids=sorted(uuids))
    return {record["uuid"]: record["name"] for record in records}


async def run(
    settings: Settings, queries: list[GraphQuery], k: int, num_results: int
) -> list[dict[str, Any]]:
    """질의마다 검색 → 엣지의 출처 에피소드를 메시지 id로 바꿔 Recall@k를 계산한다."""
    from p0.graph import build_graphiti

    graphiti = build_graphiti(settings)
    rows: list[dict[str, Any]] = []
    try:
        for query in queries:
            edges = await graphiti.search(
                query.query_text, group_ids=[settings.group_id], num_results=num_results
            )
            uuid_to_name = await episode_names(settings, {uuid for edge in edges for uuid in edge.episodes})
            retrieved: list[str] = []
            for edge in edges:
                for uuid in edge.episodes:
                    name = uuid_to_name.get(uuid)
                    if name and name not in retrieved:
                        retrieved.append(name)
            rows.append(
                {
                    "query_id": query.query_id,
                    "needs_graph": query.needs_graph,
                    "query_text": query.query_text,
                    "relevant": list(query.relevant_ids),
                    "retrieved": retrieved,
                    "recall_at_k": round(recall_at_k(retrieved, query.relevant_ids, k), 3),
                    "facts": [edge.fact for edge in edges],
                }
            )
    finally:
        await graphiti.close()
    return rows


def print_table(rows: list[dict[str, Any]], k: int) -> None:
    """질의별 Recall@k와 검색된 메시지 id를 출력한다."""
    print(f"\n== graph_queries Recall@{k} (group_id 기준)")
    for row in rows:
        flag = "D" if row["needs_graph"] else "-"
        print(f"  {row['query_id']} [{flag}] recall={row['recall_at_k']:.2f}  {row['query_text']}")
        print(f"       정답 {';'.join(row['relevant'])}")
        print(f"       검색 {';'.join(row['retrieved'][:k]) or '(없음)'}")
    if rows:
        mean = sum(r["recall_at_k"] for r in rows) / len(rows)
        print(f"  평균 recall@{k} = {mean:.2f} ({len(rows)}개 질의)")


def main(argv: list[str] | None = None) -> int:
    """진입점."""
    args = parse_args(argv)
    try:
        settings = load_settings(str(ROOT / ".env"))
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2
    queries = load_queries(args.queries)
    if not queries:
        print("정답이 채워진 질의가 없습니다.", file=sys.stderr)
        return 2

    rows = asyncio.run(run(settings, queries, args.k, args.num_results))
    print_table(rows, args.k)

    runs_dir = ROOT / "runs"
    runs_dir.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
    path = runs_dir / f"queries-{stamp}.json"
    path.write_text(
        json.dumps(
            {"group_id": settings.group_id, "k": args.k, "llm_model": settings.llm_model, "rows": rows},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"→ {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
