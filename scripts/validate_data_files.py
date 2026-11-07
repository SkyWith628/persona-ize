"""비전공자가 편집하는 접점 파일(CSV/JSON)의 형식을 검사한다. CI와 `make test` 전에 돈다.

검사 대상
- evals/eval_cases.csv : 필수 열, 빈 원문, relationship_type 값
- graph/alias_cases.csv, graph/graph_queries.csv : 필수 열
- data/synthetic/*.json : backend 스파이크의 데이터셋 규칙 (speaker, sent_at 시간대, id 중복)
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "graph" / "graphiti_p0"))

from p0.episodes import DatasetError, build_episodes, load_dataset  # noqa: E402

RELATIONSHIPS = {"superior", "colleague", "subordinate", "friend", "family", "partner", "acquaintance"}

REQUIRED = {
    ROOT / "evals" / "eval_cases.csv": {
        "case_id",
        "relationship_type",
        "contact_alias",
        "intent",
        "situation",
        "draft_text",
        "must_preserve",
    },
    ROOT / "graph" / "alias_cases.csv": {"case_id", "canonical_name", "mention"},
    ROOT / "graph" / "graph_queries.csv": {
        "query_id",
        "contact",
        "query_text",
        "relevant_message_ids",
        "needs_graph",
    },
}


def check_csv(path: Path, required: set[str]) -> list[str]:
    errors: list[str] = []
    if not path.exists():
        return [f"{path.relative_to(ROOT)}: 파일 없음"]
    with path.open(encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        cols = set(reader.fieldnames or [])
        missing = required - cols
        if missing:
            errors.append(f"{path.relative_to(ROOT)}: 열 누락 {sorted(missing)}")
            return errors
        ids: set[str] = set()
        id_col = "case_id" if "case_id" in cols else "query_id"
        for n, row in enumerate(reader, start=2):
            rid = (row.get(id_col) or "").strip()
            if not rid:
                errors.append(f"{path.name}:{n}: {id_col} 비어 있음")
            elif rid in ids:
                errors.append(f"{path.name}:{n}: {id_col} 중복 {rid}")
            ids.add(rid)
            if "draft_text" in cols and not (row.get("draft_text") or "").strip():
                errors.append(f"{path.name}:{n}: draft_text 비어 있음")
            rel = row.get("relationship_type")
            if rel is not None and rel not in RELATIONSHIPS:
                errors.append(
                    f"{path.name}:{n}: relationship_type '{rel}' 은 {sorted(RELATIONSHIPS)} 중 하나여야 함"
                )
            if "needs_graph" in cols and row["needs_graph"] not in {"Y", "N"}:
                errors.append(f"{path.name}:{n}: needs_graph 는 Y/N")
    return errors


def check_json(path: Path) -> list[str]:
    try:
        episodes = build_episodes(load_dataset(path))
    except (DatasetError, ValueError) as exc:
        return [f"{path.relative_to(ROOT)}: {exc}"]
    return [] if episodes else [f"{path.relative_to(ROOT)}: 메시지 없음"]


def main() -> int:
    errors: list[str] = []
    for path, required in REQUIRED.items():
        errors += check_csv(path, required)
    for path in sorted((ROOT / "data" / "synthetic").glob("*.json")):
        errors += check_json(path)
    if errors:
        print("\n".join(errors))
        print(f"\n{len(errors)}개 문제")
        return 1
    print("접점 파일 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
