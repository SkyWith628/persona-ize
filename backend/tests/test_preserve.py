"""보존 항목 추출 규칙. 누락이 오탐보다 위험하므로 '뽑혀야 하는 것'을 중심으로 본다."""

from __future__ import annotations

import pytest

from app.ai.intent import classify_intent
from app.ai.preserve import extract_preserved_items, facts_only, polarity


def kinds(text: str) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for i in extract_preserved_items(text):
        out.setdefault(i.kind, set()).add(i.normalized)
    return out


def test_design_doc_example() -> None:
    k = kinds("오늘 정리해서 내일 오전까지 보내드릴게요.")
    assert {"오늘", "내일", "오전"} <= k["datetime"]
    assert "약속" in k["promise"]


@pytest.mark.parametrize(
    "text",
    [
        "보고서 금요일까지 드릴게요",  # 띄어 쓴 서술어
        "제가 할게요",
        "회의는 15:30에 갈게요",  # 'ㄹ게요'로 끝나는 말
        "내일 다시 볼게",
        "저녁은 제가 살게",
        "자료 넣을게요.",
        "그건 제가 할께요",
    ],
)
def test_promise_endings(text: str) -> None:
    assert "약속" in kinds(text).get("promise", set())


@pytest.mark.parametrize(
    "text",
    [
        "날개가 예쁘네요",
        "그렇게 말게 되면",
        "얼게 두지 마세요",
        "게 요리 먹으러 가요",
        "힘들게 해서 미안해요",  # 'ㄹ게' 부사형은 문장 끝이 아니면 약속이 아니다
        "길게 말씀드리면",
    ],
)
def test_not_promise(text: str) -> None:
    assert "promise" not in kinds(text)


def test_dates_and_quantities() -> None:
    k = kinds("11월 5일까지 자료 3부 드리겠습니다. 다음 주 수요일 오후 3시 회의요.")
    assert {"11월5일", "다음주수요일", "오후3시"} <= k["datetime"]
    assert "3부" in k["quantity"]


def test_impossibility_and_possibility() -> None:
    assert "불가" in polarity(
        extract_preserved_items("이번 주 금요일은 어렵고, 다음 주 수요일까지는 가능합니다.")
    )
    assert "가능" in polarity(extract_preserved_items("수요일까지는 가능합니다."))
    assert "불가" in polarity(extract_preserved_items("목요일이면 테스트를 못 돌립니다."))


def test_people_with_honorifics() -> None:
    k = kinds("지현 팀장님, 박지현씨에게 전달해 주세요. 서연 선배도요.")
    assert {"지현", "박지현", "서연"} <= k["person"]


def test_bare_title_is_not_a_fact() -> None:
    """이름 없는 호칭은 말투 요소다. 추가·삭제해도 사실 변경이 아니다."""
    assert "person" not in kinds("팀장님, 내일 오전까지 보내드리겠습니다.")
    assert "person" not in kinds("형, 이번 주 토요일 가능?")


def test_facts_only_excludes_predicates() -> None:
    items = extract_preserved_items("내일 보내드릴게요. 어렵습니다.")
    assert all(k in {"datetime", "quantity", "person"} for k, _ in facts_only(items))


def test_numbers_with_separators_are_one_item() -> None:
    """2026-11-23: '4,500 pcs'가 '500'으로 잘려, '3,500'으로 바뀌어도 검증이 알지 못했다."""
    k = kinds("공급가액 98,400,000 이고 USD 15.27, 4,500 pcs 입니다. PO 4500-887213, 17.")
    assert {"98400000", "15.27", "4500", "887213", "17"} == k["quantity"]  # 견줄 때는 쉼표를 지운다
    # 단위가 붙은 수는 예전처럼 단위와 함께 (그리고 수만으로도) 잡힌다
    assert {"52000원", "52000", "1000개", "1000"} == kinds("52,000원으로 1,000개")["quantity"]
    assert kinds("52000원으로 1000개")["quantity"] == kinds("52,000원으로 1,000개")["quantity"]
    # 화면에 보이는 글자는 초안 그대로다
    assert "4,500" in {i.text for i in extract_preserved_items("4,500 pcs")}
    # 한 자리 정수와 퍼센트 앞의 수는 예전처럼 따로 잡지 않는다
    assert kinds("3 times, 2.8% off")["quantity"] == {"2.8%"}


def test_english_dates_weekdays_and_times() -> None:
    k = kinds("will send by Friday Oct 17. 2.8% from Nov 15, call at 8pm Eastern tomorrow")
    assert {"oct17", "nov15", "fri", "8pm", "tomorrow"} == k["datetime"]
    # 문장 끝 마침표는 시각에 붙이지 않는다. 'p.m.'의 점은 시각의 일부다
    by_text = {
        i.text: i.normalized
        for i in extract_preserved_items("by Friday 3pm. Or 9:30 a.m. works")
        if i.kind == "datetime"
    }
    assert by_text["3pm"] == "3pm"
    assert by_text["9:30 a.m."] == "9:30am"
    # 표기가 달라도 같은 값이다
    same = kinds("I will send it by friday, October 17th. From 15 Nov, call at 8 PM, Tomorrow.")
    assert same["datetime"] == k["datetime"]
    assert kinds("9:30 a.m. next week, Sat 14 Oct")["datetime"] == {"9:30am", "nextweek", "sat", "oct14"}
    # 날짜가 아닌 것: 조동사 may, 보통 낱말 sat·sun, 다른 낱말의 일부
    assert "datetime" not in kinds("I may 3 times. The marketing 5 team sat in the sun on a monthly basis.")


def test_clock_times() -> None:
    """2026-11-23: '14:30'을 읽는 규칙이 없어 '15:30'으로 바뀌어도 통과했다."""
    assert {"14:30", "9:05"} <= kinds("회의는 14:30, 출발은 09:05")["datetime"]
    assert "datetime" not in kinds("비율은 3:2, 점수는 12:60")


def test_single_letter_weekday_is_not_the_start_of_another_word() -> None:
    assert kinds("이번 주 일정 공유드립니다")["datetime"] == {"이번주"}
    assert kinds("다음 주 수정본 드릴게요")["datetime"] == {"다음주"}
    # 낱말이 거기서 끝나거나 날짜 조사가 붙으면 요일이다
    assert "이번주금" in kinds("이번 주 금까지 드릴게요")["datetime"]
    assert "이번주금" in kinds("이번 주 금, 괜찮으세요?")["datetime"]
    assert "다음주수요일" in kinds("다음 주 수요일에 봬요")["datetime"]


def test_words_starting_with_an_are_not_negation() -> None:
    for text in ("안녕하세요", "안내 드립니다", "오늘 안에 보내겠습니다", "회의실 안으로 오세요", "안전하게"):
        assert "부정" not in polarity(extract_preserved_items(text)), text
    for text in ("금요일은 안 됩니다", "안됩니다", "못 갑니다", "시간이 없습니다", "하지 않았습니다"):
        assert polarity(extract_preserved_items(text)) & {"부정", "불가"}, text
    assert "불가" in polarity(extract_preserved_items("금요일은 어렵겠습니다"))


def _signals(text: str) -> set[str]:
    """가능/불가/부정 방향 + 약속 유무."""
    items = extract_preserved_items(text)
    return polarity(items) | {i.normalized for i in items if i.kind == "promise"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I can't send it by Friday", {"불가"}),
        ("I can’t make it", {"불가"}),  # 굽은 아포스트로피
        ("I cant make it", {"불가"}),  # 아포스트로피를 빠뜨린 꼴
        ("I cannot send it", {"불가"}),
        ("We can not ship this week", {"불가"}),
        ("I won't be able to join", {"불가"}),
        ("We will not ship it", {"불가"}),
        ("I'm unable to attend", {"불가"}),
        ("I wasn't able to finish", {"불가"}),
        ("That is not possible", {"불가"}),
        ("It's impossible this week", {"불가"}),
        ("It's not impossible", {"가능"}),
        ("I can send it by Friday", {"가능"}),
        ("Friday works for me", {"가능"}),
        ("Is it possible to move it?", {"가능"}),
        ("I don't have the file", {"부정"}),
        ("It doesn’t matter", {"부정"}),
        ("It isn't ready", {"부정"}),
        ("That is no longer needed", {"부정"}),
        ("I never got it", {"부정"}),
        ("It is not ready", {"부정"}),
        ("I'll send the draft tomorrow", {"약속"}),
        ("I will send the draft", {"약속"}),
        ("we’ll confirm the date", {"약속"}),
        ("We will confirm", {"약속"}),
        ("will send the signed agreement", {"약속"}),
        ("I'm going to check it", {"약속"}),
        # 약속이 아닌 것, 부정이 아닌 것
        ("I will not send it", {"불가"}),
        ("He is ill and well rested", set()),
        ("Not only the price but also the date changed", set()),
        ("We can not only ship but also install", {"가능"}),
        ("No worries", set()),
        ("PO no. 4500", set()),
        ("Please note the scan and the cancellation", set()),
    ],
)
def test_english_polarity_and_promise(text: str, expected: set[str]) -> None:
    assert _signals(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 불가 + 부정 = 가능
        ("금요일은 어렵지 않습니다", {"가능"}),
        ("어렵지는 않아요", {"가능"}),
        ("힘들지 않습니다", {"가능"}),
        ("불가능하지 않습니다", {"가능"}),
        ("안 되지 않습니다", {"가능"}),
        ("못 가는 것은 아닙니다", {"가능"}),
        ("어려운 건 아니에요", {"가능"}),
        # 가능 + 부정 = 불가
        ("금요일까지는 가능하지 않습니다", {"불가"}),
        ("될 것 같지 않습니다", {"불가"}),
        ("괜찮지 않습니다", {"불가"}),
        ("할 수 없습니다", {"불가", "부정"}),  # 예전 그대로
        # 물음꼴은 한 번 더 뒤집힌다
        ("금요일은 어렵지 않을까 싶습니다", {"불가"}),
        ("가능하지 않을까요?", {"가능"}),
        # 예전 그대로인 것
        ("금요일은 어렵습니다", {"불가"}),
        ("금요일은 힘들 것 같습니다", {"불가"}),
        ("금요일은 어렵지만 수요일은 됩니다", {"불가", "가능"}),
        ("이번 주는 힘들지만 맞춰 보겠습니다", {"불가", "약속"}),
        ("금요일까지 가능합니다", {"가능"}),
        ("하지 않았습니다", {"부정"}),
    ],
)
def test_korean_double_negation(text: str, expected: set[str]) -> None:
    assert _signals(text) == expected


def test_empty_text() -> None:
    assert extract_preserved_items("") == []
    assert extract_preserved_items("   ") == []


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("어제 일정 건은 제가 말이 좀 딱딱했던 것 같아 죄송합니다.", "apology"),
        ("이번 주 금요일은 어렵고, 다음 주 수요일까지는 가능합니다.", "refusal"),
        ("자료 공유 부탁드립니다.", "request"),
        ("회의 시간 괜찬으신가요? 몇 시가 좋을까요", "schedule"),
        ("대시보드 개편 오늘 배포 완료했습니다!", "report"),
        ("ㅋㅋ 그러게", "other"),
    ],
)
def test_intent(text: str, intent: str) -> None:
    assert classify_intent(text) == intent
