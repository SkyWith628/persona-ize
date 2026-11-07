"""의도 분류 (부탁 / 거절 / 사과 / 일정 조율 / 보고 / 기타).

1차는 키워드 규칙이다. 모델 보조 분류는 3주차에 추가하고, 둘이 다르면 규칙 결과를 유지한 채 경고를 남긴다.
"""

from __future__ import annotations

import re

from app.domain.rewrite import Intent

_RULES: tuple[tuple[Intent, re.Pattern[str]], ...] = (
    ("apology", re.compile(r"죄송|미안|사과|실수|늦어서|불편")),
    (
        "refusal",
        re.compile(
            r"어렵(?:습니다|겁니다|고|네요|다|을|어)|힘들|못\s*[가-힣]|불가|안\s*될|거절|곤란|참석이?\s*어려"
        ),
    ),
    (
        "request",
        re.compile(
            r"부탁|요청|주실\s*수|주시겠|해\s*주|해주|가능할까|괜찮을까|될까요|될까\?"
            r"|주세요|주실래|해줄래|해 줄래"
        ),
    ),
    ("schedule", re.compile(r"일정|조율|미팅|회의|약속|시간\s*괜찮|언제|몇\s*시|날짜|옮기|변경|연기|미루")),
    (
        "report",
        re.compile(
            r"보고|공유드립니다|공유합니다|완료|진행\s*상황|결과|말씀드립니다|전달드립니다|알려드립니다"
        ),
    ),
)


def classify_intent(draft_text: str, situation: str = "") -> Intent:
    """원문과 상황 설명을 합쳐 첫 번째로 맞는 규칙의 의도를 돌려준다. 순서가 우선순위다."""
    text = f"{situation} {draft_text}"
    for intent, pattern in _RULES:
        if pattern.search(text):
            return intent
    return "other"
