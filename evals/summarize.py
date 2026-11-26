"""`run_eval.py` 결과 CSV를 조건별로 집계해 비교표(마크다운·CSV)를 만든다. 모델 호출 없음.

사용 예:
    python evals/summarize.py evals/results/eval-20261124-dev16-abcd-repeat3.csv
    python evals/summarize.py 결과1.csv 결과2.csv --by-case --csv 조건별.csv --case-csv 사례별.csv
    python evals/summarize.py 결과.csv --scores evals/blind/<이름>-merged.csv --out 비교표.md
    python evals/summarize.py 결과.csv --price claude-opus-5-5=4:20      # 단가(USD/100만 토큰) 덮어쓰기
    python evals/summarize.py 결과.csv --no-cost

읽는 열은 `run_eval.py`가 쓰는 이름 그대로다(`condition`·`status`·`attempts`·`latency_ms`·`input_tokens`·
`output_tokens`·`model_version`·`recall`·`context_recall`·`fm*`). 예전 결과 파일처럼 열이 없으면 "—"다.
파일을 여럿 주면 줄을 그대로 합친다 — 같은 (사례, 조건, 회차)가 두 번 들어오면 경고한다(재확인 실행을 본
실행에 합치면 실행 수가 부풀려진다).

계산
- 추천 비율 = `status == recommended` ÷ 실행 수. 설계서 §8의 "핵심 사실 보존"과 나란히 적지만 **보존 규칙을
  통과했다는 뜻**이지 사람이 확인한 보존율이 아니다. 재생성 = `attempts > 1`인 실행, 모델 호출 수는
  `attempts`의 합이다.
- 지연 백분위는 선형 보간(정렬한 값의 (n-1)·p 번째 자리, numpy 기본과 같다): 1건이면 그 값, p50은
  `statistics.median`과 같다. 지연에는 재생성 시간이 들어 있다.
- recall은 값이 있는 줄만 평균낸다(정답 `relevant_msg_ids`가 없는 사례와 A/B는 빈칸).
- 비용 = 토큰 × 단가이고 **어림값**이다(캐시·재시도 과금, Console 청구액과 다를 수 있다). fake 프로바이더
  줄은 토큰이 글자 수 기반이라 비용을 내지 않는다. fake 줄이 섞인 조건은 추천 비율·지연도 목표와 견주지 않는다
  (fake는 원문을 그대로 돌려줘 항상 통과한다).

사람 점수
- `--scores`는 `scripts/make_blind_sheet.py merge`가 쓰는 긴 형식(`<이름>-merged.csv`: 시트 열 + `condition`
  + `statuses`)을 읽는다. 점수 열은 숫자로만 채워진 루브릭 열이다(`--score-columns`로 고를 수 있다). 루브릭이
  아직 v0이라 어느 열이 "스타일 5점"인지 정해지지 않았다 — 평균은 고른 열 전체의 평균이고 표에 열 이름을
  적는다.
- "C가 A보다 선호"(쌍대 선호)는 기록 형식이 아직 없다(`merge` 출력에 선호 열이 없다). `Preference`를 받는
  자리만 있고 읽는 코드는 없다 — 형식이 정해지면 `load_preference`를 채운다.
"""

from __future__ import annotations

import argparse
import csv
import io
import math
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.admin_service import estimate_token_cost  # noqa: E402
from app.tools.blind_sheet import SHEET_FIXED_COLUMNS  # noqa: E402

# 설계서(docs/design/project-design.md) §8 지표 표의 초기 목표. 실측 뒤 조정될 수 있다 — 고칠 곳은 여기뿐이다.
TARGETS = {
    "fact_preservation": 0.98,  # 핵심 사실 보존 (이상)
    "style_score": 4.0,  # 블라인드 5점 평균 (이상)
    "c_over_a": 0.60,  # C가 A보다 선호되는 비율 (이상)
    "recall_at_5": 0.80,  # Recall@5 (이상)
    "latency_p95_ms": 8000,  # 지연 p95 (이하)
}

# 기본 단가: USD / 100만 토큰 (입력, 출력). 출처는 저장소의 `.env.example` 주석(2026-11-11 기준, Console
# 요금표에서 옮긴 값 — 관리자 화면의 `COST_REWRITE_*_PER_MTOK`과 같은 수). 요금이 바뀌면 `--price`로 덮어쓴다.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
}
PRICE_SOURCE = ".env.example 주석, 2026-11-11 기준"

FAKE_PREFIX = "fake"
FLAG_SIGNALS = ["fm24_no_examples", "fm25_evidence_unused", "fm32_register_warned", "fm33_fallback"]
# 점수 열에서 빼는 것: 시트의 고정 열과 merge가 붙이는 열
NON_SCORE_COLUMNS = {*SHEET_FIXED_COLUMNS, "condition", "statuses"}
DASH = "—"
NOT_SCORED = "채점 전"


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fp:
        return [{k: (v or "") for k, v in row.items() if k is not None} for row in csv.DictReader(fp)]


def percentile(values: list[float], p: float) -> float | None:
    """선형 보간 백분위(p는 0~1). 빈 목록은 None, 1건이면 그 값."""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * p
    low = math.floor(pos)
    high = math.ceil(pos)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _int(value: str) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def is_fake(row: dict[str, str]) -> bool:
    return row.get("model_version", "").startswith(FAKE_PREFIX)


@dataclass
class ConditionStats:
    condition: str
    runs: int = 0
    recommended: int = 0
    fallback: int = 0
    other_status: int = 0
    regenerated: int | None = None  # attempts 열이 없으면 None
    model_calls: int | None = None
    latency_n: int = 0
    latency_p50: float | None = None
    latency_p95: float | None = None
    latency_max: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    recall_n: int = 0
    recall_mean: float | None = None
    context_recall_n: int = 0
    context_recall_mean: float | None = None
    cost_usd: float | None = None
    fake_runs: int = 0
    unpriced_models: list[str] = field(default_factory=list)
    # 실패 신호: 열이 없는 파일뿐이면 None
    signals: dict[str, int | None] = field(default_factory=dict)
    first_checks: Counter[str] = field(default_factory=Counter)
    length_ratio_mean: float | None = None

    @property
    def pass_rate(self) -> float | None:
        return self.recommended / self.runs if self.runs else None

    @property
    def measured(self) -> bool:
        """추천 비율·지연을 목표와 견줄 수 있는가 (fake 줄이 섞이면 아니다)."""
        return self.runs > 0 and self.fake_runs == 0


def summarize_rows(
    condition: str, rows: list[dict[str, str]], prices: dict[str, tuple[float, float]] | None
) -> ConditionStats:
    s = ConditionStats(condition=condition, runs=len(rows))
    statuses = Counter(r.get("status", "") for r in rows)
    s.recommended = statuses["recommended"]
    s.fallback = statuses["fallback_original"]
    s.other_status = s.runs - s.recommended - s.fallback

    attempts = [a for r in rows if (a := _int(r.get("attempts", ""))) is not None]
    if attempts:
        s.regenerated = sum(1 for a in attempts if a > 1)
        s.model_calls = sum(attempts)

    latencies = [v for r in rows if (v := _float(r.get("latency_ms", ""))) is not None]
    s.latency_n = len(latencies)
    s.latency_p50 = percentile(latencies, 0.50)
    s.latency_p95 = percentile(latencies, 0.95)
    s.latency_max = max(latencies) if latencies else None

    s.input_tokens = sum(_int(r.get("input_tokens", "")) or 0 for r in rows)
    s.output_tokens = sum(_int(r.get("output_tokens", "")) or 0 for r in rows)

    recalls = [v for r in rows if (v := _float(r.get("recall", ""))) is not None]
    s.recall_n, s.recall_mean = len(recalls), _mean(recalls)
    ctx = [v for r in rows if (v := _float(r.get("context_recall", ""))) is not None]
    s.context_recall_n, s.context_recall_mean = len(ctx), _mean(ctx)

    s.fake_runs = sum(1 for r in rows if is_fake(r))
    if prices is not None and rows and s.fake_runs == 0:
        total = 0.0
        unpriced: set[str] = set()
        for r in rows:
            tin, tout = _int(r.get("input_tokens", "")) or 0, _int(r.get("output_tokens", "")) or 0
            if tin == 0 and tout == 0:
                continue  # 호출이 실패해 토큰이 없는 줄(model_version이 unknown일 수 있다)
            price = prices.get(r.get("model_version", ""))
            cost = estimate_token_cost(tin, tout, *price) if price else None
            if cost is None:
                unpriced.add(r.get("model_version", "") or "(빈칸)")
            else:
                total += cost
        s.unpriced_models = sorted(unpriced)
        s.cost_usd = None if unpriced else total

    for name in FLAG_SIGNALS:
        present = [r[name] for r in rows if name in r and r[name] != ""]
        s.signals[name] = sum(1 for v in present if v.strip().lower() == "true") if present else None
    if any("fm11_first_checks" in r for r in rows):
        for r in rows:
            for check in filter(None, (c.strip() for c in r.get("fm11_first_checks", "").split(";"))):
                s.first_checks[check] += 1
        s.signals["fm11_first_checks"] = sum(1 for r in rows if r.get("fm11_first_checks", "").strip())
    else:
        s.signals["fm11_first_checks"] = None
    fm13 = [a for r in rows if (a := _int(r.get("fm13_attempts", ""))) is not None]
    s.signals["fm13_attempts"] = sum(1 for a in fm13 if a > 1) if fm13 else None
    ratios = [v for r in rows if (v := _float(r.get("fm23_length_ratio", ""))) is not None]
    s.length_ratio_mean = _mean(ratios)
    return s


# ---------------------------------------------------------------------------
# 사람 점수
# ---------------------------------------------------------------------------


@dataclass
class HumanScores:
    """블라인드 채점 결과(조건별). `columns`는 점수로 읽은 루브릭 열."""

    columns: list[str]
    items: dict[str, int]  # 조건 → 채점된 항목 수
    means: dict[str, dict[str, float]]  # 조건 → 열 → 평균
    overall: dict[str, float]  # 조건 → 고른 열 전체의 평균


@dataclass
class Preference:
    """쌍대 선호(C 대 A). 기록 형식이 정해지면 `load_preference`가 만든다."""

    preferred: int  # C를 고른 수
    total: int  # 판정 수

    @property
    def rate(self) -> float | None:
        return self.preferred / self.total if self.total else None


def load_preference(path: Path) -> Preference:
    """쌍대 선호 기록을 읽는다 — 형식이 아직 없다(루브릭 v0, `make_blind_sheet.py merge`에 선호 열 없음)."""
    raise NotImplementedError("쌍대 선호 기록 형식이 정해지지 않았다 (evals/style_rubric.md v0)")


def _is_number(value: str) -> bool:
    return _float(value) is not None


def summarize_scores(rows: list[dict[str, str]], columns: list[str] | None = None) -> HumanScores | None:
    """`make_blind_sheet.py merge`의 긴 형식 줄 → 조건별 평균. 점수가 하나도 없으면 None(채점 전).

    `columns`를 주지 않으면 채워진 값이 모두 숫자인 루브릭 열을 점수로 본다(merge의 조건별 평균과 같은 기준).
    """
    if not rows:
        return None
    if "condition" not in rows[0]:
        raise ValueError(
            "점수 파일에 condition 열이 없다 — make_blind_sheet.py merge가 만든 *-merged.csv를 준다"
        )
    if columns is None:
        columns = []
        for col in rows[0]:
            if col in NON_SCORE_COLUMNS:
                continue
            values = [r[col] for r in rows if r.get(col, "").strip()]
            if values and all(_is_number(v) for v in values):
                columns.append(col)
    else:
        missing = [c for c in columns if c not in rows[0]]
        if missing:
            raise ValueError(f"점수 파일에 없는 열: {missing}")
    scores = HumanScores(columns=columns, items={}, means={}, overall={})
    for cond in sorted({r["condition"] for r in rows}):
        cond_rows = [r for r in rows if r["condition"] == cond]
        every: list[float] = []
        means: dict[str, float] = {}
        scored_items = 0
        for col in columns:
            vals = [v for r in cond_rows if (v := _float(r.get(col, ""))) is not None]
            if vals:
                means[col] = statistics.fmean(vals)
                every.extend(vals)
        for r in cond_rows:
            scored_items += any(_is_number(r.get(col, "")) for col in columns)
        if every:
            scores.items[cond] = scored_items
            scores.means[cond] = means
            scores.overall[cond] = statistics.fmean(every)
    return scores if scores.overall else None


# ---------------------------------------------------------------------------
# 전체 요약
# ---------------------------------------------------------------------------


@dataclass
class Summary:
    sources: list[str]
    conditions: list[ConditionStats]
    total: ConditionStats
    models: Counter[str]
    prompt_versions: list[str]
    validator_versions: list[str]
    warnings: list[str]
    prices: dict[str, tuple[float, float]] | None
    scores: HumanScores | None = None
    preference: Preference | None = None
    cases: list[dict[str, str]] = field(default_factory=list)

    def by_condition(self, condition: str) -> ConditionStats | None:
        return next((c for c in self.conditions if c.condition == condition), None)


def build_summary(
    rows: list[dict[str, str]],
    *,
    sources: list[str] | None = None,
    prices: dict[str, tuple[float, float]] | None = None,
    score_rows: list[dict[str, str]] | None = None,
    score_columns: list[str] | None = None,
    preference: Preference | None = None,
    conditions: list[str] | None = None,
) -> Summary:
    """prices=None이면 비용을 내지 않는다."""
    if conditions:
        rows = [r for r in rows if r.get("condition", "") in conditions]
    names = conditions or sorted({r.get("condition", "") for r in rows})
    stats = [summarize_rows(c, [r for r in rows if r.get("condition", "") == c], prices) for c in names]
    total = summarize_rows("전체", rows, prices)

    warnings: list[str] = []
    keys = Counter((r.get("case_id", ""), r.get("condition", ""), r.get("repeat", "")) for r in rows)
    dupes = sum(1 for n in keys.values() if n > 1)
    if dupes:
        warnings.append(
            f"같은 (사례, 조건, 회차)가 두 번 이상 들어온 것이 {dupes}개다 — "
            "여러 실행을 합쳤다면 실행 수가 부풀려진다."
        )
    fake = sum(1 for r in rows if is_fake(r))
    if fake:
        warnings.append(
            f"fake 프로바이더 결과 {fake}줄: 토큰이 글자 수 기반이라 비용을 내지 않고, "
            "추천 비율·지연도 품질 측정이 아니다(원문을 그대로 돌려준다)."
        )
    unpriced = sorted({m for s in stats for m in s.unpriced_models})
    if unpriced:
        warnings.append(f"단가를 모르는 모델 {unpriced}: 그 조건의 비용은 비웠다 (--price 모델=입력:출력).")
    other = total.other_status
    if other:
        warnings.append(
            f"recommended·fallback_original이 아닌 status {other}줄(추천 비율의 분모에는 들어간다)."
        )

    return Summary(
        sources=sources or [],
        conditions=stats,
        total=total,
        models=Counter(r.get("model_version", "") or "(빈칸)" for r in rows),
        prompt_versions=sorted({r["prompt_version"] for r in rows if r.get("prompt_version")}),
        validator_versions=sorted({r["validator_version"] for r in rows if r.get("validator_version")}),
        warnings=warnings,
        prices=prices,
        scores=summarize_scores(score_rows, score_columns) if score_rows is not None else None,
        preference=preference,
        cases=case_rows(rows, names),
    )


def case_rows(rows: list[dict[str, str]], conditions: list[str]) -> list[dict[str, str]]:
    """사례 × 조건 한 줄씩(긴 형식). 사례는 처음 나온 순서."""
    out: list[dict[str, str]] = []
    case_ids = list(dict.fromkeys(r.get("case_id", "") for r in rows))
    for cid in case_ids:
        for cond in conditions:
            group = [r for r in rows if r.get("case_id", "") == cid and r.get("condition", "") == cond]
            if not group:
                continue
            s = summarize_rows(cond, group, None)
            out.append(
                {
                    "case_id": cid,
                    "condition": cond,
                    "runs": str(s.runs),
                    "recommended": str(s.recommended),
                    "fallback": str(s.fallback),
                    "regenerated": "" if s.regenerated is None else str(s.regenerated),
                    "recall_mean": "" if s.recall_mean is None else f"{s.recall_mean:.2f}",
                    "context_recall_mean": ""
                    if s.context_recall_mean is None
                    else f"{s.context_recall_mean:.2f}",
                    "latency_p50_ms": "" if s.latency_p50 is None else f"{s.latency_p50:.0f}",
                }
            )
    return out


def better_cases(cases: list[dict[str, str]], better: str, than: str, key: str = "recall_mean") -> list[str]:
    """`better` 조건의 값이 `than` 조건보다 큰 사례 (둘 다 값이 있을 때만). 예: recall이 D > C인 사례."""
    table: dict[str, dict[str, float]] = {}
    for row in cases:
        value = _float(row.get(key, ""))
        if value is not None:
            table.setdefault(row["case_id"], {})[row["condition"]] = value
    return [cid for cid, v in table.items() if better in v and than in v and v[better] > v[than] + 1e-9]


# ---------------------------------------------------------------------------
# 출력
# ---------------------------------------------------------------------------


def _pct(value: float | None) -> str:
    return DASH if value is None else f"{value * 100:.1f}%"


def _sec(ms: float | None) -> str:
    return DASH if ms is None else f"{ms / 1000:.1f}초"


def _num(value: int | None) -> str:
    return DASH if value is None else f"{value:,}"


def _usd(value: float | None) -> str:
    return DASH if value is None else f"${value:.2f}"


def _table(header: list[str], body: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in body]
    return lines


def _cost_cell(s: ConditionStats, prices: dict | None) -> str:
    if prices is None:
        return DASH
    if s.fake_runs:
        return f"{DASH} (fake)"
    return _usd(s.cost_usd)


def _verdict(value: float | None, target: float, *, lower_is_better: bool = False) -> str:
    if value is None:
        return "미측정"
    ok = value <= target if lower_is_better else value >= target
    return "달성" if ok else "**미달**"


def target_rows(summary: Summary) -> list[list[str]]:
    """목표 대비 표의 줄: [지표, 목표, 값, 판정]."""
    out: list[list[str]] = []
    stats = [*summary.conditions, summary.total] if len(summary.conditions) > 1 else summary.conditions

    def per_condition(fmt, value_of, target: float, lower: bool = False, only_measured: bool = True):
        cells, verdicts = [], []
        for s in stats:
            value = value_of(s)
            if only_measured and not s.measured:
                value = None
            if value is None:
                continue
            cells.append(f"{s.condition} {fmt(value)}")
            if _verdict(value, target, lower_is_better=lower) != "달성":
                verdicts.append(s.condition)
        if not cells:
            return DASH, "미측정"
        return " / ".join(cells), (f"**미달** ({', '.join(verdicts)})" if verdicts else "달성")

    value, verdict = per_condition(_pct, lambda s: s.pass_rate, TARGETS["fact_preservation"])
    out.append(
        [
            "핵심 사실 보존 (보존 규칙 통과율 — 사람 확인 전)",
            f"{TARGETS['fact_preservation']:.0%} 이상",
            value,
            verdict,
        ]
    )

    if summary.scores:
        sc = summary.scores
        cells = [f"{c} {v:.2f}" for c, v in sc.overall.items()]
        missed = [c for c, v in sc.overall.items() if c != "ORIGINAL" and v < TARGETS["style_score"]]
        out.append(
            [
                f"스타일 블라인드 5점 (열: {', '.join(sc.columns)})",
                f"평균 {TARGETS['style_score']:g} 이상",
                " / ".join(cells),
                f"**미달** ({', '.join(missed)})" if missed else "달성",
            ]
        )
    else:
        out.append(["스타일 블라인드 5점", f"평균 {TARGETS['style_score']:g} 이상", NOT_SCORED, "미측정"])

    pref = summary.preference
    if pref and pref.rate is not None:
        out.append(
            [
                "C가 A보다 선호",
                f"{TARGETS['c_over_a']:.0%} 이상",
                f"{_pct(pref.rate)} ({pref.preferred}/{pref.total})",
                _verdict(pref.rate, TARGETS["c_over_a"]),
            ]
        )
    else:
        out.append(
            ["C가 A보다 선호", f"{TARGETS['c_over_a']:.0%} 이상", f"{NOT_SCORED} (기록 형식 미정)", "미측정"]
        )

    retrieval = [s for s in summary.conditions if s.recall_mean is not None]
    if retrieval:
        missed = [s.condition for s in retrieval if s.recall_mean < TARGETS["recall_at_5"]]
        out.append(
            [
                "Recall@5 (`recall` 열 평균, 검색 k는 실행 설정)",
                f"{TARGETS['recall_at_5']:g} 이상",
                " / ".join(f"{s.condition} {s.recall_mean:.2f}" for s in retrieval),
                f"**미달** ({', '.join(missed)})" if missed else "달성",
            ]
        )
    else:
        out.append(
            [
                "Recall@5 (`recall` 열 평균, 검색 k는 실행 설정)",
                f"{TARGETS['recall_at_5']:g} 이상",
                DASH,
                "미측정",
            ]
        )

    value, verdict = per_condition(_sec, lambda s: s.latency_p95, TARGETS["latency_p95_ms"], lower=True)
    out.append(["지연 p95", f"{TARGETS['latency_p95_ms'] / 1000:g}초 이하", value, verdict])
    return out


def render_markdown(summary: Summary, *, by_case: bool = False) -> str:
    stats = summary.conditions
    cols = [*stats, summary.total] if len(stats) > 1 else stats
    lines: list[str] = ["# 평가 집계", ""]
    if summary.sources:
        lines.append("- 입력: " + ", ".join(f"`{s}`" for s in summary.sources))
    lines.append(
        f"- 실행 {summary.total.runs}회 · 모델: "
        + (", ".join(f"{m} {n}회" for m, n in sorted(summary.models.items())) or DASH)
    )
    if summary.prompt_versions or summary.validator_versions:
        lines.append(
            f"- 프롬프트: {', '.join(summary.prompt_versions) or DASH} · "
            f"검증 규칙: {', '.join(summary.validator_versions) or DASH}"
        )
    for w in summary.warnings:
        lines.append(f"- 주의: {w}")
    if not stats:
        lines += ["", "결과 줄이 없다."]
        return "\n".join(lines) + "\n"

    lines += ["", "## 목표 대비 (설계서 §8의 초기 목표)", ""]
    lines += _table(["지표", "목표", "측정값", "판정"], target_rows(summary))

    lines += ["", "## 조건별", ""]

    def row(label: str, cell) -> list[str]:
        return [label, *[cell(s) for s in cols]]

    def per_run(total: int, s: ConditionStats) -> str:
        return f"{total / s.runs:,.0f}" if s.runs else DASH

    body = [
        row("실행 수", lambda s: str(s.runs)),
        row("추천 / 원문 유지", lambda s: f"{s.recommended} / {s.fallback}"),
        row("추천 비율 (규칙 통과)", lambda s: _pct(s.pass_rate)),
        row("재생성한 실행", lambda s: _num(s.regenerated)),
        row("모델 호출 수 (재생성 포함)", lambda s: _num(s.model_calls)),
        row("지연 p50", lambda s: _sec(s.latency_p50)),
        row("지연 p95", lambda s: _sec(s.latency_p95)),
        row("지연 최대", lambda s: _sec(s.latency_max)),
        row("입력 토큰 합 (실행당)", lambda s: f"{s.input_tokens:,} ({per_run(s.input_tokens, s)})"),
        row("출력 토큰 합 (실행당)", lambda s: f"{s.output_tokens:,} ({per_run(s.output_tokens, s)})"),
        row(
            "recall 평균 (값 있는 실행 수)",
            lambda s: DASH if s.recall_mean is None else f"{s.recall_mean:.2f} ({s.recall_n})",
        ),
        row(
            "context_recall 평균 (값 있는 실행 수)",
            lambda s: (
                DASH
                if s.context_recall_mean is None
                else f"{s.context_recall_mean:.2f} ({s.context_recall_n})"
            ),
        ),
        row("비용 어림 (USD)", lambda s: _cost_cell(s, summary.prices)),
        row(
            "실행당 비용 어림 (USD)",
            lambda s: DASH if s.cost_usd is None or not s.runs else f"${s.cost_usd / s.runs:.4f}",
        ),
    ]
    lines += _table(["", *[s.condition for s in cols]], body)
    if summary.prices is not None:
        used = sorted(m for m in summary.models if m in summary.prices)
        price_text = ", ".join(
            f"{m} 입력 ${summary.prices[m][0]:g} / 출력 ${summary.prices[m][1]:g}" for m in used
        )
        lines += [
            "",
            f"비용은 **어림값**이다: 토큰 × 단가(100만 토큰당 {price_text or '해당 모델 없음'}; "
            f"기본 단가 출처 {PRICE_SOURCE}). "
            "캐시·그래프 수집 비용은 들어 있지 않고 Console 청구액과 대조하지 않았다.",
        ]

    lines += ["", "## 실패 신호 (`fm*` 열 — 참고 신호이고 실패 판정이 아니다)", ""]
    signal_labels = [
        ("fm11_first_checks", "fm11 1차 시도가 검증에 걸림"),
        ("fm13_attempts", "fm13 시도 2회 이상"),
        ("fm24_no_examples", "fm24 문체 예시 없음 (C·D)"),
        ("fm25_evidence_unused", "fm25 근거를 적지 않음"),
        ("fm32_register_warned", "fm32 말투 경고"),
        ("fm33_fallback", "fm33 원문 유지"),
    ]
    body = [row(label, lambda s, n=name: _num(s.signals.get(n))) for name, label in signal_labels]
    body.append(
        row(
            "fm23 길이 비율 평균",
            lambda s: DASH if s.length_ratio_mean is None else f"{s.length_ratio_mean:.2f}",
        )
    )
    for check in sorted(summary.total.first_checks):
        body.append(row(f"fm11 종류: `{check}`", lambda s, c=check: str(s.first_checks.get(c, 0))))
    lines += _table(["", *[s.condition for s in cols]], body)

    lines += ["", "## 사람 점수 (블라인드)", ""]
    if summary.scores:
        sc = summary.scores
        header = ["조건", "채점 항목 수", *sc.columns, "평균"]
        body = [
            [
                c,
                str(sc.items[c]),
                *[f"{sc.means[c][col]:.2f}" if col in sc.means[c] else DASH for col in sc.columns],
            ]
            + [f"{sc.overall[c]:.2f}"]
            for c in sc.overall
        ]
        lines += _table(header, body)
        lines += [
            "",
            "평균은 위 열 전체의 평균이다 — 루브릭 v0이라 어느 열이 스타일 점수인지는 아직 정해지지 않았다.",
        ]
    else:
        lines.append(
            f"{NOT_SCORED} — `scripts/make_blind_sheet.py merge`의 `*-merged.csv`를 `--scores`로 준다."
        )

    if by_case:
        lines += ["", "## 사례별 (추천 수/실행 수 · recall 평균)", ""]
        names = [s.condition for s in stats]
        table: dict[str, dict[str, dict[str, str]]] = {}
        for c in summary.cases:
            table.setdefault(c["case_id"], {})[c["condition"]] = c
        body = []
        for cid, per in table.items():
            cells = []
            for name in names:
                c = per.get(name)
                if c is None:
                    cells.append(DASH)
                    continue
                cell = f"{c['recommended']}/{c['runs']}"
                if c["recall_mean"]:
                    cell += f" · {c['recall_mean']}"
                cells.append(cell)
            body.append([cid, *cells])
        lines += _table(["사례", *names], body)
        if "C" in names and "D" in names:
            lines += [
                "",
                "- D의 recall이 C보다 높은 사례: "
                + (", ".join(better_cases(summary.cases, "D", "C")) or "없음"),
                "- C의 recall이 D보다 높은 사례: "
                + (", ".join(better_cases(summary.cases, "C", "D")) or "없음"),
                "- D의 추천 수가 C보다 적은 사례: "
                + (", ".join(better_cases(summary.cases, "C", "D", key="recommended")) or "없음"),
            ]
    return "\n".join(lines) + "\n"


CSV_FIELDS = [
    "condition",
    "runs",
    "recommended",
    "fallback_original",
    "other_status",
    "pass_rate",
    "regenerated",
    "model_calls",
    "latency_p50_ms",
    "latency_p95_ms",
    "latency_max_ms",
    "input_tokens",
    "output_tokens",
    "input_tokens_per_run",
    "output_tokens_per_run",
    "recall_mean",
    "recall_n",
    "context_recall_mean",
    "context_recall_n",
    "cost_usd_estimate",
    "fake_runs",
    "fm11_first_checks",
    "fm13_attempts",
    "fm23_length_ratio_mean",
    *FLAG_SIGNALS,
    "human_score_mean",
    "human_score_items",
]


def condition_csv_rows(summary: Summary) -> list[dict[str, str]]:
    def text(value, fmt: str = "{}") -> str:
        return "" if value is None else fmt.format(value)

    out = []
    for s in [*summary.conditions, summary.total]:
        score = summary.scores.overall.get(s.condition) if summary.scores else None
        out.append(
            {
                "condition": "ALL" if s is summary.total else s.condition,
                "runs": str(s.runs),
                "recommended": str(s.recommended),
                "fallback_original": str(s.fallback),
                "other_status": str(s.other_status),
                "pass_rate": text(s.pass_rate, "{:.4f}"),
                "regenerated": text(s.regenerated),
                "model_calls": text(s.model_calls),
                "latency_p50_ms": text(s.latency_p50, "{:.0f}"),
                "latency_p95_ms": text(s.latency_p95, "{:.0f}"),
                "latency_max_ms": text(s.latency_max, "{:.0f}"),
                "input_tokens": str(s.input_tokens),
                "output_tokens": str(s.output_tokens),
                "input_tokens_per_run": text(s.input_tokens / s.runs if s.runs else None, "{:.1f}"),
                "output_tokens_per_run": text(s.output_tokens / s.runs if s.runs else None, "{:.1f}"),
                "recall_mean": text(s.recall_mean, "{:.4f}"),
                "recall_n": str(s.recall_n),
                "context_recall_mean": text(s.context_recall_mean, "{:.4f}"),
                "context_recall_n": str(s.context_recall_n),
                "cost_usd_estimate": text(s.cost_usd, "{:.4f}"),
                "fake_runs": str(s.fake_runs),
                "fm11_first_checks": text(s.signals.get("fm11_first_checks")),
                "fm13_attempts": text(s.signals.get("fm13_attempts")),
                "fm23_length_ratio_mean": text(s.length_ratio_mean, "{:.2f}"),
                **{name: text(s.signals.get(name)) for name in FLAG_SIGNALS},
                "human_score_mean": text(score, "{:.2f}"),
                "human_score_items": text(summary.scores.items.get(s.condition) if summary.scores else None),
            }
        )
    return out


def write_csv(path: Path, rows: list[dict[str, str]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


CASE_FIELDS = [
    "case_id",
    "condition",
    "runs",
    "recommended",
    "fallback",
    "regenerated",
    "recall_mean",
    "context_recall_mean",
    "latency_p50_ms",
]


def parse_prices(specs: list[str], base: dict[str, tuple[float, float]]) -> dict[str, tuple[float, float]]:
    """`모델=입력:출력`(USD/100만 토큰)을 기본 표 위에 덮어쓴다."""
    prices = dict(base)
    for spec in specs:
        try:
            model, pair = spec.split("=", 1)
            tin, tout = pair.split(":", 1)
            prices[model.strip()] = (float(tin), float(tout))
        except ValueError as exc:
            raise ValueError(f"--price 형식은 모델=입력:출력 이다 (받은 값: {spec})") from exc
    return prices


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="run_eval 결과 CSV → 조건별 집계·비교표 (모델 호출 없음)")
    p.add_argument("results", type=Path, nargs="+", help="run_eval 요약 CSV (여러 개면 합친다)")
    p.add_argument("--conditions", default="", help="쉼표로 나눈 조건만 (기본: 파일에 있는 전부)")
    p.add_argument("--by-case", action="store_true", help="사례 × 조건 표와 C·D 비교를 덧붙인다")
    p.add_argument("--scores", type=Path, default=None, help="make_blind_sheet.py merge의 *-merged.csv")
    p.add_argument(
        "--score-columns", default="", help="점수로 읽을 루브릭 열, 쉼표 구분 (기본: 숫자 열 전부)"
    )
    p.add_argument(
        "--price",
        action="append",
        default=[],
        metavar="모델=입력:출력",
        help="단가(USD/100만 토큰)를 더하거나 덮어쓴다. 여러 번 줄 수 있다",
    )
    p.add_argument("--no-cost", action="store_true", help="비용을 내지 않는다")
    p.add_argument("--out", type=Path, default=None, help="마크다운을 이 파일에 쓴다 (기본: 표준 출력)")
    p.add_argument("--csv", type=Path, default=None, help="조건별 집계 CSV")
    p.add_argument("--case-csv", type=Path, default=None, help="사례 × 조건 CSV")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        prices = None if args.no_cost else parse_prices(args.price, DEFAULT_PRICES)
        rows: list[dict[str, str]] = []
        for path in args.results:
            rows += read_rows(path)
        summary = build_summary(
            rows,
            sources=[p.name for p in args.results],
            prices=prices,
            score_rows=read_rows(args.scores) if args.scores else None,
            score_columns=[c.strip() for c in args.score_columns.split(",") if c.strip()] or None,
            conditions=[c.strip() for c in args.conditions.split(",") if c.strip()] or None,
        )
    except (OSError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    text = render_markdown(summary, by_case=args.by_case)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"비교표 → {args.out}")
    else:
        if isinstance(sys.stdout, io.TextIOWrapper):
            sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔(cp949)에서 '—'가 깨지지 않게
        print(text, end="")
    if args.csv:
        write_csv(args.csv, condition_csv_rows(summary), CSV_FIELDS)
        print(f"조건별 CSV → {args.csv}", file=sys.stderr)
    if args.case_csv:
        write_csv(args.case_csv, summary.cases, CASE_FIELDS)
        print(f"사례별 CSV → {args.case_csv}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
