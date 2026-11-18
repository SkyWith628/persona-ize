"""평가 도구: 제안 정확도 채점(`evals/suggestion_accuracy.py`)과 설정 실험 플래그(`evals/run_eval.py`).

DB도 모델도 쓰지 않는다. 채점은 손으로 만든 입력으로, 실행 흐름은 인메모리 저장소 + fake 프로바이더 +
가짜 제안 함수로 본다 — 제안 서비스가 바뀌어도 채점 규칙의 테스트는 그대로 남는다.
"""

from __future__ import annotations

import csv
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.ai.style_suggest import Signal
from app.api.deps import build_state
from app.core.config import Settings
from app.domain.persona import DEFAULT_PERSONAS, PersonaSettings
from app.domain.titles import VALIDATOR_VERSION

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "evals"))

import run_eval  # noqa: E402
import suggestion_accuracy as sa  # noqa: E402

EXAMPLE_GOLD = ROOT / "evals" / "gold_personas.example.csv"
COLLEAGUE = DEFAULT_PERSONAS["colleague"]  # 격식 3, 호칭 없음


def _gold(
    speech: str | None = None,
    address: str | None = None,
    since: date | None = None,
    as_of: str = "2026-10-21",
) -> sa.GoldRow:
    return sa.GoldRow(contact="이서연", as_of=as_of, speech=speech, address=address, since=since)


def _card(**over: Any) -> SimpleNamespace:
    """c3(이서연)의 제안 카드 모양 (설계 5.1의 예)."""
    card: dict[str, Any] = {
        "contact_alias": "이서연",
        "speech_previous": {"level": "hae", "count": 3, "message_ids": ["c3:m02", "c3:m04", "c3:m06"]},
        "speech_current": {
            "level": "hapsyo",
            "count": 2,
            "message_ids": ["c3:m08", "c3:m10"],
            "since": "2026-10-21",
        },
        "two_valued": None,
        "address": {
            "term": "서연 파트장님",
            "count": 1,
            "since": "2026-10-21",
            "status": "candidate",
            "message_ids": ["c3:m08"],
        },
        "trigger": {"event": "파트장 발령", "at": "2026-10-17", "message_ids": ["c3:m05"]},
        "scope_hints": [{"text": "사적으로는 편하게 하자", "message_ids": ["c3:m07"]}],
        "ask": True,
        "ask_reason": "특정 자리에서만 쓰는 말투일 수 있습니다.",
        "proposed": {"formality": 4, "address_term": "서연 파트장님"},
    }
    card.update(over)
    return SimpleNamespace(**card)


def _suggestion(signals: list[Signal] | None = None, card: Any = None) -> SimpleNamespace:
    return SimpleNamespace(based_on_messages=5, settings=None, signals=signals or [], card=card)


# ---- 정답 파일 -----------------------------------------------------------------------------------


def test_read_table_skips_comment_lines_and_gold_rows_parse(tmp_path: Path) -> None:
    path = tmp_path / "gold.csv"
    path.write_text(
        "# 설명 줄\n\ncontact,as_of,speech_level,address_term,since,note\n"
        "이서연,2026-10-21,합쇼체,서연  파트장님,2026-10-21,메모, 쉼표\n"
        "박지현,2026-10-04,-,-,,\n"
        "김준호,2026-10-05,존댓말,,-,\n",
        encoding="utf-8",
    )
    rows = sa.load_gold(path)
    assert rows[0] == sa.GoldRow(
        "이서연", "2026-10-21", "hapsyo", "서연 파트장님", date(2026, 10, 21), "메모, 쉼표"
    )  # 메모 칸의 따옴표 없는 쉼표는 메모에 다시 붙는다
    assert (rows[1].speech, rows[1].address, rows[1].since) == ("none", "-", None)
    assert (rows[2].speech, rows[2].address, rows[2].since) == ("polite", None, None)  # 빈칸 = 채점 안 함


def test_gold_rejects_unknown_speech_level_and_missing_keys() -> None:
    with pytest.raises(SystemExit, match="speech_level"):
        sa.parse_gold_rows([{"contact": "이서연", "as_of": "2026-10-21", "speech_level": "공손"}])
    with pytest.raises(SystemExit, match="contact와 as_of"):
        sa.parse_gold_rows([{"contact": "이서연", "as_of": ""}])
    with pytest.raises(SystemExit, match="날짜"):
        sa.parse_gold_rows([{"contact": "이서연", "as_of": "10월 21일"}])


def test_example_gold_file_is_valid_and_names_known_contacts() -> None:
    rows = sa.load_gold(EXAMPLE_GOLD)
    names = {ch["name"] for ch in run_eval.load_characters(run_eval.DEFAULT_CHARACTERS).values()}
    assert {g.contact for g in rows} == names
    seoyeon = [g for g in rows if g.contact == "이서연"]
    assert [g.speech for g in seoyeon] == ["hae", "hae", "hapsyo", "hapsyo"]  # 바뀌기 전과 뒤
    assert EXAMPLE_GOLD.read_text(encoding="utf-8").startswith("# 예시 파일")


def test_gold_for_picks_the_latest_row_on_or_before_the_date() -> None:
    gold = [_gold("hae", as_of="2026-10-07"), _gold("hapsyo", as_of="2026-10-21")]
    assert sa.gold_for(gold, ["이서연"], "2026-10-20").speech == "hae"  # type: ignore[union-attr]
    assert sa.gold_for(gold, ["동기", "이서연"], "2026-10-22").speech == "hapsyo"  # type: ignore[union-attr]
    assert sa.gold_for(gold, ["이서연"], "").speech == "hapsyo"  # type: ignore[union-attr]
    assert sa.gold_for(gold, ["이서연"], "2026-10-01") is None
    assert sa.gold_for(gold, ["박지현"], "2026-10-22") is None


# ---- 채점 ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("gold", "pred", "base_formality", "expected"),
    [
        ("hapsyo", "hapsyo", 3, "correct"),
        ("hapsyo", "haeyo", 3, "correct"),  # 공통 세밀도: 존댓말이면 맞다
        ("polite", "hapsyo", 1, "correct"),
        ("hae", "hapsyo", 1, "wrong"),
        ("hapsyo", "hae", 3, "wrong"),
        ("hapsyo", None, 5, "silent_ok"),  # 기준 설정이 이미 존댓말
        ("hapsyo", None, 1, "missed"),
        ("hae", None, 3, "missed"),
        ("hae", None, 2, "silent_ok"),
        ("none", None, 3, "silent_ok"),  # 제안 없음이 정답
        ("none", "hae", 3, "wrong"),
        ("mixed", "mixed", 3, "correct"),
        ("mixed", "hapsyo", 3, "wrong"),
        ("hae", "mixed", 1, "wrong"),
        ("mixed", None, 3, "missed"),
        (None, "hae", 3, ""),  # 정답을 비워 둠
    ],
)
def test_score_boundary(gold: str | None, pred: str | None, base_formality: int, expected: str) -> None:
    assert sa.score_boundary(gold, pred, base_formality) == expected


@pytest.mark.parametrize(
    ("gold", "pred", "expected"),
    [
        ("mixed", "hapsyo", "correct"),  # 혼용이 정답이면 값을 정하지 않고 묻는 것이 맞다 (c3: 변화 뒤 2건)
        ("mixed", "mixed", "correct"),
        ("hapsyo", "hapsyo", "asked"),  # 카드에 보인 단계가 맞아도 값을 낸 것이 아니다 — 맞음으로 세지 않는다
        ("hae", "hapsyo", "asked"),
        ("hapsyo", "mixed", "asked"),
        ("none", "hapsyo", "wrong"),  # 제안이 없어야 하는데 물었다
        (None, "hapsyo", ""),
    ],
)
def test_score_boundary_for_a_card_that_asks(gold: str | None, pred: str, expected: str) -> None:
    assert sa.score_boundary(gold, pred, 3, ask=True) == expected
    # 말투에 대해 아무 말도 하지 않았으면 ask는 상관없다
    assert sa.score_boundary("hapsyo", None, 5, ask=True) == "silent_ok"


@pytest.mark.parametrize(
    ("gold", "pred", "base", "expected"),
    [
        ("서연 파트장님", "서연 파트장님", None, ("correct", "correct")),
        ("서연 파트장님", "파트장님", None, ("wrong", "correct")),  # 포함하면 너그러운 점수만
        ("팀장님", "지현 팀장님", None, ("wrong", "correct")),
        ("팀장님", "유진아", None, ("wrong", "wrong")),
        ("-", "서연아", None, ("wrong", "wrong")),
        ("-", None, "준호", ("silent_ok", "silent_ok")),
        ("팀장님", None, "팀장님", ("silent_ok", "silent_ok")),
        ("팀장님", None, "지현 팀장님", ("missed", "silent_ok")),
        ("서연아", None, None, ("missed", "missed")),
        (None, "서연아", None, ("", "")),
    ],
)
def test_score_address(
    gold: str | None, pred: str | None, base: str | None, expected: tuple[str, str]
) -> None:
    assert sa.score_address(gold, pred, base) == expected


def test_score_since_uses_the_tolerance_window() -> None:
    gold = date(2026, 10, 21)
    said = sa.Prediction(speech="hapsyo", since=date(2026, 10, 18))
    assert sa.score_since(gold, said, 3) == "correct"
    assert sa.score_since(gold, said, 2) == "wrong"
    assert (
        sa.score_since(gold, sa.Prediction(speech="hapsyo"), 3) == "no_date"
    )  # 전체 기간 요약은 날짜가 없다
    assert sa.score_since(gold, sa.Prediction(), 3) == "missed"
    assert sa.score_since(None, said, 3) == ""


def test_score_row_level3_is_only_for_the_register_source_and_prefers_the_observed_level() -> None:
    gold = _gold("hapsyo", "서연 파트장님", date(2026, 10, 21))
    pred = sa.Prediction(speech="haeyo", address="서연 파트장님", since=date(2026, 10, 21))
    plain = sa.score_row(gold, pred, 3, None)
    assert plain == sa.Score("correct", "correct", "correct", "correct", "")
    assert sa.score_row(gold, pred, 3, None, with_level3=True).level3 == "wrong"
    assert sa.score_row(gold, pred, 3, None, with_level3=True, observed="hapsyo").level3 == "correct"
    # 정답이 '존댓말'(구분 안 함)이면 3단계는 채점하지 않는다
    assert sa.score_row(_gold("polite"), pred, 3, None, with_level3=True).level3 == ""
    # 제안이 없어도 관찰값이 있으면 3단계를 잰다 (기준 설정이 이미 맞는 상대)
    silent = sa.score_row(_gold("hapsyo"), sa.Prediction(), 5, None, with_level3=True, observed="hapsyo")
    assert (silent.boundary, silent.level3) == ("silent_ok", "correct")
    # 해요체/합쇼체 구분은 정답이 그 둘인 줄에서만 잰다 — 반말이 정답인 줄은 분모에 넣지 않는다
    casual = sa.score_row(_gold("hae"), sa.Prediction(), 1, None, with_level3=True, observed="hae")
    assert casual.level3 == ""
    assert sa.score_row(
        _gold("haeyo"), sa.Prediction(), 3, None, with_level3=True, observed="hae"
    ).level3 == ("wrong")


def test_score_row_counts_an_asking_card_as_asked_not_correct() -> None:
    pred = sa.prediction_from_suggestion(
        _suggestion([Signal("formality", 4, "내 메시지: 10/21부터 합쇼체 2건", source="register")], _card()),
        "register",
    )
    assert pred.ask is True and pred.speech == "hapsyo"
    single = sa.score_row(_gold("hapsyo", "서연 파트장님", date(2026, 10, 21)), pred, 3, None)
    assert (single.boundary, single.address_exact, single.since) == ("asked", "correct", "correct")
    assert sa.score_row(_gold("mixed"), pred, 3, None).boundary == "correct"


# ---- 제안에서 예측 꺼내기 --------------------------------------------------------------------------


def test_prediction_reads_only_the_signals_of_the_source() -> None:
    suggestion = _suggestion(
        [
            Signal("formality", 2, "존댓말과 반말이 섞여 있음"),
            Signal("friendliness", 4, "ㅋㅋ"),
            Signal("formality", 4, "그래프: 10/17부터 존댓말", source="graph", since=date(2026, 10, 17)),
            Signal("address_term", "파트장님", "그래프: 10/21부터", source="graph", since=date(2026, 10, 21)),
        ]
    )
    messages = sa.prediction_from_suggestion(suggestion, "messages")
    assert (messages.speech, messages.address, messages.since) == ("mixed", None, None)  # 격식 2 = 혼용
    graph = sa.prediction_from_suggestion(suggestion, "graph")
    assert (graph.speech, graph.address, graph.since) == ("hapsyo", "파트장님", date(2026, 10, 17))
    assert "10/17" in graph.reasons and "ㅋㅋ" not in messages.reasons
    assert sa.prediction_from_suggestion(suggestion, "register").silent


def test_prediction_for_register_prefers_the_card() -> None:
    signals = [Signal("formality", 4, "내 메시지: 10/21부터 합쇼체 2건", source="register")]
    pred = sa.prediction_from_suggestion(_suggestion(signals, _card()), "register")
    assert (pred.speech, pred.address, pred.since, pred.ask) == (
        "hapsyo",
        "서연 파트장님",
        date(2026, 10, 21),
        True,
    )
    assert pred.trigger == "파트장 발령 (c3:m05)"
    assert pred.scope == "사적으로는 편하게 하자 (c3:m07)"
    two = sa.prediction_from_suggestion(
        _suggestion([], _card(two_valued={"hapsyo": 3, "hae": 3})), "register"
    )
    assert two.speech == "mixed"
    # 카드는 규칙 프로필의 것이다 — 다른 출처의 예측에 섞이지 않는다
    assert sa.prediction_from_suggestion(_suggestion([], _card()), "graph").silent


def test_address_only_card_is_not_a_speech_prediction() -> None:
    """호칭만 제안하는 카드의 '지금 말투'는 격식 제안이 아니다 — 말투 칸은 조용한 것으로 채점한다."""
    card = _card(
        speech_previous=None,
        speech_current={"level": "hapsyo", "count": 5, "message_ids": [], "since": "2026-10-05"},
        address={
            "term": "파트장님",
            "count": 2,
            "since": "2026-10-12",
            "status": "confirmed",
            "message_ids": [],
        },
        trigger=None,
        scope_hints=[],
        ask=False,
        ask_reason=None,
        proposed={"formality": None, "address_term": "파트장님"},
    )
    signals = [Signal("address_term", "파트장님", "2회", source="register", since=date(2026, 10, 12))]
    pred = sa.prediction_from_suggestion(_suggestion(signals, card), "register")
    assert (pred.speech, pred.address, pred.since) == (None, "파트장님", date(2026, 10, 12))
    # 조용한 다른 출처와 같은 칸 값이 나온다
    quiet = sa.Prediction()
    for gold, base_formality in (("hapsyo", 4), ("hae", 4)):
        assert sa.score_boundary(gold, pred.speech, base_formality) == sa.score_boundary(
            gold, quiet.speech, base_formality
        )
    assert sa.score_boundary("hapsyo", pred.speech, 4) == "silent_ok"
    assert sa.score_boundary("hae", pred.speech, 4) == "missed"
    # 격식을 제안하는 카드는 예전처럼 읽는다
    proposing = _card(ask=False, scope_hints=[], proposed={"formality": 4, "address_term": None})
    assert sa.prediction_from_suggestion(_suggestion([], proposing), "register").speech == "hapsyo"


def test_proposed_changes_and_matching_fields() -> None:
    assert sa.proposed_changes(_suggestion([], _card())) == {"formality": 4, "address_term": "서연 파트장님"}
    nothing = _card(proposed={"formality": None, "address_term": None})
    assert sa.proposed_changes(_suggestion([], nothing)) == {}
    from_signals = _suggestion(
        [
            Signal("formality", 1, "반말", source="register"),
            Signal("address_term", "유진아", "1회 관찰", source="register"),
            Signal("formality", 5, "전체 기간"),
        ]
    )
    assert sa.proposed_changes(from_signals) == {"formality": 1, "address_term": "유진아"}

    proposed = {"formality": 4, "address_term": "파트장님"}
    gold = _gold("hapsyo", "서연 파트장님")
    assert sa.matching_fields(proposed, gold, 3, None) == {"formality"}  # 호칭은 글자까지 같아야 한다
    assert sa.matching_fields(proposed, _gold("hae", "파트장님"), 3, None) == {"address_term"}
    assert sa.matching_fields(proposed, None, 3, None) == set()


# ---- 합계 ----------------------------------------------------------------------------------------


def test_summarize_counts_precision_and_silence_per_source() -> None:
    def row(
        source: str, pred: sa.Prediction | None, gold: sa.GoldRow, base: PersonaSettings
    ) -> dict[str, Any]:
        score = (
            sa.score_row(gold, pred, base.formality, base.address_term, with_level3=source == "register")
            if pred is not None
            else sa.Score()
        )
        return sa.detail_row(gold, source, pred, score, base.formality, base.address_term)

    after = _gold("hapsyo", "서연 파트장님", date(2026, 10, 21))
    before = _gold("hae", "서연아", as_of="2026-10-07")
    rows = [
        row("register", sa.Prediction("hapsyo", "서연 파트장님", date(2026, 10, 21)), after, COLLEAGUE),
        row("register", sa.Prediction("hae", "서연"), before, COLLEAGUE),
        row("messages", sa.Prediction("mixed"), after, COLLEAGUE),
        row("messages", sa.Prediction(), before, COLLEAGUE),
        row("graph", None, after, COLLEAGUE),
        row("graph", None, before, COLLEAGUE),
    ]
    register, graph, messages = sa.summarize(rows, sa.SOURCES)
    assert (register["rows"], register["silent_rows"], register["emitted"]) == (2, 0, 4)
    assert register["precision"] == "0.75"  # 말투 2 + 호칭 1 맞음 / 제안 4
    assert (register["address_exact_precision"], register["address_contains_precision"]) == ("0.50", "1.00")
    assert (register["since_correct"], register["since_scored"]) == (1, 1)
    # 해요/합쇼 구분은 정답이 합쇼체인 줄(after)만 채점한다. 정답이 반말인 줄(before)은 넣지 않는다
    assert (register["level3_correct"], register["level3_scored"]) == (1, 1)
    assert register["boundary_asked"] == 0
    assert (messages["silent_rows"], messages["boundary_wrong"], messages["boundary_missed"]) == (1, 1, 1)
    assert messages["address_missed"] == 2 and messages["precision"] == "0.00"
    assert graph["rows"] == 0 and graph["precision"] == ""  # 그래프가 꺼져 있었다 — 조용한 것과 구분한다
    table = sa.format_summary([register, graph, messages])
    assert "(쓸 수 없음)" in table and "register" in table


# ---- run_eval 인자 -------------------------------------------------------------------------------


def test_run_eval_defaults_leave_the_new_flags_off() -> None:
    args = run_eval.parse_args([])
    assert (args.apply_suggestion, args.gold, args.persona_override, args.invalidation) == (
        "off",
        None,
        None,
        None,
    )
    assert args.condition_list == ["A", "B"]


def test_run_eval_flag_validation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    override = tmp_path / "oracle.csv"
    override.write_text("relation,formality,address_term\n동기,4,서연 파트장님\n", encoding="utf-8")
    ok = run_eval.parse_args(
        ["--apply-suggestion", "matching", "--gold", str(EXAMPLE_GOLD), "--invalidation", "artifacts"]
    )
    assert (ok.apply_suggestion, ok.gold, ok.invalidation) == ("matching", EXAMPLE_GOLD, "artifacts")
    assert run_eval.parse_args(["--persona-override", str(override)]).persona_override == override
    for bad in (
        ["--apply-suggestion", "matching"],  # 정답 없이 matching
        ["--apply-suggestion", "all", "--persona-override", str(override)],  # 둘을 한 번에
        ["--persona-override", str(tmp_path / "missing.csv")],
        ["--invalidation", "drop"],
        ["--apply-suggestion", "some"],
    ):
        with pytest.raises(SystemExit):
            run_eval.parse_args(bad)
    capsys.readouterr()


def test_suggestion_accuracy_args() -> None:
    args = sa.parse_args(["--sources", "register, messages", "--date-tolerance", "1", "--no-graph"])
    assert (args.source_list, args.date_tolerance, args.no_graph, args.base) == (
        ["register", "messages"],
        1,
        True,
        "aliases",
    )
    assert sa.parse_args([]).source_list == ["register", "graph", "messages"]
    assert (sa.parse_args([]).segment_k, sa.parse_args(["--segment-k", "3"]).segment_k) == (None, 3)
    with pytest.raises(SystemExit):
        sa.parse_args(["--segment-k", "0"])
    with pytest.raises(SystemExit):
        sa.parse_args(["--sources", "register,llm"])


# ---- 설정에 적용 ---------------------------------------------------------------------------------


def test_apply_override_changes_only_the_filled_columns() -> None:
    persona, changed = run_eval.apply_override(COLLEAGUE, {"formality": "4", "address_term": "서연 파트장님"})
    assert (persona.formality, persona.address_term) == (4, "서연 파트장님")
    assert changed == ["formality=4", "address_term=서연 파트장님"]
    assert persona.friendliness == COLLEAGUE.friendliness and COLLEAGUE.formality == 3  # 원본은 그대로

    same, changed = run_eval.apply_override(COLLEAGUE, {"formality": "3", "address_term": "", "notes": ""})
    assert same is COLLEAGUE and changed == []

    superior = DEFAULT_PERSONAS["superior"].model_copy(update={"address_term": "지현 팀장님"})
    cleared, changed = run_eval.apply_override(superior, {"address_term": "-", "emoji": "rare"})
    assert cleared.address_term is None and cleared.emoji == "rare"
    assert changed == ["address_term=", "emoji=rare"]

    with pytest.raises(ValueError, match="formality"):
        run_eval.apply_override(COLLEAGUE, {"formality": "9"})


def test_apply_override_maps_a_gold_speech_level_to_formality() -> None:
    superior = DEFAULT_PERSONAS["superior"]  # 격식 5
    assert (
        run_eval.apply_override(superior, {"speech_level": "합쇼체"})[0].formality == 5
    )  # 범위 안이면 그대로
    assert run_eval.apply_override(COLLEAGUE, {"speech_level": "합쇼체"})[0].formality == 4
    assert run_eval.apply_override(COLLEAGUE, {"speech_level": "반말"})[0].formality == 1
    assert run_eval.apply_override(superior, {"speech_level": "해요체"})[0].formality == 3
    assert run_eval.apply_override(COLLEAGUE, {"speech_level": "존댓말"})[0] is COLLEAGUE
    assert run_eval.apply_override(COLLEAGUE, {"speech_level": "혼용"})[0] is COLLEAGUE
    # formality 열이 있으면 그쪽이 우선이다
    assert run_eval.apply_override(COLLEAGUE, {"formality": "5", "speech_level": "반말"})[0].formality == 5


def test_override_for_matches_relation_or_contact_and_follows_the_timeline() -> None:
    character = {"name": "이서연", "relation": "동기"}
    rows = [
        {"contact": "이서연", "as_of": "2026-10-07", "speech_level": "반말"},
        {"contact": "이서연", "as_of": "2026-10-21", "speech_level": "합쇼체"},
        {"relation": "팀장", "formality": "5"},
    ]
    assert run_eval.override_for(rows, character, "2026-10-20")["speech_level"] == "반말"  # type: ignore[index]
    assert run_eval.override_for(rows, character, "2026-10-22")["speech_level"] == "합쇼체"  # type: ignore[index]
    assert run_eval.override_for(rows, character, "")["speech_level"] == "합쇼체"  # type: ignore[index]
    assert run_eval.override_for(rows, character, "2026-10-01") is None
    assert run_eval.override_for(rows, {"name": "박지현", "relation": "팀장"}, "2026-10-05") == rows[2]
    assert run_eval.override_for(rows, {"name": "최유진", "relation": "친구"}, "2026-10-05") is None


def test_apply_suggestion_all_and_matching() -> None:
    suggestion = _suggestion([], _card())
    persona, changed = run_eval.apply_suggestion(COLLEAGUE, suggestion)
    assert (persona.formality, persona.address_term) == (4, "서연 파트장님")
    assert changed == ["formality=4", "address_term=서연 파트장님"]
    assert COLLEAGUE.address_term is None  # 사본에만 적용한다

    # 정답이 반말이면 격식 제안은 버리고, 글자까지 같은 호칭만 적용한다
    private = _gold("hae", "서연 파트장님")
    persona, changed = run_eval.apply_suggestion(COLLEAGUE, suggestion, mode="matching", gold=private)
    assert (persona.formality, persona.address_term, changed) == (
        3,
        "서연 파트장님",
        ["address_term=서연 파트장님"],
    )
    # 정답 줄이 없으면 아무것도 적용하지 않는다
    assert run_eval.apply_suggestion(COLLEAGUE, suggestion, mode="matching", gold=None) == (COLLEAGUE, [])
    # 제안이 없으면 그대로, 설정 길이 상한을 넘는 호칭은 버린다
    assert run_eval.apply_suggestion(COLLEAGUE, _suggestion()) == (COLLEAGUE, [])
    long_term = _card(proposed={"formality": None, "address_term": "가" * 31})
    assert run_eval.apply_suggestion(COLLEAGUE, _suggestion([], long_term)) == (COLLEAGUE, [])


# ---- 실행 흐름 (인메모리 저장소, fake 프로바이더, 가짜 제안 함수) -------------------------------------


def _settings() -> Settings:
    return Settings(_env_file=None, llm_provider="fake", embedding_provider="fake")  # type: ignore[call-arg]


async def _fake_suggest(
    state: Any, user_id: str, contact: Any, *, as_of: datetime | None, sources: frozenset[str], base: Any
) -> SimpleNamespace:
    """이서연에게 10/21 이후에만 규칙 프로필이 카드를 낸다. 나머지는 조용하다."""
    changed = as_of is not None and as_of > run_eval.parse_as_of("2026-10-20")  # type: ignore[operator]
    if contact.alias == "이서연" and sources == frozenset({"register"}) and changed:
        signal = Signal("formality", 4, "내 메시지: 10/21부터 합쇼체 2건", source="register")
        return _suggestion([signal], _card())
    return _suggestion()


@pytest.mark.anyio
async def test_suggestion_accuracy_evaluate_scores_each_gold_row_per_source() -> None:
    args = sa.parse_args(["--gold", str(EXAMPLE_GOLD), "--user", "eval-tools-accuracy"])
    state = build_state(_settings())
    try:
        rows, summary = await sa.evaluate(state, args, suggest=_fake_suggest)
    finally:
        await state.close()
    gold = sa.load_gold(EXAMPLE_GOLD)
    assert len(rows) == len(gold) * 3
    assert {r["available"] for r in rows if r["source"] == "graph"} == {"N"}  # 그래프 없이 돌았다
    hit = next(
        r for r in rows if (r["contact"], r["as_of"], r["source"]) == ("이서연", "2026-10-21", "register")
    )
    # 묻는 카드다: 정답(합쇼체)과 같은 단계를 보여 줬어도 값을 낸 것이 아니라 'asked'
    assert (hit["boundary"], hit["address_exact"], hit["since"], hit["level3"]) == (
        "asked",
        "correct",
        "correct",
        "correct",
    )
    assert (hit["pred_speech"], hit["ask"], hit["trigger"]) == ("합쇼체", "Y", "파트장 발령 (c3:m05)")
    assert (hit["base_formality"], hit["base_address"]) == (3, "")  # 동료는 호칭을 비운 기준 설정
    register = next(s for s in summary if s["source"] == "register")
    assert register["rows"] == len(gold) and register["precision"] == "1.00"  # 호칭 2건만 제안으로 센다
    assert (register["boundary_correct"], register["boundary_asked"]) == (0, 2)
    assert register["silent_rows"] == len(gold) - 2  # 이서연 10/21, 10/22만 말했다
    assert next(s for s in summary if s["source"] == "graph")["rows"] == 0


@pytest.mark.anyio
async def test_write_results_roundtrip(tmp_path: Path) -> None:
    gold = _gold("hapsyo", "서연 파트장님", date(2026, 10, 21))
    pred = sa.Prediction("hapsyo", "서연 파트장님", date(2026, 10, 21))
    rows = [sa.detail_row(gold, "register", pred, sa.score_row(gold, pred, 3, None), 3, None, 5, "hapsyo")]
    out, summary_path = sa.write_results(rows, sa.summarize(rows, ["register"]), tmp_path / "acc.csv")
    with out.open(encoding="utf-8-sig", newline="") as fp:
        read = list(csv.DictReader(fp))
    assert list(read[0]) == sa.DETAIL_FIELDS and read[0]["observed_speech"] == "합쇼체"
    with summary_path.open(encoding="utf-8-sig", newline="") as fp:
        assert list(csv.DictReader(fp))[0]["precision"] == "1.00"


async def _run_eval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> list[dict[str, str]]:
    monkeypatch.setattr(run_eval, "RESULTS_DIR", tmp_path)  # 원시 jsonl도 임시 폴더로
    monkeypatch.setattr(run_eval, "load_suggester", lambda: _fake_suggest)
    args = run_eval.parse_args([*argv, "--out", str(tmp_path / "out.csv")])
    state = build_state(_settings())
    try:
        out = await run_eval._run_with_state(state, args)
    finally:
        await state.close()
    with out.open(encoding="utf-8", newline="") as fp:
        return list(csv.DictReader(fp))


@pytest.mark.anyio
async def test_run_eval_default_run_keeps_old_columns_and_fills_the_new_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = await _run_eval(tmp_path, monkeypatch, ["--conditions", "A", "--case", "E001,E004"])
    columns = list(rows[0])
    assert columns[-4:] == [
        "validator_version",
        "persona_formality",
        "persona_address_term",
        "suggestion_applied",
    ]
    assert columns[:5] == ["case_id", "split", "relation", "condition", "repeat"]
    assert {"context_facts", "context_msg_ids", "context_recall", "context_hit", "warning_hit"} <= set(
        columns
    )
    e001, e004 = rows
    assert (e001["persona_formality"], e001["persona_address_term"]) == ("5", "지현 팀장님")
    assert (e004["persona_formality"], e004["persona_address_term"], e004["suggestion_applied"]) == (
        "3",
        "",
        "",
    )
    assert e004["result_text"] == "어제는 제가 좀 예민했던 것 같아요. 미안합니다."  # fake는 원문 그대로
    assert e004["validator_version"] in ("", VALIDATOR_VERSION)  # 워크플로가 채우면 규칙 버전


@pytest.mark.anyio
async def test_run_eval_apply_suggestion_changes_only_the_case_persona(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = await _run_eval(
        tmp_path,
        monkeypatch,
        [
            "--conditions",
            "A",
            "--case",
            "E002,E004",
            "--user",
            "eval-tools-apply",
            "--apply-suggestion",
            "all",
        ],
    )
    e002, e004 = rows
    assert (e002["persona_formality"], e002["suggestion_applied"]) == ("5", "none")
    assert (e004["persona_formality"], e004["persona_address_term"]) == ("4", "서연 파트장님")
    assert e004["suggestion_applied"] == "formality=4;address_term=서연 파트장님"


@pytest.mark.anyio
async def test_run_eval_matching_applies_only_what_agrees_with_gold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gold = tmp_path / "gold.csv"
    gold.write_text(
        "contact,as_of,speech_level,address_term,since,note\n이서연,2026-10-21,반말,서연 파트장님,,\n",
        encoding="utf-8",
    )
    rows = await _run_eval(
        tmp_path,
        monkeypatch,
        ["--conditions", "A", "--case", "E004", "--apply-suggestion", "matching", "--gold", str(gold)],
    )
    assert (rows[0]["persona_formality"], rows[0]["persona_address_term"]) == ("3", "서연 파트장님")
    assert rows[0]["suggestion_applied"] == "address_term=서연 파트장님"


@pytest.mark.anyio
async def test_run_eval_persona_override_accepts_the_gold_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = await _run_eval(
        tmp_path,
        monkeypatch,
        ["--conditions", "A", "--case", "E003,E004", "--persona-override", str(EXAMPLE_GOLD)],
    )
    e003, e004 = rows
    assert (e003["persona_formality"], e003["suggestion_applied"]) == ("1", "override:none")  # 이미 반말
    assert (e004["persona_formality"], e004["persona_address_term"]) == ("4", "서연 파트장님")
    assert e004["suggestion_applied"] == "override:formality=4;address_term=서연 파트장님"


@pytest.mark.anyio
async def test_suggestion_accuracy_with_the_real_suggestion_service() -> None:
    """실제 제안 서비스로 예시 정답을 채점한다 (그래프 없음). 설계 3.2의 c3 손계산과 맞아야 한다."""
    pytest.importorskip("app.ai.register")
    pytest.importorskip("app.services.suggestion_service")
    args = sa.parse_args(
        ["--gold", str(EXAMPLE_GOLD), "--user", "eval-tools-real", "--sources", "register,messages"]
    )
    state = build_state(_settings())
    try:
        rows, summary = await sa.evaluate(state, args)
    finally:
        await state.close()
    by_key = {(r["contact"], r["as_of"], r["source"]): r for r in rows}
    after = by_key[("이서연", "2026-10-21", "register")]
    assert (after["pred_speech"], after["pred_since"], after["observed_speech"]) == (
        "합쇼체",
        "2026-10-21",
        "합쇼체",
    )
    # 변화 뒤 2건뿐이라 카드가 묻는다 — 값을 정해 주지 않았으므로 맞음이 아니라 'asked'
    assert (after["ask"], after["boundary"]) == ("Y", "asked")
    assert (after["since"], after["level3"]) == ("correct", "correct")
    # 메시지 수 문턱이 없다: 내 메시지 2~3건인 이른 날짜에도 구간 규칙(연속 2건)만으로 반말을 읽는다
    early = by_key[("이서연", "2026-10-07", "register")]
    assert (early["based_on_messages"], early["observed_speech"]) == (2, "반말")
    assert early["pred_address"] == "서연아" and early["address_exact"] == "correct"
    # 해요/합쇼 구분은 정답이 해요체·합쇼체인 줄만 (박지현 2 + 이서연 2)
    register = next(s for s in summary if s["source"] == "register")
    assert register["level3_scored"] == 4
    assert after["pred_address"] == "서연 파트장님" and after["address_exact"] == "correct"
    # 전체 기간 요약은 바뀐 시점을 모른다 — 날짜를 말하지 못한다
    assert by_key[("이서연", "2026-10-21", "messages")]["since"] in ("no_date", "missed")
    # 대화가 시작되기 전에는 어느 출처도 제안하지 않는다
    assert by_key[("박지현", "2026-10-04", "register")]["boundary"] == "silent_ok"
    assert [s["rows"] for s in summary] == [12, 12]


@pytest.mark.anyio
async def test_suggestion_accuracy_segment_k_reaches_the_profile() -> None:
    """`--segment-k`가 프로필 계산까지 닿는다 — K=1/2/3 민감도 표를 낼 수 있다 (설계 3.2)."""
    gold = EXAMPLE_GOLD
    seen: dict[int | None, str] = {}
    for k in (None, 3):
        argv = ["--gold", str(gold), "--user", f"eval-tools-k{k}", "--sources", "register"]
        if k is not None:
            argv += ["--segment-k", str(k)]
        state = build_state(_settings())
        try:
            rows, _ = await sa.evaluate(state, sa.parse_args(argv))
        finally:
            await state.close()
        row = next(r for r in rows if (r["contact"], r["as_of"]) == ("이서연", "2026-10-21"))
        seen[k] = row["observed_speech"]
    # K=2(기본): 합쇼체 2건으로 구간이 열린다. K=3: 열리지 않고 두 값 상태(혼용)로 드러난다
    assert seen == {None: "합쇼체", 3: "혼용"}


# ---- 실패 신호 열 · 말투 지문 · 층별 겹침 (2026-11-18) -------------------------------------------


def _result(**over: Any) -> Any:
    from app.domain.rewrite import RewriteResult, ValidationReport

    base: dict[str, Any] = {
        "status": "recommended",
        "result_text": "내일 오전까지 보내드리겠습니다.",
        "intent": "report",
        "preserved_items": [],
        "validation": ValidationReport(passed=True),
        "attempts": 1,
        "model_version": "fake",
        "prompt_version": "rewrite-v3",
        "retrieval_version": "none",
        "latency_ms": 1,
    }
    return RewriteResult(**{**base, **over})


def _example(ex_id: str, purpose: str = "style") -> Any:
    from app.domain.rewrite import RetrievedExample

    return RetrievedExample(id=ex_id, purpose=purpose, text="예시", speaker_role="me")  # type: ignore[arg-type]


def test_failure_signals_are_numbers_and_flags_only() -> None:
    from app.domain.rewrite import AttemptRecord, Violation

    draft = "내일 오전까지 보낼게요."
    ok = run_eval.failure_signals(_result(evidence_ids=["s1"]), draft, [_example("s1")], "C")
    assert list(ok) == run_eval.SIGNAL_FIELDS
    assert ok["fm11_first_checks"] == "" and ok["fm13_attempts"] == 1
    assert not any(ok[k] for k in ("fm24_no_examples", "fm25_evidence_unused", "fm32_register_warned"))
    assert ok["fm33_fallback"] is False
    assert ok["fm23_length_ratio"] == f"{len('내일 오전까지 보내드리겠습니다.') / len(draft):.2f}"

    # 예시를 받고도 근거를 적지 않았다 / C인데 문체 예시가 없다(사실 맥락만 있는 것은 예시가 아니다)
    assert run_eval.failure_signals(_result(), draft, [_example("s1")], "C")["fm25_evidence_unused"] is True
    empty = run_eval.failure_signals(_result(), draft, [_example("g1", "context")], "D")
    assert (empty["fm24_no_examples"], empty["fm25_evidence_unused"]) == (True, False)
    assert run_eval.failure_signals(_result(), draft, [], "B")["fm24_no_examples"] is False  # B는 원래 없다

    violations = [
        Violation(check="fact_added", detail="'금요일'이 추가됨"),
        Violation(check="preserve_missing", detail="'내일 오전'이 빠짐"),
        Violation(check="fact_added", detail="'3시'가 추가됨"),
    ]
    fallback = run_eval.failure_signals(
        _result(
            status="fallback_original",
            result_text=draft,
            attempts=2,
            attempt_log=[AttemptRecord(attempt=1, passed=False, violations=violations)],
            warnings=["결과의 말투(반말/존댓말)가 설정한 격식과 다릅니다. 보내기 전에 확인하세요."],
        ),
        draft,
        [],
        "A",
    )
    assert fallback["fm11_first_checks"] == "fact_added;preserve_missing"  # 종류만, 문장은 없다
    assert (fallback["fm13_attempts"], fallback["fm33_fallback"], fallback["fm32_register_warned"]) == (
        2,
        True,
        True,
    )
    assert "금요일" not in "".join(str(v) for v in fallback.values())


@pytest.mark.anyio
async def test_run_eval_writes_the_signal_columns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = await _run_eval(
        tmp_path, monkeypatch, ["--conditions", "A,C", "--case", "E001", "--user", "eval-tools-signals"]
    )
    a, c = rows
    assert set(run_eval.SIGNAL_FIELDS) <= set(a)
    assert (a["fm13_attempts"], a["fm23_length_ratio"], a["fm33_fallback"]) == ("1", "1.00", "False")
    assert a["fm24_no_examples"] == "False"
    # fake 프로바이더는 받은 예시를 근거로 적는다 — 예시가 들어갔고 근거도 있으니 둘 다 거짓이다
    assert c["retrieved_ids"] and c["evidence_ids"]
    assert (c["fm24_no_examples"], c["fm25_evidence_unused"]) == ("False", "False")


@pytest.mark.anyio
async def test_run_eval_voice_fills_the_block_only_when_asked_and_only_for_c_d(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_eval.parse_args([]).voice is False
    monkeypatch.setattr(run_eval, "RESULTS_DIR", tmp_path)
    seen: dict[tuple[bool, str], str | None] = {}
    for voice in (False, True):
        argv = [
            "--conditions",
            "B,C",
            "--case",
            "E003",
            "--user",
            "eval-tools-voice",
            "--prompt",
            "rewrite-v5",
        ]
        args = run_eval.parse_args([*argv, *(["--voice"] if voice else []), "--out", str(tmp_path / "v.csv")])
        state = build_state(_settings())
        real = state.workflow.run

        async def spy(
            inp: Any, contact: Any, persona: Any, *, _voice: bool = voice, _real: Any = real
        ) -> Any:
            seen[(_voice, inp.condition)] = inp.voice_block
            return await _real(inp, contact, persona)

        monkeypatch.setattr(state.workflow, "run", spy)
        try:
            await run_eval._run_with_state(state, args)
        finally:
            await state.close()
    assert seen[(False, "B")] is None and seen[(False, "C")] is None  # 플래그 없이는 예전 입력 그대로
    assert seen[(True, "B")] is None  # B는 과거 대화를 쓰지 않는 조건이다
    assert seen[(True, "C")] and "내 메시지" in seen[(True, "C")]  # type: ignore[operator]


def test_layer_overlap_bounds() -> None:
    import layer_diff

    same = {"style": ["네 팀장님, 맡겠습니다.", "확인했습니다."], "prompt": "[설정] 격식 높음"}
    other = {"style": ["ㅇㅋㅋㅋ"], "prompt": "ㅋㅋㅋ"}
    none = {"style": [], "prompt": "[설정] 격식 높음"}
    assert layer_diff.overlap(same, dict(same)) == (1.0, 1.0)
    ids, prompt = layer_diff.overlap(same, other)
    assert ids == 0.0 and prompt == 0.0
    assert layer_diff.overlap(none, dict(none))[0] is None  # 예시가 없으면 닮음을 말할 수 없다
    assert layer_diff.overlap(same, none)[0] is None
    with pytest.raises(SystemExit):
        layer_diff.parse_args(["--provider", "anthropic"])  # --generate 없이 실제 모델을 고를 수 없다


@pytest.mark.anyio
async def test_layer_diff_compares_every_pair_without_calling_the_model_or_keeping_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import layer_diff

    out = tmp_path / "layers.csv"
    args = layer_diff.parse_args(
        ["--conditions", "B,C", "--case", "E001", "--user", "eval-tools-layers", "--voice", "--out", str(out)]
    )
    state = build_state(_settings().model_copy(update={"prompt_version": "rewrite-v5"}))

    async def boom(*_: Any, **__: Any) -> Any:
        raise AssertionError("--generate 없이는 모델을 부르지 않는다")

    monkeypatch.setattr(state.workflow.provider, "generate", boom)
    try:
        await layer_diff.run_with_state(state, args)
    finally:
        await state.close()
    raw = out.read_text("utf-8")
    with out.open(encoding="utf-8", newline="") as fp:
        rows = list(csv.DictReader(fp))
    assert list(rows[0]) == layer_diff.FIELDS
    assert len(rows) == 2 * 6  # 조건 2 × 상대 4명의 쌍 6
    b = [r for r in rows if r["condition"] == "B"]
    c = [r for r in rows if r["condition"] == "C"]
    assert all(r["style_similarity"] == "" and r["voice_a"] == "False" for r in b)  # B는 예시도 지문도 없다
    assert all(0 <= float(r["style_similarity"]) < 1 for r in c)  # 상대마다 예시 글이 다르다
    assert any(r["voice_a"] == "True" or r["voice_b"] == "True" for r in c)
    assert all(r["result_similarity"] == "" and 0 < float(r["prompt_similarity"]) < 1 for r in rows)
    case = next(x for x in run_eval.load_cases(run_eval.DEFAULT_CASES, "all") if x["case_id"] == "E001")
    assert case["original_text"] not in raw and "팀장님" not in raw  # 숫자와 관계 이름뿐이다
