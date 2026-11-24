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
