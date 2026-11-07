"""`graph/graph_queries.csv`를 읽고 검색 결과의 Recall@k를 계산한다 (네트워크 없음)."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

ID_SEPARATOR = ";"


@dataclass(frozen=True)
class GraphQuery:
    """질의 하나: 상대, 질의문, 정답 메시지 id(`c1:m03` 형식), 그래프 필요 여부."""

    query_id: str
    contact: str
    query_text: str
    relevant_ids: tuple[str, ...]
    needs_graph: bool
    note: str


def parse_ids(raw: str) -> tuple[str, ...]:
    """`c1:m03;c1:m04` → ('c1:m03', 'c1:m04'). 빈 항목과 공백은 버린다."""
    return tuple(part.strip() for part in raw.split(ID_SEPARATOR) if part.strip())


def load_queries(path: Path) -> list[GraphQuery]:
    """CSV를 읽어 질의 목록을 만든다. 정답이 비어 있는 행은 건너뛴다."""
    queries: list[GraphQuery] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            relevant = parse_ids(row.get("relevant_message_ids", ""))
            if not relevant:
                continue
            queries.append(
                GraphQuery(
                    query_id=row["query_id"].strip(),
                    contact=row.get("contact", "").strip(),
                    query_text=row["query_text"].strip(),
                    relevant_ids=relevant,
                    needs_graph=row.get("needs_graph", "").strip().upper() == "Y",
                    note=row.get("note", "").strip(),
                )
            )
    return queries


def recall_at_k(retrieved_ids: list[str], relevant_ids: tuple[str, ...] | list[str], k: int) -> float:
    """상위 k개(중복 제거, 순서 유지) 안에 든 정답 비율. 정답이 없으면 0."""
    if not relevant_ids:
        return 0.0
    seen: list[str] = []
    for item in retrieved_ids:
        if item not in seen:
            seen.append(item)
        if len(seen) >= k:
            break
    hits = set(seen) & set(relevant_ids)
    return len(hits) / len(set(relevant_ids))
