"""붙여넣기(plain)와 메시지 배열(json) 파서.

plain — 화면에 직접 붙여 넣는 줄 형식 (예전에는 화면 JS `parseImport`가 하던 일):

    2026-10-05 09:25 | 박지현 | 대시보드 개편은 언제까지 가능해요?
    2026-10-05 09:31 | 김민수 | 다음 주 수요일까지는 가능합니다.

- 날짜 시각은 `YYYY-MM-DD HH:MM[:SS]`(구분 기호 `-`·`/`·`.`, `T`, 시간대 `+09:00`/`Z` 허용).
  시간대가 없으면 `tz_offset_minutes`를 붙인다.
- 화자에 `me`/`contact`를 직접 써도 된다.
- 형식에 맞지 않는 줄은 앞 메시지의 이어지는 줄로 본다. 다만 `|`가 둘 이상인데 날짜를 못 읽은 줄은
  깨진 줄로 보고 건너뛴 수에 넣는다.

json — 기존 가져오기와 같은 메시지 배열 `[{"id"?, "speaker", "sent_at", "text"}, ...]`
(`{"messages": [...]}`로 감싼 것도 받는다). `id`가 없으면 내용으로 만든다.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from app.core.errors import InvalidInputError
from app.importers.base import ParsedImport, ParsedMessage, line_list

# 2026-10-05 09:25 · 2026/10/5 9:25:10 · 2026.10.05T09:25:10.123+09:00 · …Z
_MOMENT = re.compile(
    r"^(\d{4})[-./] ?(\d{1,2})[-./] ?(\d{1,2})\.?[T\s]+(\d{1,2}):(\d{2})"
    r"(?::(\d{2})(?:\.(\d{1,6}))?)?\s*(Z|[+-]\d{2}:?\d{2})?$"
)
_ROLE_NAMES = ("me", "contact")


def parse_moment(raw: str, tz: timezone) -> tuple[datetime, bool] | None:
    """(시각, 초가 있었는가). 읽지 못하면 None. 날짜만 있는 값은 메시지 시각으로 쓰지 않는다."""
    m = _MOMENT.match(raw.strip())
    if not m:
        return None
    zone: timezone = tz
    if m.group(8):
        if m.group(8) == "Z":
            zone = UTC
        else:
            digits = m.group(8)[1:].replace(":", "")
            offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
            zone = timezone(-offset if m.group(8)[0] == "-" else offset)
    try:
        dt = datetime(
            int(m.group(1)),
            int(m.group(2)),
            int(m.group(3)),
            int(m.group(4)),
            int(m.group(5)),
            int(m.group(6) or 0),
            int((m.group(7) or "0").ljust(6, "0")),
            tzinfo=zone,
        )
    except ValueError:  # 13월, 25시 등
        return None
    return dt, m.group(6) is not None


def looks_like_plain(line: str, tz: timezone) -> bool:
    parts = line.split("|")
    return len(parts) >= 3 and parse_moment(parts[0], tz) is not None and bool(parts[1].strip())


def parse_plain(text: str, tz: timezone) -> ParsedImport:
    parsed = ParsedImport(format="plain")
    current: ParsedMessage | None = None
    unreadable: list[int] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip():
            if current is not None:
                current.text += "\n"
            continue
        parts = line.split("|")
        moment = parse_moment(parts[0], tz) if len(parts) >= 3 else None
        speaker = parts[1].strip() if len(parts) >= 3 else ""
        if moment is not None and speaker:
            current = ParsedMessage(
                speaker=speaker,
                sent_at=moment[0],
                text=" | ".join(p.strip() for p in parts[2:]),
                has_seconds=moment[1],
            )
            parsed.messages.append(current)
            continue
        if len(parts) >= 3 or current is None:
            # 깨진 줄(구분 기호는 있는데 날짜·화자를 못 읽음) 또는 첫 메시지보다 앞의 줄
            unreadable.append(lineno)
            parsed.skipped_lines += 1
            continue
        current.text += "\n" + line  # 여러 줄 메시지
    if unreadable:
        parsed.problem(
            f"'날짜 시각 | 화자 | 내용' 형식이 아닌 줄이 {len(unreadable)}개 있습니다 "
            f"({line_list(unreadable)})."
        )
    for name in {m.speaker for m in parsed.messages}:
        if name in _ROLE_NAMES:
            parsed.suggested_roles[name] = name  # type: ignore[assignment]
    return parsed


def looks_like_message_array(data: Any) -> bool:
    items = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return False
    sample = next((m for m in items if isinstance(m, dict)), None)
    return sample is not None and "speaker" in sample and "sent_at" in sample


def parse_json_messages(data: Any, tz: timezone) -> ParsedImport:
    items = data.get("messages") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise InvalidInputError(
            "메시지 배열(JSON)이 아닙니다. [{speaker, sent_at, text}, ...] 형식으로 올려 주세요."
        )
    parsed = ParsedImport(format="json")
    if isinstance(data, dict) and isinstance(data.get("title"), str):
        parsed.title_hint = data["title"].strip() or None
    bad: list[int] = []
    for idx, item in enumerate(items, 1):
        if not isinstance(item, dict):
            bad.append(idx)
            continue
        speaker = item.get("speaker")
        body = item.get("text")
        moment = parse_moment(str(item.get("sent_at") or ""), tz)
        if not isinstance(speaker, str) or not speaker.strip() or not isinstance(body, str) or moment is None:
            bad.append(idx)
            continue
        if not body.strip():
            bad.append(idx)
            continue
        raw_id = item.get("id")
        parsed.messages.append(
            ParsedMessage(
                speaker=speaker.strip(),
                sent_at=moment[0],
                text=body,
                id=str(raw_id).strip() if raw_id not in (None, "") else "",
                # 배열의 시각은 준 그대로 둔다 (같은 초에 여러 건이면 밀리초로 순서만 남긴다)
                has_seconds=True,
            )
        )
    if bad:
        parsed.skipped_lines += len(bad)
        parsed.problem(
            f"speaker·sent_at·text를 읽지 못한 항목이 {len(bad)}개 있습니다 ({line_list(bad, '항목')})."
        )
    for name in {m.speaker for m in parsed.messages}:
        if name in _ROLE_NAMES:
            parsed.suggested_roles[name] = name  # type: ignore[assignment]
    return parsed
