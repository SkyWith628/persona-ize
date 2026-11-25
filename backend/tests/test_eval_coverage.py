"""평가 사례 칸 점검(`evals/coverage.py`)과 형식 검사기의 `--cases`(`scripts/validate_data_files.py`).

모델도 DB도 쓰지 않는다. 지금 20사례의 수치는 `evals/eval_cases.csv`를 손으로 센 값이다 — 사례를 더하면
`test_current_cases_*`의 숫자를 함께 고친다.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "evals" / "eval_cases.csv"
DRAFTS = ROOT / "evals" / "eval_cases.draft.csv"
RELATIONS = ["팀장", "동기", "동생", "친구"]


def _load(folder: str, name: str):  # noqa: ANN202
    """저장소 루트의 evals·scripts는 패키지가 아니라 파일로 읽는다."""
    path = ROOT / folder / f"{name}.py"
    if not path.is_file():
        pytest.skip(f"{folder}/{name}.py가 없는 설치입니다 (backend만 복사한 이미지).")
    spec = importlib.util.spec_from_file_location(f"pz_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass가 모듈을 찾는다
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cov():  # noqa: ANN201
    return _load("evals", "coverage")


@pytest.fixture(scope="module")
def validator():  # noqa: ANN201
    return _load("scripts", "validate_data_files")


def _case(case_id: str, relation: str, situation: str, **extra: str) -> dict[str, str]:
    row = {
        "case_id": case_id,
        "relation": relation,
        "situation": situation,
        "needs_graph": "N",
        "relevant_msg_ids": "",
        "split": "dev",
    }
    row.update(extra)
    return row


# ---------- 칸 집계 ----------


def test_tally_counts_empty_and_overfull_cells(cov) -> None:
    rows = [_case(f"E{i:03d}", "팀장", "거절") for i in range(1, 4)] + [_case("E004", "동기", "사과")]
    got = cov.tally(rows, RELATIONS, cov.SITUATIONS, per_cell=2)

    assert got.count(("팀장", "거절")) == 3
    assert (got.shortfall(("팀장", "거절")), got.surplus(("팀장", "거절"))) == (0, 1)  # 넘치는 칸
    assert (got.shortfall(("동기", "사과")), got.surplus(("동기", "사과"))) == (1, 0)
    assert got.shortfall(("친구", "부탁")) == 2  # 빈 칸
    assert got.target == 4 * 5 * 2
    # 넘친 사례는 다른 칸의 모자람을 메우지 않는다: 빈 칸 18 × 2 + 하나 든 칸 1
    assert got.total_shortfall == 18 * 2 + 1
    assert got.total_surplus == 1


def test_tally_keeps_unknown_relation_and_situation_out_of_the_grid(cov) -> None:
    rows = [
        _case("E001", "팀장", "거절"),
        _case("E002", "선배", "거절"),  # characters.csv에 없는 관계
        _case("E003", "팀장", "축하"),  # 가이드에 없는 상황
        _case("E004", "팀장", ""),
    ]
    got = cov.tally(rows, RELATIONS, cov.SITUATIONS, per_cell=1)

    assert sum(got.count(c) for c in got.cells()) == 1
    assert got.off_axis == [("E002", "선배", "거절"), ("E003", "팀장", "축하"), ("E004", "팀장", "")]
    text = cov.render(got, rows, [], cov.COLUMNS, "x.csv")
    assert "## 축에 없는 값" in text
    assert "| E002 | 선배 | 거절 |" in text
    assert "| E004 | 팀장 | (빈 값) |" in text


def test_drafts_fill_cells_but_are_shown_apart(cov) -> None:
    cases = [_case("E001", "팀장", "거절")]
    drafts = [_case("E002", "팀장", "거절"), _case("E003", "친구", "부탁")]
    got = cov.tally(cases, RELATIONS, cov.SITUATIONS, per_cell=2, drafts=drafts)

    assert got.shortfall(("팀장", "거절")) == 0
    assert got.shortfall(("친구", "부탁")) == 1
    text = cov.render(got, cases, drafts, cov.COLUMNS, "x.csv")
    assert "사례 1개 + 검토 전 초안 2개" in text
    assert "| 팀장 | 0+0 (-2) | 1+1 |" in text
    assert "0+1 (-1)" in text


def test_case_kind_uses_only_needs_graph_and_rooms(cov) -> None:
    assert cov.case_kind(_case("E1", "팀장", "거절")) == cov.KIND_NONE
    assert cov.case_kind(_case("E1", "팀장", "거절", relevant_msg_ids="c1:m02;c1:m03")) == cov.KIND_HISTORY
    same = _case("E1", "동기", "사과", needs_graph="Y", relevant_msg_ids="c3:m05;c3:m08")
    cross = _case("E1", "동기", "거절", needs_graph="Y", relevant_msg_ids="c3:m05; c1:m10")
    assert cov.case_kind(same) == cov.KIND_GRAPH_SAME
    assert cov.case_kind(cross) == cov.KIND_GRAPH_CROSS
    assert cov.case_kind(_case("E1", "팀장", "거절", needs_graph="")) == cov.KIND_UNSET


# ---------- 초안 틀 ----------


def test_next_case_ids_continue_after_the_largest_number(cov) -> None:
    rows = [_case("E019", "팀장", "거절"), _case("E020", "팀장", "사과"), _case("메모", "팀장", "사과")]
    assert cov.next_case_ids(rows, 3) == ["E021", "E022", "E023"]
    assert cov.next_case_ids([], 2) == ["E001", "E002"]
    assert cov.next_case_ids([_case("E999", "팀장", "거절")], 2) == ["E1000", "E1001"]


def test_template_has_one_blank_row_per_missing_slot(cov, tmp_path: Path) -> None:
    cases = [_case("E001", "팀장", "일정 지연 양해"), _case("E002", "팀장", "일정 지연 양해")]
    drafts = [_case("E007", "팀장", "거절")]
    got = cov.tally(cases, ["팀장"], cov.SITUATIONS, per_cell=2, drafts=drafts)
    rows = cov.template_rows(got, cases + drafts)

    # 찬 칸은 건너뛰고, 번호는 초안의 번호 뒤로 잇는다
    assert [(r["case_id"], r["situation"]) for r in rows] == [
        ("E008", "거절"),
        ("E009", "사과"),
        ("E010", "사과"),
        ("E011", "부탁"),
        ("E012", "부탁"),
        ("E013", "일정 조율"),
        ("E014", "일정 조율"),
    ]
    out = tmp_path / "todo.csv"
    cov.write_template(out, rows)
    with out.open(encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        assert reader.fieldnames == cov.COLUMNS
        written = list(reader)
    for row in written:
        assert row["relation"] == "팀장"
        blank = [c for c in cov.COLUMNS if c not in {"case_id", "relation", "situation"}]
        assert [row[c] for c in blank] == [""] * len(blank)  # 자리 채움 글자 없이 빈 값


def test_main_writes_template_and_refuses_to_overwrite_its_input(cov, tmp_path: Path, capsys) -> None:
    out = tmp_path / "todo.csv"
    assert cov.main(["--per-cell", "3", "--template", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "| 관계 \\ 상황 | 일정 지연 양해 | 거절 | 사과 | 부탁 | 일정 조율 | 합 | 모자람 |" in printed
    assert "빈 행 40개 (E021~E060)" in printed
    assert sum(1 for _ in out.open(encoding="utf-8")) == 41

    copy = tmp_path / "cases.csv"
    copy.write_bytes(CASES.read_bytes())
    assert cov.main(["--cases", str(copy), "--template", str(copy)]) == 2
    assert copy.read_bytes() == CASES.read_bytes()


# ---------- 지금 저장소의 파일 ----------


def test_columns_and_axes_match_the_validator(cov, validator) -> None:
    assert set(cov.COLUMNS) == validator.REQUIRED[validator.EVAL_CASES]
    assert cov.load_relations(cov.DEFAULT_CHARACTERS) == RELATIONS
    with CASES.open(encoding="utf-8-sig", newline="") as fp:
        assert next(csv.reader(fp)) == cov.COLUMNS


def test_current_cases_fill_every_cell_once(cov) -> None:
    rows, columns = cov.load_rows(CASES)
    assert len(rows) == 20
    for per_cell, shortfall in ((1, 0), (2, 20), (3, 40), (6, 100)):
        got = cov.tally(rows, RELATIONS, cov.SITUATIONS, per_cell)
        assert {got.count(c) for c in got.cells()} == {1}
        assert (got.total_shortfall, got.total_surplus, got.off_axis) == (shortfall, 0, [])

    text = cov.render(cov.tally(rows, RELATIONS, cov.SITUATIONS, 6), rows, [], columns, "eval_cases.csv")
    assert "| 팀장 | 1 (-5) | 1 (-5) | 1 (-5) | 1 (-5) | 1 (-5) | 5 | 25 |" in text
    assert "| 합 | 4 | 4 | 4 | 4 | 4 | 20 | 100 |" in text
    assert "| dev | 16 | 80% |" in text
    assert "| test | 4 | 20% |" in text
    assert "검토 상태를 적는 열이 없다" in text


def test_current_cases_by_kind(cov) -> None:
    rows, _ = cov.load_rows(CASES)
    by_kind: dict[str, list[str]] = {}
    for row in rows:
        by_kind.setdefault(cov.case_kind(row), []).append(row["case_id"])

    assert by_kind[cov.KIND_GRAPH_CROSS] == ["E010", "E018", "E019"]
    assert by_kind[cov.KIND_GRAPH_SAME] == ["E004", "E009", "E011"]
    assert by_kind[cov.KIND_NONE] == ["E001", "E003", "E014", "E016"]
    assert len(by_kind[cov.KIND_HISTORY]) == 10
    assert cov.KIND_UNSET not in by_kind


def test_draft_file_continues_ids_in_distinct_cells(cov) -> None:
    cases, _ = cov.load_rows(CASES)
    drafts, columns = cov.load_rows(DRAFTS)
    assert columns == cov.COLUMNS
    assert 1 <= len(drafts) <= 4
    assert [d["case_id"] for d in drafts] == cov.next_case_ids(cases, len(drafts))
    assert len({(d["relation"], d["situation"]) for d in drafts}) == len(drafts)
    assert cov.tally(cases, RELATIONS, cov.SITUATIONS, 6, drafts).off_axis == []


# ---------- 형식 검사기 --cases ----------


def test_validator_default_run_is_unchanged(validator, capsys) -> None:
    assert validator.main([]) == 0
    assert capsys.readouterr().out.startswith("접점 파일 OK (등장인물 4, 사례 20, ")


def test_validator_checks_the_draft_file_with_the_same_rules(validator, capsys) -> None:
    assert validator.main(["--cases", str(DRAFTS)]) == 0
    assert "사례 4," in capsys.readouterr().out


def test_validator_reports_reused_ids_and_blank_template_rows(cov, validator, tmp_path: Path, capsys) -> None:
    reused = tmp_path / "reused.csv"
    lines = CASES.read_text(encoding="utf-8").splitlines()
    reused.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
    assert validator.main(["--cases", str(reused)]) == 1
    assert "reused.csv:2: case_id E001 가 eval_cases.csv 에 이미 있음" in capsys.readouterr().out

    cases, _ = cov.load_rows(CASES)
    todo = tmp_path / "todo.csv"
    cov.write_template(todo, cov.template_rows(cov.tally(cases, RELATIONS, cov.SITUATIONS, 2), cases))
    assert validator.main(["--cases", str(todo)]) == 1  # 빈 틀은 통과하지 못한다
    assert "todo.csv:2: original_text 비어 있음" in capsys.readouterr().out
