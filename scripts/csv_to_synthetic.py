"""시나리오 담당의 시트 CSV → data/synthetic JSON (검사 포함).

사용 예 (저장소 루트):
    .venv/Scripts/python.exe scripts/csv_to_synthetic.py 받은파일.csv --out data/synthetic/dialogs_week2.json
    .venv/Scripts/python.exe scripts/csv_to_synthetic.py 받은파일.csv --out data/synthetic/sample.json
    (--out이 이미 있으면 그 파일에 합친다)

CSV 열: 대화방 | 번호 | 날짜시간 | 말한 사람 | 내용 (+ 상대, 제목).
자세한 규칙은 backend/app/tools/synthetic_csv.py 머리말.
끝나면 scripts/validate_data_files.py를 돌려 다른 접점 파일과의 정합성까지 확인한다.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.tools.synthetic_csv import (  # noqa: E402
    ConversionError,
    convert,
    load_characters,
    read_rows,
    write_dataset,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="시트 CSV → 합성 대화 JSON")
    p.add_argument("csv", type=Path, help="시나리오 담당이 내려받은 CSV (UTF-8)")
    p.add_argument("--out", type=Path, required=True, help="쓸 JSON. 있으면 합친다")
    p.add_argument("--me", default=None, help="사용자 이름 (기존 JSON에 있으면 그 값, 없으면 김민수)")
    p.add_argument("--contact", default=None, help="CSV에 '상대' 열이 없을 때 모든 대화방의 상대")
    p.add_argument("--characters", type=Path, default=ROOT / "evals" / "characters.csv")
    p.add_argument("--no-validate", action="store_true", help="끝에 validate_data_files.py를 돌리지 않는다")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    existing = json.loads(args.out.read_text(encoding="utf-8")) if args.out.exists() else None
    me = args.me or (existing or {}).get("user", {}).get("name") or "김민수"
    try:
        dataset, report = convert(
            read_rows(args.csv), me, load_characters(args.characters), existing, default_contact=args.contact
        )
    except ConversionError as exc:
        print("변환하지 않았습니다. 고칠 것:")
        for line in exc.problems:
            print("  -", line)
        return 1
    write_dataset(dataset, args.out)
    print(f"→ {args.out}")
    print(
        f"대화방 추가 {report.conversations_added}, 갱신 {report.conversations_updated}, "
        f"메시지 추가 {report.messages_added}건, 이미 있어 건너뜀 {report.messages_skipped}건"
    )
    for w in report.warnings:
        print("주의:", w)
    if not args.no_validate:
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_data_files.py")], cwd=ROOT)
        return result.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
