"""한국어 상대 날짜 표현을 메시지 시각 기준 절대 날짜로 풀어 본문에 주석을 붙인다.

haiku-4-5는 요일을 보여 줘도 '다음 주 수요일' 같은 산술을 자주 틀린다 (2026-11-08 3·4차 수집).
그래서 코드가 먼저 계산해 `다음 주 수요일(=2026-10-14)`처럼 적어 주고, 모델은 그 값을 옮겨 쓰기만 한다.

규칙 (주는 월~일, 날짜는 메시지 시각을 **한국 시간**으로 읽은 것 — 저장소가 UTC로 돌려줘도 같다):
- 그제/어제/오늘/내일/모레 → 해당 날짜
- 이번/다음/지난 주 X요일 → 그 주의 X요일
- X요일(수식어 없음) → 같은 대화방 앞 메시지가 그 요일의 날짜를 특정했고 아직 지나지 않았으면 그 날짜
  (`DiscourseContext`). 아니면 문맥 의존이므로 두 후보를 적는다: 오늘 이후 가장 가까운 X요일 "또는"
  그 다음 주. 오늘이 그 요일이면 다음 주부터 (사람은 오늘을 '오늘'이라 부른다). 선택은 모델이 맥락으로 한다.
- 이번/다음/지난 주말 → 그 주 토~일
- 이번/다음/지난 주 → 그 주 월~일
- 이번/다음/지난 달 → 연-월
- N월 N일 → 같은 해의 그 날짜
오전/오후/저녁 같은 시간대 표현은 건드리지 않는다.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from app.domain.conversation import local_time

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


class DiscourseContext:
    """같은 대화방 안에서 앞 메시지가 특정한 날짜를 기억해, 뒤의 수식어 없는 요일을 그 날짜로 푼다.

    예: "다음 주 수요일(=10-14)까지 가능합니다" 뒤의 "수요일로 조정 부탁드립니다"는 10-14다.
    앵커는 그 날짜가 지나기 전까지만 유효하다. 대화방마다 하나를 만들어 메시지를 시간 순으로 넘긴다.
    """

    def __init__(self) -> None:
        self._anchors: dict[int, date] = {}

    def remember(self, day: date) -> None:
        self._anchors[day.weekday()] = day

    def anchor_for(self, weekday: int, today: date) -> date | None:
        anchored = self._anchors.get(weekday)
        return anchored if anchored is not None and anchored >= today else None


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _shift_month(day: date, months: int) -> tuple[int, int]:
    index = day.year * 12 + (day.month - 1) + months
    return index // 12, index % 12 + 1


def _resolve(match: re.Match[str], today: date, context: DiscourseContext | None = None) -> str | None:
    """매치 하나를 'YYYY-MM-DD' 또는 범위 문자열로 바꾼다. 못 풀면 None.

    요일이 특정되는 표현(이번/다음 주 X요일, 주말, N월 N일)은 context에 앵커로 남기고,
    수식어 없는 요일은 앵커가 있으면 그 날짜, 없으면 두 후보를 돌려준다.
    """
    g = match.groupdict()
    if g["day"]:
        return (today + timedelta(days=DAY_OFFSET[g["day"]])).isoformat()
    if g["wk_mod"]:
        monday = _monday(today) + timedelta(weeks=WEEK_OFFSET[g["wk_mod"]])
        day = monday + timedelta(days=WEEKDAY_INDEX[g["wk_day"]])
        if context is not None:
            context.remember(day)
        return day.isoformat()
    if g["we_mod"]:
        monday = _monday(today) + timedelta(weeks=WEEK_OFFSET[g["we_mod"]])
        saturday, sunday = monday + timedelta(days=5), monday + timedelta(days=6)
        if context is not None:
            context.remember(saturday)
            context.remember(sunday)
        return f"{saturday.isoformat()}~{sunday:%m-%d}"
    if g["w_mod"]:
        monday = _monday(today) + timedelta(weeks=WEEK_OFFSET[g["w_mod"]])
        return f"{monday.isoformat()}~{monday + timedelta(days=6):%m-%d}"
    if g["m_mod"]:
        year, month = _shift_month(today, WEEK_OFFSET[g["m_mod"]])
        return f"{year}-{month:02d}"
    if g["abs_month"]:
        try:
            day = date(today.year, int(g["abs_month"]), int(g["abs_day"]))
        except ValueError:
            return None
        if context is not None:
            context.remember(day)
        return day.isoformat()
    if g["bare_day"]:
        weekday = WEEKDAY_INDEX[g["bare_day"]]
        anchored = context.anchor_for(weekday, today) if context is not None else None
        if anchored is not None:
            # 앞 메시지가 특정한 같은 요일의 날짜를 이어받는다 ("다음 주 수요일" → "수요일로 조정").
            return anchored.isoformat()
        # 앵커가 없으면 문맥 의존이다. 산술만 대신해 주고 선택은 모델에 남긴다: 가장 가까운 날과 그 다음 주.
        ahead = (weekday - today.weekday()) % 7 or 7
        nearest = today + timedelta(days=ahead)
        return f"{nearest.isoformat()} 또는 {nearest + timedelta(days=7):%Y-%m-%d}"
    return None


def resolve_relative_dates(
    text: str, reference: datetime, context: DiscourseContext | None = None
) -> list[tuple[str, str]]:
    """본문에서 찾은 (표현, 절대 날짜) 목록. 순서는 등장 순. context를 주면 앵커를 읽고 갱신한다."""
    today = local_time(reference).date()
    found: list[tuple[str, str]] = []
    for match in _PATTERN.finditer(text):
        value = _resolve(match, today, context)
        if value is not None:
            found.append((match.group(0), value))
    return found


def annotate_relative_dates(text: str, reference: datetime, context: DiscourseContext | None = None) -> str:
    """상대 날짜 표현 뒤에 `(=YYYY-MM-DD)`를 붙인다. 이미 주석이 있으면 다시 붙이지 않는다."""
    today = local_time(reference).date()

    def replace(match: re.Match[str]) -> str:
        expr = match.group(0)
        if text[match.end() : match.end() + 2] == "(=":
            return expr
        value = _resolve(match, today, context)
        return expr if value is None else f"{expr}(={value})"

    return _PATTERN.sub(replace, text)
