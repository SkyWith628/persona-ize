"""최종 비교 보고서의 뼈대: `run_eval.py` 결과 CSV → 수치·표가 채워진 마크다운. 모델 호출 없음.

사용 예:
    python evals/report.py evals/results/<결과>.csv --out 보고서.md
    python evals/report.py 본실행.csv 재확인.csv --replace --out 보고서.md   # 겹치는 실행은 뒤 파일 것으로
    python evals/report.py 결과.csv --scores evals/blind/<이름>-merged.csv --update 보고서.md
    python evals/report.py 결과.csv --show-text                              # 위반 문구·결과 문장도 싣는다

수치는 스크립트가 채우고 해석·판정·결론은 사람이 쓴다.

- **사람이 쓰는 자리**는 `> ✍`로 시작하는 인용 줄 하나로 통일한다(찾기: `✍`). 채우기 전에는
  `(사람이 쓴다 …)`가 들어 있다.
- **자동 구역**은 `<!-- auto:start 이름 sha=… -->` … `<!-- auto:end 이름 -->` 사이다. `--update 파일`은 그
  구역만 갈아 끼우고 구역 밖(사람이 쓴 글)은 한 글자도 건드리지 않는다. 구역 안을 손으로 고치면 다음 갱신 때
  지워지므로, `sha`(구역 내용의 지문)가 맞지 않으면 갱신을 거부한다(`--discard-auto-edits`로 넘긴다).
  표식이 깨졌거나(짝 없음·겹침·이름 중복) 구역 이름이 지금의 뼈대와 다르면 파일을 쓰지 않고 오류로 끝낸다.
- `--out`은 이미 있는 파일을 덮어쓰지 않는다(사람이 쓴 글을 지우지 않게) — 갱신은 `--update`,
  새로 만들려면 `--overwrite`.

집계는 `evals/summarize.py`의 것을 그대로 쓴다(목표 대비·조건별·`fm*`·사람 점수·사례별 표). 여기서 더하는
것은 ① 버전 섞임 점검 ② 단계별 차이(A→B, B→C, C→D) ③ D 채택 조건 네 가지의 근거 표 ④ 실패·재생성 실행
목록 ⑤ 관계 × 상황 칸별 표(`evals/coverage.py`의 칸 정의) ⑥ 지연 분석(11/25 지연 분석이 손으로 낸 것)이다.

D 채택 조건(`docs/design/design.md` ⑤ 평가)에 배치한 열 — 열의 뜻은 `run_eval.py`가 채우는 방식 그대로다
- 흩어진 사건 연결: `recall`(정답 `relevant_msg_ids` 중 검색기가 돌려준 예시의 출처가 덮은 비율 — 문체 예시와
  사실 맥락을 합치고 프롬프트 상한으로 자르기 전)과 `context_recall`(프롬프트의 [사실 맥락]에 실제로 들어간
  예시의 출처만), 사례 파일의 `needs_graph`·`relevant_msg_ids`(대화방 수).
- 시간·갱신: `context_hit`·`warning_hit`(사례의 선택 열 `expected_context` 핵심어가 사실 맥락 / 경고 문장에
  나온 비율)과 `fm32_register_warned`. 열이 비어 있으면 자동 근거가 없다.
- 맥락 오염: `fm11_first_checks`의 `fact_added`, `violations`·`first_attempt_violations`가 따옴표로 적은
  "추가된 항목"이 그 실행의 `context_facts`에 있는지, `fm23_length_ratio`.
- 비용·지연: `latency_ms`·`attempts`·`input_tokens`·`output_tokens`. 그래프 수집 비용은 결과 CSV에 없다.

지연 분석
- "시도 1회"는 `attempts == 1`인 실행이다(재생성은 지연을 두 배쯤으로 만든다).
- 상관은 피어슨, 어림식은 시도 1회인 실행 전체(모든 조건)의 최소제곱 직선 `지연 = 절편 + 기울기 × 출력 토큰`.
- 단계 차이의 "쌍"은 같은 (사례, 회차)의 두 조건 실행이고 둘 다 시도 1회인 쌍만 센다.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))

import summarize as sm  # noqa: E402


def _load_coverage():  # noqa: ANN202
    """`evals/coverage.py` — 이름이 PyPI의 coverage 패키지와 겹쳐 파일 경로로 읽는다."""
    name = "pz_coverage"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "evals" / "coverage.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclass가 모듈을 찾는다
    spec.loader.exec_module(module)
    return module


cov = _load_coverage()

DEFAULT_CASES = ROOT / "evals" / "eval_cases.csv"
DEFAULT_CHARACTERS = ROOT / "evals" / "characters.csv"

HUMAN = "> ✍"  # 사람이 쓰는 자리의 표식 (한 가지뿐이다)
TODO = "(사람이 쓴다)"
NO_EVIDENCE = "자동 근거 없음 — 사람이 사례를 읽고 쓴다."
VERDICT = f"{HUMAN} 판정: (사람이 쓴다 — 채택 / 보류 / 판단 불가)"
DASH = sm.DASH
MIN_REPEATS = 3  # 설계서 §8 "각 사례는 최소 3회 생성"
TARGET_CASES = 120  # 설계서 §8 "초기 120사례"
STAGES = [
    ("A", "B", "페르소나 설정의 효과"),
    ("B", "C", "문체 예시의 효과"),
    ("C", "D", "그래프 맥락의 효과"),
]
VERSION_FIELDS = [
    ("model_version", "모델"),
    ("prompt_version", "프롬프트"),
    ("validator_version", "검증 규칙"),
]
# 설계서에서 그대로 옮긴 문장 (테스트가 원문에 있는지 본다 — 설계서가 바뀌면 여기도 고친다)
DESIGN_DOC = "docs/design/project-design.md"
HYPOTHESES = [
    "관계별 설정이 일반적인 ‘정중하게 수정’보다 사용자 선호를 높인다.",
    "본인이 실제 작성한 과거 표현을 추가하면 페르소나만 사용할 때보다 ‘나다운 말투’ 평가가 높아진다.",
    "그래프 검색의 추가 비용은 관계·사건 연결이 필요한 사례에서만 정당화될 수 있다.",
]
D_DECISION_RULE = "C 대비 개선과 추가 비용을 근거로 채택/보류"
# docs/design/design.md "⑤ 평가"의 표: (구역 이름, 항목, 방법, D 채택 조건)
D_CONDITIONS = [
    (
        "d-scattered",
        "흩어진 사건 연결",
        "여러 메시지에 걸친 같은 사건을 맥락으로 써야 하는 사례를 별도 구성",
        "C 대비 스타일·적합성 개선",
    ),
    ("d-time", "시간·갱신", "관계가 바뀐 상대 (동기 → 상사)에 대한 첨삭", "바뀐 관계 기준으로 말투 적용"),
    (
        "d-contamination",
        "맥락 오염",
        "그래프 맥락의 날짜·사유가 결과에 새어 들어간 비율",
        "C보다 높지 않을 것",
    ),
    ("d-cost", "비용·지연", "수집 비용, 요청당 추가 지연", "p95 목표 안에서 감당 가능"),
]
ADDED_ITEM = re.compile(r"원문에 없는 (\S+) '([^']+)'")  # validation.py의 fact_added 문구


# ---------------------------------------------------------------------------
# 작은 도구
# ---------------------------------------------------------------------------


def _g(value: float | None, digits: int = 1) -> str:
    """중앙값 따위: 402.5 → `402.5`, 231.0 → `231`."""
    if value is None:
        return DASH
    return f"{round(value, digits):g}"


def _signed(value: float | None, fmt: str = "{:+.2f}") -> str:
    if value is None:
        return DASH
    text = fmt.format(value)
    # 반올림해서 0이 된 음수는 `-0.0초`가 아니라 `+0.0초`로
    return fmt.format(0) if text.startswith("-") and not re.search(r"[1-9]", text) else text


def _diff(after: float | None, before: float | None) -> float | None:
    return None if after is None or before is None else after - before


def _cell(text: str) -> str:
    """표 칸에 넣을 글: 세로줄과 줄바꿈이 표를 깨지 않게."""
    return text.replace("|", "\\|").replace("\n", " ")


def _attempts(row: dict[str, str]) -> int | None:
    return sm._int(row.get("attempts", ""))


def one_attempt(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """시도 1회인 실행 (`attempts` 열이 없는 예전 파일은 빈 목록)."""
    return [r for r in rows if _attempts(r) == 1]


def _values(rows: list[dict[str, str]], name: str) -> list[float]:
    return [v for r in rows if (v := sm._float(r.get(name, ""))) is not None]


def _pairs(rows: list[dict[str, str]], x: str, y: str) -> tuple[list[float], list[float]]:
    xs, ys = [], []
    for r in rows:
        a, b = sm._float(r.get(x, "")), sm._float(r.get(y, ""))
        if a is not None and b is not None:
            xs.append(a)
            ys.append(b)
    return xs, ys


def correlation(xs: list[float], ys: list[float]) -> float | None:
    """피어슨 상관. 2건 미만이거나 한쪽이 상수면 None."""
    try:
        return statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        return None


def linear_fit(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """최소제곱 직선 y = 절편 + 기울기·x → (절편, 기울기). 낼 수 없으면 None."""
    try:
        fit = statistics.linear_regression(xs, ys)
    except statistics.StatisticsError:
        return None
    return fit.intercept, fit.slope


# ---------------------------------------------------------------------------
# 머리말: 무엇을 돌렸나, 버전 섞임
# ---------------------------------------------------------------------------


@dataclass
class VersionMix:
    """한 실행 묶음 안에 값이 둘 이상인 버전 열."""

    column: str
    label: str
    scope: str  # "전체" 또는 "조건 D"
    values: dict[str, tuple[int, list[str]]]  # 값 → (실행 수, 사례 id)


def _mix(rows: list[dict[str, str]], column: str, label: str, scope: str) -> VersionMix | None:
    if not any(column in r for r in rows):
        return None  # 열이 없는 예전 파일뿐
    values: dict[str, tuple[int, list[str]]] = {}
    for r in rows:
        value = r.get(column, "") or "(빈칸)"
        n, cases = values.get(value, (0, []))
        if r.get("case_id", "") not in cases:
            cases.append(r.get("case_id", ""))
        values[value] = (n + 1, cases)
    return VersionMix(column, label, scope, values) if len(values) > 1 else None


def version_mixes(rows: list[dict[str, str]]) -> list[VersionMix]:
    """모델·프롬프트·검증 규칙은 전체에서, 검색기 버전은 조건 안에서 값이 섞였는지 본다.

    검색기 버전은 조건마다 다른 것이 정상이라 같은 조건 안에서만 견준다. 열이 일부 줄에만 있으면(예전 파일과
    새 파일을 합친 경우) 빈칸도 한 가지 값으로 센다.
    """
    out = [m for column, label in VERSION_FIELDS if (m := _mix(rows, column, label, "전체"))]
    for cond in sorted({r.get("condition", "") for r in rows}):
        group = [r for r in rows if r.get("condition", "") == cond]
        if m := _mix(group, "retrieval_version", "검색기", f"조건 {cond}"):
            out.append(m)
    return out


def _case_list(cases: list[str], limit: int = 8) -> str:
    shown = ", ".join(cases[:limit])
    return shown + (f" 외 {len(cases) - limit}개" if len(cases) > limit else "")


def mix_lines(mixes: list[VersionMix]) -> list[str]:
    if not mixes:
        return ["- 버전 섞임: 없음 (모델·프롬프트·검증 규칙은 전체에서, 검색기는 조건 안에서 한 가지)."]
    lines = [
        f"> **⚠ 경고 — 버전이 섞여 있다 ({', '.join(dict.fromkeys(m.label for m in mixes))}).** "
        "아래 수치는 서로 다른 버전의 실행을 합친 것이다. 조건·사례 사이의 차이를 버전 차이와 가려 읽는다.",
        "",
    ]
    body = []
    for m in mixes:
        for value, (n, cases) in sorted(m.values.items()):
            body.append([m.label, m.scope, f"`{value}`", str(n), f"{len(cases)}개: {_case_list(cases)}"])
    return lines + sm._table(["버전 열", "범위", "값", "실행 수", "사례"], body)


@dataclass
class Overview:
    runs: int
    cases: list[str]
    splits: Counter[str]  # split → 사례 수
    conditions: list[str]
    repeats: Counter[int]  # (사례, 조건)별 실행 수 → 그런 (사례, 조건)의 수
    retrieval: dict[str, list[str]]  # 조건 → 검색기 버전

    @property
    def min_repeats(self) -> int:
        return min(self.repeats) if self.repeats else 0


def overview(rows: list[dict[str, str]]) -> Overview:
    cases = list(dict.fromkeys(r.get("case_id", "") for r in rows))
    split_of = {r.get("case_id", ""): r.get("split", "") or "(빈칸)" for r in rows}
    per_cell = Counter((r.get("case_id", ""), r.get("condition", "")) for r in rows)
    retrieval: dict[str, list[str]] = {}
    for r in rows:
        if value := r.get("retrieval_version", ""):
            seen = retrieval.setdefault(r.get("condition", ""), [])
            if value not in seen:
                seen.append(value)
    return Overview(
        runs=len(rows),
        cases=cases,
        splits=Counter(split_of.values()),
        conditions=sorted({r.get("condition", "") for r in rows}),
        repeats=Counter(per_cell.values()),
        retrieval=retrieval,
    )


def overview_lines(report: Report) -> list[str]:
    ov, summary = report.overview, report.summary
    lines = [
        f"- 보고서를 만든 날: {report.made_on} (실행 날짜는 결과 CSV에 없다 — 파일 이름과 아래 ✍에 적는다)"
    ]
    lines.append("- 입력: " + (", ".join(f"`{s}`" for s in summary.sources) or DASH))
    if report.replaced is not None:
        lines.append(
            f"- 합치는 방식: `--replace` — 뒤 파일이 앞 파일의 같은 (사례, 조건, 회차) {report.replaced}개를 "
            "대체했다."
        )
    elif len(summary.sources) > 1:
        lines.append("- 합치는 방식: 줄을 그대로 이어 붙였다 (겹치는 실행은 아래 주의에 나온다).")
    splits = ", ".join(f"{name} {n}" for name, n in sorted(ov.splits.items())) or DASH
    lines.append(f"- 실행 {ov.runs}회 = 사례 {len(ov.cases)}개 ({splits}) × 조건 {', '.join(ov.conditions)}")
    if ov.repeats:
        if len(ov.repeats) == 1:
            lines.append(f"- 반복: 사례·조건마다 {ov.min_repeats}회")
        else:
            spread = ", ".join(f"{k}회인 것 {n}개" for k, n in sorted(ov.repeats.items()))
            lines.append(f"- 반복: **사례·조건마다 다르다** ({spread})")
    lines.append("- 모델: " + (", ".join(f"`{m}` {n}회" for m, n in sorted(summary.models.items())) or DASH))
    lines.append(
        "- 프롬프트: "
        + (", ".join(f"`{v}`" for v in summary.prompt_versions) or DASH)
        + " · 검증 규칙: "
        + (", ".join(f"`{v}`" for v in summary.validator_versions) or f"{DASH} (열 없음)")
    )
    for cond, versions in sorted(ov.retrieval.items()):
        if versions != ["none"]:
            lines.append(f"- 검색기 ({cond}): " + ", ".join(f"`{v}`" for v in versions))
    for w in summary.warnings:
        lines.append(f"- 주의: {w}")
    return lines + ["", *mix_lines(report.mixes)]


# ---------------------------------------------------------------------------
# 지연 분석
# ---------------------------------------------------------------------------


@dataclass
class LatencyStats:
    condition: str
    runs: int
    p50: float | None
    p95: float | None
    max: float | None
    one_runs: int | None  # attempts 열이 없으면 None
    one_p95: float | None
    output_median: float | None
    text_len_median: float | None  # 결과 문장 글자 수 (시도 1회)
    tokens_per_char_median: float | None  # 출력 토큰 ÷ 결과 글자 수 (시도 1회)
    corr_output: float | None  # 지연 ~ 출력 토큰 (시도 1회)
    corr_input: float | None  # 지연 ~ 입력 토큰 (시도 1회)


@dataclass
class PairedDiff:
    """같은 (사례, 회차)에서 뒤 조건 − 앞 조건. 둘 다 시도 1회인 쌍만."""

    before: str
    after: str
    pairs: int
    latency_median: float | None
    output_median: float | None
    # 출력 길이를 빼고 남는 지연: 중앙값끼리 (지연 차 − 기울기 × 토큰 차), 그리고 쌍마다 낸 나머지의 중앙값
    residual_of_medians: float | None = None
    residual_median: float | None = None


@dataclass
class LatencyAnalysis:
    conditions: list[LatencyStats]
    fit: tuple[float, float] | None  # (절편 ms, 출력 토큰당 ms) — 시도 1회 실행 전체
    fit_runs: int
    fit_corr: float | None
    pairs: list[PairedDiff]
    over_target: list[dict[str, str]]  # 목표를 넘긴 실행 (느린 순)
    target_ms: float = sm.TARGETS["latency_p95_ms"]


def latency_stats(condition: str, rows: list[dict[str, str]]) -> LatencyStats:
    latencies = _values(rows, "latency_ms")
    has_attempts = any(_attempts(r) is not None for r in rows)
    one = one_attempt(rows)
    lengths = [float(len(r["result_text"])) for r in one if "result_text" in r]
    ratios = [
        out / len(r["result_text"])
        for r in one
        if r.get("result_text") and (out := sm._float(r.get("output_tokens", ""))) is not None
    ]
    outputs = _values(rows, "output_tokens")
    return LatencyStats(
        condition=condition,
        runs=len(rows),
        p50=sm.percentile(latencies, 0.50),
        p95=sm.percentile(latencies, 0.95),
        max=max(latencies) if latencies else None,
        one_runs=len(one) if has_attempts else None,
        one_p95=sm.percentile(_values(one, "latency_ms"), 0.95),
        output_median=statistics.median(outputs) if outputs else None,
        text_len_median=statistics.median(lengths) if lengths else None,
        tokens_per_char_median=statistics.median(ratios) if ratios else None,
        corr_output=correlation(*_pairs(one, "latency_ms", "output_tokens")),
        corr_input=correlation(*_pairs(one, "latency_ms", "input_tokens")),
    )


def paired_diff(
    rows: list[dict[str, str]], before: str, after: str, slope: float | None = None
) -> PairedDiff:
    index = {
        (r.get("case_id", ""), r.get("repeat", ""), r.get("condition", "")): r for r in one_attempt(rows)
    }
    d_latency: list[float] = []
    d_output: list[float] = []
    for (case, repeat, cond), late in index.items():
        early = index.get((case, repeat, before))
        if cond != after or early is None:
            continue
        a, b = sm._float(late.get("latency_ms", "")), sm._float(early.get("latency_ms", ""))
        c, d = sm._float(late.get("output_tokens", "")), sm._float(early.get("output_tokens", ""))
        if None in (a, b, c, d):
            continue
        d_latency.append(a - b)
        d_output.append(c - d)
    out = PairedDiff(
        before=before,
        after=after,
        pairs=len(d_latency),
        latency_median=statistics.median(d_latency) if d_latency else None,
        output_median=statistics.median(d_output) if d_output else None,
    )
    if slope is not None and d_latency:
        out.residual_of_medians = out.latency_median - slope * out.output_median
        out.residual_median = statistics.median(
            [lat - slope * tok for lat, tok in zip(d_latency, d_output, strict=True)]
        )
    return out


def analyze_latency(rows: list[dict[str, str]], conditions: list[str]) -> LatencyAnalysis:
    rows = [r for r in rows if r.get("condition", "") in conditions]
    one = one_attempt(rows)
    xs, ys = _pairs(one, "output_tokens", "latency_ms")
    fit = linear_fit(xs, ys)
    target = sm.TARGETS["latency_p95_ms"]
    over = [r for r in rows if (v := sm._float(r.get("latency_ms", ""))) is not None and v > target]
    over.sort(key=lambda r: -float(r["latency_ms"]))
    return LatencyAnalysis(
        conditions=[latency_stats(c, [r for r in rows if r.get("condition", "") == c]) for c in conditions],
        fit=fit,
        fit_runs=len(xs),
        fit_corr=correlation(xs, ys),
        pairs=[
            paired_diff(rows, a, b, fit[1] if fit else None)
            for a, b, _ in STAGES
            if a in conditions and b in conditions
        ],
        over_target=over,
        target_ms=target,
    )


def fit_text(fit: tuple[float, float] | None) -> str:
    """`지연 ≈ 0.7초 + 출력 100토큰당 1.06초`"""
    if fit is None:
        return DASH
    intercept, slope = fit
    return f"지연 ≈ {intercept / 1000:.1f}초 + 출력 100토큰당 {slope / 10:.2f}초"


def _pair_line(p: PairedDiff) -> str:
    if not p.pairs:
        return f"- {p.before} → {p.after}: 둘 다 시도 1회인 (사례, 회차) 쌍이 없다."
    line = (
        f"- {p.before} → {p.after} (같은 사례·회차, 둘 다 시도 1회인 {p.pairs}쌍의 중앙값): "
        f"지연 {_signed(p.latency_median / 1000, '{:+.1f}초')}, 출력 토큰 "
        f"{_signed(p.output_median, '{:+.0f}')}"
    )
    if p.residual_of_medians is not None:
        line += (
            f". 출력 길이를 빼고 남는 지연: 중앙값끼리 {_signed(p.residual_of_medians / 1000, '{:+.1f}초')}"
            f", 쌍마다 낸 값의 중앙값 {_signed(p.residual_median / 1000, '{:+.1f}초')}"
        )
    return line + "."


def latency_lines(report: Report) -> list[str]:
    la = report.latency
    if not any(s.p50 is not None for s in la.conditions):
        return ["지연 값이 없다 (`latency_ms` 열)."]
    stats = la.conditions

    def row(label: str, cell) -> list[str]:
        return [label, *[cell(s) for s in stats]]

    body = [
        row("실행 수", lambda s: str(s.runs)),
        row("지연 p50", lambda s: sm._sec(s.p50)),
        row("지연 p95", lambda s: sm._sec(s.p95)),
        row("지연 최대", lambda s: sm._sec(s.max)),
        row("시도 1회인 실행 수", lambda s: sm._num(s.one_runs)),
        row("시도 1회인 실행만의 p95", lambda s: sm._sec(s.one_p95)),
        row("출력 토큰 중앙값", lambda s: _g(s.output_median)),
        row("결과 문장 글자 수 중앙값 (시도 1회)", lambda s: _g(s.text_len_median)),
        row("출력 토큰 ÷ 결과 글자 수, 중앙값 (시도 1회)", lambda s: _g(s.tokens_per_char_median, 2)),
        row("상관: 지연 ~ 출력 토큰 (시도 1회)", lambda s: _signed(s.corr_output, "{:.2f}")),
        row("상관: 지연 ~ 입력 토큰 (시도 1회)", lambda s: _signed(s.corr_input, "{:.2f}")),
    ]
    lines = sm._table(["", *[s.condition for s in stats]], body)
    lines += [
        "",
        f"- 어림식 (시도 1회인 실행 {la.fit_runs}회, 모든 조건을 합친 최소제곱 직선): **{fit_text(la.fit)}**"
        + (f" (상관 {la.fit_corr:.2f})" if la.fit_corr is not None else ""),
    ]
    lines += [_pair_line(p) for p in la.pairs]
    target = f"{la.target_ms / 1000:g}초"
    if la.over_target:
        lines += ["", f"목표 {target}를 넘긴 실행 {len(la.over_target)}회 (느린 순):", ""]
        lines += sm._table(
            ["사례", "조건", "회차", "지연", "출력 토큰", "시도"],
            [
                [
                    r.get("case_id", ""),
                    r.get("condition", ""),
                    r.get("repeat", ""),
                    sm._sec(sm._float(r.get("latency_ms", ""))),
                    r.get("output_tokens", "") or DASH,
                    r.get("attempts", "") or DASH,
                ]
                for r in la.over_target
            ],
        )
    else:
        lines += ["", f"목표 {target}를 넘긴 실행은 없다."]
    lines += [
        "",
        "지연은 요청 한 건 전체(`latency_ms`, 재생성 시간 포함)이고 "
        "측정한 날의 모델 쪽 부하에 따라 달라진다. "
        "출력 토큰에는 결과 문장 말고도 구조화 출력의 나머지 칸이 들어 있다 — CSV로는 그 안을 나눌 수 없다.",
    ]
    return lines


# ---------------------------------------------------------------------------
# 단계별 차이 (A→B, B→C, C→D)
# ---------------------------------------------------------------------------


def _latency_of(report: Report, condition: str) -> LatencyStats | None:
    return next((s for s in report.latency.conditions if s.condition == condition), None)


def stage_rows(report: Report, before: str, after: str) -> list[list[str]] | None:
    """단계 하나의 표 줄: [지표, 앞 조건, 뒤 조건, 차이]. 두 조건 가운데 하나라도 없으면 None."""
    summary = report.summary
    a, b = summary.by_condition(before), summary.by_condition(after)
    if a is None or b is None or not a.runs or not b.runs:
        return None
    la, lb = _latency_of(report, before), _latency_of(report, after)

    def per_run(total: float | None, s: sm.ConditionStats) -> float | None:
        return None if total is None else total / s.runs

    def ratio(after_value: float | None, before_value: float | None) -> str:
        if after_value is None or not before_value:
            return ""
        return f" (×{after_value / before_value:.2f})"

    def sec_diff(x: float | None, y: float | None) -> str:
        d = _diff(x, y)
        return _signed(None if d is None else d / 1000, "{:+.1f}초")

    in_a, in_b = per_run(a.input_tokens, a), per_run(b.input_tokens, b)
    out_a, out_b = per_run(a.output_tokens, a), per_run(b.output_tokens, b)
    cost_a, cost_b = per_run(a.cost_usd, a), per_run(b.cost_usd, b)
    rate_diff = _diff(b.pass_rate, a.pass_rate)
    rows = [
        ["실행 수", str(a.runs), str(b.runs), ""],
        [
            "추천 비율 (규칙 통과)",
            sm._pct(a.pass_rate),
            sm._pct(b.pass_rate),
            _signed(None if rate_diff is None else rate_diff * 100, "{:+.1f}%p"),
        ],
        ["원문 유지", str(a.fallback), str(b.fallback), _signed(b.fallback - a.fallback, "{:+d}")],
        [
            "재생성한 실행",
            sm._num(a.regenerated),
            sm._num(b.regenerated),
            _signed(_diff(b.regenerated, a.regenerated), "{:+d}"),
        ],
        ["지연 p50", sm._sec(a.latency_p50), sm._sec(b.latency_p50), sec_diff(b.latency_p50, a.latency_p50)],
        ["지연 p95", sm._sec(a.latency_p95), sm._sec(b.latency_p95), sec_diff(b.latency_p95, a.latency_p95)],
        [
            "시도 1회인 실행만의 p95",
            sm._sec(la.one_p95 if la else None),
            sm._sec(lb.one_p95 if lb else None),
            sec_diff(lb.one_p95 if lb else None, la.one_p95 if la else None),
        ],
        [
            "입력 토큰 (실행당)",
            _g(in_a, 0),
            _g(in_b, 0),
            _signed(_diff(in_b, in_a), "{:+,.0f}") + ratio(in_b, in_a),
        ],
        [
            "출력 토큰 (실행당)",
            _g(out_a, 0),
            _g(out_b, 0),
            _signed(_diff(out_b, out_a), "{:+,.0f}") + ratio(out_b, out_a),
        ],
        [
            "비용 어림 (실행당, USD)",
            DASH if cost_a is None else f"${cost_a:.4f}",
            DASH if cost_b is None else f"${cost_b:.4f}",
            _signed(_diff(cost_b, cost_a), "{:+.4f}") + ratio(cost_b, cost_a),
        ],
        [
            "recall 평균",
            DASH if a.recall_mean is None else f"{a.recall_mean:.2f}",
            DASH if b.recall_mean is None else f"{b.recall_mean:.2f}",
            _signed(_diff(b.recall_mean, a.recall_mean)),
        ],
    ]
    if summary.scores:
        sa, sb = summary.scores.overall.get(before), summary.scores.overall.get(after)
        rows.append(
            [
                "사람 점수 평균",
                DASH if sa is None else f"{sa:.2f}",
                DASH if sb is None else f"{sb:.2f}",
                _signed(_diff(sb, sa)),
            ]
        )
    else:
        rows.append(["사람 점수 평균", sm.NOT_SCORED, sm.NOT_SCORED, DASH])
    return rows


def stage_lines(report: Report, before: str, after: str) -> list[str]:
    rows = stage_rows(report, before, after)
    if rows is None:
        return [f"{before} 또는 {after} 조건의 결과가 없어 차이를 낼 수 없다."]
    lines = sm._table(["지표", before, after, f"차이 ({after} − {before})"], rows)
    pair = next((p for p in report.latency.pairs if (p.before, p.after) == (before, after)), None)
    if pair is not None:
        lines += ["", _pair_line(pair)]
    return lines


# ---------------------------------------------------------------------------
# D 채택 조건 네 가지
# ---------------------------------------------------------------------------


def _case_info(report: Report, case_id: str) -> tuple[str, str]:
    """(needs_graph, 조건 유형) — 사례 파일에 없으면 ("?", "사례 파일에 없음")."""
    case = report.case_table.get(case_id)
    if case is None:
        return "?", "사례 파일에 없음"
    rooms = len(cov.rooms(case.get("relevant_msg_ids", "")))
    return case.get("needs_graph", "") or "?", f"{cov.case_kind(case)} (대화방 {rooms}개)"


def _relation(report: Report, case_id: str) -> str:
    """사례의 관계: 결과 CSV의 `relation` 열, 없으면 사례 파일."""
    for r in report.rows:
        if r.get("case_id", "") == case_id and r.get("relation"):
            return r["relation"]
    return report.case_table.get(case_id, {}).get("relation", "") or DASH


def _case_stats(report: Report) -> dict[str, dict[str, dict[str, str]]]:
    table: dict[str, dict[str, dict[str, str]]] = {}
    for c in report.summary.cases:
        table.setdefault(c["case_id"], {})[c["condition"]] = c
    return table


def scattered_lines(report: Report) -> list[str]:
    """흩어진 사건 연결: 사례별 recall(C 대 D)과 D의 사실 맥락 recall."""
    table = _case_stats(report)
    cases = [
        cid
        for cid, per in table.items()
        if "C" in per and "D" in per and (per["C"]["recall_mean"] or per["D"]["recall_mean"])
    ]
    if not cases:
        return [
            NO_EVIDENCE,
            "",
            "C와 D가 모두 있고 `recall` 값이 있는 사례가 없다 (정답 "
            "`relevant_msg_ids`가 있는 사례를 C·D로 돌려야 "
            "`recall` 열이 채워진다).",
        ]
    body = []
    for cid in cases:
        c, d = table[cid]["C"], table[cid]["D"]
        needs, kind = _case_info(report, cid)
        body.append(
            [
                cid,
                needs,
                kind,
                c["recall_mean"] or DASH,
                d["recall_mean"] or DASH,
                d["context_recall_mean"] or DASH,
                f"{c['recommended']}/{c['runs']}",
                f"{d['recommended']}/{d['runs']}",
            ]
        )
    lines = sm._table(
        ["사례", "needs_graph", "유형", "C recall", "D recall", "D context_recall", "C 추천", "D 추천"],
        body,
    )
    subset = [c for c in report.summary.cases if c["case_id"] in cases]
    d_wins = sm.better_cases(subset, "D", "C")
    c_wins = sm.better_cases(subset, "C", "D")
    graph_cases = [cid for cid in cases if _case_info(report, cid)[0] == "Y"]
    cross = [cid for cid in cases if _case_info(report, cid)[1].startswith(cov.KIND_GRAPH_CROSS)]

    def named(ids: list[str]) -> str:
        return f"{len(ids)}건" + (f" ({', '.join(ids)})" if ids else "")

    lines += [
        "",
        f"- recall 값이 있는 사례 {len(cases)}개 가운데 **D > C {named(d_wins)}**, C > D {named(c_wins)}, "
        f"같음 {len(cases) - len(d_wins) - len(c_wins)}건.",
        f"- `needs_graph=Y`인 사례 {named(graph_cases)} 가운데 D > C는 "
        f"{named([c for c in d_wins if c in graph_cases])}.",
        f"- 다른 대화방을 이어야 하는 사례(정답이 대화방 둘 이상) {named(cross)} 가운데 D > C는 "
        f"{named([c for c in d_wins if c in cross])}.",
        "",
        "열의 뜻(`run_eval.py`): `recall`은 정답 `relevant_msg_ids` 가운데 검색기가 돌려준 예시(문체 예시 + "
        "사실 맥락, 프롬프트 상한으로 자르기 전)의 출처 메시지가 덮은 비율이다. `context_recall`은 그중 "
        "프롬프트의 [사실 맥락]에 실제로 들어간 예시의 출처만 센다(C는 사실 맥락이 없어 0이다). "
        "둘 다 **검색이 관련 메시지를 찾았는가**이고 채택 조건(스타일·적합성 개선)의 근거 후보일 뿐이다 — "
        "결과 문장이 나아졌는지는 위 사례의 C·D 결과를 사람이 읽고 채점해야 안다.",
    ]
    return lines


def _hit_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [r for r in rows if r.get("context_hit", "").strip() or r.get("warning_hit", "").strip()]


def time_lines(report: Report) -> list[str]:
    """시간·갱신: 사례가 적어 둔 핵심어(`expected_context`)가 사실 맥락 / 경고 문장에 나온 비율."""
    hits = _hit_rows(report.rows)
    conditions = [s.condition for s in report.summary.conditions]
    lines: list[str] = []
    if hits:
        body = []
        for cid in dict.fromkeys(r.get("case_id", "") for r in hits):
            for cond in conditions:
                group = [r for r in hits if r.get("case_id", "") == cid and r.get("condition", "") == cond]
                if not group:
                    continue
                ctx, warn = sm._mean(_values(group, "context_hit")), sm._mean(_values(group, "warning_hit"))
                body.append(
                    [
                        cid,
                        cond,
                        str(len(group)),
                        DASH if ctx is None else f"{ctx:.2f}",
                        DASH if warn is None else f"{warn:.2f}",
                    ]
                )
        lines += sm._table(["사례", "조건", "실행 수", "context_hit 평균", "warning_hit 평균"], body)
        lines += [
            "",
            "열의 뜻(`run_eval.py`의 `keyword_hit`): 사례의 선택 열 "
            "`expected_context`(`;`로 나눈 핵심어, 예: "
            "`파트장;존댓말`)가 프롬프트의 [사실 맥락] 문장(`context_hit`) "
            "/ 결과의 경고 문장(`warning_hit`)에 "
            "글자 그대로 나온 비율이다. 핵심어가 바뀐 관계를 가리키는 사례에서만 이 조건의 근거가 된다 — "
            "핵심어가 사건을 가리키는 사례는 4.1의 근거로 읽는다. 결과 문장의 말투가 바뀐 관계에 맞는지는 "
            "이 열로 알 수 없다.",
        ]
    warned = Counter(
        (r.get("case_id", ""), r.get("condition", ""))
        for r in report.rows
        if r.get("fm32_register_warned", "").strip().lower() == "true"
    )
    if warned:
        if lines:
            lines.append("")
        lines += sm._table(
            ["사례", "관계", "조건", "말투를 언급한 경고가 붙은 실행"],
            [[cid, _relation(report, cid), cond, f"{n}회"] for (cid, cond), n in warned.items()],
        )
        lines += [
            "",
            "열의 뜻(`run_eval.py`의 `failure_signals`): "
            "`fm32_register_warned`는 결과의 경고 문장에 '말투'라는 "
            "낱말이 있으면 참이다. 워크플로의 말투 사후 점검(`REGISTER_POST_CHECK`) 경고를 잡으려던 열이지만 "
            "**모델이 스스로 쓴 주의 문구도 잡힌다** — 어느 쪽인지는 경고 문장을 읽어야 안다. 관계가 바뀐 "
            "상대의 사례가 여기 모이면 읽어 볼 후보이고, 결과 문장의 말투가 바뀐 관계에 맞는지는 이 열로 "
            "알 수 없다.",
        ]
    if not lines:
        return [
            NO_EVIDENCE,
            "",
            "`context_hit`·`warning_hit` 열이 없거나 모두 비어 있다 — 사례 "
            "파일에 선택 열 `expected_context`가 "
            "있어야 채워진다. 말투를 언급한 경고(`fm32_register_warned`)가 붙은 실행도 없다. "
            "관계가 바뀐 상대의 사례를 골라 C·D 결과의 말투를 읽는다.",
        ]
    return lines


@dataclass
class AddedItem:
    case_id: str
    condition: str
    repeat: str
    kind: str  # person·date·…  (validation.py가 적는 종류)
    label: str
    where: str  # "1차" 또는 "최종"
    in_context: bool


def added_items(rows: list[dict[str, str]]) -> list[AddedItem]:
    """검증이 "원문에 없는 … 추가"로 잡은 항목과, 그 항목이 그 실행의 사실 맥락(`context_facts`)에 있는지."""
    out: list[AddedItem] = []
    for r in rows:
        seen: set[tuple[str, str]] = set()
        for column, where in (("first_attempt_violations", "1차"), ("violations", "최종")):
            for kind, label in ADDED_ITEM.findall(r.get(column, "")):
                if (kind, label) in seen:
                    continue
                seen.add((kind, label))
                out.append(
                    AddedItem(
                        case_id=r.get("case_id", ""),
                        condition=r.get("condition", ""),
                        repeat=r.get("repeat", ""),
                        kind=kind,
                        label=label,
                        where=where,
                        in_context=label in r.get("context_facts", ""),
                    )
                )
    return out


def contamination_lines(report: Report) -> list[str]:
    rows = report.rows
    if not any("first_attempt_violations" in r or "fm11_first_checks" in r for r in rows):
        return [
            NO_EVIDENCE,
            "",
            "`first_attempt_violations`·`fm11_first_checks`·`context_facts` 열이 없는 결과 파일이다.",
        ]
    conditions = [c for c in ("C", "D") if report.summary.by_condition(c)]
    if "D" not in conditions:
        return [NO_EVIDENCE, "", "D 조건의 결과가 없다."]
    items = added_items(rows)

    def count(cond: str, pred) -> str:
        return str(len({(i.case_id, i.repeat) for i in items if i.condition == cond and pred(i)}))

    def stat(cond: str) -> sm.ConditionStats:
        s = report.summary.by_condition(cond)
        assert s is not None
        return s

    body = [
        ["실행 수", *[str(stat(c).runs) for c in conditions]],
        [
            "1차 시도가 `fact_added`에 걸린 실행 (`fm11_first_checks`)",
            *[
                DASH
                if stat(c).signals.get("fm11_first_checks") is None
                else str(stat(c).first_checks["fact_added"])
                for c in conditions
            ],
        ],
        [
            '"원문에 없는 … 추가" 위반이 적힌 실행 (1차 또는 최종)',
            *[count(c, lambda i: True) for c in conditions],
        ],
        [
            "그중 추가된 항목이 그 실행의 `context_facts`에 있는 실행",
            *[count(c, lambda i: i.in_context) for c in conditions],
        ],
        [
            "그중 최종 결과에 남은 실행 (원문 유지로 끝남)",
            *[count(c, lambda i: i.where == "최종") for c in conditions],
        ],
        [
            "결과 길이 ÷ 초안 길이 평균 (`fm23_length_ratio`)",
            *[
                DASH if stat(c).length_ratio_mean is None else f"{stat(c).length_ratio_mean:.2f}"
                for c in conditions
            ],
        ],
    ]
    lines = sm._table(["", *conditions], body)
    mine = [i for i in items if i.condition in conditions]
    if mine:
        header = ["사례", "조건", "회차", "종류", "어디서", "사실 맥락에 있음"]
        if report.show_text:
            header.insert(4, "추가된 항목")
        listing = []
        for i in mine:
            row = [i.case_id, i.condition, i.repeat, i.kind, i.where, "예" if i.in_context else "아니오"]
            if report.show_text:
                row.insert(4, _cell(i.label))
            listing.append(row)
        lines += ["", *sm._table(header, listing)]
    lines += [
        "",
        "읽는 법: 검증(`validation.py`의 `fact_added`)은 원문에 없는 **날짜·수량·사람·호칭**이 결과에 생기면 "
        "잡는다. 잡힌 항목의 글자가 그 실행의 [사실 맥락](`context_facts`, "
        "D에만 있다)에 그대로 있으면 맥락에서 "
        "새어 들어왔을 가능성이 있는 것으로 센다(글자 일치일 뿐 원인 확인이 아니다). "
        "**사유·사정처럼 검증이 읽지 않는 내용이 샌 것은 여기 나오지 않는다** — D 결과를 사실 맥락과 나란히 "
        "읽어야 안다(`--show-text`, 원시 jsonl).",
    ]
    return lines


def cost_lines(report: Report) -> list[str]:
    rows = stage_rows(report, "C", "D")
    if rows is None:
        return [NO_EVIDENCE, "", "C 또는 D 조건의 결과가 없다."]
    keep = ("지연", "시도 1회", "입력 토큰", "출력 토큰", "비용", "재생성")
    lines = sm._table(["지표", "C", "D", "차이 (D − C)"], [r for r in rows if r[0].startswith(keep)])
    d = report.summary.by_condition("D")
    target = report.latency.target_ms
    ld = _latency_of(report, "D")
    over = [r for r in report.latency.over_target if r.get("condition") == "D"]
    if d is not None and d.latency_p95 is not None:
        verdict = "안" if d.latency_p95 <= target else "**밖**"
        line = f"- D의 지연 p95 {sm._sec(d.latency_p95)}는 목표 {target / 1000:g}초 {verdict}이다"
        if ld is not None and ld.one_p95 is not None:
            line += f" (시도 1회인 실행만 보면 {sm._sec(ld.one_p95)})"
        lines += ["", line + f". 목표를 넘긴 D 실행 {len(over)}회 / {d.runs}회."]
    pair = next((p for p in report.latency.pairs if (p.before, p.after) == ("C", "D")), None)
    if pair is not None:
        lines.append(_pair_line(pair))
    lines += [
        "- 결과 CSV에 **없는** 것: 그래프 수집 비용(에피소드 수 × "
        "`COST_GRAPH_PER_EPISODE`, haiku 호출)과 수집에 "
        "걸린 시간. 비용 어림은 첨삭 호출의 토큰 × 단가뿐이다.",
    ]
    return lines


# ---------------------------------------------------------------------------
# 실패와 재생성
# ---------------------------------------------------------------------------


def failure_runs(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """추천으로 끝나지 않았거나 재생성이 있었던 실행."""
    return [r for r in rows if r.get("status", "") != "recommended" or (_attempts(r) or 1) > 1]


def _kinds(row: dict[str, str]) -> str:
    """위반 종류. `fm11_first_checks`가 있으면 그 값, 없는 예전 파일은 문구 수만 안다."""
    if row.get("fm11_first_checks", "").strip():
        return ", ".join(f"`{k}`" for k in row["fm11_first_checks"].split(";") if k.strip())
    if "fm11_first_checks" in row:
        return DASH
    first = [v for v in row.get("first_attempt_violations", "").split(" | ") if v.strip()]
    return f"(종류 열 없음, 문구 {len(first)}건)" if first else DASH


def failure_lines(report: Report) -> list[str]:
    runs = failure_runs(report.rows)
    total = report.summary.total
    lines = [
        f"실행 {total.runs}회 가운데 원문 유지 {total.fallback}회, 재생성 {sm._num(total.regenerated)}회"
        + (f", 그 밖의 상태 {total.other_status}회" if total.other_status else "")
        + "."
    ]
    if runs:
        header = ["사례", "조건", "회차", "끝난 상태", "시도", "1차 위반 종류", "최종 위반 수", "검증 규칙"]
        if report.show_text:
            header += ["1차 위반 문구", "최종 위반 문구", "결과 문장"]
        body = []
        for r in runs:
            final = [v for v in r.get("violations", "").split(" | ") if v.strip()]
            row = [
                r.get("case_id", ""),
                r.get("condition", ""),
                r.get("repeat", ""),
                r.get("status", ""),
                r.get("attempts", "") or DASH,
                _kinds(r),
                str(len(final)),
                f"`{r['validator_version']}`" if r.get("validator_version") else DASH,
            ]
            if report.show_text:
                row += [
                    _cell(r.get("first_attempt_violations", "")) or DASH,
                    _cell(r.get("violations", "")) or DASH,
                    _cell(r.get("result_text", "")) or DASH,
                ]
            body.append(row)
        lines += ["", *sm._table(header, body)]
        if not report.show_text:
            lines += [
                "",
                "위반 문구와 결과 문장은 싣지 않았다 — `--show-text`로 다시 만들거나 결과 CSV를 본다.",
            ]
    lines += [
        "",
        "실패 신호 (`fm*` 열 — 참고 신호이고 실패 판정이 아니다. 번호는 `docs/design/design.md` ⑥):",
        "",
    ]
    return lines + sm.signal_lines(report.summary)


# ---------------------------------------------------------------------------
# 사례별: 사례 × 조건, 관계 × 상황 칸
# ---------------------------------------------------------------------------


def cell_rows(report: Report) -> tuple[list[list[str]], list[str]]:
    """관계 × 상황 칸마다 한 줄 + 칸에 넣지 못한 사례. 칸의 축은 `coverage.py`의 것이다."""
    conditions = [s.condition for s in report.summary.conditions]
    case_ids = list(dict.fromkeys(r.get("case_id", "") for r in report.rows))
    members: dict[tuple[str, str], list[str]] = {}
    outside: list[str] = []
    for cid in case_ids:
        case = report.case_table.get(cid)
        cell = (case.get("relation", ""), case.get("situation", "")) if case else ("", "")
        if cell[0] in report.relations and cell[1] in cov.SITUATIONS:
            members.setdefault(cell, []).append(cid)
        else:
            outside.append(cid)
    body = []
    for relation in report.relations:
        for situation in cov.SITUATIONS:
            ids = members.get((relation, situation), [])
            cells = []
            for cond in conditions:
                group = [
                    r for r in report.rows if r.get("case_id", "") in ids and r.get("condition", "") == cond
                ]
                if not group:
                    cells.append(DASH)
                    continue
                s = sm.summarize_rows(cond, group, None)
                cell = f"{s.recommended}/{s.runs}"
                if s.recall_mean is not None:
                    cell += f" · {s.recall_mean:.2f}"
                cells.append(cell)
            body.append([relation, situation, ", ".join(ids) or DASH, *cells])
    return body, outside


def case_section_lines(report: Report) -> list[str]:
    conditions = [s.condition for s in report.summary.conditions]
    lines = ["사례 × 조건 (추천 수/실행 수 · recall 평균):", "", *sm.case_lines(report.summary)]
    lines += ["", "관계 × 상황 칸별 (추천 수/실행 수 · recall 평균, 칸의 축은 `evals/coverage.py`):", ""]
    if not report.case_table:
        return lines + [
            f"사례 파일을 읽지 못했다 (`{report.cases_source}`) — "
            f"관계·상황은 사례 파일에서 읽는다 (`--cases`)."
        ]
    body, outside = cell_rows(report)
    lines += sm._table(["관계", "상황", "사례", *conditions], body)
    if outside:
        lines += [
            "",
            f"칸에 넣지 못한 사례 {len(outside)}개(사례 파일 `{report.cases_source}`에 없거나 축에 없는 "
            f"관계·상황): {_case_list(outside, 20)}",
        ]
    return lines


# ---------------------------------------------------------------------------
# 한계와 주의
# ---------------------------------------------------------------------------


def limit_lines(report: Report) -> list[str]:
    ov, summary = report.overview, report.summary
    lines: list[str] = []
    if ov.repeats:
        verdict = (
            "설계서 §8의 최소 3회를 채웠다"
            if ov.min_repeats >= MIN_REPEATS
            else "**설계서 §8의 최소 3회에 못 미친다**"
        )
        lines.append(f"- 반복 수: 사례·조건마다 최소 {ov.min_repeats}회 — {verdict}.")
    splits = ", ".join(f"{name} {n}" for name, n in sorted(ov.splits.items()))
    lines.append(
        f"- 사례 수: {len(ov.cases)}개 ({splits}) — 설계서 §8의 초기 목표는 {TARGET_CASES}사례다."
        + ("" if ov.splits.get("test") else " `test` 사례는 들어 있지 않다.")
    )
    runs = [s.latency_n for s in summary.conditions if s.latency_n]
    if runs:
        span = str(min(runs)) if min(runs) == max(runs) else f"{min(runs)}~{max(runs)}"
        lines.append(
            f"- p95는 조건마다 {span}회에서 낸 값이라 위쪽 두세 실행에 크게 흔들린다 (선형 보간 백분위)."
        )
    if report.mixes:
        lines.append(
            "- **버전이 섞여 있다**: "
            + "; ".join(
                f"{m.label}({m.scope}) "
                + " / ".join(f"`{v}` {n}회" for v, (n, _) in sorted(m.values.items()))
                for m in report.mixes
            )
            + ". 1절의 표를 본다."
        )
    if report.replaced is not None:
        lines.append(
            f"- 뒤 파일이 앞 파일의 실행 {report.replaced}개를 대체했다(`--replace`) — 대체되지 않은 사례는 "
            "앞 파일의 조건(버전)으로 돌린 값 그대로다."
        )
    fake = sum(s.fake_runs for s in summary.conditions)
    if fake:
        lines.append(
            f"- **fake 프로바이더 결과 {fake}회가 들어 있다** — 원문을 "
            f"그대로 돌려주므로 추천 비율·지연·비용은 "
            "품질 측정이 아니다."
        )
    lines.append(
        '- "추천"은 보존 규칙(검증)을 통과했다는 뜻이다. '
        "사람이 확인한 사실 보존율도, 말투가 좋다는 뜻도 아니다."
    )
    if summary.prices is None:
        lines.append("- 비용은 내지 않았다 (`--no-cost`).")
    else:
        lines.append(
            f"- 비용은 **어림값**이다: 토큰 × 단가(기본 단가 출처 "
            f"{sm.PRICE_SOURCE}). 캐시·재시도 과금과 그래프 "
            "수집 비용은 들어 있지 않고 Console 청구액과 대조하지 않았다."
        )
    if any(s.recall_mean is not None for s in summary.conditions):
        lines.append(
            "- `recall`의 검색 k는 결과 CSV에 없다 — 실행 때의 설정(`RETRIEVAL_TOP_K`, 기본 4)을 따르므로 "
            '설계서의 "Recall@5"와 k가 다를 수 있다. 정답(`relevant_msg_ids`)이 없는 사례는 평균에서 빠진다.'
        )
    if summary.scores is None:
        lines.append("- 사람 채점 전이다 — 스타일 점수가 없다.")
    lines.append("- 쌍대 선호(C 대 A)는 기록 형식이 아직 없어 측정하지 못했다.")
    lines.append("- 지연은 측정한 날의 모델 쪽 부하에 따라 달라진다.")
    return lines


# ---------------------------------------------------------------------------
# 뼈대 조립
# ---------------------------------------------------------------------------


@dataclass
class Report:
    title: str
    made_on: str
    rows: list[dict[str, str]]
    summary: sm.Summary
    overview: Overview
    mixes: list[VersionMix]
    latency: LatencyAnalysis
    replaced: int | None = None  # --replace를 썼을 때만: 대체된 실행 수
    case_table: dict[str, dict[str, str]] = field(default_factory=dict)  # case_id → 사례 파일의 줄
    relations: list[str] = field(default_factory=list)
    cases_source: str = ""
    show_text: bool = False


def build_report(
    rows: list[dict[str, str]],
    *,
    sources: list[str] | None = None,
    title: str = "최종 비교 보고서",
    made_on: str = "",
    prices: dict[str, tuple[float, float]] | None = None,
    score_rows: list[dict[str, str]] | None = None,
    score_columns: list[str] | None = None,
    conditions: list[str] | None = None,
    replaced: int | None = None,
    case_rows: list[dict[str, str]] | None = None,
    relations: list[str] | None = None,
    cases_source: str = "",
    show_text: bool = False,
) -> Report:
    summary = sm.build_summary(
        rows,
        sources=sources,
        prices=prices,
        score_rows=score_rows,
        score_columns=score_columns,
        conditions=conditions,
    )
    names = [s.condition for s in summary.conditions]
    rows = [r for r in rows if r.get("condition", "") in names]
    return Report(
        title=title,
        made_on=made_on or date.today().isoformat(),
        rows=rows,
        summary=summary,
        overview=overview(rows),
        mixes=version_mixes(rows),
        latency=analyze_latency(rows, names),
        replaced=replaced,
        case_table={c.get("case_id", ""): c for c in case_rows or []},
        relations=relations or [],
        cases_source=cases_source,
        show_text=show_text,
    )


def target_section_lines(report: Report) -> list[str]:
    rows = sm.target_rows(report.summary)
    verdicts = Counter("미달" if "미달" in r[3] else r[3] for r in rows)
    missed = [r[0].split(" (")[0] for r in rows if "미달" in r[3]]
    lines = sm._table(["지표", "목표", "측정값", "판정"], rows)
    lines += [
        "",
        f"목표 {len(rows)}개 가운데 달성 {verdicts['달성']} · 미달 "
        f"{verdicts['미달']} · 미측정 {verdicts['미측정']}"
        + (f" — 미달: {', '.join(missed)}" if missed else "")
        + ". 목표값은 설계서 §8의 초기 합의 값이다.",
    ]
    return lines


def sections(report: Report) -> dict[str, list[str]]:
    """자동 구역: 이름 → 줄. 이름의 집합은 입력과 상관없이 같다(`--update`가 이름으로 갈아 끼운다)."""
    if not report.summary.conditions:
        empty = ["결과 줄이 없다."]
        return {name: list(empty) for name in SECTION_NAMES}
    out = {
        "overview": overview_lines(report),
        "targets": target_section_lines(report),
        "conditions": sm.condition_lines(report.summary),
        "failures": failure_lines(report),
        "cases": case_section_lines(report),
        "latency": latency_lines(report),
        "scores": sm.score_lines(report.summary),
        "limits": limit_lines(report),
        "d-scattered": scattered_lines(report),
        "d-time": time_lines(report),
        "d-contamination": contamination_lines(report),
        "d-cost": cost_lines(report),
    }
    for before, after, _ in STAGES:
        out[f"stage-{before}{after}".lower()] = stage_lines(report, before, after)
    return out


SECTION_NAMES = [
    "overview",
    "targets",
    "conditions",
    *[f"stage-{a}{b}".lower() for a, b, _ in STAGES],
    *[name for name, *_ in D_CONDITIONS],
    "failures",
    "cases",
    "latency",
    "scores",
    "limits",
]

START = re.compile(r"^<!-- auto:start (\S+) sha=([0-9a-f]{12}) -->$")
END = re.compile(r"^<!-- auto:end (\S+) -->$")
MARKER_LIKE = re.compile(r"^<!--\s*auto:")  # 줄 머리의 표식만 본다 (글 속에서 언급한 것은 표식이 아니다)


class MarkerError(ValueError):
    """자동 구역 표식이 깨졌거나 뼈대와 맞지 않는다 — 파일을 쓰지 않는다."""


def _sha(lines: list[str]) -> str:
    """구역 내용의 지문. 줄 끝 공백은 편집기가 지우기도 해서 빼고 센다."""
    text = "\n".join(line.rstrip() for line in lines)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def region(name: str, lines: list[str]) -> list[str]:
    return [f"<!-- auto:start {name} sha={_sha(lines)} -->", *lines, f"<!-- auto:end {name} -->"]


def render(report: Report) -> str:
    """뼈대 전체. 구역 밖의 글(제목·소제목·✍ 자리)은 처음 한 번만 쓰고 그 뒤로는 사람이 관리한다."""
    auto = sections(report)

    def block(name: str) -> list[str]:
        return [*region(name, auto[name]), ""]

    def human(label: str, hint: str = "") -> list[str]:
        return [f"{HUMAN} {label}: (사람이 쓴다{' — ' + hint if hint else ''})", ""]

    lines = [
        f"# {report.title}",
        "",
        f"{HUMAN} 이 표식(`{HUMAN}`)으로 시작하는 줄이 사람이 쓰는 "
        f"자리다(찾기: `✍`). 다 쓰면 이 줄은 지운다.",
        "",
        "수치와 표는 `evals/report.py`가 결과 CSV에서 채웠다. `<!-- auto:start "
        "… -->`와 `<!-- auto:end … -->` "
        "사이는 `python evals/report.py <결과 CSV…> --update <이 파일>`로 다시 채워지므로 **그 안은 손으로 "
        "고치지 않는다**(고치면 갱신이 거부된다). 그 밖의 글은 갱신해도 그대로 남는다.",
        "",
        "## 1. 무엇을 돌렸나",
        "",
        *block("overview"),
        *human("실행 배경", "실행한 날과 명령, 왜 이 실행인가, 지난 측정과 달라진 것"),
        "## 2. 한눈에 — 목표 대비",
        "",
        *block("targets"),
        *human("해석"),
        "## 3. 조건별 비교",
        "",
        *block("conditions"),
    ]
    for i, (before, after, label) in enumerate(STAGES, start=1):
        lines += [
            f"### 3.{i} {before} → {after}: {label}",
            "",
            *block(f"stage-{before}{after}".lower()),
            *human("해석"),
        ]
    lines += [
        "## 4. D 채택 조건 네 가지",
        "",
        "조건은 `docs/design/design.md` ⑤ 평가의 표 그대로다. 표의 근거는 자동으로 낼 수 있는 것뿐이고 "
        "판정은 사람이 쓴다.",
        "",
    ]
    for i, (name, item, method, rule) in enumerate(D_CONDITIONS, start=1):
        lines += [
            f"### 4.{i} {item}",
            "",
            f"- 방법: {method}",
            f"- D 채택 조건: **{rule}**",
            "",
            *block(name),
            *human("근거 사례", "사례 id와 무엇을 봤는지"),
            VERDICT,
            "",
        ]
    lines += [
        "## 5. 실패와 재생성",
        "",
        *block("failures"),
        *human("원인", "실행마다 규칙 문제인지 모델 문제인지, 고친 것"),
        "## 6. 사례별",
        "",
        *block("cases"),
        *human("눈에 띈 사례", "어떤 유형에서 실패하는가"),
        "## 7. 지연",
        "",
        *block("latency"),
        *human("해석"),
        "## 8. 사람 채점",
        "",
        *block("scores"),
        *human("해석", "누가 몇 건을 채점했나, 채점자 사이의 차이"),
        "## 9. 한계와 주의",
        "",
        *block("limits"),
        *human("사람이 덧붙일 것", "이 측정이 말해 주지 않는 것, 그래프 수집 비용, Console 청구액"),
        "## 10. 결론",
        "",
        f"설계서(`{DESIGN_DOC}`) §2의 핵심 가설:",
        "",
        *[f"{i}. {text}" for i, text in enumerate(HYPOTHESES, start=1)],
        "",
        f'§11은 D 실험의 완료 조건을 "{D_DECISION_RULE}"로 적는다.',
        "",
        *human("C가 A보다 나은가", "가설 1·2, 근거는 2·3·8절"),
        *human("D를 채택하는가", "가설 3, 4절의 판정 넷을 모아서 — 채택 / 보류 / 판단 불가"),
        *human("무엇을 줄였는가", "범위에서 뺀 것, 하지 못한 측정"),
        *human("다음에 할 것"),
    ]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def parse_regions(text: str) -> list[tuple[str, str, int, int]]:
    """있는 보고서의 자동 구역: (이름, 적힌 sha, 시작 줄 번호, 끝 줄 번호). 표식이 깨졌으면 MarkerError."""
    found: list[tuple[str, str, int, int]] = []
    current: tuple[str, str, int] | None = None
    for no, line in enumerate(text.split("\n")):
        stripped = line.strip()
        start, end = START.match(stripped), END.match(stripped)
        if start:
            if current is not None:
                raise MarkerError(
                    f"{no + 1}번째 줄: `{current[0]}` 구역이 닫히기 전에 `{start.group(1)}` 구역이 시작한다"
                )
            if any(name == start.group(1) for name, *_ in found):
                raise MarkerError(f"{no + 1}번째 줄: `{start.group(1)}` 구역이 두 번 나온다")
            current = (start.group(1), start.group(2), no)
        elif end:
            if current is None or current[0] != end.group(1):
                opened = f"`{current[0]}`" if current else "없음"
                raise MarkerError(
                    f"{no + 1}번째 줄: 짝이 맞지 않는 끝 표식 `{end.group(1)}` (열려 있는 구역: {opened})"
                )
            found.append((current[0], current[1], current[2], no))
            current = None
        elif MARKER_LIKE.match(stripped):
            raise MarkerError(f"{no + 1}번째 줄: 자동 구역 표식의 꼴이 깨졌다: {stripped[:60]}")
    if current is not None:
        raise MarkerError(f"`{current[0]}` 구역(시작 {current[2] + 1}번째 줄)의 끝 표식이 없다")
    return found


def update_text(existing: str, auto: dict[str, list[str]], *, discard_auto_edits: bool = False) -> str:
    """있는 보고서에서 자동 구역만 갈아 끼운 글. 구역 밖은 그대로다. 문제가 있으면 MarkerError."""
    found = parse_regions(existing)
    names = [name for name, *_ in found]
    missing = [n for n in auto if n not in names]
    unknown = [n for n in names if n not in auto]
    if missing or unknown:
        parts = []
        if missing:
            parts.append(f"파일에 없는 구역 {missing}")
        if unknown:
            parts.append(f"지금 뼈대에 없는 구역 {unknown}")
        raise MarkerError(
            "자동 구역이 지금의 뼈대와 다르다 — " + ", ".join(parts) + ". 새 뼈대를 따로 만들어 글을 옮긴다."
        )
    lines = existing.split("\n")
    edited = [name for name, sha, start, end in found if _sha(lines[start + 1 : end]) != sha]
    if edited and not discard_auto_edits:
        raise MarkerError(
            f"자동 구역 안을 손으로 고친 흔적이 있다: {edited}. 갱신하면 그 수정이 지워진다 — 고친 글을 구역 "
            "밖으로 옮기거나, 버려도 되면 --discard-auto-edits."
        )
    out: list[str] = []
    cursor = 0
    for name, _, start, end in found:
        out += lines[cursor:start]
        out += region(name, auto[name])
        cursor = end + 1
    out += lines[cursor:]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 명령줄
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="run_eval 결과 CSV → 최종 비교 보고서의 뼈대 (모델 호출 없음)")
    p.add_argument("results", type=Path, nargs="+", help="run_eval 요약 CSV (여러 개면 합친다)")
    p.add_argument(
        "--replace",
        action="store_true",
        help="같은 (사례, 조건, 회차)가 겹치면 뒤 파일이 앞 파일을 대체한다 (기본: 그대로 합치고 경고)",
    )
    p.add_argument("--conditions", default="", help="쉼표로 나눈 조건만 (기본: 파일에 있는 전부)")
    p.add_argument("--scores", type=Path, default=None, help="make_blind_sheet.py merge의 *-merged.csv")
    p.add_argument(
        "--score-columns", default="", help="점수로 읽을 루브릭 열, 쉼표 구분 (기본: 숫자 열 전부)"
    )
    p.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="사례 파일 (관계·상황·needs_graph)")
    p.add_argument("--characters", type=Path, default=DEFAULT_CHARACTERS, help="관계 축 (relation 열)")
    p.add_argument("--price", action="append", default=[], metavar="모델=입력:출력", help="단가 덮어쓰기")
    p.add_argument("--no-cost", action="store_true", help="비용을 내지 않는다")
    p.add_argument("--title", default="최종 비교 보고서", help="보고서 제목 (새로 만들 때만 쓰인다)")
    p.add_argument("--date", default="", help="보고서를 만든 날 (기본: 오늘, YYYY-MM-DD)")
    p.add_argument(
        "--show-text",
        action="store_true",
        help="위반 문구·추가된 항목·결과 문장도 싣는다 (보고서가 길어진다)",
    )
    p.add_argument("--out", type=Path, default=None, help="새 보고서를 이 파일에 쓴다 (기본: 표준 출력)")
    p.add_argument("--overwrite", action="store_true", help="--out 파일이 이미 있어도 새 뼈대로 덮어쓴다")
    p.add_argument(
        "--update",
        type=Path,
        default=None,
        help="있는 보고서의 자동 구역만 갈아 끼운다 (사람이 쓴 글은 그대로)",
    )
    p.add_argument(
        "--discard-auto-edits",
        action="store_true",
        help="--update: 자동 구역 안을 손으로 고친 것이 있어도 버리고 갱신한다",
    )
    args = p.parse_args(argv)
    if args.update is not None and args.out is not None:
        p.error("--update와 --out은 함께 쓰지 않는다 (--update는 그 파일을 고쳐 쓴다)")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        prices = None if args.no_cost else sm.parse_prices(args.price, sm.DEFAULT_PRICES)
        rows, replaced = sm.merge_rows([sm.read_rows(p) for p in args.results], replace=args.replace)
        case_rows, relations = [], []
        if args.cases.is_file():
            case_rows = cov.load_rows(args.cases)[0]
        if args.characters.is_file():
            relations = cov.load_relations(args.characters)
        report = build_report(
            rows,
            sources=[p.name for p in args.results],
            title=args.title,
            made_on=args.date,
            prices=prices,
            score_rows=sm.read_rows(args.scores) if args.scores else None,
            score_columns=[c.strip() for c in args.score_columns.split(",") if c.strip()] or None,
            conditions=[c.strip() for c in args.conditions.split(",") if c.strip()] or None,
            replaced=replaced if args.replace else None,
            case_rows=case_rows,
            relations=relations,
            cases_source=cov._display(args.cases),
            show_text=args.show_text,
        )
        if args.update is not None:
            existing = args.update.read_text(encoding="utf-8")
            text = update_text(existing, sections(report), discard_auto_edits=args.discard_auto_edits)
            args.update.write_text(text, encoding="utf-8", newline="\n")
            print(f"자동 구역 {len(SECTION_NAMES)}개 갱신 → {args.update}", file=sys.stderr)
            return 0
        text = render(report)
    except MarkerError as exc:
        print(f"오류: {exc}\n파일은 고치지 않았다.", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    if args.out is None:
        if isinstance(sys.stdout, io.TextIOWrapper):
            sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔(cp949)에서 '—'·'✍'가 깨지지 않게
        print(text, end="")
        return 0
    if args.out.exists() and not args.overwrite:
        print(
            f"오류: {args.out}이(가) 이미 있다 — 사람이 쓴 글을 지우지 않게 덮어쓰지 않는다. "
            "수치만 다시 채우려면 --update, 새 뼈대로 바꾸려면 --overwrite.",
            file=sys.stderr,
        )
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"보고서 뼈대 → {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
