"""보존 항목 규칙 추출 (design.md §3-1).

정규식으로 날짜·시각, 수량, 사람, 가능/불가, 약속, 조건, 부정을 뽑는다.
누락이 오탐보다 위험하므로 넓게 잡는다. 구조화 모델 추출(보조)은 3주차에 결합한다.
같은 함수를 원문과 결과에 모두 적용해 집합으로 비교하는 것이 검증의 핵심이다 (validation.py).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.domain.rewrite import PreservedItem
from app.domain.titles import ORG_TITLES

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
# 뽑는 것은 `\d[\d,\.]*\s*(단위)`와 같다. 시간만 다르다: 숫자·쉼표·점이 이어진 덩어리의
# **처음에서만** 시작하고(`(?<![\d,\.])[,\.]*`) 덩어리 안으로 되돌아가지 않는다(`*+`) —
# 숫자만 2,000자인 글에서 제곱 시간이 들지 않게.
# 단위는 숫자·쉼표·점으로 시작하지 않으므로 덩어리 중간에서 시작하거나 끊어도 결과가 달라지지 않는다.
_QUANTITY = re.compile(
    r"(?<![\d,\.])[,\.]*(\d[\d,\.]*+\s*"
    r"(?:개|명|분|원|만\s*원|억|건|번|회|장|페이지|쪽|부|%|퍼센트|km|m|kg|g|일간|주일|주간|시간|분량|배|차|권|대))"
)
_BARE_NUMBER = re.compile(r"(?<![\d/월일시분:])\d{2,}(?![\d/월일시분%:])")

# 이름이 드러난 사람 (이름 2~4글자 + 호칭). 이름 없는 호칭('팀장님', '형')은 말투 요소라 보존 항목이 아니다.
_TITLES = "님|씨|선배|팀장님?|부장님?|과장님?|대리님?|교수님?|선생님|대표님?|이사님?|사장님?|실장님?|주임님?"
# 위 목록에 '님' 없이도 들어 있는 직함. 여기에 직함 사전(app.domain.titles)의 나머지를 더한다.
_LEGACY_TITLES = frozenset({"팀장", "부장", "과장", "대리", "교수", "대표", "이사", "사장", "실장", "주임"})
# 직함 그 자체인 낱말. '파트장님'·'센터장님'·'매니저님'의 앞부분이 사람 이름으로 잡히지 않게 한다
# (2026-11-16 이전에는 목록에 없는 직함이 '파트장'이라는 사람으로 잡혀, 호칭만 붙인 결과가
# '원문에 없는 사람 추가'로 실패했다). 한글 직함만 쓴다 — 이름 패턴이 한글만 보기 때문이다('PD' 제외).
_TITLE_WORDS = frozenset(t for t in ORG_TITLES if re.fullmatch("[가-힣]+", t)) | {"선배"}
# 이름 끝에서 떼어 낼 때는 긴 직함부터 본다 ('김부사장' → '김', '사장'을 먼저 떼어 '김부'가 되지 않게).
_TITLE_SUFFIXES = tuple(sorted(_TITLE_WORDS, key=len, reverse=True))
# 사전에서 새로 온 직함은 '님'이 붙었을 때만 이름 뒤 직함으로 본다 ('서연 파트장님' → 이름 '서연').
# '책임지고'·'거절 의사'·'변호사 선임'처럼 보통 명사로도 쓰이는 말이 많아, '님' 없이 받으면
# 앞 낱말('제가', '거절')이 사람으로 잡힌다. 예전 목록의 직함은 예전 그대로('지현 팀장' 가능).
_NEW_TITLES = "|".join(
    f"{t}님" for t in sorted(_TITLE_WORDS - _LEGACY_TITLES - {"선배", "선생"}, key=lambda t: (-len(t), t))
)
# 뒤에 조사('에게', '도')가 붙을 수 있어 뒤쪽 경계는 두지 않는다.
_PERSON = re.compile(rf"(?<![가-힣])([가-힣]{{2,4}})(\s*)(?:{_TITLES}|{_NEW_TITLES})")

# 띄어 쓴 직함 앞의 낱말이 이름이 아닌 경우 ('어제 파트장님', '우리 매니저님', '회의록은 센터장님께').
# 이런 낱말을 사람으로 잡으면 사실을 다 지킨 결과가 어순만 바꿔도("파트장님께서 어제 …") '사람 누락'으로
# 실패한다. 날짜 낱말은 날짜 항목으로 따로 보존되므로 여기서 빼도 잃는 것이 없다.
# 목록은 완전하지 않다 — 이름을 놓치는 쪽이 더 위험하므로, 이름일 수 있는 낱말은 넣지 않는다.
_NOT_NAMES = frozenset(
    {
        *("오늘", "내일", "모레", "어제", "그제", "아까", "방금", "지금", "이따", "나중", "요즘", "최근"),
        *("이번", "다음", "지난", "주말", "연휴", "오전", "오후", "아침", "점심", "저녁", "새벽", "평일"),
        *("우리", "저희", "제가", "저는", "저도", "제게", "내가", "나는", "나도", "너는", "네가", "누가"),
        *("이건", "그건", "저건", "이게", "그게", "건은", "그럼", "그리고", "그래서", "근데", "그런데"),
        *("담당", "신임", "전임", "후임", "현재", "이전", "해당", "새로", "오신", "계신", "가신", "되신"),
        *("그분", "이분", "저분", "모든", "다른", "여러", "옆팀", "타팀", "같은", "혹시", "먼저", "일단"),
        *("다시", "아직", "이미", "바로", "직접", "따로", "같이", "함께", "역시", "아마", "항상", "계속"),
    }
)
# 낱말 끝이 이 조사면 이름이 아니다 ('회의록을', '자료는', '회의에'). 이름 끝에 잘 오지 않는 글자만 둔다 —
# '은/이/가/도'는 '지은', '정이' 같은 이름과 겹쳐서 네 글자일 때만 조사로 본다 ('회의록은').
_PARTICLE_ENDS = ("는", "를", "에", "께", "에서", "에게", "한테", "부터", "까지", "으로", "이랑", "하고")
_LONG_PARTICLE_ENDS = ("은", "이", "가", "을", "도", "의", "로", "와", "과", "만")


def _plausible_name(word: str) -> bool:
    """띄어 쓴 직함 앞의 낱말이 사람 이름일 수 있는가 ('지현 팀장님'의 '지현'). 붙여 쓴 꼴에는 쓰지 않는다."""
    if word in _NOT_NAMES or word.endswith(_PARTICLE_ENDS):
        return False
    return not (len(word) >= 4 and word.endswith(_LONG_PARTICLE_ENDS))


# 가능/불가. 결과에서 방향이 바뀌면 사실 변경이다.
_IMPOSSIBLE = re.compile(
    r"어렵(?:습니다|겁니다|고|네요|다|을|어|워)|힘들(?:겁니다|어|다|것|어요|겠)|못\s*[가-힣]+|불가(?:능)?|안\s*(?:될|돼|됩|됨|되)|할\s*수\s*없|불가능|어려울"
)
_POSSIBLE = re.compile(
    r"가능(?:합니다|해요|해|함|할)|할\s*수\s*있|될\s*것\s*같|됩니다|괜찮(?:습니다|아요|아)"
)

# 약속·의지 표현 (서술어). 결과에서 사라지면 약속이 빠진 것이다.
# 한글이 이어진 덩어리의 처음에서만 시작한다(`(?<![가-힣])`). `[가-힣]+`가 덩어리 끝까지 갔다가 마지막 어미로
# 되돌아오므로 덩어리 중간에서 시작해 맞는 것은 처음에서 시작해도 똑같이 맞는다 —
# 결과는 같고 시간만 선형이 된다.
_PROMISE = re.compile(
    r"(?<![가-힣])[가-힣]+(?:드리겠습니다|드릴게요|드릴께요|드리겠어요|겠습니다|할게요|할께요|하겠습니다|할게|할께|보낼게|보내드릴|보내겠|맡겠|처리하겠|확인하겠|전달하겠|공유하겠|진행하겠)"
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


def _person_name(captured: str) -> str | None:
    """이름 자리에 잡힌 낱말에서 비교할 이름을 고른다. 이름이 아니라 직함이면 None.

    - '파트장'(+님), '팀장'(+님): 이름 없는 직함 → 보존 항목이 아니다.
    - '김팀장'(+님), '지현선배'(+님): 직함을 떼고 '김', '지현'. 띄어 쓴 '지현 팀장님'은 정규식이 이미
      이름 '지현'만 잡는다.
    - 뗀 나머지가 또 직함이면('부장대리') 이름이 없는 것으로 본다.
    """
    if captured in _TITLE_WORDS:
        return None
    for title in _TITLE_SUFFIXES:
        if captured.endswith(title):
            name = captured[: -len(title)]
            return None if name in _TITLE_WORDS else name
    return captured


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

    for m in _QUANTITY.finditer(text):
        add("quantity", m.group(1).strip())
    for s in _find(_BARE_NUMBER, text):
        add("quantity", s)

    for m in _PERSON.finditer(text):
        if m.group(2) and not _plausible_name(m.group(1)):
            continue  # '어제 파트장님': 앞 낱말이 이름이 아니다 — 이름 없는 직함은 보존 항목이 아니다
        name = _person_name(m.group(1))
        if name is not None:
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
