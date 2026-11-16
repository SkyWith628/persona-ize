"""말투 관찰과 프로필 (`app.ai.register`) — 설계 3.1, 3.2.

문장 라벨과 프로필 기대값은 사람이 읽고 정한 것이다 (분류기 출력을 그대로 옮겨 적은 것이 아니다).
프로필 테스트는 합성 데이터 `data/synthetic/sample_conversations.json`의 실제 메시지를 쓴다.
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.ai import register
from app.ai.register import (
    LEVEL_KO,
    address_terms,
    build_profile,
    classify_sentence,
    formality_for,
    level_of_formality,
    message_label,
    observe,
    polite,
    split_sentences,
)

KST = timezone(timedelta(hours=9))
SAMPLE = Path(__file__).resolve().parents[2] / "data" / "synthetic" / "sample_conversations.json"

# ── 문장 분류 ───────────────────────────────────────────────────────────────────────────────────
SENTENCES: list[tuple[str, str]] = [
    # 합쇼체
    ("이번 주 주간보고 초안 공유드립니다.", "hapsyo"),
    ("다음 주 수요일까지는 가능합니다.", "hapsyo"),
    ("대시보드 개편 오늘 배포 완료했습니다!", "hapsyo"),
    ("네, 금요일 오전까지 정리하겠습니다.", "hapsyo"),
    ("혹시 내일 시간 괜찮으십니까?", "hapsyo"),
    ("회의실로 와 주십시오.", "hapsyo"),
    ("수요일로 갑시다.", "hapsyo"),
    ("넵 알겠습니당 ㅎㅎ", "hapsyo"),
    ("감사합니다 팀장님!", "hapsyo"),
    ("죄송합니다만", "hapsyo"),
    ("확인 부탁드립니다~", "hapsyo"),
    ("자료는 여기 있습니다 https://example.com/doc", "hapsyo"),
    ("먼저 들어가십쇼", "hapsyo"),
    # 해요체
    ("팀장님, 안녕하세요.", "haeyo"),
    ("근데 대시보드 개편은 언제까지 가능해요?", "haeyo"),
    ("금요일에 고객사 미팅이 있어서요.", "haeyo"),
    ("목요일까지는 안 될까요?", "haeyo"),
    ("저도 급했네요.", "haeyo"),
    ("그럼 3시에 보시죠?", "haeyo"),
    ("제가 할게요", "haeyo"),
    ("날씨가 좋군요", "haeyo"),
    ("넵 확인했어용ㅎㅎ", "haeyo"),
    ("알겠어요~~", "haeyo"),
    ("내일요?", "haeyo"),
    ("맛있게 드세요 😊", "haeyo"),
    ("금방 갈게요!!", "haeyo"),
    ("그거 제 거예요", "haeyo"),
    ("수고했어요 민수씨.", "haeyo"),
    ("아 그렇죠 ㅋㅋㅋ", "haeyo"),
    ("괜찮아요오", "haeyo"),
    # 반말 (해체)
    ("알지ㅋㅋ", "hae"),
    ("토요일 저녁 맞지?", "hae"),
    ("케이크는 내가 살게", "hae"),
    ("팀장님이랑 일정 때문에 좀 부딪혔어.", "hae"),
    ("다행이다 ㅎㅎ", "hae"),
    ("담엔 너가 사라", "hae"),
    ("헐 축하해!!", "hae"),
    ("근데 이제 회사에선 존댓말 써야겠네 ㅋㅋ", "hae"),
    ("응 다음엔 무조건 간다!", "hae"),
    ("대신 다음 달에 밥 한번 먹자", "hae"),
    ("민수야 점심 뭐 먹을래?", "hae"),
    ("근데 너 팀장님한테 혼났다며", "hae"),
    ("금요일까지만 알려줘!", "hae"),
    ("나 그거 어제 했거든", "hae"),
    ("너 어디야?", "hae"),
    ("밥 먹었냐", "hae"),
    ("숙제 다 했니?", "hae"),
    ("같이 갈까?", "hae"),
    ("진짜 고마워 ㅠㅠ", "hae"),
    ("그건 좀 아닌 것 같아", "hae"),
    ("아직 모르겠어 ㅠ", "hae"),
    ("나 먼저 간다", "hae"),
    ("어제 영화 봤다", "hae"),
    ("이게 더 좋다", "hae"),
    ("잠깐만 기다려", "hae"),
    ("그러지 마", "hae"),
    ("다음엔 꼭 와", "hae"),
    # 반말 — '요'가 빠진 연결 어미로 끝난 말
    ("워크숍 발표 준비해야 돼서", "hae"),
    ("내일 하려고 ㅋㅋ", "hae"),
    ("가고 싶은데", "hae"),
    ("사과는 했고?", "hae"),
    # 알 수 없음 — 서술어 어미가 없는 줄
    ("넵", "unknown"),
    ("네", "unknown"),
    ("예!", "unknown"),
    ("아니요", "unknown"),
    ("ㅇㅋ", "unknown"),
    ("내일 10시 회의실 B", "unknown"),
    ("확인 완료", "unknown"),
    ("별일 아님", "unknown"),
    ("걱정 ㄴㄴ", "unknown"),
    ("서연아 오늘 국밥 ㄱ?", "unknown"),
    ("😊👍", "unknown"),
    ("https://example.com/a?b=1", "unknown"),
    ("ㅋㅋㅋㅋ", "unknown"),
    ("금요일까지", "unknown"),
    ("회의 자료 필요", "unknown"),
    ("오늘 저녁 피자", "unknown"),
    ("주말에 바다", "unknown"),
    ("이번 주 공지", "unknown"),
    ("영어 단어", "unknown"),
    ("회의 내용", "unknown"),
    ("유진아", "unknown"),
    ("", "unknown"),
]


@pytest.mark.parametrize(("text", "expected"), SENTENCES)
def test_classify_sentence(text: str, expected: str) -> None:
    assert classify_sentence(text) == expected


def test_sentence_table_is_large_and_covers_every_level() -> None:
    assert len(SENTENCES) >= 60
    assert {label for _, label in SENTENCES} == {"hapsyo", "haeyo", "hae", "unknown"}


def test_split_sentences_uses_punctuation_laughter_and_newlines() -> None:
    assert split_sentences("알지ㅋㅋ 토요일 저녁 맞지? 케이크는 내가 살게") == [
        "알지ㅋㅋ",
        "토요일 저녁 맞지?",
        "케이크는 내가 살게",
    ]
    assert split_sentences("가고 싶은데 아직 모르겠어 ㅠ 회사 일정 봐서 알려줄게") == [
        "가고 싶은데 아직 모르겠어 ㅠ",
        "회사 일정 봐서 알려줄게",
    ]
    assert split_sentences("확인했습니다.\n\n내일 뵙겠습니다") == ["확인했습니다.", "내일 뵙겠습니다"]
    # 쉼표와 소수점은 경계가 아니다
    assert split_sentences("네, 3.5점 정도입니다.") == ["네, 3.5점 정도입니다."]
    assert split_sentences("   ") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # 합쇼체와 해요체가 섞인 것은 혼용이 아니다 — 둘 다 존댓말이다. 문장이 많은 단계, 같으면 합쇼체
        ("팀장님, 안녕하세요. 이번 주 주간보고 초안 공유드립니다.", "hapsyo"),  # 해요체 1 + 합쇼체 1
        ("확인했습니다. 내일 봬요!", "hapsyo"),
        ("안녕하세요. 자료 공유드립니다. 확인 부탁드립니다. 감사합니다.", "hapsyo"),  # 3/4
        ("안녕하세요. 자료 보내요. 확인 부탁드립니다.", "haeyo"),  # 해요체 2 + 합쇼체 1
        # 혼용은 반말과 존댓말이 한 메시지에 섞였을 때만이다
        ("확인했습니다. 근데 나 먼저 간다", "mixed"),  # 존댓말 1 + 반말 1
        ("안녕하세요. 자료 보내요. 나 먼저 간다", "mixed"),  # 존댓말 2/3 = 67%
        ("알겠습니다. 감사해요. 확인했어요. 나 간다", "haeyo"),  # 존댓말 3/4 = 75% → 그 안에서 많은 단계
        # 메시지 중간의 '요' 빠진 연결 어미는 반말로 세지 않는다 — 말줄임·ㅠㅠ에서 조각이 나뉘었을 뿐이다
        ("제가 어제 좀 바빠서... 죄송합니다", "hapsyo"),
        ("죄송한데... 내일 가능할까요?", "haeyo"),
        ("늦어서ㅠㅠ 죄송합니다", "hapsyo"),
        ("어제 보내주신 파일 봤는데 ㅠㅠ 열리지 않습니다", "hapsyo"),
        ("오늘 회의 3시에 한다고 들었는데.. 맞나요?", "haeyo"),
        ("저도 가고 싶은데 ㅜㅜ 일이 있어서요", "haeyo"),
        # 뒤에 말을 끝내는 문장이 없으면 연결 어미가 곧 끝이다 (반말)
        ("내일 하려고 ㅋㅋ 걱정 ㄴㄴ", "hae"),
        ("가려고.. 근데 비가 와서..", "hae"),
        ("늦어서ㅠㅠ 미안해", "hae"),
        ("팀장님이랑 일정 때문에 좀 부딪혔어. 별일 아님", "hae"),  # 알 수 없는 줄은 집계에서 뺀다
        ("넵", "unknown"),
        ("내일 10시 회의실 B", "unknown"),
        ("", "unknown"),
    ],
)
def test_message_label(text: str, expected: str) -> None:
    assert message_label(text) == expected


def test_polite_and_formality_mapping() -> None:
    assert [polite(x) for x in ("hapsyo", "haeyo", "hae", "mixed", "unknown")] == [
        True,
        True,
        False,
        None,
        None,
    ]
    assert [formality_for(x, False) for x in ("hae", "haeyo", "hapsyo")] == [1, 3, 4]
    assert [formality_for(x, True) for x in ("hae", "haeyo", "hapsyo")] == [1, 3, 4]
    assert formality_for("mixed", False) is None
    assert formality_for("unknown", True) is None
    assert [level_of_formality(n) for n in (1, 2, 3, 4, 5)] == ["hae", "hae", "haeyo", "hapsyo", "hapsyo"]
    assert set(LEVEL_KO) == {"hapsyo", "haeyo", "hae", "mixed", "unknown"}


# ── 호칭 ────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "alias", "expected"),
    [
        # 호격
        ("팀장님, 안녕하세요. 이번 주 주간보고 초안 공유드립니다.", "박지현", ["팀장님"]),
        ("지현 팀장님, 어제 일정 건은 제가 말이 좀 딱딱했던 것 같아 죄송합니다.", "박지현", ["지현 팀장님"]),
        ("네 팀장님, 맡겠습니다. 자료는 11월 5일까지 드리겠습니다.", "박지현", ["팀장님"]),
        ("서연 파트장님, 오늘 스탠드업 회의록 공유드립니다.", "이서연", ["서연 파트장님"]),
        ("서연아 오늘 국밥 ㄱ?", "이서연", ["서연아"]),
        ("유진아 미안 이번엔 못 갈 것 같아. 워크숍 발표 준비해야 돼서", "최유진", ["유진아"]),
        ("미안 준호야 오늘은 안 되겠다", "김준호", ["준호야"]),
        ("형 이번 주말에 엄마 생신인 거 알지?", "김민수", ["형"]),
        ("김팀장님, 보고드립니다.", "김영수", ["김팀장님"]),
        ("선배님 혹시 시간 되세요?", "정하늘", ["선배님"]),
        ("근데 서연, 그거 봤어?", "이서연", ["서연"]),
        ("지현씨 안녕하세요", "박지현", ["지현씨"]),
        # 지칭 (제3자 언급, 조사가 붙은 말) — 호칭이 아니다
        ("팀장님이랑 일정 때문에 좀 부딪혔어. 별일 아님", "김준호", []),
        ("근데 너 팀장님한테 혼났다며", "김민수", []),
        ("팀장님께 보고드렸습니다.", "박지현", []),
        ("서연이가 그러던데", "이서연", []),
        ("오늘 서연 파트장님이 발표한대", "이서연", []),
        ("어제 팀장님 만났어", "김준호", []),
        ("좋아 내가 날짜 잡을게", "최유진", []),  # '좋아'는 이름+아가 아니다
        ("대리 운전 불렀어", "김준호", []),  # 님 없는 직함은 쉼표가 있어야 호칭
        ("네, 금요일 오전까지 정리하겠습니다.", "이서연", []),
        ("", "이서연", []),
    ],
)
def test_address_terms(text: str, alias: str, expected: list[str]) -> None:
    assert address_terms(text, alias) == expected


def test_observe_reads_label_counts_and_address() -> None:
    sent = datetime(2026, 10, 5, 9, 52, tzinfo=KST)
    obs = observe(
        "c1:m05",
        sent,
        "팀장님, 목요일이면 테스트를 못 돌립니다. 죄송하지만 수요일로 조정 부탁드립니다.",
        "박지현",
    )
    assert (obs.message_id, obs.sent_at, obs.label) == ("c1:m05", sent, "hapsyo")
    assert obs.counts == {"hapsyo": 2, "haeyo": 0, "hae": 0, "unknown": 0}
    assert obs.address_terms == ("팀장님",)


def test_observe_name_vocative_marks_banmal_only_when_ending_is_unknown() -> None:
    sent = datetime(2026, 10, 7, 12, 11, tzinfo=KST)
    # 어미는 없지만('ㄱ?') 이름+아 호격이 있다 → 반말. 문장 건수는 그대로 '알 수 없음'이다.
    obs = observe("c3:m02", sent, "서연아 오늘 국밥 ㄱ?", "이서연")
    assert obs.label == "hae"
    assert obs.counts == {"hapsyo": 0, "haeyo": 0, "hae": 0, "unknown": 1}
    # 어미가 읽히면 호격이 라벨을 바꾸지 않는다
    assert observe("x:1", sent, "서연아 내일 봬요", "이서연").label == "haeyo"
    # 직함 호칭은 말투를 알려 주지 않는다
    assert observe("x:2", sent, "팀장님, 내일 10시 회의실 B", "박지현").label == "unknown"


# ── 프로필: 합성 데이터 ─────────────────────────────────────────────────────────────────────────
def _own(conversation_id: str) -> tuple[str, list[tuple[str, datetime, str]]]:
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    conv = next(c for c in data["conversations"] if c["conversation_id"] == conversation_id)
    rows = [
        (f"{conversation_id}:{m['id']}", datetime.fromisoformat(m["sent_at"]), m["text"])
        for m in conv["messages"]
        if m["speaker"] == "me"
    ]
    return conv["contact"], rows


def _at(day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=KST)


def test_c3_before_the_change_is_banmal_with_a_candidate_address() -> None:
    alias, rows = _own("c3")
    p = build_profile(rows, alias, as_of=_at(20, 0, 0))  # 발령 소식(10/17) 뒤, 첫 존댓말(10/21) 앞

    assert p.observations == 3
    assert p.unknown == 0
    assert len(p.speech) == 1
    cur = p.current_speech
    assert cur is not None
    assert (cur.value, cur.count, cur.status) == ("hae", 3, "confirmed")
    assert cur.message_ids == ("c3:m02", "c3:m04", "c3:m06")
    assert (cur.start, cur.end) == (_at(7, 12, 11), None)
    assert p.previous_speech is None
    assert p.two_valued is None

    assert [(s.value, s.count, s.status) for s in p.address] == [("서연아", 1, "candidate")]
    assert p.current_address is not None
    assert p.current_address.message_ids == ("c3:m02",)
    assert p.previous_address is None
    assert p.senior_hint is False


def test_c3_after_the_change_is_hapsyo_with_previous_banmal() -> None:
    alias, rows = _own("c3")
    p = build_profile(rows, alias)

    assert p.observations == 5
    assert p.unknown == 0
    assert [(s.value, s.count, s.status) for s in p.speech] == [
        ("hae", 3, "previous"),
        ("hapsyo", 2, "confirmed"),
    ]
    prev, cur = p.previous_speech, p.current_speech
    assert prev is not None
    assert cur is not None
    assert prev.message_ids == ("c3:m02", "c3:m04", "c3:m06")
    assert (prev.start, prev.end) == (_at(7, 12, 11), _at(21, 10, 5))
    assert cur.message_ids == ("c3:m08", "c3:m10")
    assert (cur.start, cur.end) == (_at(21, 10, 5), None)
    # 변화 뒤의 두 건이 모두 합쇼체라 두 값 상태가 아니다 (설계 3.2: 범위 단서는 그래프 조회가 보여 준다)
    assert p.two_valued is None

    assert [(s.value, s.count, s.status) for s in p.address] == [
        ("서연아", 1, "previous"),
        ("서연 파트장님", 1, "candidate"),
    ]
    assert p.current_address is not None
    assert p.current_address.message_ids == ("c3:m08",)
    assert p.current_address.start == _at(21, 10, 5)
    assert p.previous_address is not None
    assert p.previous_address.end == _at(21, 10, 5)
    assert p.senior_hint is True  # '파트장'


def test_c3_one_polite_message_does_not_open_a_segment_but_shows_two_values() -> None:
    alias, rows = _own("c3")
    p = build_profile(rows, alias, as_of=_at(21, 10, 10))  # m08까지만 보인다

    assert p.observations == 4
    assert [(s.value, s.count, s.status) for s in p.speech] == [("hae", 3, "confirmed")]
    assert p.two_valued == {"hapsyo": 1, "hae": 3}
    assert p.current_address is not None
    assert (p.current_address.value, p.current_address.status) == ("서연 파트장님", "candidate")


def test_c3_as_of_is_exclusive() -> None:
    alias, rows = _own("c3")
    assert build_profile(rows, alias, as_of=_at(21, 10, 5)).observations == 3  # m08의 시각 그 자체는 빠진다
    assert build_profile(rows, alias, as_of=_at(7, 12, 0)).observations == 0


def test_c3_k_sensitivity() -> None:
    alias, rows = _own("c3")
    # K=1: 존댓말 한 건(m08)만으로 구간이 열린다
    k1 = build_profile(rows, alias, as_of=_at(21, 10, 10), k=1)
    assert [(s.value, s.count) for s in k1.speech] == [("hae", 3), ("hapsyo", 1)]
    assert k1.two_valued is None
    # K=2 (기본): 전체 데이터에서 열린다
    k2 = build_profile(rows, alias, k=2)
    assert [(s.value, s.count) for s in k2.speech] == [("hae", 3), ("hapsyo", 2)]
    # K=3: 합쇼체 두 건으로는 열리지 않는다 — 대신 두 값 상태로 드러난다
    k3 = build_profile(rows, alias, k=3)
    assert [(s.value, s.count, s.status) for s in k3.speech] == [("hae", 3, "confirmed")]
    assert k3.two_valued == {"hapsyo": 2, "hae": 3}


def test_c1_stable_hapsyo_and_confirmed_title() -> None:
    alias, rows = _own("c1")
    p = build_profile(rows, alias)

    assert p.observations == 6
    assert p.unknown == 0
    # m01은 "안녕하세요"(해요체) + "공유드립니다"(합쇼체)다 — 둘 다 존댓말이라 혼용이 아니고 합쇼체로 센다
    assert observe(*rows[0], alias).label == "hapsyo"
    assert len(p.speech) == 1
    cur = p.current_speech
    assert cur is not None
    assert (cur.value, cur.count, cur.status) == ("hapsyo", 6, "confirmed")
    assert cur.message_ids == ("c1:m01", "c1:m03", "c1:m05", "c1:m07", "c1:m09", "c1:m11")
    assert cur.start == datetime(2026, 10, 5, 9, 10, tzinfo=KST)
    assert p.previous_speech is None
    assert p.two_valued is None

    # '팀장님' 4회(m01, m05, m09, m11). 한 번 나온 '지현 팀장님'(m07)은 구간을 열지도 끊지도 않는다
    assert [(s.value, s.count, s.status) for s in p.address] == [("팀장님", 4, "confirmed")]
    assert p.current_address is not None
    assert p.current_address.message_ids == ("c1:m01", "c1:m05", "c1:m09", "c1:m11")
    assert p.previous_address is None
    assert p.candidate_address is None
    assert p.senior_hint is True


def test_c1_one_off_address_at_the_end_is_a_separate_candidate() -> None:
    alias, rows = _own("c1")
    p = build_profile(rows, alias, as_of=datetime(2026, 10, 7, 0, 0, tzinfo=KST))  # m07까지

    # 한 번 나온 다른 호칭은 확정된 호칭을 '이전'으로 밀어내지 않는다 — 따로 후보로 둔다
    assert [(s.value, s.count, s.status) for s in p.address] == [("팀장님", 2, "confirmed")]
    assert p.current_address is not None and p.current_address.value == "팀장님"
    assert p.previous_address is None
    cand = p.candidate_address
    assert cand is not None
    assert (cand.value, cand.count, cand.status, cand.message_ids) == (
        "지현 팀장님",
        1,
        "candidate",
        ("c1:m07",),
    )
    assert [(s.value, s.count) for s in p.speech] == [("hapsyo", 4)]


def test_c2_stable_banmal_without_address() -> None:
    alias, rows = _own("c2")
    p = build_profile(rows, alias)

    assert p.observations == 4
    assert p.unknown == 0
    assert [(s.value, s.count, s.status) for s in p.speech] == [("hae", 4, "confirmed")]
    assert p.current_speech is not None
    assert p.current_speech.message_ids == ("c2:m02", "c2:m04", "c2:m06", "c2:m08")
    assert p.two_valued is None
    # m04의 '팀장님이랑'은 제3자 언급이다
    assert p.address == ()
    assert p.current_address is None
    assert p.senior_hint is False


def test_c4_stable_banmal_with_candidate_name_vocative() -> None:
    alias, rows = _own("c4")
    p = build_profile(rows, alias)

    assert p.observations == 3
    assert [(s.value, s.count, s.status) for s in p.speech] == [("hae", 3, "confirmed")]
    assert p.current_speech is not None
    assert p.current_speech.message_ids == ("c4:m02", "c4:m04", "c4:m06")
    assert p.two_valued is None
    assert [(s.value, s.count, s.status, s.message_ids) for s in p.address] == [
        ("유진아", 1, "candidate", ("c4:m04",))
    ]
    assert p.senior_hint is False


@pytest.mark.parametrize("conversation_id", ["c1", "c2", "c3", "c4"])
def test_profile_is_independent_of_input_order(conversation_id: str) -> None:
    alias, rows = _own(conversation_id)
    expected = build_profile(rows, alias)
    rng = random.Random(7)
    for _ in range(5):
        shuffled = rows[:]
        rng.shuffle(shuffled)
        assert build_profile(shuffled, alias) == expected
    assert build_profile(list(reversed(rows)), alias) == expected
    # 같은 메시지가 두 번 들어와도 한 번만 센다
    assert build_profile(rows + rows, alias) == expected


def test_empty_input() -> None:
    p = build_profile([], "이서연")
    assert p.speech == ()
    assert p.address == ()
    assert p.current_speech is None
    assert p.previous_speech is None
    assert p.current_address is None
    assert p.previous_address is None
    assert p.two_valued is None
    assert p.senior_hint is False
    assert (p.observations, p.unknown) == (0, 0)


# ── 프로필: 구간 규칙 ───────────────────────────────────────────────────────────────────────────
def _series(*texts: str) -> list[tuple[str, datetime, str]]:
    base = datetime(2026, 11, 1, 9, 0, tzinfo=KST)
    return [(f"t:m{i:02d}", base + timedelta(hours=i), text) for i, text in enumerate(texts, start=1)]


def test_unknown_and_mixed_messages_neither_open_nor_break_a_segment() -> None:
    rows = _series(
        "오늘 뭐 해?",  # 반말
        "넵",  # 알 수 없음
        "나 먼저 간다",  # 반말 → 구간 열림 (알 수 없음이 사이에 있어도)
        "확인했습니다. 근데 나 먼저 간다",  # 혼용 (존댓말 + 반말)
        "내일 10시 회의실 B",  # 알 수 없음
        "이따 봐",  # 반말
    )
    p = build_profile(rows, "홍길동")
    assert [(s.value, s.count, s.message_ids) for s in p.speech] == [("hae", 3, ("t:m01", "t:m03", "t:m06"))]
    assert (p.observations, p.unknown) == (6, 2)
    assert p.two_valued is None


def test_single_deviation_does_not_break_the_current_segment() -> None:
    rows = _series("확인했습니다.", "알겠습니다.", "ㅇㅇ 그래", "보고드리겠습니다.", "감사합니다.")
    p = build_profile(rows, "홍길동")
    assert [(s.value, s.count) for s in p.speech] == [("hapsyo", 4)]
    assert p.current_speech is not None
    assert p.current_speech.message_ids == ("t:m01", "t:m02", "t:m04", "t:m05")
    assert p.two_valued == {"hapsyo": 4, "hae": 1}


def test_alternating_levels_never_confirm_but_report_two_values() -> None:
    rows = _series("확인했습니다.", "나 먼저 간다", "알겠습니다.", "이따 봐")
    p = build_profile(rows, "홍길동")
    assert p.speech == ()
    assert p.current_speech is None
    assert p.two_valued == {"hapsyo": 2, "hae": 2}


def test_two_valued_looks_only_at_the_recent_window_after_the_change() -> None:
    # 반말 구간 뒤 합쇼체 구간. 합쇼체로 바뀐 뒤 7건 중 첫 건 다음에 반말이 한 번 끼어 있다
    rows = _series(
        "뭐 해?",
        "나 간다",
        "확인했습니다.",
        "알겠습니다.",
        "이따 봐",
        "보고드리겠습니다.",
        "감사합니다.",
        "공유드립니다.",
        "전달하겠습니다.",
        "수정했습니다.",
        "완료했습니다.",
    )
    p = build_profile(rows, "홍길동")
    assert [(s.value, s.count) for s in p.speech] == [("hae", 2), ("hapsyo", 8)]
    # 변화 뒤 분류 메시지는 9건(합쇼체 8, 반말 1)이고, 최근 6건에는 반말이 없다
    assert register.TWO_VALUED_WINDOW == 6
    assert p.two_valued is None
    # 창 안에 들어오는 시점에는 드러난다
    earlier = build_profile(rows, "홍길동", as_of=rows[7][1])
    assert earlier.two_valued == {"hapsyo": 4, "hae": 1}


def test_address_becomes_confirmed_after_two_uses_and_changes_with_two_new_uses() -> None:
    rows = _series(
        "지현씨, 자료 확인 부탁드려요.",
        "지현씨 안녕하세요",
        "팀장님, 보고드립니다.",
        "팀장님, 확인 부탁드립니다.",
    )
    p = build_profile(rows, "박지현")
    assert [(s.value, s.count, s.status) for s in p.address] == [
        ("지현씨", 2, "previous"),
        ("팀장님", 2, "confirmed"),
    ]
    assert p.previous_address is not None
    assert p.previous_address.end == rows[2][1]
    assert p.senior_hint is True
    before = build_profile(rows, "박지현", as_of=rows[2][1])
    assert [(s.value, s.count, s.status) for s in before.address] == [("지현씨", 2, "confirmed")]
    assert before.senior_hint is False


def test_naive_and_aware_datetimes_do_not_crash() -> None:
    rows = [
        ("a:1", datetime(2026, 11, 1, 9, 0), "나 간다"),
        ("a:2", datetime(2026, 11, 1, 10, 0, tzinfo=KST), "이따 봐"),
    ]
    p = build_profile(rows, "홍길동", as_of=datetime(2026, 11, 2))
    assert p.observations == 2


# ── 리뷰에서 나온 사례 (2026-11-16) ─────────────────────────────────────────────────────────────
def _speech(p: register.RegisterProfile) -> list[tuple[str, int, str]]:
    return [(s.value, s.count, s.status) for s in p.speech]


def test_shift_to_polite_written_in_mixed_polite_style_opens_a_polite_segment() -> None:
    """존댓말로 바꾼 뒤 합쇼체와 해요체를 한 메시지에 섞어 써도 예전 반말이 '지금 말투'로 남지 않는다."""
    rows = _series(
        "알겠어",
        "내일 봐",
        "확인했습니다. 내일 봬요!",
        "감사합니다. 자료는 오후에 드릴게요.",
        "죄송합니다. 조금 늦을 것 같아요.",
        "네 알겠습니다. 곧 갈게요",
    )
    p = build_profile(rows, "홍길동")
    assert _speech(p) == [("hae", 2, "previous"), ("hapsyo", 4, "confirmed")]
    assert p.current_speech is not None
    assert p.current_speech.start == rows[2][1]
    assert p.two_valued is None


def test_hapsyo_and_haeyo_continue_each_others_run() -> None:
    """메시지 단위로 합쇼체와 해요체가 번갈아 나와도 존댓말 구간이 열린다 (반말/존댓말 경계를 먼저 본다)."""
    rows = _series("알겠어", "내일 봐", *["알겠습니다", "알겠어요"] * 3)
    p = build_profile(rows, "홍길동")
    assert _speech(p) == [("hae", 2, "previous"), ("hapsyo", 3, "confirmed")]
    cur = p.current_speech
    assert cur is not None
    # 구간은 존댓말로 바뀐 첫 메시지에서 시작하고, 구간 값(합쇼체)으로 관찰된 메시지만 센다
    assert cur.start == rows[2][1]
    assert cur.message_ids == ("t:m03", "t:m05", "t:m07")
    assert p.previous_speech is not None and p.previous_speech.end == rows[2][1]
    # 두 존댓말 단계가 섞여 있다는 것은 두 값 상태로 드러난다 (제안은 값을 정하지 않고 묻는다)
    assert p.two_valued == {"hapsyo": 3, "haeyo": 3}
    # 해요체로 먼저 넘어가도 같다: 연속 두 건의 다수 단계(같으면 합쇼체)
    q = build_profile(_series("알겠어", "내일 봐", "알겠어요", "알겠습니다"), "홍길동")
    assert _speech(q) == [("hae", 2, "previous"), ("hapsyo", 1, "confirmed")]
    # 존댓말 한 건 + 반말 한 건은 같은 쪽이 아니다 — 구간을 열지 않는다
    r = build_profile(_series("알겠습니다", "확인했습니다", "알겠어요", "내일 봐"), "홍길동")
    assert _speech(r) == [("hapsyo", 2, "confirmed")]


def test_a_segment_absent_from_the_recent_messages_is_not_confirmed() -> None:
    """현재 구간 뒤로 반말+존댓말 혼용 메시지만 이어지면 구간은 닫히지 않지만 '지금'의 것도 아니다."""
    assert register.RECENT_WINDOW == 6
    old = ["알겠어", "내일 봐"]
    mixed = "확인했습니다. 근데 나 먼저 간다"
    fresh = build_profile(_series(*old, *[mixed] * 5), "홍길동")
    assert _speech(fresh) == [("hae", 2, "confirmed")]  # 최근 6건 안에 m02가 있다
    stale = build_profile(_series(*old, *[mixed] * 6), "홍길동")
    assert _speech(stale) == [("hae", 2, "candidate")]
    # 말투를 알 수 없는 메시지("넵")는 아무것도 말해 주지 않는다 — 창에서 뺀다
    quiet = build_profile(_series(*old, *["넵"] * 10), "홍길동")
    assert _speech(quiet) == [("hae", 2, "confirmed")]


@pytest.mark.parametrize(
    ("text", "alias", "expected"),
    [
        # 문장 가운데의 쉼표는 호칭 표시가 아니다
        ("다음 주 이사, 잘 마무리할게", "이서연", []),
        ("오늘 팀장님, 과장님 다 휴가래요", "김지현", []),
        ("죄송합니다 팀장님, 내일 드리겠습니다", "김지현", []),
        # 님 없는 직함은 이 상대의 것이라는 표시(별칭·이름)가 있어야 한다
        ("의사, 간호사 모두 친절했습니다", "김지현 팀장님", []),
        ("책임, 의무 다 중요합니다", "김지현", []),
        ("대리, 이거 봐", "김지현", []),
        ("대리, 이거 봐", "지현 대리", ["대리"]),
        ("김대리, 이거 봐", "김대리", ["김대리"]),
        ("지현 대리, 이거 봐", "김지현", ["지현 대리"]),
        # 호칭 꼴이 이어지는 나열
        ("부장님, 차장님, 과장님 모두 참석하십니다", "김지현", []),
        ("팀장님, 과장님 다 휴가래요", "김지현", []),
        # 나열이 아니면 그대로 호칭이다
        ("팀장님, 부장님 오셨어요", "김지현", ["팀장님"]),
        ("팀장님, 부장님께 보고드렸습니다", "김지현", ["팀장님"]),
        # 문장 첫머리: 줄의 처음, 대답 뒤, 마침표 뒤
        ("안녕하세요. 팀장님, 보고드립니다", "김지현", ["팀장님"]),
        ("박 팀장님, 안녕하세요", "박지현", ["박 팀장님"]),
    ],
)
def test_address_terms_need_a_vocative_position(text: str, alias: str, expected: list[str]) -> None:
    assert address_terms(text, alias) == expected


def test_a_noun_that_is_also_a_title_does_not_become_the_address() -> None:
    rows = _series("서연아 뭐해", "서연아 밥 먹자", "서연아 내일 봐", "다음 주 이사, 잘 마무리할게", "알겠어")
    p = build_profile(rows, "이서연")
    assert [(s.value, s.count, s.status) for s in p.address] == [("서연아", 3, "confirmed")]
    assert p.candidate_address is None
    assert p.senior_hint is False


def test_one_off_address_does_not_displace_a_confirmed_one_or_flip_the_senior_hint() -> None:
    rows = _series("서연아 뭐해", "서연아 밥 먹자", "파트장님, 내일 봬요")
    p = build_profile(rows, "이서연")
    assert p.current_address is not None
    assert (p.current_address.value, p.current_address.status) == ("서연아", "confirmed")
    assert p.candidate_address is not None and p.candidate_address.value == "파트장님"
    assert p.senior_hint is False  # 한 번의 관찰로 위계 힌트를 뒤집지 않는다
    # 두 번째로 쓰면 확정되고 그때 힌트가 바뀐다
    again = build_profile([*rows, *_series("", "", "", "파트장님, 확인 부탁드립니다")[3:]], "이서연")
    assert [(s.value, s.status) for s in again.address] == [("서연아", "previous"), ("파트장님", "confirmed")]
    assert again.candidate_address is None
    assert again.senior_hint is True


def test_address_is_confirmed_by_two_uses_even_when_not_consecutive() -> None:
    """설계 3.2: 호칭은 2회 이상이면 확정. 사이에 다른 호칭이 한 번 끼어도 2회는 2회다."""
    rows = _series("팀장님, 보고드립니다", "지현 팀장님, 확인 부탁드립니다", "팀장님, 감사합니다")
    p = build_profile(rows, "김지현")
    assert [(s.value, s.count, s.status, s.message_ids) for s in p.address] == [
        ("팀장님", 2, "confirmed", ("t:m01", "t:m03"))
    ]
    assert p.candidate_address is None


def test_name_vocative_label_needs_the_alias() -> None:
    """이름+아 호격으로 반말을 읽는 규칙은 별칭이 있어야 한다 — 별칭 없이 부르면 다른 구간이 나온다."""
    rows = _series("알겠습니다", "감사합니다", "서연아 오늘 국밥 ㄱ?", "서연아 ㅇㅋ?", "넵")
    with_alias = build_profile(rows, "이서연")
    assert _speech(with_alias) == [("hapsyo", 2, "previous"), ("hae", 2, "confirmed")]
    assert _speech(build_profile(rows, "")) == [("hapsyo", 2, "confirmed")]


@pytest.mark.parametrize(
    "text",
    [
        "ㅋ" * 20000 + "가",  # 꼬리 글자가 길게 이어진 뒤 다른 글자
        "!" * 50000 + "a",
        "좋" + "아" * 20000 + "가",  # 늘여 쓴 끝 글자
        "요" + "오" * 20000 + "가",
        "가님 " * 3000,  # 님으로 끝나는 낱말 수천 개 (재귀였다면 RecursionError)
        "ㅋ" * 20000 + "가, " + "팀장님, " * 2000,
    ],
)
def test_pathological_input_is_linear_and_does_not_raise(text: str) -> None:
    started = time.perf_counter()
    assert message_label(text) in LEVEL_KO
    address_terms(text, "김지현")
    observe("x:1", datetime(2026, 11, 1, tzinfo=KST), text, "김지현")
    # 제곱 시간이던 때는 한 건에 2~14초였다. 느린 CI를 감안해도 1초면 충분하다.
    assert time.perf_counter() - started < 1.0


def test_trailing_call_is_stripped_at_most_three_times() -> None:
    assert classify_sentence("감사합니다 팀장님") == "hapsyo"
    assert classify_sentence("감사합니다 " + "팀장님 " * 3) == "hapsyo"
    assert classify_sentence("감사합니다 " + "팀장님 " * 4) == "unknown"
