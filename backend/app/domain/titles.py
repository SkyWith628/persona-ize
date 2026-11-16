"""직함·호칭 사전 (docs/graph-redesign-2026-11-16.md 3.2, 5.5).

두 곳이 같은 사전을 쓴다.
- 말투 프로필(`app.ai.register`): 호칭의 직함에서 "상대가 윗사람"이라는 힌트를 얻는다.
- 보존 항목 추출(`app.ai.preserve`): '파트장', '센터장' 같은 말을 사람 이름이 아니라 직함으로 본다.

사전을 고치면 보존 항목 추출 결과가 달라진다 — `VALIDATOR_VERSION`을 올리고 기준 조건을 다시 돌린다.
"""

from __future__ import annotations

VALIDATOR_VERSION = "rules-2026-11-16"
"""보존 항목 추출·검증 규칙의 버전. 평가 결과에 남겨, 규칙이 바뀐 앞뒤의 수치를 섞어 비교하지 않게 한다."""

# 조직의 직책. '님'이 붙든 안 붙든 같은 직함이다.
ORG_TITLES: frozenset[str] = frozenset(
    {
        "팀장",
        "파트장",
        "센터장",
        "실장",
        "본부장",
        "부서장",
        "그룹장",
        "지점장",
        "부장",
        "차장",
        "과장",
        "대리",
        "주임",
        "사원",
        "매니저",
        "리더",
        "책임",
        "선임",
        "수석",
        "이사",
        "상무",
        "전무",
        "부사장",
        "사장",
        "대표",
        "회장",
        "원장",
        "소장",
        "교수",
        "선생",
        "박사",
        "변호사",
        "의사",
        "간호사",
        "코치",
        "감독",
        "작가",
        "기자",
        "PD",
    }
)

# 상대가 윗사람임을 뜻하는 직함·호칭 (말투 프로필의 위계 힌트). 직급이 낮거나 중립인 것은 넣지 않는다.
SENIOR_TITLES: frozenset[str] = frozenset(
    {
        "팀장",
        "파트장",
        "센터장",
        "실장",
        "본부장",
        "부서장",
        "그룹장",
        "지점장",
        "부장",
        "차장",
        "과장",
        "이사",
        "상무",
        "전무",
        "부사장",
        "사장",
        "대표",
        "회장",
        "원장",
        "소장",
        "교수",
        "선생",
        "선배",
        "형",
        "누나",
        "언니",
        "오빠",
        "어머니",
        "아버지",
        "엄마",
        "아빠",
    }
)

HONORIFIC_SUFFIXES: tuple[str, ...] = ("님", "씨")


def strip_honorific(word: str) -> str:
    """'파트장님' → '파트장', '지현씨' → '지현'. 접미사가 없으면 그대로."""
    word = word.strip()
    for suffix in HONORIFIC_SUFFIXES:
        if len(word) > len(suffix) and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


def title_of(term: str) -> str | None:
    """호칭 끝의 직함. '서연 파트장님' → '파트장', '팀장님' → '팀장', '서연아' → None."""
    words = term.replace(",", " ").split()
    if not words:
        return None
    last = strip_honorific(words[-1])
    if last in ORG_TITLES or last in SENIOR_TITLES:
        return last
    # 띄어쓰지 않은 이름+직함 ('김팀장님')
    for title in sorted(ORG_TITLES, key=len, reverse=True):
        if len(last) > len(title) and last.endswith(title):
            return title
    return None


def is_title(word: str) -> bool:
    """낱말이 (님·씨를 떼고) 직함 그 자체인가. '파트장님' → True, '지현' → False."""
    return strip_honorific(word) in ORG_TITLES


def is_senior_term(term: str) -> bool:
    """호칭이 상대가 윗사람임을 뜻하는가 ('서연 파트장님', '형', '선배님')."""
    title = title_of(term)
    if title is not None and title in SENIOR_TITLES:
        return True
    words = term.replace(",", " ").split()
    return bool(words) and strip_honorific(words[-1]) in SENIOR_TITLES
