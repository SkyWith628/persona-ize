"""블라인드 평가 시트: run_eval 결과에서 조건(A/B/C/D)을 숨기고 섞은 채점용 CSV + 정답 키.

- 같은 사례에서 결과 문장이 똑같은 조건들은 **한 줄로 합친다**(채점 수고 절약, 정답 키에 조건 여러 개).
- 항목 id는 무작위 6자리(`b3f9a1`)라 순서나 번호로 조건을 유추할 수 없다. 정렬은 사례 순이 아니라 셔플.
- 원문 유지(fallback_original)도 한 줄로 들어간다 — 채점자는 원문과 같다는 것만 본다.
- 루브릭 열은 비워 둔다. 기본 열은 평가 담당의 `style_rubric.md`가 확정되면 바꾼다(`--columns`).
- `merge`: 채점된 시트 + 키 → 조건이 붙은 긴 형식 CSV와 조건별 평균 표.
"""

from __future__ import annotations

import csv
import random
import statistics
from dataclasses import dataclass
from pathlib import Path

DEFAULT_RUBRIC_COLUMNS = ["격식", "친근함", "호칭", "길이", "자연스러움", "사실_의심(Y/N)", "메모"]
SHEET_FIXED_COLUMNS = ["item_id", "case_id", "relation", "situation", "original_text", "candidate_text"]


@dataclass
class SheetBuild:
    sheet_rows: list[dict[str, str]]
    key_rows: list[dict[str, str]]


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fp:
        return [{k: (v or "") for k, v in row.items() if k is not None} for row in csv.DictReader(fp)]


def build_sheet(
    results: list[dict[str, str]],
    cases: list[dict[str, str]],
    seed: int = 7,
    rubric_columns: list[str] | None = None,
    include_original: bool = True,
) -> SheetBuild:
    """results: run_eval 요약 CSV 행들(case_id·condition·repeat·result_text·status).

    cases: eval_cases.csv 행들.
    """
    rubric_columns = rubric_columns or DEFAULT_RUBRIC_COLUMNS
    case_by_id = {c["case_id"]: c for c in cases}
    rng = random.Random(seed)

    # 사례별로 같은 문장은 합친다
    grouped: dict[tuple[str, str], list[dict[str, str]]] = {}
    for r in results:
        key = (r["case_id"], r["result_text"].strip())
        grouped.setdefault(key, []).append(r)
    if include_original:
        # 원문 자체도 한 줄 (기준선: 채점자가 "바꾸지 않은 것"에 몇 점을 주는지)
        for cid, case in case_by_id.items():
            if any(r["case_id"] == cid for r in results):
                grouped.setdefault((cid, case["original_text"].strip()), []).append(
                    {"case_id": cid, "condition": "ORIGINAL", "repeat": "0", "status": "original"}
                )

    used: set[str] = set()
    sheet_rows: list[dict[str, str]] = []
    key_rows: list[dict[str, str]] = []
    for (cid, text), rows in grouped.items():
        case = case_by_id.get(cid)
        if case is None:
            raise ValueError(f"eval_cases.csv에 없는 case_id: {cid}")
        while True:
            item_id = f"{rng.randrange(16**6):06x}"
            if item_id not in used:
                used.add(item_id)
                break
        sheet_rows.append(
            {
                "item_id": item_id,
                "case_id": cid,
                "relation": case.get("relation", ""),
                "situation": case.get("situation", ""),
                "original_text": case.get("original_text", ""),
                "candidate_text": text,
                **{col: "" for col in rubric_columns},
            }
        )
        key_rows.append(
            {
                "item_id": item_id,
                "case_id": cid,
                "conditions": ";".join(sorted({r["condition"] for r in rows})),
                "repeats": ";".join(sorted({str(r.get("repeat", "")) for r in rows})),
                "statuses": ";".join(sorted({r.get("status", "") for r in rows})),
                "candidate_text": text,
            }
        )
    rng.shuffle(sheet_rows)
    key_rows.sort(key=lambda k: (k["case_id"], k["conditions"]))
    return SheetBuild(sheet_rows=sheet_rows, key_rows=key_rows)


def write_csv(path: Path, rows: list[dict[str, str]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fp:  # 엑셀이 한글을 바로 읽게 BOM
        writer = csv.DictWriter(fp, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def merge_scores(
    scored_sheet: list[dict[str, str]],
    key_rows: list[dict[str, str]],
    rubric_columns: list[str] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """채점된 시트에 조건을 붙인다. (긴 형식 행들, 조건별 평균 표). 숫자 열만 평균낸다."""
    rubric_columns = rubric_columns or DEFAULT_RUBRIC_COLUMNS
    key_by_item = {k["item_id"]: k for k in key_rows}
    long_rows: list[dict[str, str]] = []
    for row in scored_sheet:
        key = key_by_item.get(row["item_id"])
        if key is None:
            raise ValueError(f"키에 없는 item_id: {row['item_id']}")
        for cond in key["conditions"].split(";"):
            long_rows.append({**row, "condition": cond, "statuses": key["statuses"]})

    numeric_cols = []
    for col in rubric_columns:
        values = [r[col] for r in long_rows if r.get(col, "").strip()]
        if values and all(_is_number(v) for v in values):
            numeric_cols.append(col)
    summary: list[dict[str, str]] = []
    for cond in sorted({r["condition"] for r in long_rows}):
        rows = [r for r in long_rows if r["condition"] == cond]
        entry = {"condition": cond, "items": str(len(rows))}
        for col in numeric_cols:
            vals = [float(r[col]) for r in rows if r.get(col, "").strip()]
            entry[col] = f"{statistics.mean(vals):.2f}" if vals else ""
        flagged = [r for r in rows if r.get("사실_의심(Y/N)", "").strip().upper() in ("Y", "YES", "예")]
        entry["사실_의심_비율"] = f"{len(flagged) / len(rows):.2f}" if rows else ""
        summary.append(entry)
    return long_rows, summary


def _is_number(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False
