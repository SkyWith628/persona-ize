"""보존 항목 규칙 추출 (design.md §3-1).

정규식으로 날짜·시각, 수량, 사람, 가능/불가, 약속, 조건, 부정을 뽑는다.
영어는 날짜·요일·시각과 수, 그리고 부정·가능/불가·약속을 읽는다 (조건과 사람 이름은 아직 없다).
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

# '이번 주 금'처럼 '요일' 없이 쓴 요일 한 글자는 낱말이 거기서 끝나거나 날짜 조사가 붙을 때만 읽는다.
# 뒤에 다른 한글이 이어지면 다음 낱말의 첫 글자다 — '이번 주 일정'의 '일', '다음 주 수정'의 '수'
# (2026-11-23 이전에는 '이번 주 일'이라는 날짜로 잡혀, '이번 주의 일정'으로 바꾼 결과가 실패했다).
_WEEKDAY_END = "요일|(?=까지|부터|쯤|에는|에도|에(?![가-힣]))|(?![가-힣])"

_DATETIME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"(?:{_RELATIVE_DAY})(?:\s*(?:{_WEEKDAY})(?:{_WEEKDAY_END}))?"),
    re.compile(rf"(?<![가-힣])(?:{_WEEKDAY})요일"),
    re.compile(r"\d{1,2}\s*월\s*\d{1,2}\s*일"),
    re.compile(r"\d{1,2}\s*/\s*\d{1,2}(?!\d)"),
    re.compile(r"(?:오전|오후|아침|점심|저녁|밤|새벽)(?:\s*\d{1,2}\s*시(?:\s*반|\s*\d{1,2}\s*분)?)?"),
    re.compile(r"(?<![가-힣\d])\d{1,2}\s*시(?:\s*반|\s*\d{1,2}\s*분)?(?!간)"),
    re.compile(r"\d{1,2}\s*시간\s*(?:뒤|후|안|이내)"),
    re.compile(r"(?:이번|다음|지난)\s*(?:주|달|분기|학기)"),
)
# '14:30' 꼴의 시각 (2026-11-23). 수 규칙은 콜론 옆의 수를 빼는데 이 꼴을 읽는 날짜 규칙이 없어서,
# '14:30'을 '15:30'으로 바꿔도 검증이 알지 못했다. 분은 00~59만 — '3:2' 같은 비율은 시각이 아니다.
# am/pm이 붙은 것은 영어 시각 규칙이 읽는다 ('9:00 am'과 '9am'이 같은 값이 되도록 여기서는 뺀다).
_CLOCK = re.compile(r"(?<![\d:])(?P<hour>\d{1,2}):(?P<minute>[0-5]\d)(?![\d:])(?!\s?[apAP]\.?[mM])")

# 숫자 + 단위. '시/월/일'은 날짜 패턴에서 처리하므로 제외한다.
# 뽑는 것은 `\d[\d,\.]*\s*(단위)`와 같다. 시간만 다르다: 숫자·쉼표·점이 이어진 덩어리의
# **처음에서만** 시작하고(`(?<![\d,\.])[,\.]*`) 덩어리 안으로 되돌아가지 않는다(`*+`) —
# 숫자만 2,000자인 글에서 제곱 시간이 들지 않게.
# 단위는 숫자·쉼표·점으로 시작하지 않으므로 덩어리 중간에서 시작하거나 끊어도 결과가 달라지지 않는다.
_QUANTITY = re.compile(
    r"(?<![\d,\.])[,\.]*(\d[\d,\.]*+\s*"
    r"(?:개|명|분|원|만\s*원|억|건|번|회|장|페이지|쪽|부|%|퍼센트|km|m|kg|g|일간|주일|주간|시간|분량|배|차|권|대))"
)
# 단위 없는 수. 자릿수 쉼표('4,500', '98,400,000')와 소수('15.27')는 한 덩어리로 읽는다 (2026-11-23).
# 예전에는 두 자리 이상 숫자 덩어리만 봐서 '4,500 pcs'가 '500'이 됐다 — '4,500'을 '3,500'으로 바꿔도
# 검증이 알지 못했다. 한 자리 정수('3')는 예전처럼 보지 않는다. 덩어리 안으로 되돌아가지 않는다(`++`).
_BARE_NUMBER = re.compile(
    r"(?<![\d/월일시분:])(?:\d{1,3}(?:,\d{3})++(?:\.\d++)?|\d++\.\d++|\d{2,}+)(?![\d/월일시분%:])"
)

# 영어 날짜·요일·시각 (2026-11-23). 영어로 주고받는 대화에서 'Oct 17'이 '17'로만 잡혀, 달이나 요일이
# 바뀌어도 검증이 알지 못했다. 표기가 달라도 같은 값으로 견준다: 'Oct 17' = 'October 17th' = '17 Oct',
# 'friday' = 'Fri', '8pm' = '8 PM'.
_EN_MONTHS = {
    **dict.fromkeys(("jan", "january"), "jan"),
    **dict.fromkeys(("feb", "february"), "feb"),
    **dict.fromkeys(("mar", "march"), "mar"),
    **dict.fromkeys(("apr", "april"), "apr"),
    "may": "may",
    **dict.fromkeys(("jun", "june"), "jun"),
    **dict.fromkeys(("jul", "july"), "jul"),
    **dict.fromkeys(("aug", "august"), "aug"),
    **dict.fromkeys(("sep", "sept", "september"), "sep"),
    **dict.fromkeys(("oct", "october"), "oct"),
    **dict.fromkeys(("nov", "november"), "nov"),
    **dict.fromkeys(("dec", "december"), "dec"),
}
# 'may'는 조동사이기도 해서 대문자로 시작할 때만 달로 본다 ('I may 3 times'는 날짜가 아니다).
_EN_MONTH = "(?i:" + "|".join(sorted((m for m in _EN_MONTHS if m != "may"), key=len, reverse=True)) + ")|May"
_EN_MONTH_DAY = re.compile(
    rf"(?<![A-Za-z])(?P<month>{_EN_MONTH})\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?![\dA-Za-z])",
)
_EN_DAY_MONTH = re.compile(
    rf"(?<![\d,\.])(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<month>{_EN_MONTH})(?![A-Za-z])"
)
_EN_WEEKDAYS = {
    **dict.fromkeys(("mon", "monday"), "mon"),
    **dict.fromkeys(("tue", "tues", "tuesday"), "tue"),
    **dict.fromkeys(("wed", "wednesday"), "wed"),
    **dict.fromkeys(("thu", "thur", "thurs", "thursday"), "thu"),
    **dict.fromkeys(("fri", "friday"), "fri"),
    **dict.fromkeys(("sat", "saturday"), "sat"),
    **dict.fromkeys(("sun", "sunday"), "sun"),
}
# 'sat'·'sun'은 보통 낱말이기도 해서 줄임말은 대문자로 시작할 때만 요일로 본다 ('Sat', 'Sun').
_EN_WEEKDAY = re.compile(
    r"(?<![A-Za-z])(?:(?i:monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|mon|tues|tue|wed|thurs|thur|thu|fri)|Sat|Sun)(?![A-Za-z])"
)
_EN_TIME = re.compile(
    # 'pm.'의 마침표는 문장 끝이다 — 점은 'p.m.' 꼴일 때만 시각의 일부로 읽는다
    r"(?<![\d:])(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s?(?P<half>[ap])(?:m|\.m\.?)(?![A-Za-z])",
    re.IGNORECASE,
)
_EN_RELATIVE = re.compile(
    r"(?<![A-Za-z])(?:today|tomorrow|yesterday|tonight|(?:this|next|last)\s+(?:week|month|quarter|year))"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)

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
# '어렵지만'·'힘들지만'도 불가다 (2026-11-24, 예전에는 어미 목록에 없어 읽지 못했다).
# '힘들 것 같습니다'는 띄어 쓴 꼴도 읽는다 (2026-11-24) — 예전에는 '힘들것'만 읽어, '어렵습니다'를
# '힘들 것 같습니다'로 바꾼 결과가 "불가가 사라졌다"로 실패했다.
_IMPOSSIBLE = re.compile(
    r"어렵(?:겠|습니다|겁니다|고|네요|다|을|어|워|지만)|힘들(?:지만|겁니다|어|다|\s?것|어요|겠)|못\s*[가-힣]+|불가(?:능)?|안\s*(?:될|돼|됩|됨|되)|할\s*수\s*없|불가능|어려울"
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
# '안'으로 시작하는 보통 낱말은 뺀다 — '안녕하세요', '안내', '안전', '오늘 안에', '안으로' (2026-11-23).
# 예전에는 인사말 '안녕'이 부정으로 읽혀, 불가가 사라진 결과가 "부정이 남았다"로 통과했고,
# 인사말로 시작한 초안은 "원문에 이미 부정이 있다"로 보아 새로 생긴 불가를 검사하지 않았다.
_NEGATION = re.compile(
    r"(?<![가-힣])(?:안(?!녕|내|전|부|건|심|정|쪽|팎|에|으로)|못|않|없|아니|아닙)(?=[가-힣\s])"
)

# ── 이중 부정과 물음꼴 (2026-11-24) ─────────────────────────────────────────
# 불가 표현에 부정이 붙으면 가능이고('어렵지 않습니다'), 가능 표현에 부정이 붙으면 불가다('가능하지
# 않습니다'). 예전에는 '어렵지 않습니다'가 일반 부정으로만 읽혀 '어렵습니다'를 이렇게 뒤집은 결과가
# "부정이 남았다"로 통과했고, '가능합니다' → '가능하지 않습니다'도 불가로 읽히지 않아 통과했다.
# 부정 뒤가 물음꼴이면 뜻이 한 번 더 뒤집힌다: '어렵지 않을까 싶습니다'는 불가, '가능하지 않을까요'는 가능.
# 여기서 읽은 자리는 가린 뒤에 아래 규칙(_IMPOSSIBLE·_POSSIBLE·_NEGATION)을 돌린다 — 같은 자리의
# '않'·'안 되'가 다시 부정·불가로 읽히지 않게. 반복은 모두 한 글자·상한 있는 것뿐이다(중첩 없음).
_IMPOSSIBLE_STEMS = r"어렵|힘들|불가능하|불가하|안\s?되"
_NEGATED_STEM = re.compile(
    rf"(?:(?P<imp>{_IMPOSSIBLE_STEMS})|가능하|괜찮|될\s?것\s?같)(?:지(?:는|도)?|진)\s?않"
    r"(?P<ask>을까|을지|겠습니까|나요|나(?![가-힣]))?"
)
# '-ㄴ 것은 아니다' 꼴: '어려운 건 아닙니다', '못 가는 것은 아니에요', '할 수 없는 건 아니지만'.
_NOT_THAT = re.compile(
    r"(?:(?P<imp>어려운|힘든|불가능한|불가한|안\s?되는|수\s?없는|못\s?[가-힣]{1,6})|가능한)"
    r"\s?(?:것은|것이|것도|건|게)\s?아(?:니|닙)"
)

# ── 영어의 부정·가능/불가·약속 (2026-11-24) ─────────────────────────────────
# 한국어와 같은 kind·값으로 넣어 validation.py의 방향 검사와 약속 검사가 그대로 걸리게 한다.
# 아포스트로피는 곧은 것·굽은 것을 다 읽고, 빠뜨린 것('cant', 'dont')도 읽는다.
_AP = "['’‘`ʼ]"
# 'not only … but also'는 부정이 아니다. 먼저 가려 'can not only'가 불가로 읽히지 않게 한다.
_EN_NOT_ONLY = re.compile(r"(?<![A-Za-z])not\s+only(?![A-Za-z])", re.IGNORECASE)
_EN_NOT_IMPOSSIBLE = re.compile(r"(?<![A-Za-z])not\s+impossible(?![A-Za-z])", re.IGNORECASE)
_EN_IMPOSSIBLE = re.compile(
    rf"(?<![A-Za-z])(?:can{_AP}?t|cannot|can\s+not|couldn{_AP}?t|could\s+not"
    rf"|(?:won{_AP}?t|will\s+not)(?:\s+be\s+able\s+to)?"
    r"|unable\s+to|not\s+able\s+to|not\s+possible|impossible)(?![A-Za-z])"
    rf"|n{_AP}t\s+able\s+to(?![A-Za-z])",  # "wasn't able to"
    re.IGNORECASE,
)
# 'can'은 명사('a can of')보다 조동사가 압도적이라 낱말 경계만 지킨다. 넓히지 않는다.
_EN_POSSIBLE = re.compile(
    r"(?<![A-Za-z])(?:can|able\s+to|possible|works\s+for\s+(?:me|us)|that\s+works)(?![A-Za-z])",
    re.IGNORECASE,
)
# 'no' 한 낱말은 부정으로 읽지 않는다 ('No worries', 'PO no. 4500').
_EN_NEGATION = re.compile(
    rf"(?<![A-Za-z])(?:(?:don|doesn|didn|isn|aren|wasn|weren|haven|hasn|hadn|wouldn|shouldn|mustn|ain){_AP}?t"
    r"|not|never|no\s+longer)(?![A-Za-z])",
    re.IGNORECASE,
)
_EN_PROMISE_VERBS = (
    r"send|confirm|share|check|update|get\s+back|follow\s+up|let\s+you\s+know|reply|call|deliver"
    r"|forward|provide|review|finish|prepare|handle|arrange|schedule|ship|pay|fix|look\s+into|make\s+sure"
    r"|take\s+care|do"
)
# "I'll"·"we'll"은 아포스트로피가 있어야 한다('Ill', 'well'은 다른 낱말). 뒤가 not·never면 약속이 아니다.
_EN_PROMISE = re.compile(
    rf"(?<![A-Za-z])(?:(?:I|we)(?:{_AP}ll|\s+will)|(?:I{_AP}?m|I\s+am|we{_AP}re|we\s+are)\s+going\s+to|gonna)"
    r"\s+(?!not(?![A-Za-z])|never(?![A-Za-z]))[A-Za-z]+"
    rf"|(?<![A-Za-z])will\s+(?:{_EN_PROMISE_VERBS})(?![A-Za-z])",
    re.IGNORECASE,
)

_MASK = "·"  # 한글도 로마자도 공백도 아닌 글자 — 가린 자리가 앞뒤 낱말과 이어져 새로 맞는 일이 없다


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


_GROUP_COMMA = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")


def _norm_quantity(text: str) -> str:
    """수량을 견줄 값. 자릿수 쉼표는 표기일 뿐이다 — '52000원'과 '52,000원'은 같은 값이다."""
    return _GROUP_COMMA.sub("", _norm(text))


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

    for m in _EN_MONTH_DAY.finditer(text):
        add("datetime", m.group(0).strip(), f"{_EN_MONTHS[m['month'].lower()]}{int(m['day'])}")
    for m in _EN_DAY_MONTH.finditer(text):
        add("datetime", m.group(0).strip(), f"{_EN_MONTHS[m['month'].lower()]}{int(m['day'])}")
    for m in _EN_WEEKDAY.finditer(text):
        add("datetime", m.group(0), _EN_WEEKDAYS[m.group(0).lower()])
    for m in _EN_TIME.finditer(text):
        minute = f":{m['minute']}" if m["minute"] and m["minute"] != "00" else ""
        add("datetime", m.group(0).strip(), f"{int(m['hour'])}{minute}{m['half'].lower()}m")
    for m in _EN_RELATIVE.finditer(text):
        add("datetime", m.group(0), _norm(m.group(0)).lower())

    for m in _CLOCK.finditer(text):
        add("datetime", m.group(0), f"{int(m['hour'])}:{m['minute']}")

    for m in _QUANTITY.finditer(text):
        add("quantity", m.group(1).strip(), _norm_quantity(m.group(1)))
    for s in _find(_BARE_NUMBER, text):
        add("quantity", s, _norm_quantity(s))

    for m in _PERSON.finditer(text):
        if m.group(2) and not _plausible_name(m.group(1)):
            continue  # '어제 파트장님': 앞 낱말이 이름이 아니다 — 이름 없는 직함은 보존 항목이 아니다
        name = _person_name(m.group(1))
        if name is not None:
            add("person", m.group(0).strip(), name)

    # 방향이 뒤집히는 꼴(이중 부정, 영어의 'can't')을 먼저 읽고 그 자리를 가린다. `rest`는 가린 글이다.
    def take(pattern: re.Pattern[str], source: str, value: str | None, flipped: str | None = None) -> str:
        """맞은 자리를 possibility로 담고 가린 글을 돌려준다. `imp` 묶음이 맞으면 `flipped` 값을 쓴다."""

        def on_match(m: re.Match[str]) -> str:
            groups = m.groupdict()
            result = flipped if flipped is not None and groups.get("imp") else value
            if result is not None and groups.get("ask"):
                result = "가능" if result == "불가" else "불가"  # 물음꼴은 한 번 더 뒤집는다
            if result is not None:
                add("possibility", m.group(0).strip(), result)
            return _MASK * len(m.group(0))

        return pattern.sub(on_match, source)

    rest = take(_NEGATED_STEM, text, "불가", flipped="가능")
    rest = take(_NOT_THAT, rest, "불가", flipped="가능")
    rest = take(_EN_NOT_ONLY, rest, None)
    rest = take(_EN_NOT_IMPOSSIBLE, rest, "가능")
    rest = take(_EN_IMPOSSIBLE, rest, "불가")

    for s in _find(_IMPOSSIBLE, rest):
        add("possibility", s, "불가")
    for s in _find(_POSSIBLE, rest):
        add("possibility", s, "가능")
    for s in _find(_EN_POSSIBLE, rest):
        add("possibility", s, "가능")

    for s in _find(_PROMISE, text):
        add("promise", s, "약속")
    for s in _find(_EN_PROMISE, rest):  # 가린 글에서: "I will not send"는 약속이 아니다
        add("promise", s, "약속")

    for s in _find(_CONDITION, text):
        add("condition", s)

    negation = _NEGATION.search(rest) or _EN_NEGATION.search(rest)
    if negation:
        add("negation", negation.group(0), "부정")

    return list(items.values())


def facts_only(items: list[PreservedItem]) -> set[tuple[str, str]]:
    """'추가된 사실' 검사 대상 — 날짜·수량·사람만. 서술어는 문체에 따라 바뀔 수 있다."""
    return {(i.kind, i.normalized) for i in items if i.kind in {"datetime", "quantity", "person"}}


def polarity(items: list[PreservedItem]) -> set[str]:
    """가능/불가/부정의 방향 집합."""
    return {i.normalized for i in items if i.kind in {"possibility", "negation"}}
