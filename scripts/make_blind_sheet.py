"""블라인드 평가 시트 만들기 / 채점 결과 합치기.

만들기:
    .venv/Scripts/python.exe scripts/make_blind_sheet.py build evals/results/<결과>.csv
      → evals/blind/<이름>-sheet.csv (채점자에게 줄 것), evals/blind/<이름>-key.csv (팀장만, git 제외)
합치기 (채점 뒤):
    .venv/Scripts/python.exe scripts/make_blind_sheet.py merge <채점된 시트>.csv <이름>-key.csv
      → <이름>-merged.csv (조건 붙은 긴 형식), <이름>-summary.csv (조건별 평균)

시트 열: item_id, case_id, relation, situation, original_text, candidate_text, 그리고 비어 있는 루브릭 열.
루브릭 열 이름은 --columns "격식,친근함,…" 로 바꾼다 (style_rubric.md 확정 뒤).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.tools.blind_sheet import (  # noqa: E402
    DEFAULT_RUBRIC_COLUMNS,
    SHEET_FIXED_COLUMNS,
    _read_csv,
    build_sheet,
    merge_scores,
    write_csv,
)

BLIND_DIR = ROOT / "evals" / "blind"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="블라인드 평가 시트")
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="결과 CSV → 시트 + 키")
    b.add_argument("results", type=Path, help="run_eval 요약 CSV")
    b.add_argument("--cases", type=Path, default=ROOT / "evals" / "eval_cases.csv")
    b.add_argument("--name", default=None, help="출력 이름 (기본: 결과 파일 이름)")
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--columns", default=",".join(DEFAULT_RUBRIC_COLUMNS), help="루브릭 열, 쉼표 구분")
    b.add_argument("--no-original", action="store_true", help="원문 기준선 줄을 넣지 않는다")
    m = sub.add_parser("merge", help="채점된 시트 + 키 → 조건별 표")
    m.add_argument("scored", type=Path)
    m.add_argument("key", type=Path)
    m.add_argument("--columns", default=",".join(DEFAULT_RUBRIC_COLUMNS))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    columns = [c.strip() for c in args.columns.split(",") if c.strip()]
    if args.command == "build":
        results = _read_csv(args.results)
        cases = _read_csv(args.cases)
        build = build_sheet(
            results, cases, seed=args.seed, rubric_columns=columns, include_original=not args.no_original
        )
        name = args.name or args.results.stem
        sheet = BLIND_DIR / f"{name}-sheet.csv"
        key = BLIND_DIR / f"{name}-key.csv"
        write_csv(sheet, build.sheet_rows, SHEET_FIXED_COLUMNS + columns)
        write_csv(
            key, build.key_rows, ["item_id", "case_id", "conditions", "repeats", "statuses", "candidate_text"]
        )
        conds = sorted({c for k in build.key_rows for c in k["conditions"].split(";")})
        print(f"시트 {len(build.sheet_rows)}줄 → {sheet}")
        print(f"키 → {key}  (채점자에게 주지 않는다. git 제외)")
        print(f"조건: {conds}, 사례 {len({k['case_id'] for k in build.key_rows})}개")
        return 0
    long_rows, summary = merge_scores(_read_csv(args.scored), _read_csv(args.key), columns)
    stem = args.scored.stem.replace(".scored", "").replace("-sheet", "")
    merged = BLIND_DIR / f"{stem}-merged.csv"
    summary_path = BLIND_DIR / f"{stem}-summary.csv"
    write_csv(merged, long_rows, list(long_rows[0].keys()) if long_rows else ["item_id"])
    write_csv(summary_path, summary, list(summary[0].keys()) if summary else ["condition"])
    print(f"긴 형식 {len(long_rows)}줄 → {merged}")
    print(f"조건별 평균 → {summary_path}")
    for row in summary:
        print("  ", row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
