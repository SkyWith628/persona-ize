"""샘플 대화를 Graphiti에 에피소드로 넣고 소요 시간 보고서를 남긴다.

사용 예:
    python ingest.py --dry-run              # 네트워크 없이 변환 결과만 확인
    python ingest.py --limit 5              # 앞 5개만 수집 (비용 확인용)
    python ingest.py                        # 전체 수집
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from p0.episodes import EpisodeInput, build_episodes, load_dataset
from p0.schema import EDGE_TYPE_MAP, EDGE_TYPES, ENTITY_TYPES, EXTRACTION_INSTRUCTIONS

ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = ROOT.parents[1] / "data" / "synthetic" / "sample_conversations.json"
RUNS_DIR = ROOT / "runs"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI 인자를 해석한다."""
    parser = argparse.ArgumentParser(description="Graphiti P0 수집")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA, help="대화 데이터셋 JSON 경로")
    parser.add_argument("--limit", type=int, default=None, help="앞에서부터 N개 에피소드만 수집")
    parser.add_argument("--dry-run", action="store_true", help="수집하지 않고 에피소드만 출력")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit은 1 이상이어야 합니다.")
    return args


def print_episodes(episodes: list[EpisodeInput]) -> None:
    """dry-run 출력."""
    for ep in episodes:
        print(f"{ep.reference_time.isoformat()}  {ep.name:<12} {ep.body}")
    print(f"\n총 {len(episodes)}개 에피소드")


async def ingest(episodes: list[EpisodeInput]) -> int:
    """에피소드를 시간 순으로 하나씩 수집한다. 실패하면 그 지점에서 멈춘다.

    순서대로 넣어야 이전 사실의 무효화가 올바르게 일어나므로 병렬로 넣지 않는다.
    """
    # Graphiti/Neo4j 의존성은 실제 수집할 때만 불러온다 (dry-run은 설정 없이 동작).
    # graphiti_core는 import 시점에 SEMAPHORE_LIMIT를 읽으므로 .env를 먼저 로드한다.
    from p0.config import load_settings

    settings = load_settings(str(ROOT / ".env"))

    from p0.graph import build_graphiti

    graphiti = build_graphiti(settings)
    report: dict[str, Any] = {
        "started_at": datetime.now(UTC).isoformat(),
        "group_id": settings.group_id,
        "llm_model": settings.llm_model,
        "embedding_provider": settings.embedding_provider,
        "embedding_model": settings.embedding_model,
        "episodes": [],
    }
    exit_code = 0
    try:
        await graphiti.build_indices_and_constraints()
        for idx, ep in enumerate(episodes, start=1):
            t0 = time.perf_counter()
            try:
                result = await graphiti.add_episode(
                    name=ep.name,
                    episode_body=ep.body,
                    source_description=ep.source_description,
                    # graphiti 프롬프트는 REFERENCE_TIME을 UTC로 전제하고 valid_at도 UTC(Z)로 받는다.
                    # +09:00 그대로 넘기면 모델이 벽시계 시각을 UTC로 옮겨 적는 경우가 섞인다 (11-08 1차).
                    reference_time=ep.reference_time.astimezone(UTC),
                    group_id=settings.group_id,
                    entity_types=ENTITY_TYPES,
                    edge_types=EDGE_TYPES,
                    edge_type_map=EDGE_TYPE_MAP,
                    # strict_edge_types는 add_episode_bulk에만 있다(0.31.2).
                    # 자유 엣지명 억제는 지시문으로 한다.
                    custom_extraction_instructions=EXTRACTION_INSTRUCTIONS,
                )
            except Exception as exc:  # 어떤 단계에서 실패했는지 보고서에 남기고 중단한다.
                report["failed"] = {
                    "episode": ep.name,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                print(f"[{idx}/{len(episodes)}] 실패 {ep.name}: {exc}", file=sys.stderr)
                exit_code = 1
                break
            elapsed = time.perf_counter() - t0
            report["episodes"].append(
                {
                    "name": ep.name,
                    "seconds": round(elapsed, 2),
                    "nodes": len(result.nodes),
                    "edges": len(result.edges),
                }
            )
            print(
                f"[{idx}/{len(episodes)}] {ep.name} {elapsed:5.1f}s "
                f"노드 {len(result.nodes)} 엣지 {len(result.edges)}"
            )
    finally:
        await graphiti.close()
        write_report(report)
    return exit_code


def write_report(report: dict[str, Any]) -> Path:
    """runs/ 아래에 수집 보고서를 저장하고 요약을 출력한다."""
    done = report["episodes"]
    report["finished_at"] = datetime.now(UTC).isoformat()
    report["summary"] = {
        "ingested": len(done),
        "total_seconds": round(sum(e["seconds"] for e in done), 1),
        "avg_seconds": round(sum(e["seconds"] for e in done) / len(done), 2) if done else None,
    }
    RUNS_DIR.mkdir(exist_ok=True)
    path = RUNS_DIR / f"ingest-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n수집 {report['summary']['ingested']}건, 합계 {report['summary']['total_seconds']}초 → {path}")
    return path


def main(argv: list[str] | None = None) -> int:
    """진입점."""
    args = parse_args(argv)
    episodes = build_episodes(load_dataset(args.data))
    if args.limit is not None:
        episodes = episodes[: args.limit]
    if args.dry_run:
        print_episodes(episodes)
        return 0
    from p0.config import ConfigError

    try:
        return asyncio.run(ingest(episodes))
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
