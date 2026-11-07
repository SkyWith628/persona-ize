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
