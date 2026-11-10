"""파서가 틀리게 읽던 입력들 (2026-11-11 정확성 검토 D6).

본문 속의 날짜처럼 생긴 줄, 첫 줄이 빈 메시지, 범위 밖 연도, 텔레그램의 숨은 전달 메시지와 빈 내보내기,
붙여넣기 본문의 `|`, 날짜 줄 없이 PC 창에서 복사한 내용. 픽스처는 합성이다(실물 내보내기 검증 전).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.errors import InvalidInputError
from app.importers import parse_import

KST = timezone(timedelta(hours=9))


def _texts(parsed) -> list[str]:  # noqa: ANN001
    return [m.text for m in parsed.messages]


def _days(parsed) -> list[int]:  # noqa: ANN001
    return [m.sent_at.day for m in parsed.messages]


# ---- 카카오톡: 본문 속의 날짜처럼 생긴 줄 ----------------------------------------------------------


def test_pc_export_date_like_body_lines_do_not_shift_later_dates() -> None:
    text = (
        "--------------- 2026년 10월 5일 월요일 ---------------\n"
        "[박지현] [오전 9:25] 일정 정리합니다\n"
        "2026년 10월 7일 수요일\n"
        "회의실 A\n"
        "[김민수] [오전 9:31] 네, 금요일 건도 확인했습니다.\n"
        "2026년 10월 9일 금요일\n"
        "[박지현] [오전 9:40] 좋아요\n"
        "2026년 10월 12일 오후 3:00\n"
        "[김민수] [오전 9:41] 확인\n"
        "--------------- 2026년 10월 6일 화요일 ---------------\n"
        "[김민수] [오후 2:05] 다음 날입니다\n"
    )
    p = parse_import(text)
    assert _texts(p) == [
        "일정 정리합니다\n2026년 10월 7일 수요일\n회의실 A",
        "네, 금요일 건도 확인했습니다.\n2026년 10월 9일 금요일",
        "좋아요\n2026년 10월 12일 오후 3:00",
        "확인",
        "다음 날입니다",
    ]
    # 예전: 본문의 요일 줄이 날짜 구분선으로 읽혀 그 뒤 메시지가 7일·9일로 밀렸다
    assert _days(p) == [5, 5, 5, 5, 6]
    assert (p.variant, p.system_lines, p.skipped_lines, p.problems) == ("pc", 2, 0, [])


def test_day_lines_without_dashes_still_date_pc_snippets() -> None:
    """대시 없이 붙여 넣은 조각: 요일 줄 뒤에 메시지가 오면 날짜 줄, 본문이 이어지면 본문이다."""
    text = (
        "2026년 10월 5일 월요일\n"
        "[박지현] [오전 9:25] 일정 정리합니다\n"
        "2026년 10월 7일 수요일\n"
        "회의실 A\n"
        "\n"
        "2026년 10월 6일 화요일\n"
        "[김민수] [오후 2:05] 네\n"
    )
    p = parse_import(text)
    assert _texts(p) == ["일정 정리합니다\n2026년 10월 7일 수요일\n회의실 A", "네"]
    assert _days(p) == [5, 6]
    assert p.messages[1].sent_at == datetime(2026, 10, 6, 14, 5, tzinfo=KST)


def test_android_message_is_not_cut_at_a_date_time_line_in_its_body() -> None:
    text = (
        "2026년 10월 5일 오전 9:25, 박지현 : 다음 회의 일정입니다\n"
        "2026년 10월 7일 오후 3:00\n"
        "2026년 10월 8일 오전 10:00, 장소는 회의실 A\n"
        "꼭 참석해 주세요\n"
        "2026년 10월 5일 오전 9:31, 회원님 : 네 알겠습니다.\n"
        "2026년 10월 6일 오전 8:00\n"
        "2026년 10월 6일 오전 8:01, 최대리님이 들어왔습니다.\n"
        "2026년 10월 6일 오전 8:05, 박지현 : 좋은 아침입니다\n"
    )
    p = parse_import(text)
    # 예전: 첫 메시지가 "다음 회의 일정입니다"에서 끊기고 뒤 두 줄은 시스템 줄, "꼭 참석…"은 버려졌다
    assert _texts(p) == [
        "다음 회의 일정입니다\n2026년 10월 7일 오후 3:00\n"
        "2026년 10월 8일 오전 10:00, 장소는 회의실 A\n꼭 참석해 주세요",
        "네 알겠습니다.",
        "좋은 아침입니다",
    ]
    # 내보내기가 넣은 날짜만 있는 줄과 입장 안내는 그대로 시스템 줄이다 (뒤에 메시지가 온다)
    assert (p.system_lines, p.skipped_lines, p.problems) == (2, 0, [])
    assert _days(p) == [5, 5, 6]


def test_ios_body_with_several_date_like_lines_in_a_row() -> None:
    text = (
        "2026년 10월 5일 월요일\n"
        "2026. 10. 5. 오전 9:25, 박지현 : 다음 일정입니다\n"
        "2026년 10월 7일 수요일\n"
        "2026. 10. 7. 오후 3:00\n"
        "회의실 A에서 봅시다\n"
        "2026년 10월 6일 화요일\n"
        "2026. 10. 6. 14:05, 김민수 : 네\n"
        "2026. 10. 6. 14:06, 김민수님이 나갔습니다.\n"
    )
    p = parse_import(text)
    assert _texts(p) == [
        "다음 일정입니다\n2026년 10월 7일 수요일\n2026. 10. 7. 오후 3:00\n회의실 A에서 봅시다",
        "네",
    ]
    assert (p.variant, p.system_lines, p.skipped_lines) == ("ios", 3, 0)  # 구분선 2 + 퇴장 1


def test_message_whose_first_line_is_empty_is_kept() -> None:
    text = (
        "2026년 10월 5일 오전 9:25, 박지현 : 언제까지 가능해요?\n"
        "2026년 10월 5일 오전 9:31, 회원님 : \n"
        "금요일은 어렵고\n"
        "수요일까지는 가능합니다.\n"
        "2026년 10월 5일 오전 9:40, 박지현 : \n"
        "2026년 10월 5일 오전 9:41, 박지현 : 네\n"
    )
    p = parse_import(text)
    # 예전: 둘째 메시지가 시스템 줄로 읽혀, 본문 두 줄이 앞 사람(박지현)의 말에 붙었다
    assert [(m.speaker, m.text) for m in p.messages] == [
        ("박지현", "언제까지 가능해요?"),
        ("회원님", "금요일은 어렵고\n수요일까지는 가능합니다."),
        ("박지현", "네"),
    ]
    assert (p.system_lines, p.skipped_lines) == (1, 0)  # 본문이 아예 없는 9:40 메시지


def test_pc_window_copy_without_any_date_line_says_what_is_wrong() -> None:
    text = "[박지현] [오전 9:25] 언제까지 가능해요?\n[김민수] [오전 9:31] 수요일까지 가능합니다.\n"
    with pytest.raises(InvalidInputError) as err:
        parse_import(text)
    message = err.value.message
    assert "2건" in message and "날짜가 없어" in message and "내보내기" in message


def test_date_like_lines_are_scanned_in_linear_time() -> None:
    """날짜처럼 생긴 줄이 수만 개 이어져도 줄마다 끝까지 훑지 않는다 (한 번 본 결과를 다시 쓴다)."""
    import time

    text = (
        "2026년 10월 5일 오전 9:25, 박지현 : 일정\n"
        + "2026년 10월 7일 수요일\n" * 40_000
        + "끝\n2026년 10월 5일 오전 9:31, 회원님 : 네\n"
    )
    started = time.perf_counter()
    p = parse_import(text)
    assert time.perf_counter() - started < 3.0
    # 날짜 줄 4만 개는 뒤에 본문("끝")이 이어지므로 첫 메시지의 본문이고, 4,000자를 넘어 빠진다
    assert _texts(p) == ["네"] and "4000자를 넘는" in " ".join(p.problems)


# ---- 범위 밖 연도 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "0001년 1월 1일 오전 9:25, 박지현 : 연도가 1년",
        "9999년 12월 31일 오후 11:59, 박지현 : 연도가 9999년",
        "1899년 1월 1일 오전 9:25, 박지현 : 너무 옛날",
    ],
)
def test_out_of_range_years_are_reported_not_crashed(bad: str) -> None:
    text = bad + "\n2026년 10월 5일 오전 9:25, 박지현 : 정상 줄\n"
    for offset in (540, -720, 840):  # 시간대 변환이 넘치는 방향 모두
        p = parse_import(text, tz_offset_minutes=offset)
        assert _texts(p) == ["정상 줄"]
        assert p.skipped_lines == 1 and "1990~2100년" in p.problems[0]
    with pytest.raises(InvalidInputError, match="1990~2100년"):
        parse_import(bad)  # 그 줄뿐이면 무엇이 문제인지 알려 준다


def test_out_of_range_years_in_other_formats() -> None:
    plain = "0001-01-01 00:00 | 박지현 | 연도가 1년\n2026-10-05 09:25 | 박지현 | 정상 줄\n"
    assert _texts(parse_import(plain)) == ["정상 줄"]
    pc = (
        "--------------- 9999년 12월 31일 금요일 ---------------\n[박지현] [오후 11:59] 끝의 끝\n"
        "--------------- 2026년 10월 5일 월요일 ---------------\n[박지현] [오전 9:25] 정상 줄\n"
    )
    assert _texts(parse_import(pc, tz_offset_minutes=-720)) == ["정상 줄"]
    array = [
        {"speaker": "me", "sent_at": "0001-01-01T00:00:00+09:00", "text": "연도가 1년"},
        {"speaker": "me", "sent_at": "2026-10-05T09:25:00+09:00", "text": "정상 줄"},
    ]
    assert _texts(parse_import(json.dumps(array, ensure_ascii=False))) == ["정상 줄"]
    telegram = {
        "name": "박지현",
        "type": "personal_chat",
        "id": 1,
        "messages": [
            {"id": 1, "type": "message", "date": "0001-01-01T00:00:00", "from": "박지현", "text": "1년"},
            {"id": 2, "type": "message", "date": "2026-10-05T09:25:00", "from": "박지현", "text": "정상 줄"},
        ],
    }
    assert _texts(parse_import(json.dumps(telegram, ensure_ascii=False))) == ["정상 줄"]


def test_json_import_api_rejects_out_of_range_years(client: TestClient, user_a: dict[str, str]) -> None:
    """JSON 가져오기는 하나라도 틀리면 아무것도 저장하지 않는다 — 예전에는 500이었다."""
    cid = client.post(
        "/v1/contacts", json={"alias": "박지현", "relationship_type": "superior"}, headers=user_a
    ).json()["id"]
    conv = client.post("/v1/conversations", json={"contact_id": cid}, headers=user_a).json()["id"]
    for moment in ("0001-01-01T00:00:00+09:00", "9999-12-31T23:59:59-12:00"):
        body = {
            "messages": [
                {"id": "m1", "speaker": "me", "sent_at": moment, "text": "범위 밖"},
                {"id": "m2", "speaker": "me", "sent_at": "2026-10-05T09:25:00+09:00", "text": "정상"},
            ]
        }
        r = client.post(f"/v1/conversations/{conv}/imports", json=body, headers=user_a)
        assert r.status_code == 422, r.text
        assert "1990~2100년" in r.json()["message"]
    assert client.get(f"/v1/conversations/{conv}/stats", headers=user_a).json()["messages"] == 0


# ---- 텔레그램 -------------------------------------------------------------------------------------


def _tg(messages: list[dict], **extra: object) -> str:
    chat = {"name": "박지현", "type": "personal_chat", "id": 7, "messages": messages, **extra}
    return json.dumps(chat, ensure_ascii=False)


def test_telegram_forwarded_messages_are_excluded_even_when_the_origin_is_hidden() -> None:
    base = {"type": "message", "date": "2026-10-05T09:25:00", "from": "김민수"}
    text = _tg(
        [
            {**base, "id": 1, "text": "제가 쓴 말"},
            {**base, "id": 2, "text": "채널에서 전달한 글", "forwarded_from": "뉴스 채널"},
            # 보낸 사람이 계정을 숨겼다: 값이 null이다 (예전에는 내 말로 들어갔다)
            {**base, "id": 3, "text": "숨긴 계정에서 전달한 글", "forwarded_from": None},
        ]
    )
    p = parse_import(text)
    assert _texts(p) == ["제가 쓴 말"]
    assert p.system_lines == 2


def test_telegram_full_export_without_any_conversation_is_rejected() -> None:
    for chats in ([], [{"name": "빈 방", "type": "private_group", "id": 1, "messages": []}]):
        full = {"personal_information": {"first_name": "민수"}, "chats": {"list": chats}}
        with pytest.raises(InvalidInputError, match="가져올 대화가 없습니다"):
            parse_import(json.dumps(full, ensure_ascii=False))


def test_empty_telegram_export_is_422_at_the_api(client: TestClient, user_a: dict[str, str]) -> None:
    full = {"personal_information": {"first_name": "민수"}, "chats": {"list": []}}
    r = client.post("/v1/imports/parse", json={"text": json.dumps(full)}, headers=user_a)
    assert r.status_code == 422, r.text  # 예전: 200에 빈 chats 목록 — 화면이 '대화를 고르세요'에서 멈췄다
    assert r.json()["code"] == "invalid_input" and "가져올 대화가 없습니다" in r.json()["message"]


# ---- 붙여넣기 -------------------------------------------------------------------------------------


def test_plain_keeps_pipes_inside_the_text_and_continuation_lines_with_pipes() -> None:
    text = (
        "2026-10-05 09:25 | 박지현 | 선택지는 A|B|C 입니다\n"
        "2026-10-05 09:31 | 김민수 | 표로 정리했습니다\n"
        "| 항목 | 값 |\n"
        "| 일정 | 수요일 |\n"
        "a | b | c\n"
        "2026-13-05 09:40 | 박지현 | 달이 13월인 깨진 줄\n"
        "2026-10-05 09:52 | 김민수 | 끝\n"
    )
    p = parse_import(text)
    assert _texts(p) == [
        "선택지는 A|B|C 입니다",  # 예전: "A | B | C"로 바뀌었다
        # 예전: `|`가 둘 이상인 이어지는 줄은 깨진 줄로 버려졌다
        "표로 정리했습니다\n| 항목 | 값 |\n| 일정 | 수요일 |\na | b | c",
        "끝",
    ]
    # 날짜로 시작하는데 못 읽은 줄만 깨진 줄이다
    assert p.skipped_lines == 1 and "6번째 줄" in p.problems[0]
