"""대화 내보내기 파서 (`app.importers`).

형식 감지, 변형별 읽기, 같은 분 안 순서, 내용 기반 id, 개인정보 가림을 본다.

카카오톡 픽스처는 **합성**이다(실물 내보내기 샘플 검증 전). 알려진 변형을 2차 자료대로 만들었다.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.core.errors import InvalidInputError
from app.importers import detect_format, parse_import
from app.importers.base import content_id
from app.importers.pii import mask_pii

KST = timezone(timedelta(hours=9))

ANDROID = """박지현 님과 카카오톡 대화
저장한 날짜 : 2026년 10월 20일 오후 3:12

2026년 10월 5일 오전 9:25, 박지현 : 민수씨, 개편은 언제까지 가능해요?
2026년 10월 5일 오전 9:31, 회원님 : 솔직히 이번 주 금요일은 어렵고,
다음 주 수요일까지는 가능합니다.

테스트 일정 때문입니다.
2026년 10월 5일 오전 9:31, 회원님 : 죄송합니다.
2026년 10월 5일 오전 9:31, 박지현 : 사진
2026년 10월 5일 오후 12:05, 박지현 : 목요일까지는 안 될까요?
2026년 10월 6일 오전 8:00
2026년 10월 6일 오전 8:01, 박지현님이 들어왔습니다.
2026년 10월 6일 오전 12:10, 회원님 : 네 알겠습니다.
2026년 10월 6일 오후 1:00, 박지현 : 이모티콘
2026년 10월 6일 오후 1:01, 박지현 : 삭제된 메시지입니다.
2026년 10월 6일 오후 1:02, 박지현 : 파일: 보고서_최종.pdf
"""

IOS = """박지현 님과 카카오톡 대화
저장한 날짜 : 2026. 10. 20. 오후 3:12

2026년 10월 5일 월요일
2026. 10. 5. 오전 9:25, 박지현 : 민수씨, 개편은 언제까지 가능해요?
2026. 10. 5. 오전 9:31, 김민수 : 솔직히 이번 주 금요일은 어렵고,
다음 주 수요일까지는 가능합니다.
2026년 10월 6일 화요일
2026. 10. 6. 14:05, 박지현 : 목요일까지는 안 될까요?
2026. 10. 6. 14:06, 김민수님이 나갔습니다.
"""

PC = """박지현 님과 카카오톡 대화
저장한 날짜 : 2026-10-20 15:12:30

[박지현] [오전 9:00] 날짜 구분선보다 앞에 있는 줄
--------------- 2026년 10월 5일 월요일 ---------------
[박지현] [오전 9:25] 민수씨, 개편은 언제까지 가능해요?
[김민수] [오전 9:31] 솔직히 이번 주 금요일은 어렵고,
다음 주 수요일까지는 가능합니다.
[김민수] [오전 9:31] 죄송합니다.
박지현님이 나갔습니다.
--------------- 2026년 10월 6일 화요일 ---------------
[박지현] [오후 2:05] 목요일까지는 안 될까요?
[박지현] [오후 2:06] 동영상
"""

PC_ENGLISH = """KakaoTalk Chats with Jihyun Park
Date Saved : 2026-10-20 15:12:30

--------------- Monday, October 5, 2026 ---------------
[Jihyun Park] [9:25 AM] When can you finish the dashboard?
[Minsu Kim] [9:31 AM] Friday is hard,
but next Wednesday works.
[Jihyun Park] [12:05 PM] Photo
Jihyun Park left this chatroom.
--------------- Tuesday, October 6, 2026 ---------------
[Minsu Kim] [12:10 AM] Got it.
"""

CSV = (
    "Date,User,Message\n"
    '2026-10-05 09:25:10,"박지현","민수씨, 개편은 언제까지 가능해요?"\n'
    '2026-10-05 09:31:02,"김민수","솔직히 이번 주 금요일은 어렵고,\n다음 주 수요일까지는 가능합니다."\n'
    '2026-10-05 09:31:02,"김민수","그는 ""괜찮다""고 했어요"\n'
    '2026-10-05 09:40:00,"박지현","사진"\n'
    'not-a-date,"박지현","깨진 행"\n'
    '2026-10-05 09:41:00,"박지현"\n'
)

TELEGRAM = {
    "name": "박지현",
    "type": "personal_chat",
    "id": 777,
    "messages": [
        {"id": 1, "type": "service", "date": "2026-10-05T09:00:00", "action": "phone_call"},
        {
            "id": 2,
            "type": "message",
            "date": "2026-10-05T09:25:00",
            "date_unixtime": "1791159900",
            "from": "박지현",
            "from_id": "user123",
            "text": "민수씨, 개편은 언제까지 가능해요?",
        },
        {
            "id": 3,
            "type": "message",
            "date": "2026-10-05T09:31:00",
            "from": "김민수",
            "from_id": "user999",
            "text": ["솔직히 ", {"type": "bold", "text": "금요일"}, "은 어렵습니다."],
        },
        {
            "id": 4,
            "type": "message",
            "date": "2026-10-05T09:32:00",
            "from": "박지현",
            "text": "",
            "photo": "x.jpg",
        },
        {
            "id": 5,
            "type": "message",
            "date": "2026-10-05T09:33:00",
            "from": "김민수",
            "forwarded_from": "뉴스 채널",
            "text": "전달받은 글",
        },
        {"id": 6, "type": "message", "from": "김민수", "text": "시각이 없는 메시지"},
    ],
}

PLAIN = """2026-10-05 09:25 | 박지현 | 대시보드 개편은 언제까지 가능해요?
2026-10-05 09:31 | 김민수 | 이번 주 금요일은 어렵고 | 다음 주 수요일까지는 가능합니다.
이어지는 줄입니다.
2026-13-05 09:40 | 박지현 | 달이 13월인 깨진 줄
2026/10/05 09:52:30 | me | 수요일로 조정 부탁드립니다.
2026-10-05T10:05:00+00:00 | contact | 알겠어요.
"""


def _texts(parsed) -> list[str]:  # noqa: ANN001
    return [m.text for m in parsed.messages]


def test_kakao_android_multiline_system_lines_and_me_alias() -> None:
    p = parse_import(ANDROID)
    assert (p.format, p.variant, p.title_hint) == ("kakao", "android", "박지현")
    assert _texts(p) == [
        "민수씨, 개편은 언제까지 가능해요?",
        "솔직히 이번 주 금요일은 어렵고,\n다음 주 수요일까지는 가능합니다.\n\n테스트 일정 때문입니다.",
        "죄송합니다.",
        "목요일까지는 안 될까요?",
        "네 알겠습니다.",
    ]
    # 날짜만 있는 줄 1 + 입장 1 + 자리표시 4(사진·이모티콘·삭제된 메시지·파일) = 시스템 줄 6
    assert (p.system_lines, p.skipped_lines, p.problems) == (6, 0, [])
    assert p.suggested_roles == {"회원님": "me", "박지현": "contact"}
    assert [name for name, _ in p.speakers()] == ["회원님", "박지현"] or dict(p.speakers()) == {
        "회원님": 3,
        "박지현": 2,
    }
    # 오전/오후: 오후 12:05 → 12:05, 오전 12:10 → 00:10. 시간대는 기본 +09:00
    assert p.messages[0].sent_at == datetime(2026, 10, 5, 9, 25, tzinfo=KST)
    assert p.messages[3].sent_at == datetime(2026, 10, 5, 12, 5, tzinfo=KST)
    assert p.messages[4].sent_at == datetime(2026, 10, 6, 0, 10, tzinfo=KST)


def test_same_minute_order_is_kept_in_milliseconds() -> None:
    p = parse_import(ANDROID)
    same_minute = [
        m for m in p.messages if m.sent_at.replace(microsecond=0) == datetime(2026, 10, 5, 9, 31, tzinfo=KST)
    ]
    assert [m.text[:3] for m in same_minute] == ["솔직히", "죄송합"]
    assert [m.sent_at.microsecond for m in same_minute] == [0, 1000]  # 나온 순서대로 0ms, 1ms
    assert sorted(p.messages, key=lambda m: m.sent_at) == p.messages  # 시각 순 = 파일 순


def test_kakao_ios_day_separators_and_24h_clock() -> None:
    p = parse_import(IOS)
    assert (p.format, p.variant, p.title_hint) == ("kakao", "ios", "박지현")
    assert _texts(p) == [
        "민수씨, 개편은 언제까지 가능해요?",
        "솔직히 이번 주 금요일은 어렵고,\n다음 주 수요일까지는 가능합니다.",
        "목요일까지는 안 될까요?",
    ]
    assert p.messages[2].sent_at == datetime(2026, 10, 6, 14, 5, tzinfo=KST)  # 24시간제
    assert (p.system_lines, p.skipped_lines) == (3, 0)  # 날짜 구분선 2 + 퇴장 1
    assert p.suggested_roles == {"박지현": "contact", "김민수": "me"}  # 제목의 이름이 상대


def test_kakao_pc_inherits_date_from_separator() -> None:
    p = parse_import(PC)
    assert (p.format, p.variant) == ("kakao", "pc")
    assert _texts(p) == [
        "민수씨, 개편은 언제까지 가능해요?",
        "솔직히 이번 주 금요일은 어렵고,\n다음 주 수요일까지는 가능합니다.",
        "죄송합니다.",
        "목요일까지는 안 될까요?",
    ]
    assert [m.sent_at.date().isoformat() for m in p.messages] == ["2026-10-05"] * 3 + ["2026-10-06"]
    assert p.messages[3].sent_at == datetime(2026, 10, 6, 14, 5, tzinfo=KST)
    assert p.system_lines == 4  # 구분선 2 + 퇴장 1 + 동영상 1
    # 구분선보다 앞의 메시지는 날짜를 몰라 가져오지 않는다 — 숨기지 않고 센다
    assert p.skipped_lines == 1
    assert len(p.problems) == 1 and "날짜를 알 수 없는" in p.problems[0] and "4번째 줄" in p.problems[0]


def test_kakao_pc_english_locale() -> None:
    p = parse_import(PC_ENGLISH)
    assert (p.format, p.variant, p.title_hint) == ("kakao", "pc", "Jihyun Park")
    assert _texts(p) == [
        "When can you finish the dashboard?",
        "Friday is hard,\nbut next Wednesday works.",
        "Got it.",
    ]
    assert p.messages[0].sent_at == datetime(2026, 10, 5, 9, 25, tzinfo=KST)
    assert p.messages[2].sent_at == datetime(2026, 10, 6, 0, 10, tzinfo=KST)  # 12:10 AM
    assert p.system_lines == 4  # 구분선 2 + Photo + left
    assert p.suggested_roles == {"Jihyun Park": "contact", "Minsu Kim": "me"}


def test_kakao_csv_quotes_newlines_and_bad_rows() -> None:
    p = parse_import(CSV)
    assert (p.format, p.variant) == ("kakao", "csv")
    assert _texts(p) == [
        "민수씨, 개편은 언제까지 가능해요?",
        "솔직히 이번 주 금요일은 어렵고,\n다음 주 수요일까지는 가능합니다.",
        '그는 "괜찮다"고 했어요',
    ]
    assert p.messages[0].sent_at == datetime(2026, 10, 5, 9, 25, 10, tzinfo=KST)  # 초가 있으면 그대로
    # 같은 초의 두 메시지는 밀리초로 순서를 남긴다
    assert [m.sent_at.microsecond for m in p.messages[1:]] == [0, 1000]
    assert (p.system_lines, p.skipped_lines) == (1, 2)  # 사진 / 깨진 날짜·열 부족
    assert "읽지 못한 행이 2개" in p.problems[0]


def test_reupload_and_longer_overlapping_export_keep_the_same_ids() -> None:
    first = parse_import(ANDROID)
    again = parse_import(ANDROID)
    assert [m.id for m in first.messages] == [m.id for m in again.messages]  # 결정적
    assert len({m.id for m in first.messages}) == len(first.messages)
    same = "2026년 10월 7일 오전 9:00, 회원님 : 네 알겠습니다.\n"
    longer = parse_import(ANDROID + same + same)
    ids = [m.id for m in longer.messages]
    assert ids[: len(first.messages)] == [m.id for m in first.messages]  # 겹치는 범위는 같은 id
    assert len(set(ids)) == len(ids) == len(first.messages) + 2  # 같은 분·같은 본문도 서로 다른 id
    assert all(len(i) <= 64 for i in ids)


def test_ids_do_not_depend_on_speaker_name_or_variant() -> None:
    """같은 대화를 휴대폰(내 이름이 '회원님')과 PC(실명)에서 내보내도 같은 메시지는 같은 id."""
    android = (
        "2026년 10월 5일 오전 9:25, 박지현 : 언제까지 가능해요?\n"
        "2026년 10월 5일 오전 9:31, 회원님 : 수요일까지 가능합니다.\n"
    )
    pc = (
        "--------------- 2026년 10월 5일 월요일 ---------------\n"
        "[박지현] [오전 9:25] 언제까지 가능해요?\n[김민수] [오전 9:31] 수요일까지 가능합니다.\n"
    )
    csv_text = 'Date,User,Message\n2026-10-05 09:25:40,"박지현","언제까지 가능해요?"\n'
    a, b, c = parse_import(android), parse_import(pc), parse_import(csv_text)
    assert [m.id for m in a.messages] == [m.id for m in b.messages]
    assert c.messages[0].id == a.messages[0].id  # 초가 있어도 id는 분 단위
    assert a.messages[0].id == content_id(datetime(2026, 10, 5, 0, 25, tzinfo=UTC), "언제까지 가능해요?", 0)
    assert a.messages[0].id.startswith("2610050025-")


def test_tz_offset_changes_times_and_ids() -> None:
    kst = parse_import(PC)
    utc = parse_import(PC, tz_offset_minutes=0)
    assert utc.messages[0].sent_at == datetime(2026, 10, 5, 9, 25, tzinfo=UTC)
    assert utc.messages[0].sent_at != kst.messages[0].sent_at
    assert utc.messages[0].id != kst.messages[0].id  # 다른 시각이면 다른 메시지로 본다 (문서화된 한계)


def test_telegram_single_chat() -> None:
    p = parse_import(json.dumps(TELEGRAM, ensure_ascii=False))
    assert (p.format, p.variant, p.title_hint) == ("telegram", None, "박지현")
    assert _texts(p) == ["민수씨, 개편은 언제까지 가능해요?", "솔직히 금요일은 어렵습니다."]
    assert [m.id for m in p.messages] == ["tg2", "tg3"]
    # date_unixtime이 있으면 그것(UTC)을 쓴다 → 주어진 시간대로 표시
    assert p.messages[0].sent_at == datetime.fromtimestamp(1791159900, tz=UTC)
    assert p.messages[0].sent_at.utcoffset() == timedelta(hours=9)
    assert p.messages[1].sent_at == datetime(2026, 10, 5, 9, 31, tzinfo=KST)  # date + tz_offset
    assert p.system_lines == 3  # service + 사진만 있는 메시지 + 전달받은 메시지
    assert p.skipped_lines == 1 and "시각을 읽지 못한" in p.problems[0]
    assert p.suggested_roles == {"박지현": "contact", "김민수": "me"}


def test_telegram_full_export_lists_chats_then_parses_the_chosen_one() -> None:
    other = {
        "name": "최유진",
        "type": "personal_chat",
        "id": 888,
        "messages": [
            {"id": 9, "type": "message", "date": "2026-10-07T20:00:00", "from": "최유진", "text": "밥 먹자"}
        ],
    }
    full = {
        "personal_information": {"first_name": "민수", "last_name": "김"},
        "chats": {
            "list": [TELEGRAM, other, {"name": "빈 방", "type": "private_group", "id": 1, "messages": []}]
        },
    }
    text = json.dumps(full, ensure_ascii=False)
    listing = parse_import(text)
    assert listing.messages == [] and listing.chats is not None
    assert [(c.id, c.name, c.type, c.messages) for c in listing.chats] == [
        ("777", "박지현", "personal_chat", 5),
        ("888", "최유진", "personal_chat", 1),
        ("1", "빈 방", "private_group", 0),
    ]
    assert "chat_id" in listing.problems[0]

    chosen = parse_import(text, chat_id="888")
    assert chosen.chats is None and _texts(chosen) == ["밥 먹자"] and chosen.title_hint == "최유진"
    with pytest.raises(InvalidInputError, match="chat_id"):
        parse_import(text, chat_id="nope")

    # 메시지가 있는 대화가 하나뿐이면 고르지 않아도 된다
    single = {"chats": {"list": [other, {"name": "빈 방", "id": 1, "messages": []}]}}
    assert _texts(parse_import(json.dumps(single, ensure_ascii=False))) == ["밥 먹자"]


def test_plain_lines_continuation_and_broken_lines() -> None:
    p = parse_import(PLAIN)
    assert (p.format, p.variant) == ("plain", None)
    assert _texts(p) == [
        "대시보드 개편은 언제까지 가능해요?",
        "이번 주 금요일은 어렵고 | 다음 주 수요일까지는 가능합니다.\n이어지는 줄입니다.",
        "수요일로 조정 부탁드립니다.",
        "알겠어요.",
    ]
    assert p.skipped_lines == 1 and "4번째 줄" in p.problems[0]  # 13월
    assert p.messages[2].sent_at == datetime(2026, 10, 5, 9, 52, 30, tzinfo=KST)
    assert p.messages[3].sent_at == datetime(2026, 10, 5, 10, 5, tzinfo=UTC)  # 줄에 시간대가 있으면 그것
    assert p.suggested_roles == {"me": "me", "contact": "contact"}


def test_json_message_array_with_and_without_ids() -> None:
    items = [
        {
            "id": "m01",
            "speaker": "박지현",
            "sent_at": "2026-10-05T09:25:00+09:00",
            "text": "언제까지 가능해요?",
        },
        {"speaker": "김민수", "sent_at": "2026-10-05 09:31", "text": "수요일까지 가능합니다."},
        {"speaker": "김민수", "sent_at": "언젠가", "text": "시각이 깨진 항목"},
        {
            "id": "m01",
            "speaker": "박지현",
            "sent_at": "2026-10-05T09:40:00+09:00",
            "text": "id가 겹치는 항목",
        },
        "문자열 항목",
    ]
    p = parse_import(json.dumps(items, ensure_ascii=False))
    assert p.format == "json"
    assert [m.id for m in p.messages][0] == "m01" and p.messages[1].id.startswith("2610050031-")
    assert _texts(p) == ["언제까지 가능해요?", "수요일까지 가능합니다."]
    assert p.messages[1].sent_at == datetime(2026, 10, 5, 9, 31, tzinfo=KST)  # 시간대가 없으면 tz_offset
    assert p.skipped_lines == 3 and len(p.problems) == 2
    wrapped = parse_import(json.dumps({"messages": items[:2]}, ensure_ascii=False), "json")
    assert len(wrapped.messages) == 2


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (ANDROID, "kakao"),
        (IOS, "kakao"),
        (PC, "kakao"),
        (CSV, "kakao"),
        ("[박지현] [오전 9:25] 붙여 넣은 PC 조각", "kakao"),  # '['로 시작하지만 JSON이 아니다
        (PLAIN, "plain"),
        (json.dumps(TELEGRAM), "telegram"),
        (json.dumps({"chats": {"list": []}}), "telegram"),
        (json.dumps([{"speaker": "me", "sent_at": "2026-10-05T09:00:00+09:00", "text": "안녕"}]), "json"),
    ],
)
def test_detect_format(text: str, expected: str) -> None:
    assert detect_format(text) == expected


@pytest.mark.parametrize(
    ("text", "fmt", "needle"),
    [
        ("", "auto", "비어"),
        ("   \n\n", "auto", "비어"),
        ("그냥 아무 글이나 붙여 넣었습니다.\n형식이 없습니다.", "auto", "형식을 알 수 없습니다"),
        ('{"hello": "world"}', "auto", "JSON"),
        ("[]", "auto", "한 건도"),
        ("박지현 님과 카카오톡 대화\n저장한 날짜 : 2026년 10월 20일 오후 3:12\n", "auto", "한 건도"),
        ("2026년 13월 45일 오전 9:25, 박지현 : 날짜가 깨진 줄", "kakao", "한 건도"),
        ("이건 JSON이 아님", "telegram", "JSON"),
        ('{"name": "x"}', "telegram", "messages"),
        ("이건 JSON이 아님", "json", "JSON"),
        ("한 줄짜리 글", "plain", "한 건도"),
        ("[박지현] [오전 9:25] 날짜 구분선이 없는 PC 조각", "kakao", "날짜를 알 수 없는"),
    ],
)
def test_broken_input_is_rejected_with_a_reason(text: str, fmt: str, needle: str) -> None:
    with pytest.raises(InvalidInputError) as err:
        parse_import(text, fmt)  # type: ignore[arg-type]
    assert needle in err.value.message


def test_size_limit_and_overlong_messages() -> None:
    with pytest.raises(InvalidInputError, match="너무 깁니다"):
        parse_import(PLAIN, max_chars=50)
    assert parse_import(PLAIN, max_chars=len(PLAIN)).messages  # 딱 맞으면 통과
    long_line = "2026-10-05 09:25 | 박지현 | " + "가" * 4001 + "\n2026-10-05 09:26 | 박지현 | 짧은 글\n"
    p = parse_import(long_line)
    assert _texts(p) == ["짧은 글"] and p.skipped_lines == 1 and "4000자를 넘는" in p.problems[0]


def test_input_cleanup_bom_nul_and_nbsp() -> None:
    text = "\ufeff[Jihyun] [9:25\u202fAM] hello\x00 there\n"
    with pytest.raises(InvalidInputError):  # 날짜 구분선이 없어 가져올 것은 없지만, 깨지지 않고 이유를 말한다
        parse_import(text)
    ok = parse_import("--------------- Monday, October 5, 2026 ---------------\n" + text)
    assert ok.messages[0].text == "hello there" and ok.messages[0].sent_at.hour == 9


def test_decomposed_hangul_is_normalized() -> None:
    """맥에서 온 풀어쓴 자모(NFD)도 완성형(NFC)과 같은 글자·같은 id로 읽는다."""
    import unicodedata

    line = "2026-10-05 09:25 | 박지현 | 언제까지 가능해요?\n"
    decomposed = unicodedata.normalize("NFD", line)
    assert decomposed != line and len(decomposed) > len(line)
    a, b = parse_import(line), parse_import(decomposed)
    assert (b.messages[0].speaker, b.messages[0].text) == ("박지현", "언제까지 가능해요?")
    assert a.messages[0].id == b.messages[0].id


@pytest.mark.parametrize(
    ("text", "expected", "count"),
    [
        ("제 번호는 010-1234-5678 입니다", "제 번호는 [전화번호] 입니다", 1),
        ("01012345678로 연락 주세요", "[전화번호]로 연락 주세요", 1),
        ("+82 10-1234-5678 / 010.1234.5678", "[전화번호] / [전화번호]", 2),
        ("사무실 02-123-4567, 031-123-4567", "사무실 [전화번호], [전화번호]", 2),
        ("메일은minsu.kim+work@example.co.kr입니다", "메일은[이메일]입니다", 1),
        ("주민번호 900101-1234567 이에요", "주민번호 [주민등록번호] 이에요", 1),
        ("주민등록번호: 9001011234567", "주민등록번호: [주민등록번호]", 1),
        ("카드 1234-5678-9012-3456 로 결제", "카드 [카드번호] 로 결제", 1),
        ("카드번호 4111111111111111", "카드번호 [카드번호]", 1),  # 검증 숫자가 맞는 16자리
        ("신한 110-123-456789 김민수", "신한 [계좌번호] 김민수", 1),
        ("계좌번호 1002123456789 로 보내줘", "계좌번호 [계좌번호] 로 보내줘", 1),
        ("010-1234-5678, a@b.com, 110-123-456789", "[전화번호], [이메일], [계좌번호]", 3),
    ],
)
def test_mask_pii_true_positives(text: str, expected: str, count: int) -> None:
    assert mask_pii(text) == (expected, count)


@pytest.mark.parametrize(
    "text",
    [
        "2026-10-05 09:25에 만나요",
        "회의는 2026.10.05(월) 14:00~15:30",
        "총 1,234,000원 입금했습니다",
        "견적은 12345678원이고 수량은 3000000000개",
        "버전 1.2.3, 서버 192.168.0.12",
        "주문번호 2026100512345678 확인 부탁드립니다",  # 16자리지만 카드 번호대가 아니다
        "바코드 8801043015394",  # 13자리지만 '주민번호' 낱말이 없다
        "ISBN 978-89-123-4567-8",
        "점수 3-2, 좌석 12-3, 기간 10-05~10-12",
        "문서번호 2026-10-05-01",
        "대표번호 1588-1234",
        "오늘 10시 30분, 3층 302호에서 봬요",
        "",
    ],
)
def test_mask_pii_leaves_dates_amounts_and_plain_numbers(text: str) -> None:
    assert mask_pii(text) == (text, 0)


def test_parse_with_mask_counts_and_ids_use_the_stored_text() -> None:
    text = "2026-10-05 09:25 | 박지현 | 제 번호는 010-1234-5678, 메일은 jh@example.com 이에요\n"
    plain = parse_import(text)
    masked = parse_import(text, mask=True)
    assert plain.masked_pii == 0 and "010-1234-5678" in plain.messages[0].text
    assert masked.masked_pii == 2
    assert masked.messages[0].text == "제 번호는 [전화번호], 메일은 [이메일] 이에요"
    # id는 저장되는 본문(가린 뒤)으로 만든다 — 가려진 원문을 해시로 되짚을 수 없게
    assert masked.messages[0].id == content_id(masked.messages[0].sent_at, masked.messages[0].text, 0)
    assert masked.messages[0].id != plain.messages[0].id
