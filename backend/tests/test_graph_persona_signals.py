"""그래프의 관계 사실에서 읽는 페르소나 갱신 제안 (2026-11-16).

D 조건에서 그래프 사실은 첨삭 문장을 거의 바꾸지 못했다 — 문장을 바꾸는 것은 설정이다. 그래프는 사실마다
시각이 있어 "언제부터 존댓말인지", "언제부터 '파트장님'인지"를 말할 수 있다.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.domain.persona import PersonaSettings, default_persona
from app.graph.persona_signals import graph_persona_signals
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
    _import(client, contact["id"], ["밥 먹었어", "그래 가자", "축하해", "네 알겠습니다", "공유드립니다"])
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
    _import(client, contact["id"], ["네 알겠습니다"])  # 5건 미만 — 메시지로는 추정하지 않는다
    client.app.state.container.graph_store = FakeGraphStore(  # type: ignore[attr-defined]
        entities={"이서연": "n-peer", "김민수": "n-me"}, relationships={("n-me", "n-peer"): PROMOTED}
    )
    body = client.get(f"/v1/contacts/{contact['id']}/persona/suggestion", headers=HEADERS).json()
    assert [s["source"] for s in body["signals"]] == ["graph", "graph"]
    assert body["settings"]["formality"] == 4 and body["settings"]["address_term"] == "파트장님"
