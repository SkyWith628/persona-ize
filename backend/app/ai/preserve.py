"""보존 항목 규칙 추출 (design.md §3-1).

정규식으로 날짜·시각, 수량, 사람, 가능/불가, 약속, 조건, 부정을 뽑는다.
누락이 오탐보다 위험하므로 넓게 잡는다. 구조화 모델 추출(보조)은 3주차에 결합한다.
같은 함수를 원문과 결과에 모두 적용해 집합으로 비교하는 것이 검증의 핵심이다 (validation.py).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.domain.rewrite import PreservedItem

_WEEKDAY = "월|화|수|목|금|토|일"
_RELATIVE_DAY = "오늘|내일|모레|어제|그제|이번\\s*주|다음\\s*주|지난\\s*주|이번\\s*달|다음\\s*달|주말|연휴"

_DATETIME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"(?:{_RELATIVE_DAY})(?:\s*(?:{_WEEKDAY})(?:요일)?)?"),
    re.compile(rf"(?<![가-힣])(?:{_WEEKDAY})요일"),
    re.compile(r"\d{1,2}\s*월\s*\d{1,2}\s*일"),
    re.compile(r"\d{1,2}\s*/\s*\d{1,2}(?!\d)"),
    re.compile(r"(?:오전|오후|아침|점심|저녁|밤|새벽)(?:\s*\d{1,2}\s*시(?:\s*반|\s*\d{1,2}\s*분)?)?"),
    re.compile(r"(?<![가-힣\d])\d{1,2}\s*시(?:\s*반|\s*\d{1,2}\s*분)?(?!간)"),
    re.compile(r"\d{1,2}\s*시간\s*(?:뒤|후|안|이내)"),
    re.compile(r"(?:이번|다음|지난)\s*(?:주|달|분기|학기)"),
)

# 숫자 + 단위. '시/월/일'은 날짜 패턴에서 처리하므로 제외한다.
_QUANTITY = re.compile(
    r"\d[\d,\.]*\s*(?:개|명|분|원|만\s*원|억|건|번|회|장|페이지|쪽|부|%|퍼센트|km|m|kg|g|일간|주일|주간|시간|분량|배|차|권|대)"
)
_BARE_NUMBER = re.compile(r"(?<![\d/월일시분:])\d{2,}(?![\d/월일시분%:])")

# 이름이 드러난 사람 (이름 2~4글자 + 호칭). 이름 없는 호칭('팀장님', '형')은 말투 요소라 보존 항목이 아니다.
_TITLES = "님|씨|선배|팀장님?|부장님?|과장님?|대리님?|교수님?|선생님|대표님?|이사님?|사장님?|실장님?|주임님?"
_BARE_TITLES = {
    "팀장",
    "부장",
    "과장",
    "대리",
    "교수",
    "선생",
    "대표",
    "이사",
    "사장",
    "실장",
    "주임",
    "선배",
}
# 뒤에 조사('에게', '도')가 붙을 수 있어 뒤쪽 경계는 두지 않는다.
_PERSON = re.compile(rf"(?<![가-힣])([가-힣]{{2,4}})\s*(?:{_TITLES})")

# 가능/불가. 결과에서 방향이 바뀌면 사실 변경이다.
_IMPOSSIBLE = re.compile(
    r"어렵(?:습니다|겁니다|고|네요|다|을|어|워)|힘들(?:겁니다|어|다|것|어요|겠)|못\s*[가-힣]+|불가(?:능)?|안\s*(?:될|돼|됩|됨|되)|할\s*수\s*없|불가능|어려울"
)
_POSSIBLE = re.compile(
    r"가능(?:합니다|해요|해|함|할)|할\s*수\s*있|될\s*것\s*같|됩니다|괜찮(?:습니다|아요|아)"
)

# 약속·의지 표현 (서술어). 결과에서 사라지면 약속이 빠진 것이다.
_PROMISE = re.compile(
    r"[가-힣]+(?:드리겠습니다|드릴게요|드릴께요|드리겠어요|겠습니다|할게요|할께요|하겠습니다|할게|할께|보낼게|보내드릴|보내겠|맡겠|처리하겠|확인하겠|전달하겠|공유하겠|진행하겠)"
)

# 조건 표현.
_CONDITION = re.compile(r"[가-힣\s]{1,15}(?:하면|이면|라면|되면|경우|다면|시에는|때는)(?![가-힣])")

# 부정 표현 (가능/불가와 겹치지 않는 일반 부정).
_NEGATION = re.compile(r"(?<![가-힣])(?:안|못|않|없|아니)(?=[가-힣\s])")


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _find(pattern: re.Pattern[str], text: str) -> Iterable[str]:
    for m in pattern.finditer(text):
        s = m.group(0).strip()
        if s:
            yield s


def extract_preserved_items(text: str) -> list[PreservedItem]:
    """원문에서 보존 항목을 추출한다. 같은 (kind, normalized)는 한 번만 담는다."""
    items: dict[tuple[str, str], PreservedItem] = {}

    def add(kind: str, raw: str, normalized: str | None = None) -> None:
        norm = normalized if normalized is not None else _norm(raw)
        key = (kind, norm)
        if key not in items:
            items[key] = PreservedItem(kind=kind, text=raw, normalized=norm)  # type: ignore[arg-type]

    for pat in _DATETIME_PATTERNS:
        for s in _find(pat, text):
            add("datetime", s)

    for s in _find(_QUANTITY, text):
        add("quantity", s)
    for s in _find(_BARE_NUMBER, text):
        add("quantity", s)

    for m in _PERSON.finditer(text):
        name = m.group(1)
        if name in _BARE_TITLES or name.endswith(
            ("팀장", "부장", "과장", "대리", "교수", "대표", "이사", "사장", "실장", "주임")
        ):
            # '지현 팀장님' → 이름 '지현'만 비교 대상. '김팀장님' 같은 성+직책은 이름으로 본다.
            name = name[:-2] if name not in _BARE_TITLES and len(name) > 2 else name
            if name in _BARE_TITLES:
                continue
        add("person", m.group(0).strip(), name)

    for s in _find(_IMPOSSIBLE, text):
        add("possibility", s, "불가")
    for s in _find(_POSSIBLE, text):
        add("possibility", s, "가능")

    for s in _find(_PROMISE, text):
        add("promise", s, "약속")

    for s in _find(_CONDITION, text):
        add("condition", s)

    if _NEGATION.search(text):
        add("negation", _NEGATION.search(text).group(0), "부정")  # type: ignore[union-attr]

    return list(items.values())


def facts_only(items: list[PreservedItem]) -> set[tuple[str, str]]:
    """'추가된 사실' 검사 대상 — 날짜·수량·사람만. 서술어는 문체에 따라 바뀔 수 있다."""
    return {(i.kind, i.normalized) for i in items if i.kind in {"datetime", "quantity", "person"}}


def polarity(items: list[PreservedItem]) -> set[str]:
    """가능/불가/부정의 방향 집합."""
    return {i.normalized for i in items if i.kind in {"possibility", "negation"}}
