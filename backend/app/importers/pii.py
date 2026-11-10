"""가져온 대화 본문의 개인정보 패턴 가림 (`IMPORT_MASK_PII`).

API로 들어온 가져오기에만 쓴다 (`ImportService.import_messages(..., mask_pii=True)`, `import_parsed`).
평가·합성 적재 스크립트는 `ImportService`를 그대로 불러 가림 없이 넣는다.

가리는 것: 이메일, 주민등록번호, 카드 번호, 전화번호, 계좌 번호 → `[이메일]` 같은 표시로 바꾼다.
규칙은 **보수적**이다 — 날짜(2026-10-05), 금액(1,234,000원), 시각, 일반 숫자는 건드리지 않는다. 그 대신
놓치는 것이 있다 (구분 기호 없는 지역번호 전화, 하이픈 없는 주민등록번호·계좌번호는 앞에 낱말이 있을 때만).
가림은 저장 전에 한 번만 하고 원문은 남기지 않는다. 이름·주소·사진 설명 같은 자유 서술은 가리지 못한다.
"""

from __future__ import annotations

import re
from collections.abc import Callable

EMAIL_TOKEN = "[이메일]"
RRN_TOKEN = "[주민등록번호]"
CARD_TOKEN = "[카드번호]"
PHONE_TOKEN = "[전화번호]"
ACCOUNT_TOKEN = "[계좌번호]"

# 앞뒤 경계는 ASCII로만 본다 — 한글 조사("메일은a@b.com입니다")가 붙어도 잡히게.
_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
    r"(?![A-Za-z0-9-])"
)

_RRN_DATE = r"\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])"
# 하이픈이 있으면 그대로, 없으면(13자리 숫자) 앞에 '주민…번호' 낱말이 있을 때만 — 바코드·주문번호 오탐 방지.
_RRN_HYPHEN = re.compile(rf"(?<![\d-]){_RRN_DATE} ?- ?[1-8]\d{{6}}(?![\d-])")
_RRN_KEYWORD = re.compile(rf"(주민(?:등록)?\s*번호\D{{0,6}})({_RRN_DATE}[1-8]\d{{6}})(?!\d)")

_CARD_GROUPED = re.compile(r"(?<![\d-])\d{4}([ -])\d{4}\1\d{4}\1\d{4}(?![\d-])")
_CARD_AMEX = re.compile(r"(?<![\d-])3[47]\d{2}([ -])\d{6}\1\d{5}(?![\d-])")
_CARD_PLAIN = re.compile(r"(?<!\d)\d{16}(?!\d)")
# 붙여 쓴 16자리는 카드사 번호대 + 검증 숫자(Luhn)가 맞을 때만. 2026…으로 시작하는 주문번호는 걸리지 않는다.
_CARD_PREFIX = re.compile(r"^(?:4|5[1-5]|2[3-6]|222[1-9]|22[3-9]|27[01]|2720|3[5-8]|6|9)")

_PHONE_MOBILE = re.compile(r"(?<![\d-])(?:\+82[-. ]?1|01)[016789][-. ]?\d{3,4}[-. ]?\d{4}(?![\d-])")
# 지역번호·070·050x는 구분 기호가 있을 때만 (02-123-4567). 붙여 쓴 숫자는 금액·수량과 구분할 수 없다.
_PHONE_AREA = re.compile(
    r"(?<![\d-])(?:\+82[-. ]?(?:2|3[1-3]|4[1-4]|5[1-5]|6[1-4]|70)|0(?:2|3[1-3]|4[1-4]|5[1-5]|6[1-4]|70|50\d))"
    r"[-. ]\d{3,4}[-. ]\d{4}(?![\d-])"
)

_ACCOUNT_HYPHEN = re.compile(r"(?<![\d-])\d{2,6}(?:-\d{2,7}){2,4}(?![\d-])")
_ACCOUNT_KEYWORD = re.compile(
    r"((?:계좌(?:\s*번호)?|은행|뱅크|농협|수협|신협|우체국|새마을금고|토스)\s*[:：]?\s*)(\d{10,14})(?!\d)"
)
_DATE_LIKE = re.compile(r"^(?:19|20)\d{2}-(?:0?[1-9]|1[0-2])-(?:0?[1-9]|[12]\d|3[01])(?:-|$)")


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


def _sub(
    pattern: re.Pattern[str], text: str, repl: str | Callable[[re.Match[str]], str | None]
) -> tuple[str, int]:
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


def _card_plain(m: re.Match[str]) -> str | None:
    digits = m.group(0)
    return CARD_TOKEN if _CARD_PREFIX.match(digits) and _luhn_ok(digits) else None


def _account_hyphen(m: re.Match[str]) -> str | None:
    raw = m.group(0)
    digits = raw.replace("-", "")
    if not 10 <= len(digits) <= 14 or _DATE_LIKE.match(raw):
        return None
    return ACCOUNT_TOKEN


def mask_pii(text: str) -> tuple[str, int]:
    """개인정보 패턴을 표시로 바꾼 본문과 가린 건수."""
    if not text or not any(ch.isdigit() or ch == "@" for ch in text):
        return text, 0
    total = 0
    steps: tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str | None]], ...] = (
        (_EMAIL, EMAIL_TOKEN),
        (_RRN_HYPHEN, RRN_TOKEN),
        (_RRN_KEYWORD, lambda m: m.group(1) + RRN_TOKEN),
        (_CARD_GROUPED, CARD_TOKEN),
        (_CARD_AMEX, CARD_TOKEN),
        (_CARD_PLAIN, _card_plain),
        (_PHONE_MOBILE, PHONE_TOKEN),
        (_PHONE_AREA, PHONE_TOKEN),
        (_ACCOUNT_HYPHEN, _account_hyphen),
        (_ACCOUNT_KEYWORD, lambda m: m.group(1) + ACCOUNT_TOKEN),
    )
    for pattern, repl in steps:
        text, n = _sub(pattern, text, repl)
        total += n
    return text, total
