"""비전공자가 편집하는 접점 파일(CSV/JSON)의 형식을 검사한다. CI와 `make test` 전에 돈다.

검사 대상 (열 정의는 docs/guides/dataset-guide.md)
- evals/characters.csv : 등장인물 표. relation 유일, relationship_type 값
- evals/eval_cases.csv : 필수 열, relation이 characters에 있는지, needs_graph/split 값,
                         must_not_change ⊆ preserve_items, relevant_msg_ids가 실제 메시지인지, as_of 날짜
- graph/alias_cases.csv, graph/graph_queries.csv : 필수 열, 메시지 id 존재
- data/synthetic/*.json : 스파이크의 데이터셋 규칙 (speaker, sent_at 시간대, id 중복)
"""

from __future__ import annotations

import csv
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "graph" / "graphiti_p0"))

from p0.episodes import DatasetError, build_episodes, load_dataset  # noqa: E402

RELATIONSHIPS = {
    "superior",
    "colleague",
    "subordinate",
    "friend",
    "family",
    "partner",
    "acquaintance",
}
PRESERVE_KINDS = {
    "날짜",
    "시간",
    "수량",
    "금액",
    "사람",
    "행동",
    "가능여부",
    "약속",
    "조건",
}
MSG_REF = re.compile(r"^[A-Za-z0-9_-]+:[A-Za-z0-9_-]+$")

CHARACTERS = ROOT / "evals" / "characters.csv"
EVAL_CASES = ROOT / "evals" / "eval_cases.csv"
ALIAS_CASES = ROOT / "graph" / "alias_cases.csv"
GRAPH_QUERIES = ROOT / "graph" / "graph_queries.csv"
SYNTHETIC_DIR = ROOT / "data" / "synthetic"

REQUIRED: dict[Path, set[str]] = {
    CHARACTERS: {"name", "relation", "relationship_type"},
    EVAL_CASES: {
        "case_id",
        "relation",
        "situation",
        "original_text",
        "preserve_items",
        "must_not_change",
        "expected_notes",
        "needs_graph",
        "relevant_msg_ids",
        "as_of",
        "split",
    },
    ALIAS_CASES: {"case_id", "canonical_name", "mention"},
    GRAPH_QUERIES: {
        "query_id",
        "contact",
        "query_text",
        "relevant_message_ids",
        "needs_graph",
    },
}


def rel(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


def read_rows(path: Path, required: set[str]) -> tuple[list[dict[str, str]], list[str]]:
    """CSV를 읽고 필수 열·id 중복을 검사한다. 열이 빠지면 행 검사는 건너뛴다."""
    if not path.exists():
        return [], [f"{rel(path)}: 파일 없음"]
    with path.open(encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        cols = set(reader.fieldnames or [])
        missing = required - cols
        if missing:
            return [], [f"{rel(path)}: 열 누락 {sorted(missing)} (docs/guides/dataset-guide.md 참고)"]
        rows = [{k: (v or "").strip() for k, v in row.items()} for row in reader]
    errors: list[str] = []
    id_col = next((c for c in ("case_id", "query_id", "relation") if c in cols), None)
    if id_col:
        seen: set[str] = set()
        for n, row in enumerate(rows, start=2):
            rid = row[id_col]
            if not rid:
                errors.append(f"{path.name}:{n}: {id_col} 비어 있음")
            elif rid in seen:
                errors.append(f"{path.name}:{n}: {id_col} 중복 {rid}")
            seen.add(rid)
    return rows, errors


def split_items(value: str) -> list[str]:
    return [v.strip() for v in value.split(";") if v.strip()]


def message_refs() -> set[str]:
    """data/synthetic/*.json 의 모든 `대화방id:메시지id`."""
    refs: set[str] = set()
    for path in sorted(SYNTHETIC_DIR.glob("*.json")):
        try:
            data = load_dataset(path)
        except (DatasetError, ValueError):
            continue  # check_json 이 보고한다
        for conv in data.get("conversations", []):
            for msg in conv.get("messages", []):
                refs.add(f"{conv.get('conversation_id')}:{msg.get('id')}")
    return refs


def check_refs(name: str, n: int, value: str, refs: set[str]) -> list[str]:
    errors: list[str] = []
    for ref in split_items(value):
        if not MSG_REF.match(ref):
            errors.append(f"{name}:{n}: 메시지 참조 '{ref}' 는 대화방id:메시지id 형식 (예 c1:m03)")
        elif ref not in refs:
            errors.append(f"{name}:{n}: 메시지 '{ref}' 가 data/synthetic 에 없음")
    return errors


def check_characters(rows: list[dict[str, str]]) -> tuple[set[str], list[str]]:
    errors: list[str] = []
    for n, row in enumerate(rows, start=2):
        if not row["name"]:
            errors.append(f"characters.csv:{n}: name 비어 있음")
        if row["relationship_type"] not in RELATIONSHIPS:
            errors.append(
                f"characters.csv:{n}: relationship_type '{row['relationship_type']}' 은 "
                f"{sorted(RELATIONSHIPS)} 중 하나"
            )
    return {r["relation"] for r in rows if r["relation"]}, errors


def check_eval_cases(rows: list[dict[str, str]], relations: set[str], refs: set[str]) -> list[str]:
    errors: list[str] = []
    name = EVAL_CASES.name
    for n, row in enumerate(rows, start=2):
        if not row["original_text"]:
            errors.append(f"{name}:{n}: original_text 비어 있음")
        if relations and row["relation"] not in relations:
            errors.append(f"{name}:{n}: relation '{row['relation']}' 이 characters.csv 에 없음")
        if row["needs_graph"] not in {"Y", "N"}:
            errors.append(f"{name}:{n}: needs_graph 는 Y/N")
        if row["split"] not in {"dev", "test"}:
            errors.append(f"{name}:{n}: split 은 dev/test")
        preserve = split_items(row["preserve_items"])
        if not preserve:
            errors.append(f"{name}:{n}: preserve_items 비어 있음")
        for item in preserve:
            kind = item.split("=", 1)[0]
            if "=" not in item or kind not in PRESERVE_KINDS:
                errors.append(
                    f"{name}:{n}: preserve_items '{item}' 는 종류=내용, 종류는 {sorted(PRESERVE_KINDS)}"
                )
        for item in split_items(row["must_not_change"]):
            if item not in preserve:
                errors.append(f"{name}:{n}: must_not_change '{item}' 가 preserve_items 에 없음")
        if row["needs_graph"] == "Y" and not row["relevant_msg_ids"]:
            errors.append(f"{name}:{n}: needs_graph=Y 이면 relevant_msg_ids 를 채운다")
        errors += check_refs(name, n, row["relevant_msg_ids"], refs)
        if row["as_of"]:
            try:
                date.fromisoformat(row["as_of"])
            except ValueError:
                errors.append(f"{name}:{n}: as_of '{row['as_of']}' 는 YYYY-MM-DD")
    return errors


def check_graph_queries(rows: list[dict[str, str]], refs: set[str]) -> list[str]:
    errors: list[str] = []
    name = GRAPH_QUERIES.name
    for n, row in enumerate(rows, start=2):
        if row["needs_graph"] not in {"Y", "N"}:
            errors.append(f"{name}:{n}: needs_graph 는 Y/N")
        errors += check_refs(name, n, row["relevant_message_ids"], refs)
    return errors


def check_json(path: Path) -> list[str]:
    try:
        episodes = build_episodes(load_dataset(path))
    except (DatasetError, ValueError) as exc:
        return [f"{rel(path)}: {exc}"]
    return [] if episodes else [f"{rel(path)}: 메시지 없음"]


def main() -> int:
    errors: list[str] = []
    for path in sorted(SYNTHETIC_DIR.glob("*.json")):
        errors += check_json(path)
    refs = message_refs()

    characters, errs = read_rows(CHARACTERS, REQUIRED[CHARACTERS])
    errors += errs
    relations, errs = check_characters(characters)
    errors += errs

    cases, errs = read_rows(EVAL_CASES, REQUIRED[EVAL_CASES])
    errors += errs
    if cases:
        errors += check_eval_cases(cases, relations, refs)

    _, errs = read_rows(ALIAS_CASES, REQUIRED[ALIAS_CASES])
    errors += errs

    queries, errs = read_rows(GRAPH_QUERIES, REQUIRED[GRAPH_QUERIES])
    errors += errs
    if queries:
        errors += check_graph_queries(queries, refs)

    if errors:
        print("\n".join(errors))
        print(f"\n{len(errors)}개 문제")
        return 1
    print(
        f"접점 파일 OK (등장인물 {len(characters)}, 사례 {len(cases)}, "
        f"질의 {len(queries)}, 메시지 {len(refs)})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
