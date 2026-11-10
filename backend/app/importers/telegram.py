"""텔레그램 Desktop '채팅 기록 내보내기'의 `result.json` 파서.

- 대화 하나를 내보낸 파일: `{"name", "type": "personal_chat", "id", "messages": [...]}`.
- 계정 전체 내보내기: `{"personal_information": {...}, "chats": {"list": [대화, ...]}}` — 대화가 여럿이면
  목록(`ParsedImport.chats`)만 돌려주고, `chat_id`로 하나를 고르게 한다. 메시지가 있는 대화가
  하나뿐이면 그것을 쓴다.
- 메시지: `type`이 `message`인 것만. `service`(입·퇴장, 통화 등)와 본문 없는 미디어, 전달받은 메시지
  (`forwarded_from` — 화자가 쓴 글이 아니다)는 시스템 줄로 센다.
- 시각: `date_unixtime`이 있으면 그것(UTC), 없으면 `date`(현지 시각)에 `tz_offset_minutes`를 붙인다.
- 본문: 문자열, 또는 문자열·`{"type": "bold", "text": "…"}` 조각의 배열 → 이어 붙인다.
- id: 텔레그램 메시지 id(`tg<id>`). 같은 대화를 다시 내보내 올려도 겹치는 메시지는 건너뛴다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timezone
from typing import Any

from app.core.errors import InvalidInputError
from app.importers.base import ChatChoice, ParsedImport, ParsedMessage

UNKNOWN_SPEAKER = "알 수 없음"
SAVED_MESSAGES_NAME = "저장한 메시지"


def looks_like_telegram(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if isinstance(data.get("chats"), dict) and isinstance(data["chats"].get("list"), list):
        return True
    messages = data.get("messages")
    if not isinstance(messages, list):
        return False
    sample = next((m for m in messages if isinstance(m, dict)), None)
    return sample is None or "from" in sample or "date" in sample or "date_unixtime" in sample


def _flatten(text: Any) -> str:
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        parts: list[str] = []
        for piece in text:
            if isinstance(piece, str):
                parts.append(piece)
            elif isinstance(piece, dict) and isinstance(piece.get("text"), str):
                parts.append(piece["text"])
        return "".join(parts)
    return ""


def _moment(message: dict[str, Any], tz: timezone) -> datetime | None:
    unix = message.get("date_unixtime")
    if unix is not None:
        try:
            return datetime.fromtimestamp(int(unix), tz=UTC).astimezone(tz)
        except (TypeError, ValueError, OverflowError, OSError):
            pass
    raw = message.get("date")
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=tz)
    return None


def _chat_name(chat: dict[str, Any]) -> str:
    name = chat.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return SAVED_MESSAGES_NAME if chat.get("type") == "saved_messages" else "(이름 없음)"


def _count_messages(chat: dict[str, Any]) -> int:
    messages = chat.get("messages")
    if not isinstance(messages, list):
        return 0
    return sum(1 for m in messages if isinstance(m, dict) and m.get("type") == "message")


def _me_name(data: dict[str, Any]) -> str | None:
    info = data.get("personal_information")
    if not isinstance(info, dict):
        return None
    name = " ".join(str(info.get(k) or "").strip() for k in ("first_name", "last_name")).strip()
    return name or None


def parse_telegram(data: Any, tz: timezone, chat_id: str | None = None) -> ParsedImport:
    if not isinstance(data, dict):
        raise InvalidInputError("텔레그램 내보내기(result.json) 형식이 아닙니다.")
    me_name = _me_name(data)
    chats_block = data.get("chats")
    if isinstance(chats_block, dict) and isinstance(chats_block.get("list"), list):
        chats = [c for c in chats_block["list"] if isinstance(c, dict)]
        choices = [
            ChatChoice(
                id=str(c.get("id", i)), name=_chat_name(c), type=c.get("type"), messages=_count_messages(c)
            )
            for i, c in enumerate(chats)
        ]
        chosen: dict[str, Any] | None = None
        if chat_id is not None:
            chosen = next((c for c, ch in zip(chats, choices, strict=True) if ch.id == str(chat_id)), None)
            if chosen is None:
                raise InvalidInputError(f"chat_id '{chat_id}'에 해당하는 대화가 내보내기 파일에 없습니다.")
        else:
            with_messages = [c for c, ch in zip(chats, choices, strict=True) if ch.messages > 0]
            if len(with_messages) == 1:
                chosen = with_messages[0]
        if chosen is None:
            parsed = ParsedImport(format="telegram", chats=choices)
            if not choices:
                parsed.problem("내보내기 파일에 대화가 없습니다.")
            else:
                parsed.problem(
                    f"계정 전체 내보내기입니다. 대화 {len(choices)}개 중 가져올 하나를 골라 주세요 (chat_id)."
                )
            return parsed
        return _parse_chat(chosen, tz, me_name)
    if not isinstance(data.get("messages"), list):
        raise InvalidInputError("텔레그램 내보내기(result.json)에 messages 배열이 없습니다.")
    return _parse_chat(data, tz, me_name)


def _parse_chat(chat: dict[str, Any], tz: timezone, me_name: str | None) -> ParsedImport:
    parsed = ParsedImport(format="telegram", title_hint=_chat_name(chat))
    undated = 0
    for m in chat.get("messages") or []:
        if not isinstance(m, dict):
            parsed.skipped_lines += 1
            continue
        if m.get("type") != "message" or m.get("forwarded_from"):
            parsed.system_lines += 1  # service(입·퇴장·통화), 전달받은 메시지
            continue
        body = _flatten(m.get("text")).strip()
        if not body:
            parsed.system_lines += 1  # 사진·스티커·파일만 있는 메시지
            continue
        when = _moment(m, tz)
        if when is None:
            undated += 1
            parsed.skipped_lines += 1
            continue
        speaker = m.get("from") or m.get("from_id") or UNKNOWN_SPEAKER
        message_id = m.get("id")
        parsed.messages.append(
            ParsedMessage(
                speaker=str(speaker),
                sent_at=when,
                text=body,
                id=f"tg{message_id}" if message_id is not None else "",
                has_seconds=True,
            )
        )
    if undated:
        parsed.problem(f"시각을 읽지 못한 메시지 {undated}건은 가져오지 않습니다.")
    _suggest_roles(parsed, chat, me_name)
    return parsed


def _suggest_roles(parsed: ParsedImport, chat: dict[str, Any], me_name: str | None) -> None:
    names = [name for name, _ in parsed.speakers()]
    chat_type = chat.get("type")
    if chat_type == "saved_messages":
        for name in names:
            parsed.suggested_roles[name] = "me"
        return
    if me_name and me_name in names:
        parsed.suggested_roles[me_name] = "me"
    if chat_type != "personal_chat":
        return
    # 1:1 대화: 대화 이름이 상대 이름이다. 나머지 한 명이 나.
    title = parsed.title_hint or ""
    if title in names and parsed.suggested_roles.get(title) != "me":
        parsed.suggested_roles[title] = "contact"
    if len(names) == 2:
        known = [n for n in names if n in parsed.suggested_roles]
        if len(known) == 1:
            other = next(n for n in names if n != known[0])
            parsed.suggested_roles[other] = (
                "me" if parsed.suggested_roles[known[0]] == "contact" else "contact"
            )
