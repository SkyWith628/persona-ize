"""그래프의 관계 사실에서 읽는 페르소나 갱신 제안 (2026-11-16).

D 조건에서 그래프 사실은 첨삭 문장을 거의 바꾸지 못했다 — 문장을 바꾸는 것은 설정이다. 그래프는 사실마다
시각이 있어 "언제부터 존댓말인지", "언제부터 '파트장님'인지"를 말할 수 있다.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.domain.persona import PersonaSettings, default_persona
from app.graph.persona_signals import called_in, graph_persona_signals
from app.graph.store import RelationshipFact
from app.main import create_app
from tests.test_graph import FakeGraphStore


def _t(day: int, hour: int = 1) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=UTC)


def _fact(
    uuid: str,
    day: int,
    speech: str | None = None,
    term: str | None = None,
    hierarchy: str | None = None,
    hour: int = 1,
) -> RelationshipFact:
    return RelationshipFact(uuid, f"사실 {uuid}", _t(day, hour), speech, term, hierarchy)


# 동기가 10/17에 파트장이 되어 말투와 호칭이 바뀐 대화 (평가 사례 E004의 그래프)
PROMOTED = [
    _fact("f1", 7, "반말", None, "equal"),
    _fact("f2", 17, "존댓말"),
    _fact("f3", 21, "존댓말", "파트장님", "target_higher"),
]


def test_signals_say_when_the_speech_level_and_the_address_term_changed() -> None:
    base = default_persona("colleague")  # 격식 3, 호칭 없음
    signals = graph_persona_signals(PROMOTED, base)
    assert [(s.field, s.value, s.source, s.since) for s in signals] == [
        ("formality", 4, "graph", date(2026, 10, 17)),
        ("address_term", "파트장님", "graph", date(2026, 10, 21)),
    ]
    assert signals[0].reason == "그래프: 10/17부터 존댓말 (그 전에는 반말), 상대가 윗사람 — 관계 사실 2건"
    assert signals[1].reason == "그래프: 10/21부터 '파트장님'(이)라고 부름 — 관계 사실 1건"


def test_signals_are_silent_when_the_settings_already_match() -> None:
    saved = PersonaSettings(formality=4, address_term="서연 파트장님")  # 더 구체적인 호칭이 이미 있다
    assert graph_persona_signals(PROMOTED, saved) == []
    # 더 격식 있는 설정을 낮추자고 하지 않는다
    assert graph_persona_signals(PROMOTED, PersonaSettings(formality=5, address_term="파트장님")) == []


def test_signals_respect_the_time_order_not_the_given_order() -> None:
    shuffled = [PROMOTED[2], PROMOTED[0], PROMOTED[1]]
    assert [s.value for s in graph_persona_signals(shuffled, default_persona("colleague"))] == [4, "파트장님"]
    # 10/18 시점: 호칭은 아직 모르고, 윗사람이라는 것도 아직 확인되지 않았다 → 존댓말 '-요'체(3)는 지금과 같다
    assert graph_persona_signals(PROMOTED, default_persona("colleague"), as_of=_t(18)) == []
    assert [s.value for s in graph_persona_signals(PROMOTED, PersonaSettings(formality=1), as_of=_t(18))] == [
        3
    ]


def test_casual_speech_lowers_a_polite_setting_and_mixed_is_not_evidence() -> None:
    friend = [_fact("a", 3, "존댓말"), _fact("b", 9, "반말"), _fact("c", 12, "혼용"), _fact("d", 14, "반말")]
    signals = graph_persona_signals(friend, PersonaSettings(formality=4))
    assert [(s.field, s.value) for s in signals] == [("formality", 1)]
    assert signals[0].reason == "그래프: 10/09부터 반말 (그 전에는 존댓말) — 관계 사실 2건"
    # 이미 반말 설정이면 말하지 않는다. '혼용'뿐이면 근거가 없다
    assert graph_persona_signals(friend, PersonaSettings(formality=2)) == []
    assert graph_persona_signals([_fact("m", 3, "혼용")], PersonaSettings(formality=4)) == []


def test_address_term_follows_the_latest_one_and_names_the_previous() -> None:
    facts = [_fact("a", 5, term="서연아"), _fact("b", 21, term=" 파트장님 "), _fact("c", 22, term="파트장님")]
    signals = graph_persona_signals(facts, PersonaSettings(address_term="서연아"))
    assert [(s.field, s.value, s.since) for s in signals] == [
        ("address_term", "파트장님", date(2026, 10, 21))
    ]
    assert (
        signals[0].reason == "그래프: 10/21부터 '파트장님'(이)라고 부름 (그 전에는 '서연아') — 관계 사실 2건"
    )
    # 설정 길이 상한을 넘는 값·빈 값·시각 없는 사실은 버린다
    junk = [
        _fact("long", 5, term="가" * 31),
        _fact("blank", 6, term="  "),
        RelationshipFact("no-time", "시각 없음", None, "존댓말", "선배님", "target_higher"),
    ]
    assert graph_persona_signals(junk, PersonaSettings()) == []


def test_address_term_must_be_something_i_actually_called_the_contact() -> None:
    """2026-11-23 실제 내보내기: 추출기가 채운 호칭의 대부분이 부른 말이 아니었다."""
    mine = [
        "상무님, 지시하신 3분기 매출 점검 자료 정리 시작합니다.",
        "부장님과 얘기한 뒤 오늘 안에 답드리겠습니다.",  # 제3자를 가리키기만 했다
        "2동 하역장으로 배차 잡아 뒀습니다. 기사님 연락처는 출고 전날 드리겠습니다.",
        "이과장님, 안녕하세요.",
        "수고하셨어요 영수 씨. 내일 봬요.",
    ]

    def suggest(*terms: str, current: str | None = None) -> list[object]:
        facts = [_fact(f"f{i}", 5 + i, term=term) for i, term in enumerate(terms)]
        base = PersonaSettings(address_term=current)
        return [s.value for s in graph_persona_signals(facts, base, me_names=["김과장"], own_texts=mine)]

    # 말하는 사람 이름의 일부, 이름 조각, 제3자, 메시지에 없는 이름은 버린다
    assert suggest("과장") == []
    assert suggest("님") == []
    assert suggest("상무님", "부장님") == ["상무님"]  # 더 최근인 '부장님'은 부른 말이 아니다
    assert suggest("기사님") == []
    assert suggest("박영수") == []
    assert suggest("과장님") == []  # '이과장님' 속의 '과장님'은 다른 낱말의 뒷부분이다
    # 부른 말은 그대로 제안한다: 쉼표 앞, 문장 끝
    assert suggest("이과장님") == ["이과장님"]
    assert suggest("영수 씨") == ["영수 씨"]
    # 메시지를 주지 않으면 내 이름만 본다
    facts = [_fact("a", 5, term="과장"), _fact("b", 6, term="부장님")]
    assert [s.value for s in graph_persona_signals(facts[:1], PersonaSettings(), me_names=["김과장"])] == []
    assert [s.value for s in graph_persona_signals(facts, PersonaSettings(), me_names=["김과장"])] == [
        "부장님"
    ]


def test_called_in_reads_calls_not_mentions() -> None:
    assert called_in("서연아", ["서연아 오늘 점심 같이 먹자"])  # 맨 앞, 쉼표 없이
    assert called_in("파트장님", ["네 알겠습니다 파트장님"])  # 글 끝
    assert called_in("Michael", ["Hi Michael, thanks for the update."])
    assert not called_in("파트장님", ["파트장님이 오늘 회의하자고 하셨어"])
    assert not called_in("팀장", ["생산팀장과 같이 갑니다."])
    assert not called_in("a.b", ["aXb,"])  # 호칭 속 글자는 정규식으로 읽지 않는다


def _seen(fact: RelationshipFact, day: int, hour: int) -> RelationshipFact:
    """근거 메시지 발화 시각(observed_at)을 붙인 사실."""
    return replace(fact, observed_at=_t(day, hour))


def test_order_and_dates_follow_the_evidence_message_time_not_the_model_valid_at() -> None:
    # 실측: 모델이 한국 시간을 UTC로 적어 valid_at이 근거 메시지보다 9시간 늦은 사실이 섞인다.
    # 10/16 23:30 KST(=14:30Z)에 반말, 10/17 08:00 KST(=10/16 23:00Z)에 존댓말·'파트장님'으로 바뀌었다.
    # 반말 사실의 valid_at만 9시간 밀려(23:30Z) 존댓말 사실(23:00Z)보다 뒤에 온다.
    casual = replace(
        _fact("late", 16, "반말", "서연아"),
        valid_at=datetime(2026, 10, 16, 23, 30, tzinfo=UTC),
        observed_at=datetime(2026, 10, 16, 14, 30, tzinfo=UTC),
    )
    polite = _seen(_fact("ok", 16, "존댓말", "파트장님", "target_higher", hour=23), 16, 23)
    signals = graph_persona_signals([casual, polite], PersonaSettings(formality=1, address_term="서연아"))
    assert [(s.field, s.value, s.since) for s in signals] == [
        ("formality", 4, date(2026, 10, 17)),
        ("address_term", "파트장님", date(2026, 10, 17)),
    ]
    assert signals[0].reason == "그래프: 10/17부터 존댓말 (그 전에는 반말), 상대가 윗사람 — 관계 사실 1건"

    # 날짜: 10/20 22:00 KST(=13:00Z)의 메시지인데 valid_at은 22:00Z(= 10/21 07:00 KST)로 적혔다
    shifted = _seen(_fact("s", 20, term="선배님", hour=22), 20, 13)
    (signal,) = graph_persona_signals([shifted], PersonaSettings())
    assert signal.since == date(2026, 10, 20) and signal.reason.startswith("그래프: 10/20부터")


def test_facts_without_an_evidence_time_fall_back_to_valid_at() -> None:
    # observed_at이 없는 사실(근거 에피소드를 못 찾음)은 예전처럼 valid_at으로 줄을 선다 — 섞여 있어도 된다
    assert all(f.observed_at is None for f in PROMOTED)
    mixed = [PROMOTED[0], _seen(replace(PROMOTED[1], valid_at=None), 17, 1), PROMOTED[2]]
    signals = graph_persona_signals(mixed, default_persona("colleague"))
    assert [(s.field, s.value, s.since) for s in signals] == [
        ("formality", 4, date(2026, 10, 17)),
        ("address_term", "파트장님", date(2026, 10, 21)),
    ]


def test_as_of_cuts_on_the_evidence_message_time() -> None:
    # 근거 메시지는 10/17 20:00Z, valid_at은 9시간 밀린 10/18 05:00Z
    fact = _seen(_fact("f", 18, "존댓말", hour=5), 17, 20)
    base = PersonaSettings(formality=1)
    # 10/18 00:00Z 시점: 메시지는 이미 있었다 (valid_at 기준이면 빠졌을 사실)
    assert [s.value for s in graph_persona_signals([fact], base, as_of=_t(18, 0))] == [3]
    # 10/17 12:00Z 시점: 아직 없던 메시지. 경계는 '이전'(<)이다
    assert graph_persona_signals([fact], base, as_of=_t(17, 12)) == []
    assert graph_persona_signals([fact], base, as_of=_t(17, 20)) == []
    # 반대로 valid_at이 as_of보다 앞이어도 근거 메시지가 뒤면 뺀다
    early_stamp = _seen(_fact("g", 10, "존댓말"), 19, 1)
    assert graph_persona_signals([early_stamp], base, as_of=_t(18, 0)) == []


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(Settings(_env_file=None, llm_provider="fake", embedding_provider="fake")))  # type: ignore[call-arg]


HEADERS = {"X-User-Id": "user-a"}


def _contact(client: TestClient, alias: str = "이서연") -> dict:
    return client.post(
        "/v1/contacts", json={"alias": alias, "relationship_type": "colleague"}, headers=HEADERS
    ).json()


def _import(client: TestClient, contact_id: str, texts: list[str], me_name: str | None = "김민수") -> None:
    conv = client.post(
        "/v1/conversations",
        json={"contact_id": contact_id, "title": "방", "source_key": "c3", "me_name": me_name},
        headers=HEADERS,
    )
    assert conv.status_code == 201, conv.text
    messages = [
        {"id": f"m{i:02d}", "speaker": "me", "sent_at": f"2026-10-{7 + i:02d}T12:00:00+09:00", "text": t}
        for i, t in enumerate(texts)
    ]
    r = client.post(
        f"/v1/conversations/{conv.json()['id']}/imports", json={"messages": messages}, headers=HEADERS
    )
    assert r.status_code in (200, 201, 202), r.text


def test_suggestion_api_appends_graph_signals_and_they_win_in_settings(client: TestClient) -> None:
    contact = _contact(client)
    # 내 메시지: 앞은 반말, 뒤는 존댓말 — 겉모습만 보면 "섞여 있음"(격식 2)
    _import(
        client, contact["id"], ["밥 먹었어", "그래 가자", "축하해", "네 알겠습니다", "파트장님, 공유드립니다"]
    )
    store = FakeGraphStore(
        entities={"이서연": "n-peer", "김민수": "n-me"}, relationships={("n-me", "n-peer"): PROMOTED}
    )
    client.app.state.container.graph_store = store  # type: ignore[attr-defined]

    body = client.get(f"/v1/contacts/{contact['id']}/persona/suggestion", headers=HEADERS).json()

    by_source = [(s["field"], s["value"], s["source"], s["since"]) for s in body["signals"]]
    assert ("formality", 2, "messages", None) in by_source
    assert by_source[-2:] == [
        ("formality", 4, "graph", "2026-10-17"),
        ("address_term", "파트장님", "graph", "2026-10-21"),
    ]
    # 같은 항목은 뒤에 온 그래프 근거가 제안 설정에 반영된다
    assert body["settings"]["formality"] == 4 and body["settings"]["address_term"] == "파트장님"
    assert body["based_on_messages"] == 5


def test_suggestion_api_without_graph_or_nodes_is_unchanged(client: TestClient) -> None:
    contact = _contact(client)
    _import(client, contact["id"], ["밥 먹었어", "그래 가자", "축하해", "네 알겠습니다", "공유드립니다"])
    url = f"/v1/contacts/{contact['id']}/persona/suggestion"
    plain = client.get(url, headers=HEADERS).json()  # 그래프 꺼짐
    # 그래프 근거는 없다. 말투 프로필 근거(register, 기본 켜짐)는 그래프와 무관하게 나온다 — 이 이력은
    # 반말 3건 → 합쇼체 2건이라 register가 격식을 제안한다 (2026-11-16 이전에는 messages만 있었다).
    assert all(s["source"] != "graph" for s in plain["signals"])
    assert all(s["since"] is None for s in plain["signals"] if s["source"] == "messages")

    container = client.app.state.container  # type: ignore[attr-defined]
    # 상대 노드가 없다 / 내 노드가 없다 → 메시지 근거만
    for entities in ({"김민수": "n-me"}, {"이서연": "n-peer"}):
        container.graph_store = FakeGraphStore(
            entities=entities, relationships={("n-me", "n-peer"): PROMOTED}
        )
        assert client.get(url, headers=HEADERS).json() == plain

    # 그래프가 죽어도 화면은 막지 않는다
    class Down(FakeGraphStore):
        async def find_entity_uuid(self, group_id, name):  # noqa: ANN001, ANN201
            raise RuntimeError("neo4j down")

    container.graph_store = Down()
    assert client.get(url, headers=HEADERS).json() == plain


def test_suggestion_api_gives_graph_signals_even_with_few_messages(client: TestClient) -> None:
    contact = _contact(client)
    _import(client, contact["id"], ["네 알겠습니다 파트장님"])  # 5건 미만 — 메시지로는 추정하지 않는다
    client.app.state.container.graph_store = FakeGraphStore(  # type: ignore[attr-defined]
        entities={"이서연": "n-peer", "김민수": "n-me"}, relationships={("n-me", "n-peer"): PROMOTED}
    )
    body = client.get(f"/v1/contacts/{contact['id']}/persona/suggestion", headers=HEADERS).json()
    # 문장 끝의 '파트장님'은 말투 프로필도 읽는다(2026-11-24, 1회 관찰) — 그래프 근거 둘은 그대로 붙는다
    assert [s["source"] for s in body["signals"]] == ["register", "graph", "graph"]
    assert body["settings"]["formality"] == 4 and body["settings"]["address_term"] == "파트장님"


def test_graph_address_does_not_override_the_one_counted_in_my_messages(client: TestClient) -> None:
    """말투 프로필이 내 메시지에서 2회 이상 확인한 호칭이 있으면 추출된 호칭은 제안하지 않는다."""
    contact = _contact(client, "김상무님")
    # '영수 씨'는 내 메시지에 부른 말로 나오지만(대조 통과) 이 상대의 호칭으로 확인된 것은 '상무님'이다
    texts = ["상무님, 보고드립니다.", "상무님, 확인했습니다.", "수고하셨어요 영수 씨.", "네 알겠습니다."]
    _import(client, contact["id"], texts, me_name="김과장")
    facts = [_fact("a", 8, term="상무님"), _fact("b", 9, term="영수 씨"), _fact("c", 10, term="과장")]
    store = FakeGraphStore(
        entities={"김상무님": "n-peer", "김과장": "n-me"}, relationships={("n-me", "n-peer"): facts}
    )
    client.app.state.container.graph_store = store  # type: ignore[attr-defined]

    body = client.get(f"/v1/contacts/{contact['id']}/persona/suggestion", headers=HEADERS).json()

    from_graph = [
        s["value"] for s in body["signals"] if s["source"] == "graph" and s["field"] == "address_term"
    ]
    assert from_graph == []  # 더 최근인 '영수 씨'도, 내 이름의 일부인 '과장'도 내지 않는다
    # 말투 프로필은 '영수 씨'를 한 번 나온 후보로 읽고(2026-11-24) 확정된 '상무님'은 그대로 둔다.
    # 이 메시지가 해요체로 읽히면서 두 말투가 섞인 카드(묻는 카드)가 되어 설정 값은 정해 주지 않는다.
    (from_register,) = [s for s in body["signals"] if s["field"] == "address_term"]
    assert (from_register["source"], from_register["value"]) == ("register", "영수 씨")
    assert "1회 관찰 (그 전에는 '상무님')" in from_register["reason"]
    assert body["card"]["address"]["status"] == "candidate" and body["card"]["ask"] is True
    assert body["settings"] is None
