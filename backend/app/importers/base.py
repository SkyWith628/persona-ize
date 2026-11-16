"""가져오기 파서의 공통 결과와 뒷정리(같은 분 안 순서, 내용 기반 id).

파서는 원문 → `ParsedImport`(형식·변형·제목 힌트·메시지[화자, 시각, 본문]·건너뛴 줄 수·
시스템 줄 수·문제 목록)만 만든다. 화자 지정(me/contact/skip)과 저장은 `ImportService.import_parsed`가 한다.

메시지 id (`ParsedMessage.id`)
- 원본에 id가 있으면 그것을 쓴다 (텔레그램 `tg<id>`, json의 `id`).
- 없으면 **내용으로** 만든다: `UTC 분 + 같은 분·같은 본문 안의 순번 + 본문`의 해시. 화자 이름과 파일 안 위치는
  넣지 않는다 — 같은 내보내기를 다시 올리거나, 범위가 겹치는 더 긴 내보내기를 올리거나, 기기가 달라 내 이름이
  `회원님`/실명으로 다르게 나와도 같은 메시지는 같은 id가 된다 (저장소가 id 중복을 건너뛴다).
- 한계: ① 내보내기의 처음·끝이 어떤 분의 중간에서 잘렸고 그 분 안에 같은 본문이 여러 번 있으면 순번이 달라져
  중복 저장되거나 하나가 빠질 수 있다. ② `tz_offset_minutes`를 다르게 주면 시각이 달라져 다른 메시지로 본다.
  ③ 개인정보 가림 설정을 켜고 끄면 가려진 메시지의 id가 달라진다 (id는 저장되는 본문으로 만든다 —
  가려진 원문을 해시로 되짚을 수 없게 하려는 것).

같은 분 안의 순서: 초가 없는 형식은 그 분 안에서 나온 순서대로 0, 1, 2… 밀리초를 더한다. 초가 있는 형식도
같은 초에 여러 건이면 같은 방식으로 밀리초를 더한다 (PostgreSQL의 `ORDER BY sent_at`이 순서를 지키도록).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal

ImportFormat = Literal["kakao", "telegram", "plain", "json"]
RequestedFormat = Literal["auto", "kakao", "telegram", "plain", "json"]
SuggestedRole = Literal["me", "contact"]

MAX_MESSAGE_CHARS = 4000  # NewMessage.text 검증과 같은 값
MAX_ID_CHARS = 64  # messages.source_key 열 길이
MAX_PROBLEMS = 20
DEFAULT_TZ_OFFSET_MINUTES = 540  # 한국 시간
MIN_YEAR, MAX_YEAR = 1990, 2100  # 이 밖의 시각은 깨진 줄로 본다 (카카오톡 2010~, 텔레그램 2013~)


@dataclass
class ParsedMessage:
    speaker: str
    sent_at: datetime  # 시간대 포함
    text: str
    id: str = ""  # 원본이 준 id. 비어 있으면 finalize가 내용으로 만든다
    has_seconds: bool = False  # 원본 시각에 초가 있었는가


@dataclass
class ChatChoice:
    """텔레그램 계정 전체 내보내기 안의 대화 하나 (고르기용 목록)."""

    id: str
    name: str
    type: str | None
    messages: int


@dataclass
class ParsedImport:
    format: ImportFormat
    variant: str | None = None  # 카카오톡: android | ios | pc | csv
    title_hint: str | None = None
    messages: list[ParsedMessage] = field(default_factory=list)
    skipped_lines: int = 0  # 읽지 못해 버린 줄(행·항목) 수 — 숨기지 않는다
    system_lines: int = 0  # 날짜 구분선·입퇴장·사진/이모티콘 자리표시 등 문체 자료가 아닌 줄
    problems: list[str] = field(default_factory=list)
    suggested_roles: dict[str, SuggestedRole] = field(default_factory=dict)
    masked_pii: int = 0
    chats: list[ChatChoice] | None = None  # 값이 있으면 대화를 먼저 골라야 한다 (chat_id)

    def problem(self, message: str) -> None:
        if len(self.problems) < MAX_PROBLEMS and message not in self.problems:
            self.problems.append(message)

    def speakers(self) -> list[tuple[str, int]]:
        """(이름, 메시지 수) — 많이 말한 순, 같으면 먼저 나온 순."""
        counts: dict[str, int] = {}
        for m in self.messages:
            counts[m.speaker] = counts.get(m.speaker, 0) + 1
        return sorted(counts.items(), key=lambda kv: -kv[1])


def tz_from_offset(minutes: int) -> timezone:
    return timezone(timedelta(minutes=minutes))


def has_unstorable_text(value: object) -> bool:
    """문자열(목록·사전이면 그 안쪽까지)에 NUL이나 짝 없는 대리 문자(surrogate)가 있는가.

    둘 다 PostgreSQL의 text·JSONB에 넣을 수 없다 — 검사 없이 저장하면 DB 오류(500)가 난다.
    """
    if isinstance(value, str):
        if "\x00" in value:
            return True
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return True
        return False
    if isinstance(value, dict):
        return any(has_unstorable_text(k) or has_unstorable_text(v) for k, v in value.items())
    if isinstance(value, list | tuple):
        return any(has_unstorable_text(v) for v in value)
    return False


def repair_unicode(text: str) -> str:
    """짝 없는 대리 문자를 `?`로 바꾼 글. 멀쩡한 글은 받은 객체 그대로 돌려준다.

    JSON의 `\\ud800` 같은 이스케이프로 들어온다. pydantic은 이런 문자열을 `str`로 받지 않으므로
    요청 모델의 검증보다 앞에서 고친다.
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", errors="replace").decode("utf-8")
    return text


def normalize_text(text: str) -> str:
    """줄 끝 공백·줄바꿈 표기 차이를 없앤 본문 (저장·해시 공통)."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


def content_id(sent_at: datetime, text: str, occurrence: int) -> str:
    """내용 기반 메시지 id (27자). 앞 10자는 UTC 분(yymmddHHMM)이라 눈으로 시각을 짐작할 수 있다."""
    minute = sent_at.astimezone(UTC).replace(second=0, microsecond=0)
    raw = f"{minute:%Y%m%d%H%M}\n{occurrence}\n{normalize_text(text)}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{minute:%y%m%d%H%M}-{digest}"


def line_list(numbers: list[int], unit: str = "줄", limit: int = 5) -> str:
    """문제 문구에 넣을 번호 예시: '3, 17, 40번째 줄 등'."""
    shown = ", ".join(str(n) for n in numbers[:limit])
    return f"{shown}번째 {unit}" + (" 등" if len(numbers) > limit else "")


def in_range(moment: datetime) -> bool:
    """메시지 시각으로 쓸 수 있는 범위인가 (시간대 변환이 넘치는 0001년·9999년 같은 값을 거른다)."""
    try:
        return MIN_YEAR <= moment.astimezone(UTC).year <= MAX_YEAR
    except (OverflowError, ValueError):
        return False


def clean(parsed: ParsedImport) -> ParsedImport:
    """본문 정리 → 빈 메시지·너무 긴 메시지·범위 밖 시각 제외. 여러 번 불러도 결과가 같다.

    **개인정보 가림보다 먼저** 부른다 — 가림 정규식이 수십만 자짜리 '메시지'를 훑지 않게.
    """
    kept: list[ParsedMessage] = []
    too_long = out_of_range = 0
    for m in parsed.messages:
        m.speaker = m.speaker.strip()
        if len(m.text) > MAX_MESSAGE_CHARS * 2:
            # 정리해도 상한을 넘는다 — 긴 글을 정리하는 비용도 쓰지 않는다
            m.text = m.text.strip()
        else:
            m.text = normalize_text(m.text)
        if not m.text or not m.speaker:
            parsed.skipped_lines += 1
            continue
        if len(m.text) > MAX_MESSAGE_CHARS:
            too_long += 1
            continue
        if not in_range(m.sent_at):
            out_of_range += 1
            continue
        kept.append(m)
    if too_long:
        parsed.skipped_lines += too_long
        parsed.problem(f"{MAX_MESSAGE_CHARS}자를 넘는 메시지 {too_long}건은 가져오지 않습니다.")
    if out_of_range:
        parsed.skipped_lines += out_of_range
        parsed.problem(
            f"날짜가 {MIN_YEAR}~{MAX_YEAR}년을 벗어난 메시지 {out_of_range}건은 가져오지 않습니다."
        )
    parsed.messages = kept
    return parsed


def finalize(parsed: ParsedImport) -> ParsedImport:
    """파서 공통 뒷정리. 순서: 정리(`clean`) → 같은 분 안 순서 → id.

    개인정보 가림은 `clean`과 이 함수 **사이에** 한다 (id는 저장되는 본문으로 만든다).
    """
    kept = clean(parsed).messages

    # 같은 분(초가 있으면 같은 초) 안의 순서를 밀리초로 남긴다.
    order: dict[datetime, int] = {}
    for m in kept:
        bucket = (
            m.sent_at.replace(microsecond=0) if m.has_seconds else m.sent_at.replace(second=0, microsecond=0)
        )
        k = order.get(bucket, 0)
        order[bucket] = k + 1
        m.sent_at = bucket + timedelta(milliseconds=min(k, 999 if m.has_seconds else 59_999))

    occurrences: dict[tuple[datetime, str], int] = {}
    seen: set[str] = set()
    out: list[ParsedMessage] = []
    duplicate_ids = 0
    for m in kept:
        if not m.id:
            minute = m.sent_at.astimezone(UTC).replace(second=0, microsecond=0)
            key = (minute, m.text)
            n = occurrences.get(key, 0)
            occurrences[key] = n + 1
            m.id = content_id(m.sent_at, m.text, n)
        elif len(m.id) > MAX_ID_CHARS:
            m.id = "x" + hashlib.sha256(m.id.encode("utf-8")).hexdigest()[:31]
        if m.id in seen:
            duplicate_ids += 1
            continue
        seen.add(m.id)
        out.append(m)
    if duplicate_ids:
        parsed.skipped_lines += duplicate_ids
        parsed.problem(f"id가 겹치는 메시지 {duplicate_ids}건은 처음 것만 가져옵니다.")
    parsed.messages = out
    return parsed
