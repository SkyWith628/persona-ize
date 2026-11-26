"""평가 집계(`evals/summarize.py`): 결과 CSV → 조건별 표. 모델도 DB도 쓰지 않는다 — 손으로 만든 줄로 본다."""

from __future__ import annotations

import csv
import statistics
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evals"))

import summarize as sm  # noqa: E402

PRICES = {"claude-opus-5-5": (4.0, 20.0)}
DEV16 = ROOT / "evals" / "results" / "eval-20261124-dev16-abcd-repeat3.csv"


def _row(case: str = "E001", cond: str = "A", repeat: int = 1, **over: object) -> dict[str, str]:
    row: dict[str, object] = {
        "case_id": case,
        "condition": cond,
        "repeat": repeat,
        "status": "recommended",
        "attempts": 1,
        "latency_ms": 1000,
        "input_tokens": 100,
        "output_tokens": 50,
        "model_version": "claude-opus-5-5",
        "recall": "",
        "fm11_first_checks": "",
        "fm13_attempts": 1,
        "fm23_length_ratio": "1.00",
        "fm24_no_examples": False,
        "fm25_evidence_unused": False,
        "fm32_register_warned": False,
        "fm33_fallback": False,
    }
    row.update(over)
    return {k: str(v) for k, v in row.items()}


def _write(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def _targets(summary: sm.Summary) -> dict[str, list[str]]:
    return {r[0].split(" (")[0]: r for r in sm.target_rows(summary)}


@pytest.mark.parametrize(
    ("values", "p50", "p95"),
    [
        ([700], 700, 700),  # 1건: 그 값
        ([1000, 2000], 1500, 1950),  # 2건: 보간
        ([500, 500, 500], 500, 500),  # 동률
        ([3, 1, 2, 2], 2, 2.85),  # 정렬되지 않은 입력, 가운데 동률
        (list(range(1, 101)), 50.5, 95.05),
    ],
)
def test_percentile_boundaries(values: list[float], p50: float, p95: float) -> None:
    assert sm.percentile(values, 0.50) == pytest.approx(p50)
    assert sm.percentile(values, 0.95) == pytest.approx(p95)
    assert sm.percentile(values, 0.50) == pytest.approx(statistics.median(values))
    assert sm.percentile(values, 1.0) == max(values)
    assert sm.percentile([], 0.95) is None


def test_counts_rates_tokens_and_latency_per_condition() -> None:
    rows = [
        _row(repeat=1, latency_ms=1000),
        _row(repeat=2, latency_ms=3000, attempts=2, fm13_attempts=2, fm11_first_checks="fact_added"),
        _row(
            repeat=3,
            latency_ms=2000,
            attempts=2,
            fm13_attempts=2,
            status="fallback_original",
            fm33_fallback=True,
            fm11_first_checks="fact_added;preserve_missing",
        ),
        _row(cond="C", recall="0.50", fm32_register_warned=True),
        _row(cond="C", repeat=2, recall="1.00"),
        _row(cond="C", repeat=3, recall=""),  # 정답이 없는 사례는 평균에서 빠진다
    ]
    summary = sm.build_summary(rows, prices=PRICES)
    a, c = summary.by_condition("A"), summary.by_condition("C")
    assert a is not None and c is not None
    assert (a.runs, a.recommended, a.fallback, a.regenerated, a.model_calls) == (3, 2, 1, 2, 5)
    assert a.pass_rate == pytest.approx(2 / 3)
    assert (a.latency_p50, a.latency_max) == (2000, 3000)
    assert a.latency_p95 == pytest.approx(2900)
    assert (a.input_tokens, a.output_tokens) == (300, 150)
    assert a.cost_usd == pytest.approx((300 * 4 + 150 * 20) / 1_000_000)
    assert a.recall_mean is None and a.recall_n == 0
    assert a.signals["fm11_first_checks"] == 2 and a.signals["fm13_attempts"] == 2
    assert a.signals["fm33_fallback"] == 1
    assert a.first_checks == {"fact_added": 2, "preserve_missing": 1}
    assert (c.recall_n, c.recall_mean) == (2, pytest.approx(0.75))
    assert c.signals["fm32_register_warned"] == 1
    assert summary.total.runs == 6 and summary.total.pass_rate == pytest.approx(5 / 6)
    assert summary.warnings == []

    text = sm.render_markdown(summary)
    assert "| 추천 / 원문 유지 | 2 / 1 | 3 / 0 | 5 / 1 |" in text
    assert "| 지연 p95 | 2.9초 | 1.0초 |" in text
    assert "| 입력 토큰 합 (실행당) | 300 (100) |" in text
    assert "어림값" in text and sm.PRICE_SOURCE in text


def test_targets_mark_missed_and_unmeasured() -> None:
    rows = [_row(latency_ms=9000), _row(repeat=2, status="fallback_original")]
    rows += [_row(cond="C", repeat=i, recall="0.50") for i in (1, 2)]
    by_label = _targets(sm.build_summary(rows, prices=PRICES))
    assert by_label["핵심 사실 보존"][3] == "**미달** (A, 전체)"
    assert by_label["Recall@5"][2:] == ["C 0.50", "**미달** (C)"]
    assert by_label["지연 p95"][3] == "**미달** (A)"  # 전체 p95는 7.8초(보간)
    assert by_label["스타일 블라인드 5점"][2:] == [sm.NOT_SCORED, "미측정"]
    assert by_label["C가 A보다 선호"][3] == "미측정"
    # 목표값은 한 곳(TARGETS)에서 읽는다
    assert by_label["핵심 사실 보존"][1] == "98% 이상" and by_label["지연 p95"][1] == "8초 이하"

    ok = sm.target_rows(sm.build_summary([_row(cond="C", recall="0.80", latency_ms=8000)], prices=PRICES))
    assert [r[3] for r in ok] == ["달성", "미측정", "미측정", "달성", "달성"]  # 경계값은 달성


def test_empty_input_and_condition_without_rows() -> None:
    empty = sm.build_summary([], prices=PRICES)
    assert empty.conditions == [] and empty.total.runs == 0 and empty.total.pass_rate is None
    assert "결과 줄이 없다" in sm.render_markdown(empty)

    summary = sm.build_summary([_row()], prices=PRICES, conditions=["A", "D"])
    d = summary.by_condition("D")
    assert d is not None and d.runs == 0
    assert (d.pass_rate, d.latency_p95, d.recall_mean, d.cost_usd) == (None, None, None, None)
    text = sm.render_markdown(summary, by_case=True)
    assert "| 실행 수 | 1 | 0 | 1 |" in text
    assert "| 추천 비율 (규칙 통과) | 100.0% | — | 100.0% |" in text
    assert "| E001 | 1/1 | — |" in text
    rows = {r["condition"]: r for r in sm.condition_csv_rows(summary)}
    assert rows["D"]["pass_rate"] == "" and rows["D"]["latency_p95_ms"] == ""


def test_old_result_files_without_signal_columns_show_dashes() -> None:
    old = [{"case_id": "E001", "condition": "A", "status": "recommended", "latency_ms": "10"}]
    summary = sm.build_summary(old, prices=PRICES)
    a = summary.conditions[0]
    assert a.regenerated is None and a.model_calls is None
    assert all(v is None for v in a.signals.values())
    assert "| fm33 원문 유지 | — |" in sm.render_markdown(summary)


def test_fake_provider_rows_get_no_cost_and_are_not_compared_to_targets() -> None:
    rows = [_row(model_version="fake-echo", latency_ms=0), _row(cond="B")]
    summary = sm.build_summary(rows, prices=PRICES)
    a, b = summary.by_condition("A"), summary.by_condition("B")
    assert a is not None and b is not None
    assert a.cost_usd is None and a.fake_runs == 1 and not a.measured
    assert b.cost_usd == pytest.approx((100 * 4 + 50 * 20) / 1_000_000)
    assert summary.total.cost_usd is None  # fake가 섞인 합계는 내지 않는다
    assert any("fake" in w for w in summary.warnings)
    text = sm.render_markdown(summary)
    assert "| 비용 어림 (USD) | — (fake) | $0.00 | — (fake) |" in text
    targets = _targets(summary)
    assert targets["핵심 사실 보존"][2] == "B 100.0%"  # fake 조건과 전체는 견주지 않는다
    assert targets["지연 p95"][2] == "B 1.0초"

    only_fake = _targets(sm.build_summary([_row(model_version="fake-echo")], prices=PRICES))
    assert only_fake["핵심 사실 보존"][2:] == [sm.DASH, "미측정"]
    assert only_fake["지연 p95"][2:] == [sm.DASH, "미측정"]


def test_cost_needs_a_price_and_can_be_turned_off() -> None:
    rows = [
        _row(model_version="claude-new-9"),
        _row(repeat=2, model_version="unknown", input_tokens=0, output_tokens=0),
    ]
    summary = sm.build_summary(rows, prices=PRICES)
    assert summary.conditions[0].cost_usd is None
    assert summary.conditions[0].unpriced_models == ["claude-new-9"]  # 토큰 0인 줄은 단가가 없어도 된다
    assert any("claude-new-9" in w for w in summary.warnings)

    priced = sm.build_summary(rows, prices=sm.parse_prices(["claude-new-9=1:2"], PRICES))
    assert priced.conditions[0].cost_usd == pytest.approx((100 * 1 + 50 * 2) / 1_000_000)
    with pytest.raises(ValueError, match="--price"):
        sm.parse_prices(["claude-new-9"], PRICES)

    off = sm.build_summary(rows, prices=None)
    assert off.conditions[0].cost_usd is None and off.warnings == []
    assert "어림값" not in sm.render_markdown(off)


def test_duplicate_runs_across_files_are_flagged() -> None:
    summary = sm.build_summary([_row(), _row()], prices=PRICES)
    assert summary.total.runs == 2
    assert any("두 번 이상" in w for w in summary.warnings)


def test_by_case_view_finds_cases_where_d_beats_c() -> None:
    rows = []
    for rep in (1, 2):
        d_status = "fallback_original" if rep == 1 else "recommended"
        rows += [
            _row("E018", "C", rep, recall="0.25"),
            _row("E018", "D", rep, recall="0.50"),
            _row("E019", "C", rep, recall="0.50"),
            _row("E019", "D", rep, recall="0.50"),
            _row("E020", "C", rep, recall="1.00"),
            _row("E020", "D", rep, recall="1.00", status=d_status),
            _row("E001", "C", rep),
            _row("E001", "D", rep),
        ]
    summary = sm.build_summary(rows, prices=PRICES)
    assert sm.better_cases(summary.cases, "D", "C") == ["E018"]
    assert sm.better_cases(summary.cases, "C", "D") == []
    assert sm.better_cases(summary.cases, "C", "D", key="recommended") == ["E020"]
    text = sm.render_markdown(summary, by_case=True)
    assert "| E018 | 2/2 · 0.25 | 2/2 · 0.50 |" in text
    assert "| E020 | 2/2 · 1.00 | 1/2 · 1.00 |" in text
    assert "D의 recall이 C보다 높은 사례: E018" in text
    assert "사례별" not in sm.render_markdown(summary)


def test_human_scores_from_the_merged_sheet() -> None:
    """`make_blind_sheet.py merge`의 긴 형식: 시트 열 + condition + statuses. 숫자 열만 점수다."""
    from app.tools.blind_sheet import merge_scores

    def scored(item: str, case: str, formal: str, natural: str, memo: str = "") -> dict[str, str]:
        return {
            "item_id": item,
            "case_id": case,
            "격식": formal,
            "자연스러움": natural,
            "사실_의심(Y/N)": "N" if formal else "",
            "메모": memo,
        }

    sheet = [
        scored("a1", "E001", "5", "4"),
        scored("b2", "E001", "3", "2", "어색"),
        scored("c3", "E002", "", ""),
    ]
    key = [
        {"item_id": "a1", "case_id": "E001", "conditions": "A;C", "statuses": "recommended"},
        {"item_id": "b2", "case_id": "E001", "conditions": "ORIGINAL", "statuses": "original"},
        {"item_id": "c3", "case_id": "E002", "conditions": "A", "statuses": "recommended"},
    ]
    long_rows, _ = merge_scores(sheet, key)
    scores = sm.summarize_scores(long_rows)
    assert scores is not None
    assert scores.columns == ["격식", "자연스러움"]
    assert scores.overall == {
        "A": pytest.approx(4.5),
        "C": pytest.approx(4.5),
        "ORIGINAL": pytest.approx(2.5),
    }
    assert scores.items == {"A": 1, "C": 1, "ORIGINAL": 1}  # 채점하지 않은 줄은 세지 않는다

    one = sm.summarize_scores(long_rows, ["자연스러움"])
    assert one is not None and one.overall["A"] == pytest.approx(4.0)
    with pytest.raises(ValueError, match="없는 열"):
        sm.summarize_scores(long_rows, ["나다움"])
    with pytest.raises(ValueError, match="condition"):
        sm.summarize_scores(sheet)

    summary = sm.build_summary([_row(), _row(cond="C")], prices=PRICES, score_rows=long_rows)
    style = next(r for r in sm.target_rows(summary) if r[0].startswith("스타일"))
    assert style[2] == "A 4.50 / C 4.50 / ORIGINAL 2.50" and style[3] == "달성"  # 원문 기준선은 판정에서 뺀다
    assert "| A | 1 | 5.00 | 4.00 | 4.50 |" in sm.render_markdown(summary)
    assert {r["condition"]: r["human_score_mean"] for r in sm.condition_csv_rows(summary)}["C"] == "4.50"


def test_unscored_sheet_and_missing_preference_stay_not_scored() -> None:
    blank = [{"item_id": "a1", "case_id": "E001", "격식": "", "condition": "A", "statuses": "recommended"}]
    assert sm.summarize_scores(blank) is None
    assert sm.summarize_scores([]) is None
    summary = sm.build_summary([_row()], prices=PRICES, score_rows=blank)
    assert summary.scores is None
    assert sm.NOT_SCORED in sm.render_markdown(summary)
    with pytest.raises(NotImplementedError):  # 쌍대 선호는 형식이 정해질 때까지 읽지 않는다
        sm.load_preference(Path("없는-파일.csv"))

    with_pref = sm.build_summary([_row()], prices=PRICES, preference=sm.Preference(preferred=7, total=10))
    pref = next(r for r in sm.target_rows(with_pref) if r[0].startswith("C가 A보다"))
    assert pref[2:] == ["70.0% (7/10)", "달성"]


def test_main_writes_markdown_and_both_csvs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    first = _write(tmp_path / "run1.csv", [_row(cond="C", recall="0.50"), _row(cond="D", recall="1.00")])
    second = _write(tmp_path / "run2.csv", [_row(cond="C", repeat=2, recall="0.50")])
    out, cond_csv, case_csv = tmp_path / "표.md", tmp_path / "cond.csv", tmp_path / "case.csv"
    args = [str(first), str(second), "--by-case", "--out", str(out)]
    assert sm.main([*args, "--csv", str(cond_csv), "--case-csv", str(case_csv)]) == 0
    assert "`run1.csv`, `run2.csv`" in out.read_text(encoding="utf-8")
    cond = {r["condition"]: r for r in sm.read_rows(cond_csv)}
    assert list(cond) == ["C", "D", "ALL"] and cond["C"]["runs"] == "2" and cond["ALL"]["recall_n"] == "3"
    assert list(sm.read_rows(case_csv)[0]) == sm.CASE_FIELDS

    capsys.readouterr()
    assert sm.main([str(first), "--no-cost", "--conditions", "D"]) == 0
    printed = capsys.readouterr().out
    assert "|  | D |" in printed and "| 비용 어림 (USD) | — |" in printed
    assert sm.main([str(tmp_path / "없다.csv")]) == 2
    assert sm.main([str(first), "--price", "틀린값"]) == 2


@pytest.mark.skipif(not DEV16.exists(), reason="11/24 결과 파일이 없다")
def test_reproduces_the_hand_written_2026_11_24_report() -> None:
    """`evals/results/2026-11-24-dev16-repeat3.md`의 표와 같은 수가 나온다."""
    summary = sm.build_summary(sm.read_rows(DEV16), prices=sm.DEFAULT_PRICES)
    got = {s.condition: s for s in summary.conditions}
    assert {c: (s.recommended, s.fallback) for c, s in got.items()} == {
        "A": (48, 0),
        "B": (48, 0),
        "C": (48, 0),
        "D": (46, 2),
    }
    assert {c: s.model_calls for c, s in got.items()} == {"A": 49, "B": 48, "C": 49, "D": 51}
    assert {c: (s.input_tokens, s.output_tokens) for c, s in got.items()} == {
        "A": (15473, 11912),
        "B": (16806, 11473),
        "C": (29540, 15487),
        "D": (47887, 21527),
    }
    seconds = {c: (round(s.latency_p50 / 1000, 1), round(s.latency_max / 1000, 1)) for c, s in got.items()}
    assert seconds == {"A": (3.2, 6.8), "B": (3.0, 5.0), "C": (3.9, 8.4), "D": (5.0, 13.2)}
    assert (got["C"].recall_n, round(got["C"].recall_mean, 2)) == (36, 0.90)
    assert (got["D"].recall_n, round(got["D"].recall_mean, 2)) == (36, 0.92)
    total = summary.total
    assert (total.runs, total.recommended, total.fallback, total.regenerated) == (192, 190, 2, 5)
    assert sm.better_cases(summary.cases, "D", "C") == ["E018"]
