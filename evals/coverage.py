"""평가 사례의 칸 점검: 관계 × 상황 표에 사례가 몇 개씩 있고 목표까지 몇 개 모자란지 센다.

    python evals/coverage.py                         # 칸당 6개 기준 (project-design §8: 4 × 5 × 6 = 120)
    python evals/coverage.py --per-cell 3            # 60사례 기준
    python evals/coverage.py --drafts evals/eval_cases.draft.csv      # 검토 전 초안도 함께 센다
    python evals/coverage.py --per-cell 3 --drafts evals/eval_cases.draft.csv \
        --template evals/eval_cases.todo.csv         # 모자란 칸의 빈 행을 CSV로

모델도 DB도 쓰지 않는다(표준 라이브러리만). 출력은 마크다운이다.

- 관계 축은 `evals/characters.csv`의 `relation` 값, 상황 축은 가이드(`docs/guides/dataset-guide.md`)
  6절의 다섯 값이다. 축에 없는 값을 쓴 사례는 표에 넣지 않고 따로 적는다
  (형식 검사기는 `situation` 값을 보지 않는다).
- 조건 유형은 CSV로 가를 수 있는 만큼만 가른다: `needs_graph`와 `relevant_msg_ids`(대화방 수).
- `--template`은 모자란 칸마다 빈 행을 만든 CSV를 쓴다. 사례 번호·관계·상황만 채우고 나머지는 빈 값이다.
  이 파일은 형식 검사를 통과하지 못하는 것이 정상이다(채운 줄을 `eval_cases.csv`로 옮긴 뒤 검사한다).
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "evals" / "eval_cases.csv"
DEFAULT_CHARACTERS = ROOT / "evals" / "characters.csv"

# docs/guides/dataset-guide.md 6절 `situation` 행의 값과 순서. 가이드를 고치면 여기도 고친다.
SITUATIONS = ["일정 지연 양해", "거절", "사과", "부탁", "일정 조율"]
# docs/design/project-design.md §8 "관계 4종 × 상황 5종 × 사례 6개"
DEFAULT_PER_CELL = 6
# docs/guides/dataset-guide.md 6절의 열과 순서 (`eval_cases.csv`의 머리줄)
COLUMNS = [
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
]

KIND_GRAPH_CROSS = "그래프 필요 · 다른 대화방을 이어야 함"
KIND_GRAPH_SAME = "그래프 필요 · 한 대화방 안"
KIND_HISTORY = "그래프 불필요 · 과거 메시지 지정(문체·맥락)"
KIND_NONE = "그래프 불필요 · 과거 메시지 없음"
KIND_UNSET = "(needs_graph 빈 값·잘못된 값)"
KINDS = [KIND_GRAPH_CROSS, KIND_GRAPH_SAME, KIND_HISTORY, KIND_NONE, KIND_UNSET]
SPLITS = ["dev", "test"]
SPLIT_UNSET = "(빈 값)"

CASE_ID = re.compile(r"^([A-Za-z]+)(\d+)$")


def load_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    """CSV 행(값은 앞뒤 공백 제거)과 머리줄의 열 이름."""
    with path.open(encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        columns = list(reader.fieldnames or [])
        rows = [{k: (v or "").strip() for k, v in row.items() if k} for row in reader]
    return rows, columns


def load_relations(path: Path) -> list[str]:
    """등장인물 표의 `relation` 값 (표에 적힌 순서, 중복 제거)."""
    rows, _ = load_rows(path)
    out: list[str] = []
    for row in rows:
        value = row.get("relation", "")
        if value and value not in out:
            out.append(value)
    return out


def rooms(relevant_msg_ids: str) -> set[str]:
    """`c3:m05;c1:m10` → {"c3", "c1"}"""
    return {ref.split(":", 1)[0].strip() for ref in relevant_msg_ids.split(";") if ref.strip()}


def case_kind(row: dict[str, str]) -> str:
    """조건 유형. `needs_graph`와 `relevant_msg_ids`만 본다 — 내용은 읽지 않는다."""
    needs = row.get("needs_graph", "")
    n_rooms = len(rooms(row.get("relevant_msg_ids", "")))
    if needs == "Y":
        return KIND_GRAPH_CROSS if n_rooms >= 2 else KIND_GRAPH_SAME
    if needs == "N":
        return KIND_HISTORY if n_rooms else KIND_NONE
    return KIND_UNSET


@dataclass
class Coverage:
    relations: list[str]
    situations: list[str]
    per_cell: int
    cases: Counter[tuple[str, str]] = field(default_factory=Counter)  # 확정 사례
    drafts: Counter[tuple[str, str]] = field(default_factory=Counter)  # 검토 전 초안
    off_axis: list[tuple[str, str, str]] = field(default_factory=list)  # (case_id, relation, situation)
    n_cases: int = 0
    n_drafts: int = 0

    def count(self, cell: tuple[str, str]) -> int:
        return self.cases[cell] + self.drafts[cell]

    def shortfall(self, cell: tuple[str, str]) -> int:
        return max(0, self.per_cell - self.count(cell))

    def surplus(self, cell: tuple[str, str]) -> int:
        return max(0, self.count(cell) - self.per_cell)

    def cells(self) -> list[tuple[str, str]]:
        return [(r, s) for r in self.relations for s in self.situations]

    @property
    def target(self) -> int:
        return len(self.relations) * len(self.situations) * self.per_cell

    @property
    def total_shortfall(self) -> int:
        return sum(self.shortfall(c) for c in self.cells())

    @property
    def total_surplus(self) -> int:
        return sum(self.surplus(c) for c in self.cells())


def tally(
    cases: list[dict[str, str]],
    relations: list[str],
    situations: list[str],
    per_cell: int,
    drafts: list[dict[str, str]] | None = None,
) -> Coverage:
    cov = Coverage(relations=list(relations), situations=list(situations), per_cell=per_cell)
    cov.n_cases, cov.n_drafts = len(cases), len(drafts or [])
    for counter, rows in ((cov.cases, cases), (cov.drafts, drafts or [])):
        for row in rows:
            cell = (row.get("relation", ""), row.get("situation", ""))
            if cell[0] in relations and cell[1] in situations:
                counter[cell] += 1
            else:
                cov.off_axis.append((row.get("case_id", ""), cell[0], cell[1]))
    return cov


def next_case_ids(rows: list[dict[str, str]], n: int) -> list[str]:
    """이미 쓴 번호 다음부터 n개. `E020` 다음은 `E021` (접두사·자릿수는 가장 큰 번호를 따른다)."""
    prefix, width, last = "E", 3, 0
    for row in rows:
        m = CASE_ID.match(row.get("case_id", ""))
        if m and int(m.group(2)) > last:
            prefix, width, last = m.group(1), max(width, len(m.group(2))), int(m.group(2))
    return [f"{prefix}{last + i:0{width}d}" for i in range(1, n + 1)]


def template_rows(cov: Coverage, existing: list[dict[str, str]]) -> list[dict[str, str]]:
    """모자란 칸마다 빈 행. 사람이 채울 칸은 빈 값으로 둔다(자리 채움 글자를 넣지 않는다)."""
    cells = [cell for cell in cov.cells() for _ in range(cov.shortfall(cell))]
    out: list[dict[str, str]] = []
    for case_id, (relation, situation) in zip(next_case_ids(existing, len(cells)), cells, strict=True):
        row = dict.fromkeys(COLUMNS, "")
        row.update(case_id=case_id, relation=relation, situation=situation)
        out.append(row)
    return out


def write_template(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def _table(header: list[str], body: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in body]
    return lines


def _cell_text(cov: Coverage, cell: tuple[str, str]) -> str:
    text = str(cov.cases[cell])
    if cov.n_drafts:
        text += f"+{cov.drafts[cell]}"
    if cov.shortfall(cell):
        return f"{text} (-{cov.shortfall(cell)})"
    if cov.surplus(cell):
        return f"{text} (+{cov.surplus(cell)})"
    return text


def render(
    cov: Coverage,
    cases: list[dict[str, str]],
    drafts: list[dict[str, str]],
    columns: list[str],
    source: str,
) -> str:
    """마크다운 보고서."""
    out = [f"# 평가 사례 칸 점검 — {source}", ""]
    counted = f"사례 {cov.n_cases}개" + (f" + 검토 전 초안 {cov.n_drafts}개" if cov.n_drafts else "")
    out.append(
        f"{counted}, 칸당 목표 {cov.per_cell}개 → 목표 {cov.target}개 "
        f"(관계 {len(cov.relations)} × 상황 {len(cov.situations)} × {cov.per_cell}). "
        f"모자람 **{cov.total_shortfall}**, 넘침 {cov.total_surplus}."
    )
    out += ["", "## 관계 × 상황", ""]
    legend = "칸: 사례 수" + ("+초안 수" if cov.n_drafts else "") + ", 괄호는 목표 대비 모자람(-)·넘침(+)."
    out += [legend, ""]
    body = []
    for r in cov.relations:
        row_cells = [(r, s) for s in cov.situations]
        body.append(
            [r, *[_cell_text(cov, c) for c in row_cells]]
            + [str(sum(cov.count(c) for c in row_cells)), str(sum(cov.shortfall(c) for c in row_cells))]
        )
    col_cells = [[(r, s) for r in cov.relations] for s in cov.situations]
    body.append(
        ["합", *[str(sum(cov.count(c) for c in col)) for col in col_cells]]
        + [str(sum(cov.count(c) for c in cov.cells())), str(cov.total_shortfall)]
    )
    out += _table(["관계 \\ 상황", *cov.situations, "합", "모자람"], body)

    if cov.off_axis:
        out += ["", "## 축에 없는 값 (표에 넣지 않았다)", ""]
        out += _table(
            ["case_id", "relation", "situation"],
            [[cid or "(없음)", r or "(빈 값)", s or "(빈 값)"] for cid, r, s in cov.off_axis],
        )
        out += [
            "",
            f"관계는 `characters.csv`의 값({', '.join(cov.relations)}), "
            f"상황은 가이드 6절의 값({', '.join(cov.situations)})만 센다.",
        ]

    rows = cases + drafts
    splits = Counter(r.get("split", "") if r.get("split", "") in SPLITS else SPLIT_UNSET for r in rows)
    out += ["", "## split", ""]
    split_names = SPLITS + ([SPLIT_UNSET] if splits[SPLIT_UNSET] else [])
    total = len(rows) or 1
    out += _table(
        ["split", "사례 수", "비율"],
        [[name, str(splits[name]), f"{splits[name] / total:.0%}"] for name in split_names],
    )

    out += ["", "## 조건 유형 (needs_graph · relevant_msg_ids로 가른 것)", ""]
    by_kind: dict[str, list[dict[str, str]]] = {k: [] for k in KINDS}
    for row in rows:
        by_kind[case_kind(row)].append(row)
    body = []
    for kind in KINDS:
        members = by_kind[kind]
        if kind == KIND_UNSET and not members:
            continue
        per_split = [str(sum(1 for m in members if m.get("split") == s)) for s in SPLITS]
        ids = ", ".join(m.get("case_id", "") for m in members) if kind == KIND_GRAPH_CROSS else ""
        body.append([kind, str(len(members)), *per_split, ids])
    out += _table(["유형", "사례 수", *SPLITS, "사례"], body)
    out += [
        "",
        "'다른 대화방을 이어야 함'은 `relevant_msg_ids`가 대화방 둘 이상에 걸친 사례다. "
        "같은 사건에 기대는지는 이 표로 알 수 없다 — 사람이 읽고 본다.",
    ]

    extra = [c for c in columns if c not in COLUMNS]
    out += ["", "## 검토 표시", ""]
    if extra:
        out.append(f"가이드 6절에 없는 열이 있다: {', '.join(extra)}")
    else:
        out.append(
            "CSV에 검토 상태를 적는 열이 없다(열은 가이드 6절의 11개뿐). "
            "누가 검토했는지는 이 파일로 알 수 없다 — 남기는 방법은 팀이 아직 정하지 않았다(가이드 10절)."
        )
    return "\n".join(out) + "\n"


def _display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="평가 사례의 관계 × 상황 칸 점검 (모델 호출 없음)")
    p.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    p.add_argument("--characters", type=Path, default=DEFAULT_CHARACTERS, help="관계 축 (relation 열)")
    p.add_argument(
        "--per-cell",
        type=int,
        default=DEFAULT_PER_CELL,
        help=f"칸당 목표 사례 수 (기본 {DEFAULT_PER_CELL} = 120사례, 3 = 60, 2 = 40)",
    )
    p.add_argument(
        "--drafts",
        type=Path,
        action="append",
        default=[],
        help="검토 전 초안 CSV. 칸을 채운 것으로 세고 사례 번호가 겹치지 않게 한다 (여러 번 줄 수 있다)",
    )
    p.add_argument(
        "--template", type=Path, default=None, help="모자란 칸마다 빈 행을 만든 CSV를 이 경로에 쓴다"
    )
    args = p.parse_args(argv)
    if args.per_cell < 0:
        p.error("--per-cell 은 0 이상")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cases, columns = load_rows(args.cases)
    drafts: list[dict[str, str]] = []
    for path in args.drafts:
        drafts += load_rows(path)[0]
    cov = tally(cases, load_relations(args.characters), SITUATIONS, args.per_cell, drafts)
    source = " + ".join(_display(p) for p in [args.cases, *args.drafts])
    sys.stdout.write(render(cov, cases, drafts, columns, source))

    if args.template is not None:
        taken = {p.resolve() for p in [args.cases, *args.drafts]}
        if args.template.resolve() in taken:
            print(f"\n--template 은 읽는 파일과 달라야 한다: {args.template}", file=sys.stderr)
            return 2
        rows = template_rows(cov, cases + drafts)
        write_template(args.template, rows)
        span = f" ({rows[0]['case_id']}~{rows[-1]['case_id']})" if rows else ""
        print(f"\n초안 틀: {_display(args.template)} — 빈 행 {len(rows)}개{span}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
