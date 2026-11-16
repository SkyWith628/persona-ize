"""제안 정확도: 페르소나 제안이 사람이 쓴 정답 설정과 얼마나 맞는지 잰다 (설계 6.1의 주 결과).

사용 예:
    python evals/suggestion_accuracy.py --gold evals/gold_personas.csv --embedding fake
    python evals/suggestion_accuracy.py --gold evals/gold_personas.example.csv --no-graph

첨삭 문장을 만들지 않는다. **모델을 한 번도 부르지 않는다** — LLM 프로바이더를 fake로 고정하고, 그래프에
새로 수집하지도 않는다(수집은 haiku 호출이다). 그래프 근거는 이미 수집된 그래프가 있을 때만 읽는다
(`scripts/import_synthetic.py --user eval --graph` 또는 `run_eval.py --conditions D`를 한 번 돌린 뒤).

무엇을 비교하나
- 정답 파일의 한 줄 = (상대, as_of 날짜). 그날 끝(한국 시간)까지의 대화만 보고 만든 제안을 정답과 견준다.
- 제안 출처 세 가지를 **따로** 계산한다 (`suggestion_service.suggest_persona_for_contact(sources=...)`).
  `register` 규칙 말투 프로필 / `graph` 그래프 관계 사실 / `messages` 전체 기간 요약(바뀐 시점 없음).
  세 출처 모두 같은 기준 설정(관계 유형 기본값 + characters.csv 호칭, `run_eval.build_persona`)에서 출발한다.
- 공통 세밀도로 채점한다: 반말/존댓말 경계, 호칭(글자까지 같음 + 한쪽이 다른 쪽을 포함하면 인정하는
  너그러운 점수), 바뀐 날짜(±N일, 기본 3). 해요체/합쇼체 구분은 `register`에 대해서만 따로 본다
  (정답이 해요체·합쇼체인 줄만, 규칙 프로필의 관찰값으로 — 제안이 없었어도 채점된다).
- 말투 칸은 **그 출처가 격식을 바꾸자고 했을 때만** 제안으로 센다. 호칭만 제안하는 카드에 함께 보이는
  '지금 말투'는 제안이 아니다 (세 출처 모두 같은 기준).
- 값을 정해 주지 않고 묻는 카드(`ask`)는 말투 값을 낸 것이 아니다: 정답이 혼용이면 `correct`,
  정답이 한 단계면 `asked`(맞음·틀림 어느 쪽도 아니고 정밀도에서 뺀다), 제안이 없어야 하면 `wrong`.

칸에 적히는 값
- `correct` 제안했고 정답과 맞음 / `wrong` 제안했는데 틀림
- `asked` 값을 정해 주지 않고 물었음 (정답은 한 단계였다 — 따로 센다)
- `silent_ok` 제안하지 않았고 그래도 됨(정답이 '제안 없음'이거나 기준 설정이 이미 정답과 맞음)
- `missed` 제안했어야 하는데 하지 않음 / `no_date` 제안은 했지만 바뀐 날짜를 말하지 못함
- 빈칸: 정답 파일에서 그 항목을 비워 둠(채점하지 않음)
제안 정밀도 = correct / (correct + wrong). 조용했던 횟수는 따로 센다.

결과: evals/results/suggestion-accuracy-<시각>.csv (줄별) + -summary.csv (출처별 합계)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "evals") not in sys.path:  # 실행부가 같은 폴더의 run_eval을 불러온다
    sys.path.insert(0, str(ROOT / "evals"))
DEFAULT_GOLD = ROOT / "evals" / "gold_personas.csv"
SOURCES = ("register", "graph", "messages")
DATE_TOLERANCE_DAYS = 3

# 정답 파일의 말투 칸 → 내부 값. 'polite'는 해요체/합쇼체를 가리지 않은 존댓말, 'none'은 '제안 없음이 정답'.
GOLD_SPEECH = {
    "반말": "hae",
    "해체": "hae",
    "해요체": "haeyo",
    "합쇼체": "hapsyo",
    "존댓말": "polite",
    "혼용": "mixed",
    "-": "none",
}
LEVEL_KO = {
    "hae": "반말",
    "haeyo": "해요체",
    "hapsyo": "합쇼체",
    "polite": "존댓말",
    "mixed": "혼용",
    "none": "-",
}
_THREE_LEVELS = ("hae", "haeyo", "hapsyo")
NO_ADDRESS = "-"  # 정답 호칭 칸의 '-': 호칭을 제안하지 않는 것이 정답

Suggester = Callable[..., Awaitable[Any]]


# ---------------------------------------------------------------------------------------------------
# 순수 부분: 정답 읽기 · 제안에서 예측 꺼내기 · 채점 · 합계. 저장소도 앱 코드도 필요 없다.
# ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class GoldRow:
    """정답 설정 타임라인의 한 줄. None인 항목은 채점하지 않는다."""

    contact: str
    as_of: str  # YYYY-MM-DD. 그날 끝까지의 대화를 본 시점
    speech: str | None  # hae / haeyo / hapsyo / polite / mixed / none
    address: str | None  # 호칭 문자열, 또는 NO_ADDRESS
    since: date | None  # 지금 말투(호칭)로 쓴 첫 메시지 날짜
    note: str = ""


@dataclass(frozen=True)
class Prediction:
    """한 출처의 제안을 공통 세밀도로 줄인 것. None은 그 항목에 대해 말하지 않았다는 뜻이다."""

    speech: str | None = None  # hae / haeyo / hapsyo / mixed
    address: str | None = None
    since: date | None = None
    ask: bool = False  # 값을 정해 주지 않고 사용자에게 묻는 제안 (제안 카드)
    trigger: str = ""  # 그래프가 찾은 계기 사건 (사람이 맞는지 판정한다)
    scope: str = ""  # 그래프가 찾은 범위 단서
    reasons: str = ""

    @property
    def silent(self) -> bool:
        return self.speech is None and self.address is None


@dataclass(frozen=True)
class Score:
    boundary: str = ""
    address_exact: str = ""
    address_contains: str = ""
    since: str = ""
    level3: str = ""  # 해요체/합쇼체까지 맞았는가 (register만)


def read_table(path: Path) -> list[dict[str, str]]:
    """CSV를 읽는다. `#`으로 시작하는 줄과 빈 줄은 건너뛴다 (파일 머리에 안내를 적을 수 있게).

    마지막 열(메모)에 따옴표 없이 쉼표를 쓰면 칸이 넘친다 — 넘친 칸은 마지막 열에 쉼표로 다시 붙인다.
    """
    with path.open(encoding="utf-8-sig", newline="") as fp:
        lines = [ln for ln in fp if ln.strip() and not ln.lstrip().startswith("#")]
    reader = csv.DictReader(lines)
    last = (reader.fieldnames or [""])[-1]
    out: list[dict[str, str]] = []
    for raw in reader:
        extra = raw.pop(None, None)  # type: ignore[call-overload]
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        if extra:
            row[last.strip()] = ",".join([row.get(last.strip(), ""), *extra]).strip()
        out.append(row)
    return out


def parse_day(raw: str) -> date | None:
    raw = raw.strip()
    if not raw or raw == "-":
        return None
    return datetime.strptime(raw[:10], "%Y-%m-%d").date()


def parse_gold_rows(rows: Iterable[dict[str, str]], where: str = "정답 파일") -> list[GoldRow]:
    out: list[GoldRow] = []
    for n, row in enumerate(rows, start=1):
        contact, as_of = row.get("contact", ""), row.get("as_of", "")
        if not contact or not as_of:
            raise SystemExit(f"{where} {n}번째 줄: contact와 as_of는 비울 수 없습니다.")
        try:
            parse_day(as_of)
            since = parse_day(row.get("since", ""))
        except ValueError:
            raise SystemExit(f"{where} {n}번째 줄: 날짜는 2026-10-21 형식으로 적습니다.") from None
        raw_speech = row.get("speech_level", "")
        if raw_speech and raw_speech not in GOLD_SPEECH:
            raise SystemExit(
                f"{where} {n}번째 줄: speech_level '{raw_speech}' — "
                f"{' / '.join(GOLD_SPEECH)} 중 하나이거나 빈칸이어야 합니다."
            )
        out.append(
            GoldRow(
                contact=contact,
                as_of=as_of[:10],
                speech=GOLD_SPEECH.get(raw_speech),
                address=_squash(row.get("address_term", "")) or None,
                since=since,
                note=row.get("note", ""),
            )
        )
    return out


def load_gold(path: Path) -> list[GoldRow]:
    if not path.is_file():
        raise SystemExit(
            f"정답 파일이 없습니다: {path}\n"
            "평가 담당이 evals/gold_personas.csv 를 씁니다. 형식은 evals/gold_personas.example.csv 참고."
        )
    rows = parse_gold_rows(read_table(path), str(path))
    if not rows:
        raise SystemExit(f"정답 줄이 없습니다: {path}")
    return rows


def gold_for(gold: list[GoldRow], names: Iterable[str], as_of: str) -> GoldRow | None:
    """타임라인에서 그 시점에 유효한 줄: 이름이 맞는 줄 가운데 as_of가 그날 이전인 것 중 가장 늦은 줄.

    `as_of`가 비면(지금) 가장 늦은 줄. 평가 사례의 날짜가 정답 줄의 날짜와 꼭 같지 않아도 되게 한다.
    """
    wanted = {n for n in names if n}
    rows = [g for g in gold if g.contact in wanted and (not as_of or g.as_of <= as_of[:10])]
    return max(rows, key=lambda g: g.as_of) if rows else None


def level_of_formality(formality: int) -> str:
    """격식 1~2 = 반말, 3 = 해요체, 4~5 = 합쇼체 (설계 5.1, `app.ai.register`와 같은 대응)."""
    return "hae" if formality <= 2 else "haeyo" if formality == 3 else "hapsyo"


def polite(level: str | None) -> bool | None:
    if level in ("haeyo", "hapsyo", "polite"):
        return True
    if level == "hae":
        return False
    return None


def _squash(text: str) -> str:
    return " ".join(text.split())


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """dict든 객체든 같은 이름으로 읽는다 (제안 카드는 dataclass, 그 안의 칸은 dict)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_day(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return parse_day(str(value))
    except ValueError:
        return None


def _message_ids(value: Any) -> str:
    ids = _get(value, "message_ids") or []
    return f" ({', '.join(str(i) for i in ids)})" if ids else ""


def prediction_from_suggestion(suggestion: Any, source: str) -> Prediction:
    """제안(`Suggestion`)에서 한 출처가 말한 것만 꺼낸다.

    - 근거(signal) 가운데 그 출처의 것만 본다. 격식 값은 말투 단계로 바꾼다(1~2 반말, 3 해요체, 4~5 합쇼체).
      `messages`의 격식 2는 "존댓말과 반말이 섞여 있음"이라는 뜻이라 혼용으로 본다.
    - `register`는 제안 카드가 있으면 카드가 우선이다 — 사용자가 화면에서 보는 것이 카드다. 카드의 현재 말투
      단계와 시작일, 호칭을 읽고, 두 값 상태(상황별 말투)면 혼용으로 본다.
    - 카드의 말투 단계는 카드가 **말투에 대해 말할 때만** 예측으로 읽는다: 격식 제안 값이 있거나
      (`proposed.formality`), 격식 근거가 있거나, 묻는 카드이거나, 두 값 상태일 때. 호칭만 제안하는 카드에도
      '지금 말투'는 보이지만 그것은 제안이 아니다 — 읽으면 다른 출처(조용함)와 다른 기준으로 채점된다.
    """
    speech: str | None = None
    address: str | None = None
    since: date | None = None
    address_since: date | None = None
    reasons: list[str] = []
    for s in _get(suggestion, "signals") or []:
        if _get(s, "source", "messages") != source:
            continue
        field, value = _get(s, "field"), _get(s, "value")
        if field == "formality" and isinstance(value, int):
            speech = "mixed" if source == "messages" and value == 2 else level_of_formality(value)
            since = _as_day(_get(s, "since"))
        elif field == "address_term" and value:
            address = _squash(str(value))
            address_since = _as_day(_get(s, "since"))
        else:
            continue  # 친근함·이모지는 정답 설정 타임라인의 채점 대상이 아니다
        reasons.append(str(_get(s, "reason", "")))

    ask, trigger, scope = False, "", ""
    card = _get(suggestion, "card") if source == "register" else None
    if card is not None:
        current = _get(card, "speech_current")
        ask = bool(_get(card, "ask", False))
        says_speech = (
            speech is not None
            or isinstance(_get(_get(card, "proposed"), "formality"), int)
            or ask
            or bool(_get(card, "two_valued"))
        )
        if says_speech and _get(current, "level") in _THREE_LEVELS:
            speech = _get(current, "level")
            since = _as_day(_get(current, "since")) or since
        if _get(card, "two_valued"):
            speech = "mixed"
        term = _get(_get(card, "address"), "term")
        if term:
            address = _squash(str(term))
            address_since = _as_day(_get(_get(card, "address"), "since")) or address_since
        event = _get(card, "trigger")
        if event:
            trigger = f"{_get(event, 'event', '')}{_message_ids(event)}"
        scope = " / ".join(f"{_get(h, 'text', '')}{_message_ids(h)}" for h in _get(card, "scope_hints") or [])
    return Prediction(
        speech=speech,
        address=address,
        since=since or address_since,
        ask=ask,
        trigger=trigger,
        scope=scope,
        reasons=" | ".join(r for r in reasons if r),
    )


def proposed_changes(suggestion: Any, source: str = "register") -> dict[str, Any]:
    """제안을 승인하면 설정에 들어갈 값: `{"formality": 4, "address_term": "서연 파트장님"}` 가운데 있는 것만.

    제안 카드가 있으면 카드의 `proposed`(화면의 승인 버튼이 넣는 값), 없으면 그 출처의 격식·호칭 근거.
    `run_eval.py --apply-suggestion`이 쓴다 — 평가 안에서만 적용하고 어디에도 저장하지 않는다.
    """
    out: dict[str, Any] = {}
    card = _get(suggestion, "card") if source == "register" else None
    if card is not None:
        proposed = _get(card, "proposed") or {}
        if isinstance(_get(proposed, "formality"), int):
            out["formality"] = _get(proposed, "formality")
        if _get(proposed, "address_term"):
            out["address_term"] = _squash(str(_get(proposed, "address_term")))
        return out
    for s in _get(suggestion, "signals") or []:
        if _get(s, "source", "messages") != source:
            continue
        field, value = _get(s, "field"), _get(s, "value")
        if field == "formality" and isinstance(value, int):
            out["formality"] = value
        elif field == "address_term" and value:
            out["address_term"] = _squash(str(value))
    return out


def _contains(a: str, b: str) -> bool:
    return bool(a) and bool(b) and (a in b or b in a)


def score_boundary(gold: str | None, pred: str | None, base_formality: int, *, ask: bool = False) -> str:
    """반말/존댓말 경계. 혼용이 정답이면 혼용(= 값을 정하지 않고 묻는 제안)만 맞다.

    `ask`는 값을 정해 주지 않고 묻는 카드다. 카드에 '지금 말투'가 보여도 설정에 넣을 값을 낸 것이 아니므로
    한 단계가 정답인 줄에서 맞음으로 세지 않는다(`asked`) — 묻기만 한 것이 정밀도를 올리지 않게.
    """
    if gold is None:
        return ""
    if ask and pred is not None:
        if gold == "mixed":
            return "correct"
        return "wrong" if gold == "none" else "asked"
    if pred is None:
        if gold == "none":
            return "silent_ok"
        if gold == "mixed":
            return "missed"
        return "silent_ok" if polite(level_of_formality(base_formality)) == polite(gold) else "missed"
    if gold == "none":
        return "wrong"
    if gold == "mixed" or pred == "mixed":
        return "correct" if gold == pred else "wrong"
    return "correct" if polite(pred) == polite(gold) else "wrong"


def score_address(gold: str | None, pred: str | None, base_address: str | None) -> tuple[str, str]:
    """(글자까지 같음, 포함하면 인정). '파트장님'과 '서연 파트장님'은 뒤쪽 점수에서만 맞다."""
    if gold is None:
        return "", ""
    base = _squash(base_address or "")
    if pred is None:
        if gold == NO_ADDRESS:
            return "silent_ok", "silent_ok"
        return (
            "silent_ok" if base == gold else "missed",
            "silent_ok" if _contains(base, gold) else "missed",
        )
    if gold == NO_ADDRESS:
        return "wrong", "wrong"
    return ("correct" if pred == gold else "wrong", "correct" if _contains(pred, gold) else "wrong")


def score_since(gold: date | None, pred: Prediction, tolerance_days: int) -> str:
    if gold is None:
        return ""
    if pred.silent:
        return "missed"
    if pred.since is None:
        return "no_date"
    return "correct" if abs((pred.since - gold).days) <= tolerance_days else "wrong"


def score_level3(gold: str | None, pred: str | None) -> str:
    """해요체/합쇼체 구분. 정답이 해요체·합쇼체인 줄만 채점한다 (정답이 반말인 줄은 경계 칸이 본다 —
    여기에 넣으면 구분이 걸리지 않은 줄이 분모를 채워 정확도가 부풀려진다)."""
    if gold not in ("haeyo", "hapsyo") or pred not in _THREE_LEVELS:
        return ""
    return "correct" if gold == pred else "wrong"


def score_row(
    gold: GoldRow,
    pred: Prediction,
    base_formality: int,
    base_address: str | None,
    *,
    tolerance_days: int = DATE_TOLERANCE_DAYS,
    with_level3: bool = False,
    observed: str | None = None,
) -> Score:
    """한 줄 채점. `observed`는 규칙 프로필이 본 말투 단계(기준 설정과 무관)이고 3단계 채점에만 쓴다.

    제안은 "지금 설정과 다를 때"만 나오므로, 제안만 보면 기준 설정이 이미 맞는 상대의 해요체/합쇼체 구분을
    잴 수 없다. 관찰값이 있으면 그것으로, 없으면 제안의 말투로 3단계를 채점한다.
    """
    exact, contains = score_address(gold.address, pred.address, base_address)
    return Score(
        boundary=score_boundary(gold.speech, pred.speech, base_formality, ask=pred.ask),
        address_exact=exact,
        address_contains=contains,
        since=score_since(gold.since, pred, tolerance_days),
        level3=score_level3(gold.speech, observed or pred.speech) if with_level3 else "",
    )


def matching_fields(
    proposed: dict[str, Any], gold: GoldRow | None, base_formality: int, base_address: str | None
) -> set[str]:
    """제안 값 가운데 정답과 맞는 항목 이름 (`--apply-suggestion matching`). 정답 줄이 없으면 아무것도 없다.

    채점과 같은 기준이다: 격식은 반말/존댓말 경계, 호칭은 글자까지 같아야 한다.
    """
    if gold is None:
        return set()
    ok: set[str] = set()
    if "formality" in proposed:
        level = level_of_formality(int(proposed["formality"]))
        if score_boundary(gold.speech, level, base_formality) == "correct":
            ok.add("formality")
    if "address_term" in proposed:
        if score_address(gold.address, str(proposed["address_term"]), base_address)[0] == "correct":
            ok.add("address_term")
    return ok


def _ratio(hit: int, total: int) -> str:
    return f"{hit / total:.2f}" if total else ""


SUMMARY_FIELDS = [
    "source",
    "rows",
    "silent_rows",
    "emitted",
    "precision",
    "boundary_correct",
    "boundary_wrong",
    "boundary_silent_ok",
    "boundary_missed",
    "boundary_asked",
    "boundary_precision",
    "address_exact_correct",
    "address_exact_wrong",
    "address_silent_ok",
    "address_missed",
    "address_exact_precision",
    "address_contains_precision",
    "since_correct",
    "since_scored",
    "level3_correct",
    "level3_scored",
]


def summarize(rows: list[dict[str, Any]], sources: Iterable[str]) -> list[dict[str, Any]]:
    """출처별 합계. 쓸 수 없었던 출처(그래프 꺼짐)는 줄 수 0으로 나온다."""
    out: list[dict[str, Any]] = []
    for source in sources:
        mine = [r for r in rows if r["source"] == source and r["available"] == "Y"]

        def count(column: str, status: str, mine: list[dict[str, Any]] = mine) -> int:
            return sum(1 for r in mine if r[column] == status)

        b_ok, b_bad = count("boundary", "correct"), count("boundary", "wrong")
        a_ok, a_bad = count("address_exact", "correct"), count("address_exact", "wrong")
        c_ok, c_bad = count("address_contains", "correct"), count("address_contains", "wrong")
        out.append(
            {
                "source": source,
                "rows": len(mine),
                "silent_rows": sum(1 for r in mine if not r["pred_speech"] and not r["pred_address"]),
                "emitted": b_ok + b_bad + a_ok + a_bad,
                # 제안 정밀도: 채점된 제안(말투 + 호칭) 가운데 정답과 맞은 비율
                "precision": _ratio(b_ok + a_ok, b_ok + b_bad + a_ok + a_bad),
                "boundary_correct": b_ok,
                "boundary_wrong": b_bad,
                "boundary_silent_ok": count("boundary", "silent_ok"),
                "boundary_missed": count("boundary", "missed"),
                "boundary_asked": count("boundary", "asked"),
                "boundary_precision": _ratio(b_ok, b_ok + b_bad),
                "address_exact_correct": a_ok,
                "address_exact_wrong": a_bad,
                "address_silent_ok": count("address_exact", "silent_ok"),
                "address_missed": count("address_exact", "missed"),
                "address_exact_precision": _ratio(a_ok, a_ok + a_bad),
                "address_contains_precision": _ratio(c_ok, c_ok + c_bad),
                "since_correct": count("since", "correct"),
                "since_scored": sum(1 for r in mine if r["since"]),
                "level3_correct": count("level3", "correct"),
                "level3_scored": sum(1 for r in mine if r["level3"]),
            }
        )
    return out


def format_summary(summary: list[dict[str, Any]]) -> str:
    head = [
        "출처",
        "줄",
        "조용",
        "제안",
        "정밀도",
        "경계 ○/×/놓침/물음",
        "호칭 ○/×/놓침",
        "호칭(포함)",
        "날짜",
        "해요/합쇼",
    ]
    table = [head]
    for s in summary:
        if not s["rows"]:
            table.append([s["source"], "0", "-", "-", "-", "(쓸 수 없음)", "-", "-", "-", "-"])
            continue
        table.append(
            [
                s["source"],
                str(s["rows"]),
                str(s["silent_rows"]),
                str(s["emitted"]),
                s["precision"] or "-",
                f"{s['boundary_correct']}/{s['boundary_wrong']}/{s['boundary_missed']}/{s['boundary_asked']}",
                f"{s['address_exact_correct']}/{s['address_exact_wrong']}/{s['address_missed']}",
                s["address_contains_precision"] or "-",
                f"{s['since_correct']}/{s['since_scored']}",
                f"{s['level3_correct']}/{s['level3_scored']}" if s["level3_scored"] else "-",
            ]
        )
    # 한글은 터미널에서 두 칸을 차지한다 — 칸 너비를 그 기준으로 맞춘다
    width = [max(_cells(row[i]) for row in table) for i in range(len(head))]
    return "\n".join(
        "  ".join(cell + " " * (width[i] - _cells(cell)) for i, cell in enumerate(row)).rstrip()
        for row in table
    )


def _cells(text: str) -> int:
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


DETAIL_FIELDS = [
    "contact",
    "as_of",
    "source",
    "available",
    "gold_speech",
    "gold_address",
    "gold_since",
    "base_formality",
    "base_address",
    "based_on_messages",
    "pred_speech",
    "pred_address",
    "pred_since",
    "observed_speech",
    "ask",
    "boundary",
    "address_exact",
    "address_contains",
    "since",
    "level3",
    "trigger",
    "scope_hints",
    "reasons",
    "note",
]


def detail_row(
    gold: GoldRow,
    source: str,
    pred: Prediction | None,
    score: Score,
    base_formality: int,
    base_address: str | None,
    based_on_messages: int | str = "",
    observed: str | None = None,
) -> dict[str, Any]:
    """결과 CSV의 한 줄. `pred`가 None이면 그 출처를 쓸 수 없었다는 뜻이다(합계에서 뺀다)."""
    p = pred or Prediction()
    return {
        "contact": gold.contact,
        "as_of": gold.as_of,
        "source": source,
        "available": "Y" if pred is not None else "N",
        "gold_speech": LEVEL_KO.get(gold.speech or "", ""),
        "gold_address": gold.address or "",
        "gold_since": gold.since.isoformat() if gold.since else "",
        "base_formality": base_formality,
        "base_address": base_address or "",
        "based_on_messages": based_on_messages,
        "pred_speech": LEVEL_KO.get(p.speech or "", ""),
        "pred_address": p.address or "",
        "pred_since": p.since.isoformat() if p.since else "",
        "observed_speech": LEVEL_KO.get(observed or "", ""),
        "ask": "Y" if p.ask else "",
        "boundary": score.boundary,
        "address_exact": score.address_exact,
        "address_contains": score.address_contains,
        "since": score.since,
        "level3": score.level3,
        "trigger": p.trigger,
        "scope_hints": p.scope,
        "reasons": p.reasons,
        "note": gold.note,
    }


# ---------------------------------------------------------------------------------------------------
# 실행부: 데이터셋 적재 → (상대, as_of, 출처)마다 제안 → 채점 → CSV. 여기서부터 앱 코드를 쓴다.
# ---------------------------------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="제안 정확도 (생성 없음, 모델 호출 없음)")
    p.add_argument("--gold", type=Path, default=DEFAULT_GOLD, help="정답 설정 타임라인 CSV")
    p.add_argument("--characters", type=Path, default=ROOT / "evals" / "characters.csv")
    p.add_argument("--dataset", type=Path, default=ROOT / "data" / "synthetic" / "sample_conversations.json")
    p.add_argument("--user", default="eval", help="과거 대화를 적재·조회할 사용자 id")
    p.add_argument("--sources", default=",".join(SOURCES), help="쉼표 구분: register,graph,messages")
    p.add_argument(
        "--date-tolerance", type=int, default=DATE_TOLERANCE_DAYS, help="바뀐 날짜를 맞았다고 볼 ±일수"
    )
    p.add_argument(
        "--base",
        choices=["aliases", "default"],
        default="aliases",
        help="기준 설정: aliases=관계 기본값+characters.csv 호칭(run_eval과 같음) / default=관계 기본값만",
    )
    p.add_argument("--embedding", choices=["local", "fake"], default=None, help="EMBEDDING_PROVIDER 덮어쓰기")
    p.add_argument(
        "--register-min-messages",
        type=int,
        default=None,
        help="내 메시지가 이보다 적으면 register 출처는 제안하지 않는다 (기본: 제품 값 0 = 문턱 없음)",
    )
    p.add_argument(
        "--segment-k",
        type=int,
        default=None,
        help="새 말투 구간을 여는 연속 메시지 수 K (기본: 제품 값 2). 설계 3.2의 K=1/2/3 민감도 보고용",
    )
    p.add_argument("--no-graph", action="store_true", help="그래프를 쓰지 않는다 (graph 출처·계기 조회 없음)")
    p.add_argument("--out", type=Path, default=None, help="줄별 CSV 경로")
    args = p.parse_args(argv)
    args.source_list = [s.strip().lower() for s in args.sources.split(",") if s.strip()]
    bad = [s for s in args.source_list if s not in SOURCES]
    if bad or not args.source_list:
        p.error(f"알 수 없는 출처: {bad} ({', '.join(SOURCES)} 중에서)")
    if args.date_tolerance < 0:
        p.error("--date-tolerance는 0 이상")
    if args.segment_k is not None and args.segment_k < 1:
        p.error("--segment-k는 1 이상")
    return args


def load_suggester() -> Suggester:
    """제안 함수. 평가 도구를 읽는 것만으로 서비스 계층이 올라오지 않게 쓸 때 가져온다."""
    from app.services.suggestion_service import suggest_persona_for_contact

    return suggest_persona_for_contact


async def observed_level(
    state: Any, user_id: str, contact: Any, as_of: datetime | None, k: int | None = None
) -> str | None:
    """규칙 프로필이 본 지금 말투 단계 (hae/haeyo/hapsyo, 두 말투가 섞였으면 mixed, 구간이 없으면 None).

    제안과 달리 기준 설정을 보지 않는다 — 해요체/합쇼체 구분을 재는 데만 쓴다. `k`는 `--segment-k`.
    """
    from app.ai.register import SEGMENT_K, build_profile
    from app.services.suggestion_service import own_messages

    profile = build_profile(
        await own_messages(state, user_id, contact), contact.alias, as_of=as_of, k=k or SEGMENT_K
    )
    if profile.two_valued:
        return "mixed"
    current = profile.current_speech
    return current.value if current is not None and current.status == "confirmed" else None


async def evaluate(
    state: Any,
    args: argparse.Namespace,
    suggest: Suggester | None = None,
    observe: Callable[..., Awaitable[str | None]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(줄별 결과, 출처별 합계). `state`에 데이터셋을 적재(멱등)한 뒤 정답 줄마다 세 출처의 제안을 채점한다.

    그래프에는 수집하지 않는다(`graph=False`) — 수집은 모델 호출이다. 이미 수집된 그래프만 읽는다.
    `suggest`·`observe`는 테스트가 바꿔 끼운다. `suggest`만 주면 관찰값(3단계 채점용)은 건너뛴다.
    """
    import run_eval as harness  # 같은 폴더의 평가 하니스: 등장인물·기준 설정·as_of 규칙을 그대로 쓴다

    from app.domain.persona import default_persona
    from app.services.synthetic_loader import load_synthetic

    if suggest is None:
        suggest, observe = load_suggester(), observe or observed_level
    gold = load_gold(args.gold)
    characters = harness.load_characters(args.characters)
    by_name = {ch["name"]: ch for ch in characters.values()}

    report = await load_synthetic(state, args.dataset, args.user, args.characters, graph=False)
    if report.failed:
        raise SystemExit("과거 대화 적재 실패: " + "; ".join(report.failed))
    contacts = {c.alias: c for c in await state.contacts.list(args.user)}
    graph_on = bool(getattr(state, "graph_enabled", False))
    print(
        f"과거 대화 적재: user={args.user} storage={state.storage} 상대 {len(report.contacts)} "
        f"새 메시지 {report.imported} 중복 {report.duplicates} / 그래프 {'켜짐' if graph_on else '꺼짐'}"
    )

    rows: list[dict[str, Any]] = []
    for g in gold:
        character = by_name.get(g.contact) or characters.get(g.contact)  # 이름, 아니면 relation(팀장/동기)
        contact = contacts.get(character["name"] if character else g.contact)
        if contact is None:
            raise SystemExit(f"정답 파일의 상대 '{g.contact}'가 데이터셋·characters.csv에 없습니다.")
        if character is not None and args.base == "aliases":
            base = harness.build_persona(character)
        else:
            base = default_persona(contact.relationship_type)
        as_of = harness.parse_as_of(g.as_of)
        for source in args.source_list:
            if source == "graph" and not graph_on:
                rows.append(detail_row(g, source, None, Score(), base.formality, base.address_term))
                continue
            # 값을 안 주면 제품과 같다 (메시지 수 문턱 없음, K=2). 문턱의 영향이나 K 민감도를 볼 때만 넘긴다.
            extra: dict[str, Any] = {}
            if getattr(args, "register_min_messages", None) is not None:
                extra["register_min_messages"] = args.register_min_messages
            segment_k = getattr(args, "segment_k", None)
            if segment_k is not None:
                extra["k"] = segment_k
            suggestion = await suggest(
                state, args.user, contact, as_of=as_of, sources=frozenset({source}), base=base, **extra
            )
            pred = prediction_from_suggestion(suggestion, source)
            observed = None
            if source == "register" and observe is not None:
                observed = await (
                    observe(state, args.user, contact, as_of, segment_k)
                    if segment_k is not None
                    else observe(state, args.user, contact, as_of)
                )
            score = score_row(
                g,
                pred,
                base.formality,
                base.address_term,
                tolerance_days=args.date_tolerance,
                with_level3=source == "register",
                observed=observed,
            )
            rows.append(
                detail_row(
                    g,
                    source,
                    pred,
                    score,
                    base.formality,
                    base.address_term,
                    _get(suggestion, "based_on_messages", ""),
                    observed,
                )
            )
    return rows, summarize(rows, args.source_list)


def write_results(
    rows: list[dict[str, Any]], summary: list[dict[str, Any]], out_csv: Path
) -> tuple[Path, Path]:
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    summary_csv = out_csv.with_name(out_csv.stem + "-summary.csv")
    for path, fields, data in ((out_csv, DETAIL_FIELDS, rows), (summary_csv, SUMMARY_FIELDS, summary)):
        with path.open("w", encoding="utf-8-sig", newline="") as fp:  # 엑셀에서 한글이 깨지지 않게 BOM
            writer = csv.DictWriter(fp, fieldnames=fields)
            writer.writeheader()
            writer.writerows(data)
    return out_csv, summary_csv


async def run(args: argparse.Namespace, settings: Any = None) -> Path:
    import run_eval as harness  # backend를 import 경로에 올린다

    from app.api.deps import build_state
    from app.core.config import Settings

    if settings is None:
        # LLM은 fake로 고정한다 — .env가 anthropic이어도 이 도구는 모델을 부르지 않는다.
        overrides: dict[str, str] = {"llm_provider": "fake"}
        if args.embedding:
            overrides["embedding_provider"] = args.embedding
        if args.no_graph:
            overrides["neo4j_uri"] = ""
        settings = Settings(**overrides)  # type: ignore[arg-type]  # .env도 읽는다
    state = build_state(settings)
    try:
        rows, summary = await evaluate(state, args)
    finally:
        await state.close()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_csv, summary_csv = write_results(
        rows, summary, args.out or harness.RESULTS_DIR / f"suggestion-accuracy-{stamp}.csv"
    )
    print()
    print(format_summary(summary))
    print(
        "\n경계 = 맞음/틀림/놓침/물음(값을 정하지 않고 물음 — 정밀도에서 뺀다), 호칭 = 맞음/틀림/놓침, "
        "날짜·해요/합쇼 = 맞음/채점한 줄.\n정밀도 = 맞은 제안 / 낸 제안. "
        "해요/합쇼는 정답이 해요체·합쇼체인 줄을 규칙 프로필의 관찰값으로 채점한다 (제안이 없어도)."
    )
    if args.segment_k is not None:
        print(f"구간 규칙 K={args.segment_k} (제품 기본은 2)")
    if any(r["trigger"] for r in rows):
        print("계기 사건(trigger 열)이 맞는지는 사람이 줄마다 판정합니다.")
    print(f"\n줄별 → {out_csv}\n합계 → {summary_csv}")
    return out_csv


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    asyncio.run(run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
