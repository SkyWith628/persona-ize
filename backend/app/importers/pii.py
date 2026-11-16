"""가져온 대화 본문의 개인정보 패턴 가림 (`IMPORT_MASK_PII`).

API로 들어온 가져오기에만 쓴다 (`ImportService.import_messages(..., mask_pii=True)`, `import_parsed`).
평가·합성 적재 스크립트는 `ImportService`를 그대로 불러 가림 없이 넣는다.

가리는 것: 이메일, 주민등록번호, 카드 번호, 전화번호, 계좌 번호 → `[이메일]` 같은 표시로 바꾼다.
규칙은 **보수적**이다 — 날짜(2026-10-05), 금액(1,234,000원), 시각, 점수·운송장 같은 일반 숫자는 건드리지
않는다. 그 대신 놓치는 것이 있다 (전각 숫자, 한글로 쓴 숫자, 한 자씩 띄어 쓴 번호, 은행 이름 없이 4-4-4로 쓴
계좌 등). 가림은 저장 전에 한 번만 하고 원문은 남기지 않는다. 이름·주소 같은 자유 서술은 가리지 못한다.

**시간 상한**: 모든 패턴은 반복 횟수가 정해져 있다(`{0,3}` 꼴). 반복 수가 열린 `\\s*` 둘이 맞닿으면 공백
수만 자에 제곱 시간이 걸려 서버 전체가 멈춘다(2026-11-11 보안 검토: 30KB 요청 한 건에 23초). 패턴을
고칠 때는 열린 반복(`*`, `+`)을 서로 붙이지 말고 `tests/test_importers.py`의 시간 상한 테스트를 돌린다.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime

EMAIL_TOKEN = "[이메일]"
RRN_TOKEN = "[주민등록번호]"
CARD_TOKEN = "[카드번호]"
PHONE_TOKEN = "[전화번호]"
ACCOUNT_TOKEN = "[계좌번호]"

Replacement = str | Callable[[re.Match[str]], str | None]

# 앞뒤 경계는 ASCII로만 본다 — 한글 조사("메일은a@b.com입니다")가 붙어도 잡히게.
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
    r"(?![A-Za-z0-9-])"
)

# ---- 주민등록번호: 생년월일 6자리 + 성별 숫자(1~8) + 6자리 ----
_RRN_DATE = r"\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])"
# 하이픈으로 나눈 것은 그대로 가린다 (하이픈 앞뒤 공백 허용).
_RRN_HYPHEN = re.compile(rf"(?<![\d-]){_RRN_DATE} ?- ?[1-8]\d{{6}}(?![\d-])")
# 공백으로 나눴거나 붙여 쓴 13자리는 생년월일과 성별 숫자가 서로 맞을 때만 — 바코드·주문번호 오탐 방지.
_RRN_LOOSE = re.compile(
    r"(?<![\d-])(\d{2})(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]) {0,2}([1-8])\d{6}(?![\d-])"
)
_RRN_HINT = re.compile(r"주민(?:등록)?[ \t]{0,3}번호[^\d\n]{0,6}$")

# ---- 카드 번호 ----
_CARD_GROUPED = re.compile(r"(?<![\d.-])\d{4}([ .-])\d{4}\1\d{4}\1\d{4}(?![\d.-])")
_CARD_AMEX = re.compile(r"(?<![\d-])3[47]\d{2}([ -])\d{6}\1\d{5}(?![\d-])")
_CARD_PLAIN = re.compile(r"(?<!\d)\d{16}(?!\d)")
# 붙여 쓴 16자리는 카드사 번호대 + 검증 숫자(Luhn)가 맞을 때만. 2026…으로 시작하는 주문번호는 걸리지 않는다.
_CARD_PREFIX = re.compile(r"^(?:4|5[1-5]|2[3-6]|222[1-9]|22[3-9]|27[01]|2720|3[5-8]|6|9)")

# ---- 전화번호 ----
# 자리 사이: 010-1234-5678 · 010 - 1234 - 5678 · 010 1234 5678 · 010.1234.5678 · 01012345678
_SEP = r"(?: ?[-.·] ?| )?"
_PHONE_MOBILE = re.compile(
    rf"(?<![\d-])(?:\+82[-. ]?(?:\(0\))?1[016789]|\(?01[016789]\)?){_SEP}\d{{3,4}}{_SEP}\d{{4}}(?![\d-])"
)
_AREA = r"(?:2|3[1-3]|4[1-4]|5[1-5]|6[1-4]|70|50\d)"
# 지역번호·070·050x: 구분 기호나 괄호가 있는 것 (02-123-4567, (02)123-4567, 02)123-4567)
_PHONE_AREA = re.compile(
    rf"(?<![\d-])(?:\+82[-. ]?{_AREA}|\(?0{_AREA})(?: ?[-.·] ?| |\) ?)\d{{3,4}}{_SEP}\d{{4}}(?![\d-])"
)
# 붙여 쓴 유선 번호: 0으로 시작하고 지역번호와 자릿수가 맞을 때 (0212345678).
# 금액·수량은 0으로 시작하지 않는다.
_PHONE_AREA_PLAIN = re.compile(
    r"(?<![\d.-])0(?:(?:2|3[1-3]|4[1-4]|5[1-5]|6[1-4])\d{7,8}|70\d{8}|50\d\d{7,8})(?![\d-])"
)

# ---- 계좌 번호 ----
_BANK_STRONG = r"(?:계좌(?:[ \t]{0,2}번호)?|은행|뱅크|농협|수협|신협|우체국|새마을금고|토스)"
# 은행 이름이지만 흔한 낱말이기도 한 것 — 바로 뒤에 숫자가 올 때만 계좌로 본다.
_BANK_NAME = r"(?:국민|신한|우리|하나|기업|카카오|케이|씨티|대구|부산|경남|광주|전북|제주|산업|KB|NH|IBK|SC)"
_PARTICLE = r"(?:는|은|이|가|로|으로|도)?"
_ACCOUNT_HYPHEN = re.compile(r"(?<![\d-])\d{2,6}(?:-\d{2,7}){2,4}(?![\d-])")
_ACCOUNT_KEYWORD = re.compile(
    rf"((?:{_BANK_STRONG}{_PARTICLE}|{_BANK_NAME})[ \t]{{0,3}}[:：]?[ \t]{{0,3}})(\d{{10,14}})(?!\d)"
)
_ACCOUNT_PREFIX = re.compile(rf"(?:{_BANK_STRONG}{_PARTICLE}|{_BANK_NAME})[ \t]{{0,3}}[:：]?[ \t]{{0,3}}$")
_BANK_HINT = re.compile(rf"(?:{_BANK_STRONG}|{_BANK_NAME})[^\d\n]{{0,6}}$")
# 날짜로 시작하는 숫자는 계좌로 보지 않는다: 2026-10-05-01(문서번호), 2026100512…(주문번호)
_DATE_LIKE = re.compile(r"^(?:19|20)\d{2}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])(?:-|$)")
_DATE_PLAIN = re.compile(r"^(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])")
HINT_WINDOW = 16  # 숫자 앞에서 낱말(주민번호·은행 이름)을 찾는 범위


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _before(m: re.Match[str]) -> str:
    """매치 바로 앞의 짧은 글 (낱말 단서를 찾는 데 쓴다)."""
    return m.string[max(0, m.start() - HINT_WINDOW) : m.start()]


def _sub(pattern: re.Pattern[str], text: str, repl: Replacement) -> tuple[str, int]:
    """치환하고 실제로 바꾼 횟수를 센다. repl 함수가 None을 돌려주면 그 자리는 그대로 둔다."""
    count = 0

    def _apply(m: re.Match[str]) -> str:
        nonlocal count
        out = repl(m) if callable(repl) else repl
        if out is None:
            return m.group(0)
        count += 1
        return out

    return pattern.sub(_apply, text), count


def _rrn_loose(m: re.Match[str]) -> str | None:
    """공백으로 나눴거나 붙여 쓴 13자리: 성별 숫자가 가리키는 세기와 생년이 맞아야 한다.

    3·4·7·8은 2000년대생이다 — 생년이 올해보다 뒤면 주민등록번호가 아니다(바코드 880104 3… 등).
    앞에 '주민등록번호' 낱말이 있으면 그대로 가리고, 은행·계좌 낱말 바로 뒤면 계좌 번호로 가린다.
    """
    before = _before(m)
    if _RRN_HINT.search(before):
        return RRN_TOKEN
    if " " not in m.group(0) and _ACCOUNT_PREFIX.search(before):
        return ACCOUNT_TOKEN  # '계좌 번호 : 1101234567890'처럼 은행·계좌 낱말 바로 뒤의 13자리는 계좌다
    if m.group(2) in "3478" and int(m.group(1)) > datetime.now(UTC).year % 100:
        return None
    return RRN_TOKEN


def _card_plain(m: re.Match[str]) -> str | None:
    digits = m.group(0)
    return CARD_TOKEN if _CARD_PREFIX.match(digits) and _luhn_ok(digits) else None


def _account_hyphen(m: re.Match[str]) -> str | None:
    """하이픈으로 나눈 계좌: 3~4묶음이고 5자리 이상인 묶음이 있을 때 (110-123-456789).

    4-4-4(운송장·토스), 3-4-4-2(농협), 2자리 묶음의 나열(점수·로또)은 앞에 은행·계좌 낱말이 있을 때만 가린다.
    """
    raw = m.group(0)
    groups = raw.split("-")
    if not 10 <= sum(len(g) for g in groups) <= 14 or _DATE_LIKE.match(raw):
        return None
    if 3 <= len(groups) <= 4 and max(len(g) for g in groups) >= 5:
        return ACCOUNT_TOKEN
    return ACCOUNT_TOKEN if _BANK_HINT.search(_before(m)) else None


def _account_keyword(m: re.Match[str]) -> str | None:
    """은행·계좌 낱말 뒤의 붙여 쓴 10~14자리. 날짜로 시작하는 숫자(주문번호 2026100512…)는 그대로 둔다."""
    return None if _DATE_PLAIN.match(m.group(2)) else m.group(1) + ACCOUNT_TOKEN


_STEPS: tuple[tuple[re.Pattern[str], Replacement], ...] = (
    (_EMAIL, EMAIL_TOKEN),
    (_RRN_HYPHEN, RRN_TOKEN),
    (_RRN_LOOSE, _rrn_loose),
    (_CARD_GROUPED, CARD_TOKEN),
    (_CARD_AMEX, CARD_TOKEN),
    (_CARD_PLAIN, _card_plain),
    (_PHONE_MOBILE, PHONE_TOKEN),
    (_PHONE_AREA, PHONE_TOKEN),
    (_PHONE_AREA_PLAIN, PHONE_TOKEN),
    (_ACCOUNT_HYPHEN, _account_hyphen),
    (_ACCOUNT_KEYWORD, _account_keyword),
)


def mask_pii(text: str) -> tuple[str, int]:
    """개인정보 패턴을 표시로 바꾼 본문과 가린 건수."""
    if not text or not any(ch.isdigit() or ch == "@" for ch in text):
        return text, 0
    total = 0
    for pattern, repl in _STEPS:
        text, n = _sub(pattern, text, repl)
        total += n
    return text, total
