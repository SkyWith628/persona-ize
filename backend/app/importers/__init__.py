"""대화 내보내기 파서: 형식 감지 → 형식별 파서 → 공통 결과(`ParsedImport`).

    parsed = parse_import(text, "auto", tz_offset_minutes=540, mask_pii=True)

- `kakao`: 카카오톡 내보내기 (.txt 안드로이드·iOS·PC, .csv) — `kakao.py`. **실물 샘플 검증 전.**
- `telegram`: 텔레그램 Desktop `result.json` — `telegram.py`
- `plain`: `날짜 시각 | 화자 | 내용` 붙여넣기, `json`: 메시지 배열 — `plain.py`
- 개인정보 가림 — `pii.py`. 공통 뒷정리(같은 분 안 순서, 내용 기반 id) — `base.py`

저장은 하지 않는다. `POST /v1/imports/parse`(미리보기)와 `POST /v1/conversations/{id}/imports/raw`
(화자 지정 → `ImportService.import_parsed`)가 이 결과를 쓴다. 파일 업로드(multipart)는 쓰지 않는다 —
브라우저가 파일을 읽어 JSON 본문의 `text`로 보낸다.
"""

from __future__ import annotations

import json
import unicodedata
from typing import Any

from app.core.errors import InvalidInputError
from app.importers.base import (
    DEFAULT_TZ_OFFSET_MINUTES,
    ChatChoice,
    ImportFormat,
    ParsedImport,
    ParsedMessage,
    RequestedFormat,
    content_id,
    finalize,
    tz_from_offset,
)
from app.importers.kakao import looks_like_kakao, parse_kakao
from app.importers.pii import mask_pii
from app.importers.plain import looks_like_message_array, looks_like_plain, parse_json_messages, parse_plain
from app.importers.telegram import looks_like_telegram, parse_telegram

__all__ = [
    "DEFAULT_TZ_OFFSET_MINUTES",
    "ChatChoice",
    "ImportFormat",
    "ParsedImport",
    "ParsedMessage",
    "RequestedFormat",
    "content_id",
    "detect_format",
    "mask_pii",
    "parse_import",
]

DETECT_LINES = 60  # 형식 감지에 보는 앞부분 줄 수
UNKNOWN_FORMAT = (
    "형식을 알 수 없습니다. 카카오톡 대화 내보내기(.txt·.csv), 텔레그램 내보내기(result.json), "
    "또는 '날짜 시각 | 화자 | 내용' 줄 형식으로 올려 주세요."
)


def clean_text(text: str) -> str:
    """BOM·NUL·깨진 대리 문자(surrogate)를 없애고, 줄바꿈 없는 공백(NBSP 등)을 보통 공백으로 바꾼다.

    한글은 완성형(NFC)으로 맞춘다 — 맥에서 온 풀어쓴 자모(NFD)도 같은 글자·같은 id가 되게.
    """
    text = text.encode("utf-8", errors="replace").decode("utf-8")
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\ufeff", "").replace("\x00", "")
    return text.replace("\u00a0", " ").replace("\u202f", " ").replace("\u200b", "")


def _load_json(text: str) -> Any | None:
    stripped = text.lstrip()
    if stripped[:1] not in ("{", "["):
        return None
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, RecursionError):
        return None


def detect_format(text: str) -> ImportFormat:
    """앞부분을 보고 형식을 정한다. 알 수 없으면 422."""
    data = _load_json(text)
    if data is not None:
        if looks_like_telegram(data):
            return "telegram"
        if looks_like_message_array(data):
            return "json"
        if isinstance(data, list) and not data:
            raise InvalidInputError("메시지가 한 건도 없습니다 (빈 배열).")
        if isinstance(data, dict):
            raise InvalidInputError(
                "JSON이지만 아는 모양이 아닙니다. 텔레그램 result.json이나 "
                "[{speaker, sent_at, text}, ...] 배열을 올려 주세요."
            )
    tz = tz_from_offset(DEFAULT_TZ_OFFSET_MINUTES)
    kakao = plain = 0
    seen = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        seen += 1
        if looks_like_kakao(line.rstrip()):
            kakao += 1
        elif looks_like_plain(line, tz):
            plain += 1
        if seen >= DETECT_LINES:
            break
    if kakao == 0 and plain == 0:
        raise InvalidInputError(UNKNOWN_FORMAT)
    return "kakao" if kakao >= plain else "plain"


def parse_import(
    text: str,
    fmt: RequestedFormat = "auto",
    tz_offset_minutes: int = DEFAULT_TZ_OFFSET_MINUTES,
    *,
    chat_id: str | None = None,
    mask: bool = False,
    max_chars: int | None = None,
) -> ParsedImport:
    """원문을 읽어 공통 결과로 만든다. 한 건도 못 읽으면 422(무엇이 문제인지).

    `mask=True`면 본문의 개인정보 패턴을 가리고(`masked_pii`에 건수), 그 뒤에 id를 만든다.
    `max_chars`(설정 `MAX_IMPORT_CHARS`)를 넘는 원문은 읽지 않고 422.
    텔레그램 계정 전체 내보내기에서 대화를 아직 고르지 않았으면 메시지 없이 `chats` 목록만 돌려준다.
    """
    if max_chars is not None and len(text) > max_chars:
        raise InvalidInputError(
            f"내용이 너무 깁니다 ({len(text):,}자). 한 번에 {max_chars:,}자까지 올릴 수 있습니다. "
            "기간을 나눠 내보내거나 최근 부분만 올려 주세요."
        )
    text = clean_text(text)
    if not text.strip():
        raise InvalidInputError("내용이 비어 있습니다.")
    tz = tz_from_offset(tz_offset_minutes)
    chosen: ImportFormat = detect_format(text) if fmt == "auto" else fmt

    if chosen == "telegram":
        data = _load_json(text)
        if data is None:
            raise InvalidInputError("텔레그램 내보내기는 JSON 파일(result.json)이어야 합니다.")
        parsed = parse_telegram(data, tz, chat_id)
        if parsed.chats is not None:
            return parsed  # 대화를 먼저 골라야 한다
    elif chosen == "json":
        data = _load_json(text)
        if data is None:
            raise InvalidInputError("JSON으로 읽을 수 없습니다. 메시지 배열 형식을 확인해 주세요.")
        parsed = parse_json_messages(data, tz)
    elif chosen == "kakao":
        parsed = parse_kakao(text, tz)
    else:
        parsed = parse_plain(text, tz)

    if mask:
        for m in parsed.messages:
            m.text, n = mask_pii(m.text)
            parsed.masked_pii += n
    finalize(parsed)
    if not parsed.messages:
        detail = " ".join(parsed.problems[:3])
        raise InvalidInputError(
            "메시지를 한 건도 읽지 못했습니다. 형식을 확인해 주세요." + (f" {detail}" if detail else "")
        )
    return parsed
