"""카카오톡 '대화 내보내기' 파서 (.txt — 안드로이드·iOS·PC 변형, PC/맥의 .csv).

**실물 샘플 검증 전이다.** 알려진 변형(2차 자료, 서로 엇갈림)을 합성 픽스처로 만들어 맞췄다. 그래서 관대하게
읽고, 못 읽은 줄은 숨기지 않고 센다(`skipped_lines`, `problems`). 실제 내보내기 파일을 받으면
`tests/test_importers.py`에 픽스처를 더하고 여기 정규식을 고친다.

읽는 모양
- 안드로이드: `2026년 10월 5일 오전 9:25, 박지현 : 본문` — 날짜만 있는 줄과 ` : `가 없는 줄
  (입·퇴장)은 시스템 줄.
  내보낸 본인은 `회원님`으로 나온다(→ 화자 추천 me).
- iOS: 날짜 구분선 `2026년 10월 5일 월요일`, 본문 `2026. 10. 5. 오전 9:25, 박지현 : 본문`(24시간제 `09:25`도).
- PC: 구분선 `--------------- 2026년 10월 5일 월요일 ---------------`, 본문 `[박지현] [오전 9:25] 본문`
  (날짜는 구분선에서 이어받는다). 영어 로캘 `[Name] [9:25 AM]`, `Monday, October 5, 2026`도 읽는다.
- CSV: 머리 `Date,User,Message`, 줄 `2026-10-05 09:25:10,"박지현","본문"` (따옴표 안 줄바꿈·쉼표·`""`).
- 날짜 접두가 없는 줄은 앞 메시지의 이어지는 줄(여러 줄 메시지).
- `사진`, `동영상`, `이모티콘`, `삭제된 메시지입니다.`, `파일: …` 같은 자리표시는 문체 자료가
  아니므로 시스템 줄.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timezone

from app.importers.base import ParsedImport, ParsedMessage, line_list

ME_ALIASES = {"회원님"}  # 안드로이드 내보내기에서 내보낸 본인

_MERIDIEM = r"(오전|오후|AM|PM|am|pm)"
_CLOCK = rf"(?:{_MERIDIEM} )?(\d{{1,2}}):(\d{{2}})(?::(\d{{2}}))?(?: ?{_MERIDIEM})?"

# 2026년 10월 5일 오전 9:25[, 화자 : 본문]
_KO_HEAD = re.compile(rf"^(\d{{4}})년 (\d{{1,2}})월 (\d{{1,2}})일 {_CLOCK}(?:, (.*))?$")
# 2026. 10. 5. 오전 9:25[, 화자 : 본문]  (구분 기호는 . - / 를 받는다)
_NUM_HEAD = re.compile(rf"^(\d{{4}})[.\-/] ?(\d{{1,2}})[.\-/] ?(\d{{1,2}})\.?,? {_CLOCK}(?:, (.*))?$")
# October 5, 2026 9:25 AM[, Name : text] / Oct 5, 2026 at 9:25 AM
_EN_HEAD = re.compile(rf"^([A-Za-z]{{3,9}})\.? (\d{{1,2}}), (\d{{4}}),?(?: at)? {_CLOCK}(?:, (.*))?$")
# [박지현] [오전 9:25] 본문
_PC_MESSAGE = re.compile(rf"^\[(.+?)\] \[{_CLOCK}\] ?(.*)$")
# --------------- 2026년 10월 5일 월요일 ---------------
_PC_SEPARATOR = re.compile(r"^-{3,} ?(\d{4})년 (\d{1,2})월 (\d{1,2})일(?: \S요일)? ?-{3,}$")
_PC_SEPARATOR_EN = re.compile(r"^-{3,} ?(?:[A-Za-z]+, )?([A-Za-z]{3,9})\.? (\d{1,2}), (\d{4}) ?-{3,}$")
# 2026년 10월 5일 월요일 (iOS 날짜 구분선)
_DAY_LINE = re.compile(r"^(\d{4})년 (\d{1,2})월 (\d{1,2})일 \S요일$")
_DAY_LINE_EN = re.compile(r"^(?:[A-Za-z]+, )?([A-Za-z]{3,9})\.? (\d{1,2}), (\d{4})$")

_TITLE = re.compile(r"^(.*?)(?: 님과)? 카카오톡 대화$")
_TITLE_EN = re.compile(r"^(?:KakaoTalk )?Chats? with (.+)$", re.I)
_SAVED = re.compile(r"^(?:저장한 날짜|Date Saved)\s*:", re.I)
_CSV_HEADER = re.compile(r'^"?Date"?,"?User"?,"?Message"?\s*$', re.I)

# 날짜 접두 없이 나오는 입·퇴장 줄(PC). 안드로이드·iOS는 날짜 접두 + ' : ' 없음으로 구분된다.
_SYSTEM_PHRASE = re.compile(
    r"^.{1,60}?(?:님이 들어왔습니다|님이 나갔습니다|님을 초대했습니다|님을 초대하였습니다|님을 내보냈습니다"
    r"|joined this chatroom|left this chatroom|has left the chatroom)\.?$"
    r"|^채팅방 관리자가 메시지를 가렸습니다\.?$|^.{1,60}?invited .{1,60}? to this chatroom\.?$",
    re.I,
)
# 사진·이모티콘 등 자리표시 메시지 (본문 전체가 이것일 때만)
_PLACEHOLDER = re.compile(
    r"^(?:사진(?: \d+장)?|동영상|이모티콘|\(이모티콘\)|음성메시지|삭제된 메시지입니다\.?|파일: .+"
    r"|Photo|\d+ Photos|Video|Emoticon|\(Emoticon\)|Voice Message|This message was deleted\.?|File: .+)$",
    re.I,
)

_MONTHS = {
    name: i + 1
    for i, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
    )
}


def looks_like_kakao(line: str) -> bool:
    """형식 자동 감지용: 이 줄이 카카오톡 내보내기의 머리·구분선·메시지 줄인가."""
    return bool(
        _TITLE.match(line)
        or _TITLE_EN.match(line)
        or _SAVED.match(line)
        or _CSV_HEADER.match(line)
        or _PC_SEPARATOR.match(line)
        or _PC_SEPARATOR_EN.match(line)
        or _PC_MESSAGE.match(line)
        or _DAY_LINE.match(line)
        or (_KO_HEAD.match(line) or _NUM_HEAD.match(line) or _EN_HEAD.match(line)) is not None
        and " : " in line
    )


def is_csv(text: str) -> bool:
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    return bool(_CSV_HEADER.match(first.strip()))


def is_placeholder(text: str) -> bool:
    return bool(_PLACEHOLDER.match(text.strip()))


def _month(name: str) -> int | None:
    return _MONTHS.get(name[:3].lower())


def _clock(
    pre: str | None, hour: str, minute: str, second: str | None, post: str | None
) -> tuple[int, int, int, bool] | None:
    """(시, 분, 초, 초가 있었는가). 오전/오후·AM/PM은 앞뒤 어느 쪽에 있어도 된다."""
    h, mi, s = int(hour), int(minute), int(second) if second else 0
    meridiem = (pre or post or "").upper()
    if meridiem:
        if not 1 <= h <= 12:
            return None
        if meridiem in ("오전", "AM"):
            h = 0 if h == 12 else h
        else:
            h = 12 if h == 12 else h + 12
    if not (0 <= h <= 23 and 0 <= mi <= 59 and 0 <= s <= 59):
        return None
    return h, mi, s, second is not None


def _moment(
    year: int, month: int, day: int, clock: tuple[int, int, int, bool], tz: timezone
) -> datetime | None:
    try:
        return datetime(year, month, day, clock[0], clock[1], clock[2], tzinfo=tz)
    except ValueError:  # 13월, 31일이 없는 달 등
        return None


class _Draft:
    """모으는 중인 메시지 한 건 (여러 줄일 수 있다)."""

    __slots__ = ("speaker", "sent_at", "lines", "has_seconds")

    def __init__(self, speaker: str, sent_at: datetime, first_line: str, has_seconds: bool) -> None:
        self.speaker = speaker
        self.sent_at = sent_at
        self.lines = [first_line]
        self.has_seconds = has_seconds


def _suggest_roles(parsed: ParsedImport) -> None:
    names = [name for name, _ in parsed.speakers()]
    for name in names:
        if name in ME_ALIASES:
            parsed.suggested_roles[name] = "me"
    # 1:1 방: 제목('박지현 님과 카카오톡 대화')의 이름이 상대, 나머지 한 명이 나.
    title = (parsed.title_hint or "").strip()
    if len(names) == 2 and title in names:
        other = next(n for n in names if n != title)
        parsed.suggested_roles.setdefault(title, "contact")
        if parsed.suggested_roles.get(title) == "contact":
            parsed.suggested_roles.setdefault(other, "me")
    elif len(names) == 2 and any(n in ME_ALIASES for n in names):
        other = next(n for n in names if n not in ME_ALIASES)
        parsed.suggested_roles.setdefault(other, "contact")


def parse_kakao(text: str, tz: timezone) -> ParsedImport:
    parsed = _parse_csv(text, tz) if is_csv(text) else _parse_txt(text, tz)
    _suggest_roles(parsed)
    return parsed


def _parse_txt(text: str, tz: timezone) -> ParsedImport:
    """줄 단위 상태 기계. 변형이 섞여 있어도(붙여 넣은 조각 등) 줄마다 맞는 규칙으로 읽는다."""
    parsed = ParsedImport(format="kakao")
    hits = {"android": 0, "ios": 0, "pc": 0}
    current: _Draft | None = None
    current_day: tuple[int, int, int] | None = None  # PC: 구분선에서 이어받는 날짜
    pc_mode = False
    seen_message = False
    unreadable: list[int] = []
    undated: list[int] = []

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        body = "\n".join(current.lines).strip()
        if not body or is_placeholder(body):
            parsed.system_lines += 1  # 사진·이모티콘 자리표시, 빈 메시지
        else:
            parsed.messages.append(
                ParsedMessage(current.speaker, current.sent_at, body, "", current.has_seconds)
            )
        current = None

    def start(speaker: str, when: datetime, first: str, has_seconds: bool) -> None:
        nonlocal current, seen_message
        flush()
        current = _Draft(speaker.strip(), when, first, has_seconds)
        seen_message = True

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        stripped = line.strip()

        # PC 날짜 구분선
        sep = _PC_SEPARATOR.match(stripped)
        sep_en = None if sep else _PC_SEPARATOR_EN.match(stripped)
        if sep or sep_en:
            flush()
            if sep:
                current_day = (int(sep.group(1)), int(sep.group(2)), int(sep.group(3)))
            else:
                month = _month(sep_en.group(1))
                current_day = (int(sep_en.group(3)), month, int(sep_en.group(2))) if month else None
            pc_mode = True
            parsed.system_lines += 1
            continue

        # PC 메시지 줄: [화자] [오전 9:25] 본문
        pc = _PC_MESSAGE.match(line)
        if pc:
            clock = _clock(pc.group(2), pc.group(3), pc.group(4), pc.group(5), pc.group(6))
            when = _moment(*current_day, clock, tz) if (clock and current_day) else None
            if when is None:
                flush()
                (undated if clock else unreadable).append(lineno)
                parsed.skipped_lines += 1
                continue
            start(pc.group(1), when, pc.group(7), clock[3])
            hits["pc"] += 1
            pc_mode = True
            continue

        # iOS 날짜 구분선: 2026년 10월 5일 월요일
        day = _DAY_LINE.match(stripped)
        if day:
            flush()
            current_day = (int(day.group(1)), int(day.group(2)), int(day.group(3)))
            parsed.system_lines += 1
            continue

        # 날짜로 시작하는 줄 (안드로이드·iOS·영어 로캘)
        head = _KO_HEAD.match(line)
        kind = "android"
        ymd: tuple[int, int, int] | None = None
        if head:
            ymd = (int(head.group(1)), int(head.group(2)), int(head.group(3)))
        else:
            head = _NUM_HEAD.match(line)
            if head:
                kind = "ios"
                ymd = (int(head.group(1)), int(head.group(2)), int(head.group(3)))
            else:
                head = _EN_HEAD.match(line)
                month = _month(head.group(1)) if head else None
                if head and month:
                    ymd = (int(head.group(3)), month, int(head.group(2)))
                else:
                    head = None
        if head and ymd:
            clock = _clock(head.group(4), head.group(5), head.group(6), head.group(7), head.group(8))
            when = _moment(*ymd, clock, tz) if clock else None
            rest = head.group(9)
            if when is None:
                flush()
                unreadable.append(lineno)
                parsed.skipped_lines += 1
                continue
            if rest is None or " : " not in rest:
                # 날짜만 있는 줄, 또는 입·퇴장 안내 → 시스템 줄
                flush()
                parsed.system_lines += 1
                continue
            speaker, _, body = rest.partition(" : ")
            start(speaker, when, body, clock[3])
            hits[kind] += 1
            continue

        # 머리말 (첫 메시지 앞에서만)
        if not seen_message and current is None:
            title = _TITLE.match(stripped) or _TITLE_EN.match(stripped)
            if title and parsed.title_hint is None:
                parsed.title_hint = title.group(1).strip() or None
                continue
            if _SAVED.match(stripped) or not stripped:
                continue

        # 영어 로캘 날짜 구분선: Monday, October 5, 2026
        day_en = _DAY_LINE_EN.match(stripped)
        if day_en and _month(day_en.group(1)):
            flush()
            current_day = (int(day_en.group(3)), _month(day_en.group(1)) or 1, int(day_en.group(2)))
            parsed.system_lines += 1
            continue

        if not stripped:
            if current is not None:
                current.lines.append("")  # 여러 줄 메시지 안의 빈 줄 (끝의 빈 줄은 flush가 떼어 낸다)
            continue
        if pc_mode and _SYSTEM_PHRASE.match(stripped):
            flush()
            parsed.system_lines += 1
            continue
        if current is not None:
            current.lines.append(line)  # 여러 줄 메시지의 이어지는 줄
            continue
        unreadable.append(lineno)
        parsed.skipped_lines += 1
    flush()

    if unreadable:
        parsed.problem(f"읽지 못한 줄이 {len(unreadable)}개 있습니다 ({line_list(unreadable)}).")
    if undated:
        parsed.problem(
            f"날짜 구분선보다 앞에 있어 날짜를 알 수 없는 메시지 {len(undated)}건은 가져오지 않습니다 "
            f"({line_list(undated)})."
        )
    best = max(hits, key=lambda k: hits[k])
    parsed.variant = best if hits[best] else None
    return parsed


def _csv_moment(raw: str, tz: timezone) -> tuple[datetime, bool] | None:
    """CSV의 Date 열. `2026-10-05 09:25:10`이 기본이고, 다른 변형의 날짜 표기도 받아 본다."""
    value = raw.strip()
    try:
        dt = datetime.fromisoformat(value.replace(" ", "T", 1))
    except ValueError:
        dt = None
    if dt is not None:
        has_seconds = value.count(":") >= 2
        return (dt if dt.tzinfo else dt.replace(tzinfo=tz)), has_seconds
    for pattern in (_KO_HEAD, _NUM_HEAD):
        m = pattern.match(value)
        if m:
            clock = _clock(m.group(4), m.group(5), m.group(6), m.group(7), m.group(8))
            when = _moment(int(m.group(1)), int(m.group(2)), int(m.group(3)), clock, tz) if clock else None
            if when is not None and clock is not None:
                return when, clock[3]
    return None


def _parse_csv(text: str, tz: timezone) -> ParsedImport:
    parsed = ParsedImport(format="kakao", variant="csv")
    reader = csv.reader(io.StringIO(text, newline=""))
    unreadable: list[int] = []
    try:
        rows = list(reader)
    except csv.Error as exc:
        parsed.problem(f"CSV를 읽지 못했습니다: {exc}")
        return parsed
    header_seen = False
    for rowno, row in enumerate(rows, 1):
        if not row or not any(cell.strip() for cell in row):
            continue
        if not header_seen:
            header_seen = True
            if _CSV_HEADER.match(",".join(row).strip()):
                continue
        if len(row) < 3:
            unreadable.append(rowno)
            parsed.skipped_lines += 1
            continue
        moment = _csv_moment(row[0], tz)
        if moment is None:
            unreadable.append(rowno)
            parsed.skipped_lines += 1
            continue
        speaker = row[1].strip()
        body = ",".join(row[2:]).strip()  # 따옴표 없이 쉼표가 든 본문도 붙여서 살린다
        if not speaker or not body or is_placeholder(body) or _SYSTEM_PHRASE.match(body):
            parsed.system_lines += 1
            continue
        parsed.messages.append(ParsedMessage(speaker, moment[0], body, "", moment[1]))
    if unreadable:
        parsed.problem(f"읽지 못한 행이 {len(unreadable)}개 있습니다 ({line_list(unreadable, '행')}).")
    return parsed
