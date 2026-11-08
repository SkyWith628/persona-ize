"""한국어 상대 날짜 표현을 메시지 시각 기준 절대 날짜로 풀어 본문에 주석을 붙인다.

haiku-4-5는 요일을 보여 줘도 '다음 주 수요일' 같은 산술을 자주 틀린다 (2026-11-08 3·4차 수집).
그래서 코드가 먼저 계산해 `다음 주 수요일(=2026-10-14)`처럼 적어 주고, 모델은 그 값을 옮겨 쓰기만 한다.

규칙 (주는 월~일, 날짜는 메시지 시각의 시간대 기준):
- 그제/어제/오늘/내일/모레 → 해당 날짜
- 이번/다음/지난 주 X요일 → 그 주의 X요일
- X요일(수식어 없음) → 문맥 의존이므로 두 후보를 적는다: 오늘 이후 가장 가까운 X요일 "또는" 그 다음 주.
  오늘이 그 요일이면 다음 주부터 (사람은 오늘을 '오늘'이라 부른다). 선택은 모델이 대화 맥락으로 한다.
- 이번/다음/지난 주말 → 그 주 토~일
- 이번/다음/지난 주 → 그 주 월~일
- 이번/다음/지난 달 → 연-월
- N월 N일 → 같은 해의 그 날짜
오전/오후/저녁 같은 시간대 표현은 건드리지 않는다.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

WEEKDAY_INDEX = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
WEEK_OFFSET = {"지난": -1, "이번": 0, "다음": 1}
DAY_OFFSET = {"그제": -2, "어제": -1, "오늘": 0, "내일": 1, "모레": 2}

# 긴 패턴을 앞에 둔다. 같은 위치에서 왼쪽 대안이 먼저 시도되므로 '다음 주 수요일'이 '다음 주'보다 먼저 잡힌다.
_PATTERN = re.compile(
    r"(?P<day>그제|어제|오늘|내일|모레)"
    r"|(?P<wk_mod>이번|다음|지난)\s*주\s*(?P<wk_day>[월화수목금토일])요일"
    r"|(?P<we_mod>이번|다음|지난)\s*주말"
    r"|(?P<w_mod>이번|다음|지난)\s*주(?![말간])"
    r"|(?P<m_mod>이번|다음|지난)\s*달"
    r"|(?P<abs_month>\d{1,2})월\s*(?P<abs_day>\d{1,2})일"
    r"|(?P<bare_day>[월화수목금토일])요일"
)


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _shift_month(day: date, months: int) -> tuple[int, int]:
    index = day.year * 12 + (day.month - 1) + months
    return index // 12, index % 12 + 1


def _resolve(match: re.Match[str], today: date) -> str | None:
    """매치 하나를 'YYYY-MM-DD' 또는 범위 문자열로 바꾼다. 못 풀면 None."""
    g = match.groupdict()
    if g["day"]:
        return (today + timedelta(days=DAY_OFFSET[g["day"]])).isoformat()
    if g["wk_mod"]:
        monday = _monday(today) + timedelta(weeks=WEEK_OFFSET[g["wk_mod"]])
        return (monday + timedelta(days=WEEKDAY_INDEX[g["wk_day"]])).isoformat()
    if g["we_mod"]:
        monday = _monday(today) + timedelta(weeks=WEEK_OFFSET[g["we_mod"]])
        saturday, sunday = monday + timedelta(days=5), monday + timedelta(days=6)
        return f"{saturday.isoformat()}~{sunday:%m-%d}"
    if g["w_mod"]:
        monday = _monday(today) + timedelta(weeks=WEEK_OFFSET[g["w_mod"]])
        return f"{monday.isoformat()}~{monday + timedelta(days=6):%m-%d}"
    if g["m_mod"]:
        year, month = _shift_month(today, WEEK_OFFSET[g["m_mod"]])
        return f"{year}-{month:02d}"
    if g["abs_month"]:
        try:
            return date(today.year, int(g["abs_month"]), int(g["abs_day"])).isoformat()
        except ValueError:
            return None
    if g["bare_day"]:
        # 수식어 없는 요일은 문맥 의존이다 ("수요일로 조정" = 다음 주 수요일일 수 있음, c1:m05).
        # 산술만 대신해 주고 선택은 모델에 남긴다: 가장 가까운 날과 그 다음 주.
        ahead = (WEEKDAY_INDEX[g["bare_day"]] - today.weekday()) % 7 or 7
        nearest = today + timedelta(days=ahead)
        return f"{nearest.isoformat()} 또는 {nearest + timedelta(days=7):%Y-%m-%d}"
    return None


def resolve_relative_dates(text: str, reference: datetime) -> list[tuple[str, str]]:
    """본문에서 찾은 (표현, 절대 날짜) 목록. 순서는 등장 순."""
    today = reference.date()
    found: list[tuple[str, str]] = []
    for match in _PATTERN.finditer(text):
        value = _resolve(match, today)
        if value is not None:
            found.append((match.group(0), value))
    return found


def annotate_relative_dates(text: str, reference: datetime) -> str:
    """상대 날짜 표현 뒤에 `(=YYYY-MM-DD)`를 붙인다. 이미 주석이 있으면 다시 붙이지 않는다."""
    today = reference.date()

    def replace(match: re.Match[str]) -> str:
        expr = match.group(0)
        if text[match.end() : match.end() + 2] == "(=":
            return expr
        value = _resolve(match, today)
        return expr if value is None else f"{expr}(={value})"

    return _PATTERN.sub(replace, text)
