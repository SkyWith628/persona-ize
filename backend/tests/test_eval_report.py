"""최종 비교 보고서 뼈대(`evals/report.py`)와 집계의 대체 모드(`summarize.py --replace`).

모델도 DB도 쓰지 않는다. 11/24 결과 CSV에 고정한 수치는 손으로 쓴 보고서 둘
(`2026-11-24-dev16-repeat3.md`, `2026-11-25-d-latency-analysis.md`)의 값이다.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if not (ROOT / "evals" / "report.py").is_file():
    pytest.skip("evals/report.py가 없는 설치입니다 (backend만 복사한 이미지).", allow_module_level=True)
sys.path.insert(0, str(ROOT / "evals"))

import report as rp  # noqa: E402
import summarize as sm  # noqa: E402

RESULTS = ROOT / "evals" / "results"
DEV16 = RESULTS / "eval-20261124-dev16-abcd-repeat3.csv"
RECHECK = RESULTS / "eval-20261124-dev16-recheck-E011-E014-E020.csv"
OLD = RESULTS / "eval-20261108-opus55-AB.csv"  # 검색·fm·검증 규칙 열이 없는 예전 파일
PRICES = {"claude-opus-5-5": (4.0, 20.0)}


def _row(case: str = "E001", cond: str = "A", repeat: int = 1, **over: object) -> dict[str, str]:
    row: dict[str, object] = {
        "case_id": case,
        "split": "dev",
        "relation": "팀장",
        "condition": cond,
        "repeat": repeat,
        "status": "recommended",
        "violations": "",
        "result_text": "결과 문장입니다.",
        "attempts": 1,
        "latency_ms": 1000,
        "input_tokens": 100,
        "output_tokens": 50,
        "model_version": "claude-opus-5-5",
        "prompt_version": "rewrite-v3",
        "retrieval_version": "none",
        "recall": "",
        "first_attempt_violations": "",
        "context_facts": "",
        "context_recall": "",
        "context_hit": "",
        "warning_hit": "",
        "fm11_first_checks": "",
        "fm13_attempts": 1,
        "fm23_length_ratio": "1.00",
        "fm24_no_examples": False,
        "fm25_evidence_unused": False,
        "fm32_register_warned": False,
        "fm33_fallback": False,
        "validator_version": "rules-1",
    }
    row.update(over)
    return {k: str(v) for k, v in row.items()}


def _write(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


CASES = [
    {"case_id": "E001", "relation": "팀장", "situation": "거절", "needs_graph": "N", "relevant_msg_ids": ""},
    {
        "case_id": "E002",
        "relation": "친구",
        "situation": "사과",
        "needs_graph": "Y",
        "relevant_msg_ids": "c1:m01;c4:m02",
    },
]


def _report(rows: list[dict[str, str]], **over: object) -> rp.Report:
    args: dict[str, object] = {
        "sources": ["t.csv"],
        "made_on": "2026-11-27",
        "prices": PRICES,
        "case_rows": CASES,
        "relations": ["팀장", "친구"],
        "cases_source": "cases.csv",
    }
    args.update(over)
    return rp.build_report(rows, **args)  # type: ignore[arg-type]


def _dev16(*, replace: bool = False) -> rp.Report:
    groups = [sm.read_rows(DEV16)] + ([sm.read_rows(RECHECK)] if replace else [])
    rows, replaced = sm.merge_rows(groups, replace=replace)
    return rp.build_report(
        rows,
        sources=["dev16"],
        made_on="2026-11-27",
        prices=PRICES,
        replaced=replaced if replace else None,
        case_rows=rp.cov.load_rows(rp.DEFAULT_CASES)[0],
        relations=rp.cov.load_relations(rp.DEFAULT_CHARACTERS),
        cases_source="evals/eval_cases.csv",
    )


# ---------------------------------------------------------------------------
# 대체 모드
# ---------------------------------------------------------------------------


def test_merge_keeps_every_row_by_default_and_replaces_on_request() -> None:
    first = [_row("E001", "A", 1, latency_ms=1), _row("E001", "A", 2, latency_ms=2), _row("E002", "A", 1)]
    second = [_row("E001", "A", 2, latency_ms=99), _row("E003", "A", 1)]

    rows, replaced = sm.merge_rows([first, second])
    assert (len(rows), replaced) == (5, 0)  # 기본 동작: 그대로 이어 붙인다
    assert any("두 번 이상" in w for w in sm.build_summary(rows).warnings)

    rows, replaced = sm.merge_rows([first, second], replace=True)
    assert replaced == 1
    # 대체된 줄은 앞 파일의 자리에, 새 실행은 뒤에
    assert [(r["case_id"], r["repeat"], r["latency_ms"]) for r in rows] == [
        ("E001", "1", "1"),
        ("E001", "2", "99"),
        ("E002", "1", "1000"),
        ("E003", "1", "1000"),
    ]
    assert not sm.build_summary(rows).warnings


def test_replace_does_not_collapse_duplicates_inside_one_file() -> None:
    rows, replaced = sm.merge_rows([[_row(), _row()], [_row("E009")]], replace=True)
    assert (len(rows), replaced) == (3, 0)


def test_summarize_cli_replace_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    a = _write(tmp_path / "a.csv", [_row(latency_ms=1000), _row("E002")])
    b = _write(tmp_path / "b.csv", [_row(latency_ms=3000)])
    assert sm.main([str(a), str(b), "--no-cost"]) == 0
    plain = capsys.readouterr().out
    assert "실행 3회" in plain and "두 번 이상" in plain
    assert sm.main([str(a), str(b), "--no-cost", "--replace"]) == 0
    replaced = capsys.readouterr().out
    assert "실행 2회" in replaced and "1개를 대체했다" in replaced and "두 번 이상" not in replaced


def test_dev16_recheck_replaces_three_cases() -> None:
    report = _dev16(replace=True)
    assert report.replaced == 36 and report.summary.total.runs == 192
    d = report.summary.by_condition("D")
    assert d is not None and (d.fallback, d.regenerated) == (0, 0)


# ---------------------------------------------------------------------------
# 버전 섞임
# ---------------------------------------------------------------------------


def test_single_version_run_has_no_mix_warning() -> None:
    report = _report([_row(), _row(cond="B")])
    assert report.mixes == []
    text = rp.render(report)
    assert "버전 섞임: 없음" in text and "⚠" not in text


def test_mixed_validator_versions_are_flagged_with_cases() -> None:
    rows = [
        _row("E001"),
        _row("E002", validator_version="rules-2"),
        _row("E003", validator_version="rules-2"),
    ]
    report = _report(rows)
    (mix,) = report.mixes
    assert (mix.column, mix.scope) == ("validator_version", "전체")
    assert mix.values == {"rules-1": (1, ["E001"]), "rules-2": (2, ["E002", "E003"])}
    text = rp.render(report)
    assert "⚠ 경고 — 버전이 섞여 있다 (검증 규칙)" in text
    assert "| 검증 규칙 | 전체 | `rules-2` | 2 | 2개: E002, E003 |" in text
    assert "**버전이 섞여 있다**" in "\n".join(rp.limit_lines(report))  # 한계 절에도


def test_retrieval_version_is_compared_inside_a_condition_only() -> None:
    rows = [
        _row(cond="C", retrieval_version="knn-v1"),
        _row(cond="D", retrieval_version="knn-v1+graph-v3"),
    ]
    assert rp.version_mixes(rows) == []  # 조건마다 다른 것은 정상
    rows.append(_row("E002", cond="D", retrieval_version="knn-v1+graph-v2"))
    (mix,) = rp.version_mixes(rows)
    assert (mix.label, mix.scope) == ("검색기", "조건 D")


def test_blank_version_next_to_a_value_counts_as_a_mix_but_a_missing_column_does_not() -> None:
    old = [{k: v for k, v in _row().items() if k != "validator_version"}]
    assert rp.version_mixes(old) == []
    (mix,) = rp.version_mixes([*old, _row("E002")])
    assert set(mix.values) == {"(빈칸)", "rules-1"}


def test_dev16_alone_is_one_version_and_the_replaced_set_mixes_two() -> None:
    assert _dev16().mixes == []
    (mix,) = _dev16(replace=True).mixes
    assert {v: n for v, (n, _) in mix.values.items()} == {"rules-2026-11-24.2": 156, "rules-2026-11-24.3": 36}
    assert mix.values["rules-2026-11-24.3"][1] == ["E011", "E014", "E020"]


# ---------------------------------------------------------------------------
# 단계별 차이
# ---------------------------------------------------------------------------


def test_stage_table_shows_before_after_and_difference() -> None:
    rows = [
        _row(cond="B", latency_ms=2000, input_tokens=100, output_tokens=50),
        _row(cond="C", latency_ms=3500, input_tokens=300, output_tokens=80, recall="0.50"),
        _row(
            cond="C",
            repeat=2,
            latency_ms=3500,
            input_tokens=300,
            output_tokens=80,
            status="fallback_original",
        ),
    ]
    table = {r[0]: r[1:] for r in rp.stage_rows(_report(rows), "B", "C") or []}
    assert table["실행 수"] == ["1", "2", ""]
    assert table["추천 비율 (규칙 통과)"] == ["100.0%", "50.0%", "-50.0%p"]
    assert table["원문 유지"] == ["0", "1", "+1"]
    assert table["지연 p50"] == ["2.0초", "3.5초", "+1.5초"]
    assert table["입력 토큰 (실행당)"] == ["100", "300", "+200 (×3.00)"]
    assert table["출력 토큰 (실행당)"] == ["50", "80", "+30 (×1.60)"]
    assert table["recall 평균"] == [sm.DASH, "0.50", sm.DASH]
    assert table["사람 점수 평균"] == [sm.NOT_SCORED, sm.NOT_SCORED, sm.DASH]


def test_stage_without_one_of_its_conditions_says_so() -> None:
    report = _report([_row(cond="A"), _row(cond="B")])
    assert rp.stage_rows(report, "C", "D") is None
    assert "결과가 없어" in rp.stage_lines(report, "C", "D")[0]
    assert rp.stage_rows(report, "A", "B") is not None


def test_stage_table_carries_human_scores_when_given() -> None:
    scores = [
        {"item_id": "1", "case_id": "E001", "condition": "A", "statuses": "", "style": "3"},
        {"item_id": "2", "case_id": "E001", "condition": "B", "statuses": "", "style": "4.5"},
    ]
    report = _report([_row(cond="A"), _row(cond="B")], score_rows=scores)
    table = {r[0]: r[1:] for r in rp.stage_rows(report, "A", "B") or []}
    assert table["사람 점수 평균"] == ["3.00", "4.50", "+1.50"]
    assert "| A | 1 | 3.00 | 3.00 |" in rp.render(report)  # 8절은 summarize의 표 그대로


def test_dev16_stage_c_to_d_matches_the_hand_written_report() -> None:
    table = {r[0]: r[1:] for r in rp.stage_rows(_dev16(), "C", "D") or []}
    assert table["원문 유지"] == ["0", "2", "+2"]
    assert table["입력 토큰 (실행당)"][2].endswith("(×1.62)")  # "D의 입력 토큰은 C의 1.62배"
    assert table["recall 평균"] == ["0.90", "0.92", "+0.02"]
    assert table["지연 p50"][:2] == ["3.9초", "5.0초"]


# ---------------------------------------------------------------------------
# D 채택 조건의 근거
# ---------------------------------------------------------------------------


def test_scattered_event_table_lists_cases_where_d_finds_more() -> None:
    rows = [
        _row("E001", "C", recall="1.00", context_recall="0.00"),
        _row("E001", "D", recall="1.00", context_recall="0.50"),
        _row("E002", "C", recall="0.25", context_recall="0.00"),
        _row("E002", "D", recall="0.50", context_recall="0.50"),
        _row("E003", "C"),  # 정답이 없는 사례는 표에 없다
        _row("E003", "D"),
    ]
    text = "\n".join(rp.scattered_lines(_report(rows)))
    assert (
        "| E002 | Y | 그래프 필요 · 다른 대화방을 이어야 함 (대화방 2개) | 0.25 | 0.50 | 0.50 | 1/1 | 1/1 |"
        in text
    )
    assert "E003" not in text
    assert "**D > C 1건 (E002)**, C > D 0건, 같음 1건" in text
    assert "`needs_graph=Y`인 사례 1건 (E002) 가운데 D > C는 1건 (E002)" in text


def test_d_evidence_is_marked_missing_for_old_files_without_the_columns() -> None:
    report = rp.build_report(sm.read_rows(OLD), made_on="2026-11-27", prices=PRICES)
    for lines in (
        rp.scattered_lines(report),
        rp.time_lines(report),
        rp.contamination_lines(report),
        rp.cost_lines(report),
    ):
        assert lines[0] == rp.NO_EVIDENCE
    text = rp.render(report)  # 뼈대 전체도 오류 없이 나온다
    assert text.count(rp.NO_EVIDENCE) == 4
    assert text.count(rp.VERDICT) == 4  # 판정 칸은 언제나 비어 있다


def test_time_condition_uses_keyword_hits_and_register_warnings() -> None:
    rows = [
        _row("E001", "C", context_hit="0.00", warning_hit="0.50"),
        _row("E001", "D", context_hit="1.00", warning_hit="0.50", fm32_register_warned=True),
        _row("E001", "D", repeat=2, context_hit="0.50", warning_hit="0.00"),
    ]
    text = "\n".join(rp.time_lines(_report(rows)))
    assert "| E001 | C | 1 | 0.00 | 0.50 |" in text
    assert "| E001 | D | 2 | 0.75 | 0.25 |" in text
    assert "| E001 | 팀장 | D | 1회 |" in text
    assert rp.NO_EVIDENCE not in text
    assert rp.time_lines(_report([_row(cond="C"), _row(cond="D")]))[0] == rp.NO_EVIDENCE


def test_contamination_checks_whether_the_added_item_sits_in_the_context() -> None:
    added = "원문에 없는 date '10월 3일' 이(가) 추가되었습니다."
    rows = [
        _row("E001", "C", attempts=2, fm11_first_checks="fact_added", first_attempt_violations=added),
        _row(
            "E001",
            "D",
            attempts=2,
            status="fallback_original",
            fm11_first_checks="fact_added",
            first_attempt_violations=added,
            violations=added,
            context_facts="10월 3일에 워크숍 발표를 맡기로 했다",
        ),
        _row("E002", "D", context_facts="무관한 사실"),
    ]
    items = rp.added_items(rows)
    assert [(i.condition, i.kind, i.where, i.in_context) for i in items] == [
        ("C", "date", "1차", False),
        ("D", "date", "1차", True),
    ]
    text = "\n".join(rp.contamination_lines(_report(rows)))
    assert "| 그중 추가된 항목이 그 실행의 `context_facts`에 있는 실행 | 0 | 1 |" in text
    assert "| E001 | D | 1 | date | 1차 | 예 |" in text
    assert "10월 3일" not in text  # 추가된 항목의 글자는 --show-text일 때만
    shown = "\n".join(rp.contamination_lines(_report(rows, show_text=True)))
    assert "| E001 | D | 1 | date | 10월 3일 | 1차 | 예 |" in shown


def test_cost_condition_compares_d_with_the_p95_target() -> None:
    rows = [_row(cond="C", latency_ms=4000), _row(cond="D", latency_ms=9000, input_tokens=400)]
    text = "\n".join(rp.cost_lines(_report(rows)))
    assert "| 지연 p95 | 4.0초 | 9.0초 | +5.0초 |" in text
    assert "D의 지연 p95 9.0초는 목표 8초 **밖**이다" in text and "목표를 넘긴 D 실행 1회 / 1회" in text
    assert "그래프 수집 비용" in text


def test_dev16_d_conditions() -> None:
    report = _dev16()
    scattered = "\n".join(rp.scattered_lines(report))
    # 손으로 쓴 11/24 보고서: 12사례 중 10사례는 C·D 모두 1.00, E018만 D가 낫다, 그래프 사례는 dev에 5개
    assert "recall 값이 있는 사례 12개 가운데 **D > C 1건 (E018)**, C > D 0건, 같음 11건" in scattered
    assert "`needs_graph=Y`인 사례 5건 (E004, E009, E011, E018, E019)" in scattered
    assert "| E018 | Y | 그래프 필요 · 다른 대화방을 이어야 함 (대화방 2개) | 0.25 | 0.50 |" in scattered
    assert "D의 지연 p95 11.5초는 목표 8초 **밖**이다 (시도 1회인 실행만 보면 8.1초)" in "\n".join(
        rp.cost_lines(report)
    )
    # 사람 원인 분석: E014 C의 1차 위반은 '이사'를 사람으로 읽은 규칙 오탐 — 맥락에서 온 것이 아니다
    assert [(i.case_id, i.condition, i.in_context) for i in rp.added_items(report.rows)] == [
        ("E011", "A", False),
        ("E014", "C", False),
    ]


def test_d_condition_wording_is_the_design_documents() -> None:
    design = (ROOT / "docs" / "design" / "design.md").read_text(encoding="utf-8")
    for _, item, method, rule in rp.D_CONDITIONS:
        assert f"| {item} | {method} | {rule} |" in design
    project = (ROOT / rp.DESIGN_DOC).read_text(encoding="utf-8")
    for text in [*rp.HYPOTHESES, rp.D_DECISION_RULE]:
        assert text in project


# ---------------------------------------------------------------------------
# 실패 목록, 사례별
# ---------------------------------------------------------------------------


def test_failure_list_names_runs_without_printing_sentences_by_default() -> None:
    rows = [
        _row("E001", "A"),
        _row(
            "E002",
            "D",
            repeat=3,
            attempts=2,
            status="fallback_original",
            fm11_first_checks="fact_added;preserve_missing",
            first_attempt_violations="가 | 나",
            violations="원문의 약속·의지 표현이 결과에 없습니다.",
            result_text="비밀 | 문장",
            fm33_fallback=True,
        ),
    ]
    text = "\n".join(rp.failure_lines(_report(rows)))
    assert "실행 2회 가운데 원문 유지 1회, 재생성 1회." in text
    assert (
        "| E002 | D | 3 | fallback_original | 2 | `fact_added`, `preserve_missing` | 1 | `rules-1` |" in text
    )
    assert "비밀" not in text and "약속·의지" not in text
    assert "| fm33 원문 유지 |" in text  # fm 신호 표는 summarize의 것
    shown = "\n".join(rp.failure_lines(_report(rows, show_text=True)))
    assert "비밀 \\| 문장" in shown and "약속·의지" in shown  # 세로줄은 표를 깨지 않게


def test_dev16_failure_list_is_the_five_runs_of_the_hand_written_report() -> None:
    runs = [(r["case_id"], r["condition"], r["status"]) for r in rp.failure_runs(_dev16().rows)]
    assert runs == [
        ("E011", "A", "recommended"),
        ("E014", "C", "recommended"),
        ("E020", "D", "fallback_original"),
        ("E020", "D", "fallback_original"),
        ("E020", "D", "recommended"),
    ]
    assert rp.failure_runs(_dev16(replace=True).rows) == []


def test_cells_group_cases_by_relation_and_situation() -> None:
    rows = [
        _row("E001", "C", recall="1.00"),
        _row("E002", "C", status="fallback_original"),
        _row("E999", "C"),  # 사례 파일에 없다
    ]
    body, outside = rp.cell_rows(_report(rows))
    table = {(r[0], r[1]): r[2:] for r in body}
    assert len(body) == 2 * len(rp.cov.SITUATIONS)
    assert table[("팀장", "거절")] == ["E001", "1/1 · 1.00"]
    assert table[("친구", "사과")] == ["E002", "0/1"]
    assert table[("팀장", "사과")] == [sm.DASH, sm.DASH]
    assert outside == ["E999"]
    assert "칸에 넣지 못한 사례 1개" in "\n".join(rp.case_section_lines(_report(rows)))


# ---------------------------------------------------------------------------
# 지연 분석 — 2026-11-25-d-latency-analysis.md의 수치
# ---------------------------------------------------------------------------


def test_fit_and_correlation_helpers() -> None:
    assert rp.linear_fit([100, 200, 300], [1700, 2700, 3700]) == pytest.approx((700, 10))
    assert rp.fit_text((700, 10.6)) == "지연 ≈ 0.7초 + 출력 100토큰당 1.06초"
    assert rp.correlation([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert rp.correlation([1, 1, 1], [2, 4, 6]) is None and rp.correlation([1], [2]) is None
    assert rp.linear_fit([], []) is None and rp.fit_text(None) == sm.DASH


def test_dev16_latency_matches_the_2026_11_25_analysis() -> None:
    la = _dev16().latency
    by = {s.condition: s for s in la.conditions}
    # "원래 192회에서 시도 1회인 실행만 보면 p95는 A 5.1 / B 4.6 / C 6.1 / D 8.1초"
    assert [sm._sec(by[c].one_p95) for c in "ABCD"] == ["5.1초", "4.6초", "6.1초", "8.1초"]
    assert [by[c].one_runs for c in "ABCD"] == [47, 48, 47, 45]
    # "상관은 C 0.91, D 0.95이고 입력 토큰과는 상관이 없다(C −0.04, D 0.07)"
    assert by["C"].corr_output == pytest.approx(0.91, abs=0.005)
    assert by["D"].corr_output == pytest.approx(0.95, abs=0.005)
    assert by["C"].corr_input == pytest.approx(-0.04, abs=0.005)
    assert by["D"].corr_input == pytest.approx(0.07, abs=0.005)
    # "어림식은 지연 ≈ 0.7초 + 출력 100토큰당 1.06초"
    assert rp.fit_text(la.fit) == "지연 ≈ 0.7초 + 출력 100토큰당 1.06초"
    assert la.fit_runs == 187
    # "같은 사례·회차에서 D−C의 중앙값은 지연 +1.2초, 출력 토큰 +93 … 남는 차이는 0.2초쯤"
    cd = next(p for p in la.pairs if (p.before, p.after) == ("C", "D"))
    assert (cd.pairs, cd.latency_median, cd.output_median) == (44, 1181, 93)
    assert cd.residual_of_medians == pytest.approx(195, abs=5)
    assert abs(cd.residual_median or 0) < 50  # 쌍마다 내면 0에 가깝다 — 0.2초는 중앙값끼리 뺀 값이다
    # D에서 8초를 크게 넘긴 세 실행은 모두 E020, 모두 시도 2회
    slowest = la.over_target[:3]
    assert {(r["case_id"], r["condition"], r["attempts"]) for r in slowest} == {("E020", "D", "2")}


def test_dev16_with_recheck_swapped_in_matches_the_2026_11_25_table() -> None:
    report = _dev16(replace=True)
    by = {s.condition: s for s in report.latency.conditions}
    assert [sm._sec(by[c].p50) for c in "ABCD"] == ["3.2초", "2.9초", "3.8초", "5.0초"]
    assert [sm._sec(by[c].p95) for c in "ABCD"] == ["5.3초", "4.6초", "6.1초", "8.1초"]
    assert [sm._sec(by[c].max) for c in "ABCD"] == ["6.5초", "5.0초", "6.4초", "9.6초"]
    # 문서의 "출력 토큰 중앙값 231 / 229 / 306 / 403"은 아래 값을 올림한 것이다
    assert [by[c].output_median for c in "ABCD"] == [231, 228.5, 305.5, 402.5]
    # 문서의 "결과 문장 글자 수 중앙값 59 / 59 / 58 / 58"도 같다
    assert [by[c].text_len_median for c in "ABCD"] == [59, 58.5, 57.5, 58]
    # "바꿔 넣은 뒤에도 D에서 8초를 넘는 실행은 넷이다: E009 9.6초·8.9초, E008 8.2초, E011 8.0초"
    assert [(r["case_id"], r["condition"], r["output_tokens"]) for r in report.latency.over_target] == [
        ("E009", "D", "776"),
        ("E009", "D", "784"),
        ("E008", "D", "583"),
        ("E011", "D", "670"),
    ]


def test_latency_section_handles_files_without_attempts_or_latency() -> None:
    bare = [{"case_id": "E001", "condition": "A", "repeat": "1", "status": "recommended"}]
    report = rp.build_report(bare, made_on="2026-11-27")
    assert rp.latency_lines(report) == ["지연 값이 없다 (`latency_ms` 열)."]
    no_attempts = [{**bare[0], "latency_ms": "1200", "output_tokens": "40"}]
    stats = rp.latency_stats("A", no_attempts)
    assert stats.one_runs is None and stats.one_p95 is None and stats.p95 == 1200


# ---------------------------------------------------------------------------
# 뼈대와 갱신
# ---------------------------------------------------------------------------


def test_skeleton_has_every_section_and_one_marker_for_human_text() -> None:
    text = rp.render(_dev16())
    regions = rp.parse_regions(text)
    assert [name for name, *_ in regions] == rp.SECTION_NAMES
    for heading in ("## 1. 무엇을 돌렸나", "## 4. D 채택 조건 네 가지", "### 4.3 맥락 오염", "## 10. 결론"):
        assert heading in text
    human = [line for line in text.split("\n") if "(사람이 쓴다" in line]
    assert human and all(line.startswith(rp.HUMAN) for line in human)
    assert "> ✍ D를 채택하는가:" in text and "> ✍ 무엇을 줄였는가:" in text
    assert "오늘 정리해서" not in text  # 결과 문장은 싣지 않는다
    # 같은 입력이면 같은 글
    assert rp.render(_dev16()) == text


def test_section_names_do_not_depend_on_the_input() -> None:
    for rows in ([], [_row()], sm.read_rows(OLD)):
        report = rp.build_report(rows, made_on="2026-11-27")
        assert list(rp.sections(report)) != [] and set(rp.sections(report)) == set(rp.SECTION_NAMES)
    assert "결과 줄이 없다." in rp.render(rp.build_report([], made_on="2026-11-27"))


def _write_human_text(path: Path) -> str:
    """뼈대의 ✍ 자리를 채우고 구역 밖에 문단을 더한 글."""
    text = path.read_text(encoding="utf-8")
    text = text.replace(rp.VERDICT, f"{rp.HUMAN} 판정: 보류 — E018 하나뿐이다.", 1)
    text = text.replace("## 10. 결론\n", "## 10. 결론\n\n사람이 덧붙인 문단.\n", 1)
    path.write_text(text, encoding="utf-8")
    return text


def test_update_swaps_numbers_and_keeps_human_text(tmp_path: Path) -> None:
    out = tmp_path / "report.md"
    base = ["--date", "2026-11-27", "--no-cost"]
    assert rp.main([str(DEV16), "--out", str(out), *base]) == 0
    before = _write_human_text(out)
    assert "D 11.5초" in before

    assert rp.main([str(DEV16), str(RECHECK), "--replace", "--update", str(out), *base]) == 0
    after = out.read_text(encoding="utf-8")
    assert "판정: 보류 — E018 하나뿐이다." in after and "사람이 덧붙인 문단." in after
    assert "D 11.5초" not in after and "D 8.1초" in after  # 수치는 새 입력의 것
    assert "36개를 대체했다" in after

    # 구역 밖은 한 글자도 바뀌지 않는다
    def outside(text: str) -> list[str]:
        lines = text.split("\n")
        inside = {i for _, _, start, end in rp.parse_regions(text) for i in range(start, end + 1)}
        return [line for i, line in enumerate(lines) if i not in inside]

    assert outside(after) == outside(before)
    # 같은 입력으로 다시 갱신하면 그대로다
    assert rp.main([str(DEV16), str(RECHECK), "--replace", "--update", str(out), *base]) == 0
    assert out.read_text(encoding="utf-8") == after


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda t: t.replace("<!-- auto:end targets -->\n", "", 1), "닫히기 전에"),
        (lambda t: t.replace("<!-- auto:end limits -->", "", 1), "끝 표식이 없다"),
        (lambda t: t.replace("<!-- auto:end cases -->", "<!-- auto:end latency -->", 1), "짝이 맞지 않는"),
        (
            lambda t: t.replace("<!-- auto:start scores sha=", "<!-- auto:start scores hash=", 1),
            "꼴이 깨졌다",
        ),
        (lambda t: t.replace("auto:end d-cost", "auto:end  d-cost", 1), "꼴이 깨졌다"),
        (lambda t: t + "\n" + "\n".join(rp.region("limits", ["x"])) + "\n", "두 번 나온다"),
        (lambda t: t + "\n" + "\n".join(rp.region("extra", ["x"])) + "\n", "지금 뼈대에 없는 구역"),
    ],
)
def test_update_refuses_broken_markers_and_leaves_the_file_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], damage, message: str
) -> None:
    out = tmp_path / "report.md"
    assert rp.main([str(DEV16), "--out", str(out), "--date", "2026-11-27"]) == 0
    broken = damage(_write_human_text(out))
    out.write_text(broken, encoding="utf-8")
    capsys.readouterr()
    assert rp.main([str(DEV16), str(RECHECK), "--replace", "--update", str(out)]) == 2
    err = capsys.readouterr().err
    assert message in err and "파일은 고치지 않았다" in err
    assert out.read_text(encoding="utf-8") == broken


def test_update_refuses_when_a_whole_region_was_deleted(tmp_path: Path) -> None:
    text = rp.render(_dev16())
    lines = text.split("\n")
    (start, end) = next((s, e) for name, _, s, e in rp.parse_regions(text) if name == "scores")
    cut = "\n".join(lines[:start] + lines[end + 1 :])
    with pytest.raises(rp.MarkerError, match="파일에 없는 구역"):
        rp.update_text(cut, rp.sections(_dev16()))


def test_update_notices_hand_edits_inside_an_auto_region(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "report.md"
    args = [str(DEV16), "--date", "2026-11-27"]
    assert rp.main([*args, "--out", str(out)]) == 0
    edited = out.read_text(encoding="utf-8").replace(
        "실행 192회 가운데 원문 유지 2회", "실행 192회 가운데 원문 유지 2회 (규칙 문제였다)", 1
    )
    out.write_text(edited, encoding="utf-8")
    assert rp.main([*args, "--update", str(out)]) == 2
    assert "손으로 고친 흔적이 있다: ['failures']" in capsys.readouterr().err
    assert out.read_text(encoding="utf-8") == edited
    # 줄 끝 공백만 달라진 것은 수정으로 보지 않는다
    spaced = rp.render(_dev16()).replace(
        "| 실행 수 | 48 | 48 | 48 | 48 | 192 |", "| 실행 수 | 48 | 48 | 48 | 48 | 192 |  "
    )
    assert rp.update_text(spaced, rp.sections(_dev16())) != ""
    # 버려도 된다고 하면 갱신한다
    assert rp.main([*args, "--update", str(out), "--discard-auto-edits"]) == 0
    assert "(규칙 문제였다)" not in out.read_text(encoding="utf-8")


def test_out_does_not_overwrite_an_existing_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "report.md"
    out.write_text("사람이 쓴 글", encoding="utf-8")
    assert rp.main([str(DEV16), "--out", str(out)]) == 2
    assert "--update" in capsys.readouterr().err
    assert out.read_text(encoding="utf-8") == "사람이 쓴 글"
    assert rp.main([str(DEV16), "--out", str(out), "--overwrite", "--title", "새 제목"]) == 0
    assert out.read_text(encoding="utf-8").startswith("# 새 제목\n")


def test_stdout_and_missing_input(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert rp.main([str(OLD), "--date", "2026-11-27"]) == 0
    assert capsys.readouterr().out.startswith("# 최종 비교 보고서\n")
    assert rp.main([str(tmp_path / "없는파일.csv")]) == 2
    assert "오류" in capsys.readouterr().err


def test_limits_point_out_short_repeats_fake_rows_and_missing_scores() -> None:
    rows = [_row(model_version="fake-echo"), _row("E002", recall="1.00", cond="C", split="test")]
    text = "\n".join(rp.limit_lines(_report(rows)))
    assert "최소 1회 — **설계서 §8의 최소 3회에 못 미친다**" in text
    assert "사례 수: 2개 (dev 1, test 1)" in text and "`test` 사례는 들어 있지 않다" not in text
    assert "fake 프로바이더 결과 1회" in text
    assert "`RETRIEVAL_TOP_K`" in text and "사람 채점 전이다" in text
    assert "비용은 내지 않았다" in "\n".join(rp.limit_lines(_report(rows, prices=None)))
