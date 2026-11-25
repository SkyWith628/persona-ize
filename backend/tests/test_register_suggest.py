"""말투 프로필의 설정 제안·제안 카드와 그래프의 계기 조회 (docs/design/graph-redesign-2026-11-16.md 3.3, 5.1).

세 층을 본다.
- `ai.register_suggest`: 손으로 만든 프로필 → 근거(Signal)와 카드. 분류기와 무관하게 규칙만 확인한다.
- `graph.trigger`: FakeGraphStore에서 변화 시점 앞뒤의 사건과 관계 사실을 찾는다.
- API: 메모리 저장소 + FakeGraphStore로 `GET /persona/suggestion`의 `card`와 근거 순서.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.ai.register import RegisterProfile, Segment
from app.ai.register_suggest import (
    SCOPE_ASK_REASON,
    build_card,
    has_content,
    register_signals,
    with_graph_evidence,
)
from app.core.config import Settings
from app.domain.persona import PersonaSettings, default_persona
from app.graph.store import EpisodeRef, GraphEdgeHit, GraphNodeInfo
from app.graph.trigger import TRIGGER_WINDOW, find_trigger
from app.main import create_app
from tests.test_graph import FakeGraphStore

KST = timezone(timedelta(hours=9))


def _t(day: int, hour: int = 10, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=KST)


def _seg(value: str, day: int, ids: list[str], status: str = "confirmed", end: int | None = None) -> Segment:
    return Segment(
        value=value,
        start=_t(day),
        end=_t(end) if end is not None else None,
        count=len(ids),
        message_ids=tuple(ids),
        status=status,  # type: ignore[arg-type]
    )


def _profile(
    speech: list[Segment],
    address: list[Segment] | None = None,
    two_valued: dict[str, int] | None = None,
    senior: bool = False,
    candidate: Segment | None = None,
) -> RegisterProfile:
    address = address or []
    return RegisterProfile(
        speech=tuple(speech),
        address=tuple(address),
        current_speech=speech[-1] if speech else None,
        previous_speech=speech[-2] if len(speech) > 1 else None,
        current_address=address[-1] if address else None,
        previous_address=address[-2] if len(address) > 1 else None,
        two_valued=two_valued,
        senior_hint=senior,
        observations=sum(s.count for s in speech),
        unknown=0,
        candidate_address=candidate,
    )


# 동기가 파트장이 된 대화 (c3): 반말 3건 → 10/21부터 합쇼체 2건, '서연 파트장님' 1회
PROMOTED = _profile(
    [
        _seg("hae", 7, ["c3:m02", "c3:m04", "c3:m06"], "previous", end=21),
        _seg("hapsyo", 21, ["c3:m08", "c3:m10"]),
    ],
    [_seg("서연 파트장님", 21, ["c3:m08"], "candidate")],
    senior=True,
)


def test_signals_name_the_change_date_the_counts_and_the_previous_level() -> None:
    signals = register_signals(PROMOTED, default_persona("colleague"))  # 격식 3, 호칭 없음
    assert [(s.field, s.value, s.source, s.since) for s in signals] == [
        ("formality", 4, "register", date(2026, 10, 21)),
        ("address_term", "서연 파트장님", "register", date(2026, 10, 21)),
    ]
    assert signals[0].reason == (
        "내 메시지: 10/21부터 합쇼체 2건 (그 전에는 반말 3건), 호칭으로 보아 상대가 윗사람"
    )
    # 한 번만 나온 호칭은 그렇다고 적는다
    assert signals[1].reason == "내 메시지: 10/21부터 '서연 파트장님'(이)라고 부름 — 1회 관찰"


def test_signals_are_silent_when_the_settings_already_say_it() -> None:
    assert register_signals(PROMOTED, PersonaSettings(formality=4, address_term="서연 파트장님")) == []
    # 더 격식 있는 설정을 낮추지 않고, 더 구체적인 호칭을 덮어쓰지 않는다
    short = _profile([_seg("haeyo", 3, ["c1:m01", "c1:m02"])], [_seg("팀장님", 3, ["c1:m01", "c1:m02"])])
    assert register_signals(short, PersonaSettings(formality=5, address_term="지현 팀장님")) == []
    # 해요체는 격식 3 — 반말 설정이면 올리자고 한다
    raised = register_signals(short, PersonaSettings(formality=1, address_term="팀장님"))
    assert [(s.field, s.value) for s in raised] == [("formality", 3)]
    assert raised[0].reason == "내 메시지: 10/03부터 해요체 2건"
    # 구간이 없으면(메시지 한 건, 전부 알 수 없음) 말하지 않는다
    assert register_signals(_profile([]), PersonaSettings()) == []


def test_casual_speech_lowers_a_polite_setting_but_not_a_casual_one() -> None:
    friend = _profile(
        [
            _seg("haeyo", 3, ["c2:m01", "c2:m02"], "previous", end=9),
            _seg("hae", 9, ["c2:m03", "c2:m04", "c2:m05"]),
        ],
        [
            _seg("유진 씨", 3, ["c2:m01", "c2:m02"], "previous", end=9),
            _seg("유진아", 9, ["c2:m03", "c2:m05"]),
        ],
    )
    signals = register_signals(friend, PersonaSettings(formality=4, address_term="유진 씨"))
    assert [(s.field, s.value) for s in signals] == [("formality", 1), ("address_term", "유진아")]
    assert signals[0].reason == "내 메시지: 10/09부터 반말 3건 (그 전에는 해요체 2건)"
    assert signals[1].reason == "내 메시지: 10/09부터 '유진아'(이)라고 부름 — 2회 (그 전에는 '유진 씨')"
    # 이미 반말 쪽(격식 2) 설정이면 1로 더 낮추자고 하지 않는다
    assert register_signals(friend, PersonaSettings(formality=2, address_term="유진아")) == []
    # 설정 길이 상한을 넘는 호칭은 버린다
    long = _profile([], [_seg("가" * 31, 3, ["c2:m01", "c2:m02"])])
    assert register_signals(long, PersonaSettings()) == []


def test_card_shows_both_segments_and_asks_when_the_change_is_only_two_messages_old() -> None:
    card = build_card(PROMOTED, default_persona("colleague"), "이서연")
    assert card.contact_alias == "이서연"
    assert card.speech_previous == {"level": "hae", "count": 3, "message_ids": ["c3:m02", "c3:m04", "c3:m06"]}
    assert card.speech_current == {
        "level": "hapsyo",
        "count": 2,
        "message_ids": ["c3:m08", "c3:m10"],
        "since": "2026-10-21",
    }
    assert card.address == {
        "term": "서연 파트장님",
        "count": 1,
        "since": "2026-10-21",
        "status": "candidate",
        "message_ids": ["c3:m08"],
    }
    assert card.proposed == {"formality": 4, "address_term": "서연 파트장님"}
    assert card.ask is True and card.ask_reason == "말투가 바뀐 뒤의 메시지가 2건뿐입니다."
    assert card.two_valued is None and card.trigger is None and card.scope_hints == []
    assert has_content(card)


def test_card_does_not_ask_once_the_new_level_has_three_messages() -> None:
    settled = _profile(
        [
            _seg("hae", 7, ["c3:m02", "c3:m04"], "previous", end=21),
            _seg("hapsyo", 21, ["c3:m08", "c3:m10", "c3:m12"]),
        ]
    )
    card = build_card(settled, default_persona("colleague"), "이서연")
    assert card.ask is False and card.ask_reason is None
    assert card.proposed == {"formality": 4, "address_term": None} and card.address is None
    # 그래프가 범위 단서를 찾으면 묻는 카드가 된다. 계기만 있으면 그대로다
    trigger = {"event": "파트장 발령", "at": "2026-10-17", "message_ids": ["c3:m05"]}
    assert with_graph_evidence(card, trigger, []).ask is False
    hinted = with_graph_evidence(card, trigger, [{"text": "사적으로는 편하게", "message_ids": ["c3:m07"]}])
    assert hinted.ask is True and hinted.ask_reason == SCOPE_ASK_REASON and hinted.trigger == trigger
    # 변화가 없는 상대(처음부터 같은 말투)는 묻지 않는다 — 근거가 두 건이어도
    steady = build_card(_profile([_seg("hapsyo", 5, ["c1:m02", "c1:m04"])]), PersonaSettings(), "박지현")
    assert steady.ask is False and steady.speech_previous is None and steady.proposed["formality"] == 4


def test_two_levels_in_recent_messages_ask_instead_of_picking_one() -> None:
    mixed = _profile(
        [_seg("hae", 7, ["c3:m02", "c3:m04"], "previous", end=21), _seg("hapsyo", 21, ["c3:m08", "c3:m10"])],
        two_valued={"hapsyo": 3, "hae": 3},
    )
    base = default_persona("colleague")
    assert register_signals(mixed, base) == []  # 값 하나로 합치지 않는다
    card = build_card(mixed, base, "이서연")
    assert card.two_valued == {"hapsyo": 3, "hae": 3}
    assert (
        card.ask is True
        and card.ask_reason == "최근 메시지에 두 말투가 섞여 있습니다 (합쇼체 3건 / 반말 3건)."
    )
    assert card.proposed == {"formality": None, "address_term": None}
    assert has_content(card)  # 제안할 값은 없어도 물어볼 것이 있다
    # 범위 단서가 붙어도 더 직접적인 이유(두 말투)를 남긴다
    assert (
        with_graph_evidence(card, None, [{"text": "단서", "message_ids": []}]).ask_reason == card.ask_reason
    )
    # 설정과 같아 제안할 것이 없는 카드는 보여 주지 않는다
    assert not has_content(
        build_card(PROMOTED, PersonaSettings(formality=4, address_term="서연 파트장님"), "이서연")
    )


# ── 그래프: 변화 시점 앞뒤의 계기 사건과 범위 단서 ──────────────────────────────────────────────


def _ref(uuid: str, name: str, day: int, hour: int = 17, minute: int = 40) -> EpisodeRef:
    return EpisodeRef(uuid=uuid, name=name, valid_at=_t(day, hour, minute))


def _edge(uuid: str, name: str, fact: str, episodes: list[str], source: str, target: str) -> GraphEdgeHit:
    return GraphEdgeHit(uuid, name, fact, None, None, episodes, source, target)


PEER, ME = "n-peer", "n-me"
PROMOTION_FACT = "이서연이 김민수의 파트 파트장으로 발령났다"
AGREEMENT_FACT = "김민수와 이서연은 공식 자리에서는 존댓말을, 사적으로는 편하게 말하기로 했다"


def _c3_graph(**overrides: object) -> FakeGraphStore:
    """c3의 그래프: 10/07 점심(기간 밖), 10/17 파트장 발령과 말투 합의, 10/21 회의록 공유."""
    fields: dict = {
        "entities": {"이서연": PEER},
        "nodes": [
            GraphNodeInfo(PEER, "이서연", "person", ""),
            GraphNodeInfo(ME, "김민수", "person", ""),
            GraphNodeInfo("ev-lunch", "국밥 점심", "event", ""),
            GraphNodeInfo("ev-promo", "파트장 발령", "event", ""),
            GraphNodeInfo("ev-notes", "스탠드업 회의록 공유", "event", ""),
        ],
        "events": {PEER: {"ev-lunch", "ev-promo", "ev-notes"}},
        "edges": [
            _edge("e-lunch", "Involved", "이서연이 점심을 제안했다", ["ep01"], PEER, "ev-lunch"),
            _edge("e-peer", "Relationship", "김민수는 이서연과 반말을 쓰는 입사 동기다", ["ep02"], ME, PEER),
            _edge("e-promo", "Involved", PROMOTION_FACT, ["ep05"], PEER, "ev-promo"),
            _edge("e-agree", "Relationship", AGREEMENT_FACT, ["ep06", "ep07"], ME, PEER),
            _edge("e-notes", "Involved", "김민수가 회의록을 공유했다", ["ep08"], ME, "ev-notes"),
        ],
        "refs": {
            "ep01": _ref("ep01", "c3:m01", 7, 12, 10),
            "ep02": _ref("ep02", "c3:m02", 7, 12, 11),
            "ep05": _ref("ep05", "c3:m05", 17, 17, 40),
            "ep06": _ref("ep06", "c3:m06", 17, 17, 42),
            "ep07": _ref("ep07", "c3:m07", 17, 17, 43),
            "ep08": _ref("ep08", "c3:m08", 21, 10, 5),
        },
    }
    fields.update(overrides)
    return FakeGraphStore(**fields)


CHANGED_AT = _t(21, 10, 5)  # c3:m08 — 합쇼체 구간의 시작


@pytest.mark.anyio
async def test_trigger_is_the_nearest_event_before_the_change_and_hints_are_relationship_facts() -> None:
    # 10/21의 회의록 공유는 새 말투로 쓴 첫 메시지(T) 자신이 말한 사건이다 — 계기가 아니다.
    # 10/17 발령이 계기, 10/07 점심은 기간(±7일) 밖
    trigger, hints = await find_trigger(_c3_graph(), "user-a", PEER, CHANGED_AT)
    assert trigger is not None
    assert (trigger.event, trigger.at, trigger.message_ids) == ("파트장 발령", _t(17, 17, 40), ("c3:m05",))
    # 범위 단서: 같은 기간의 관계 사실 문장과 출처. 10/07의 '입사 동기' 사실은 기간 밖이다
    assert [(h.text, h.message_ids) for h in hints] == [(AGREEMENT_FACT, ("c3:m06", "c3:m07"))]
    assert abs(_t(7, 12, 10) - CHANGED_AT) > TRIGGER_WINDOW


@pytest.mark.anyio
async def test_trigger_prefers_before_the_change_and_respects_the_window_and_as_of() -> None:
    store = _c3_graph()
    # 변화 뒤의 더 가까운 사건이 있어도 이전 사건이 먼저다
    store.nodes.append(GraphNodeInfo("ev-after", "API 문서 정리", "event", ""))
    store.events[PEER].add("ev-after")
    store.edges.append(
        _edge("e-after", "Involved", "이서연이 API 문서 정리를 부탁했다", ["ep09"], PEER, "ev-after")
    )
    store.refs["ep09"] = _ref("ep09", "c3:m09", 21, 10, 20)
    trigger, _ = await find_trigger(store, "user-a", PEER, CHANGED_AT)
    assert trigger is not None and trigger.event == "파트장 발령"
    # 이전 사건이 없으면 이후의 가까운 사건
    trigger, hints = await find_trigger(store, "user-a", PEER, _t(17, 17, 0), window=timedelta(hours=1))
    assert trigger is not None and trigger.event == "파트장 발령" and trigger.message_ids == ("c3:m05",)
    assert [h.message_ids for h in hints] == [("c3:m06", "c3:m07")]
    # 기간을 좁히면 발령은 밖이다 — T와 그 뒤에서 가장 가까운 사건이 남고, 단서는 없다
    trigger, hints = await find_trigger(store, "user-a", PEER, CHANGED_AT, window=timedelta(days=1))
    assert trigger is not None and trigger.event == "스탠드업 회의록 공유" and hints == []
    assert await find_trigger(store, "user-a", PEER, _t(1), window=timedelta(days=1)) == (None, [])
    # 평가: as_of 이후의 메시지는 못 본 것으로 한다 (m06까지만 → 단서의 출처도 m06뿐)
    trigger, hints = await find_trigger(store, "user-a", PEER, CHANGED_AT, as_of=_t(17, 17, 43))
    assert trigger is not None and trigger.event == "파트장 발령"
    assert [h.message_ids for h in hints] == [("c3:m06",)]
    assert await find_trigger(store, "user-a", PEER, CHANGED_AT, as_of=_t(17, 17, 0)) == (None, [])


@pytest.mark.anyio
async def test_trigger_lookup_never_raises() -> None:
    class Down(FakeGraphStore):
        async def neighborhood(self, group_id, node_uuid):  # noqa: ANN001, ANN201
            raise RuntimeError("neo4j down")

    assert await find_trigger(Down(), "user-a", PEER, CHANGED_AT) == (None, [])
    # 출처 에피소드를 못 찾는 엣지·시각 없는 에피소드는 건너뛴다
    store = _c3_graph(refs={"ep05": EpisodeRef("ep05", "c3:m05", None)})
    assert await find_trigger(store, "user-a", PEER, CHANGED_AT) == (None, [])
    # 시간대 없는 변화 시점은 UTC로 본다 (예외 없이)
    naive = datetime(2026, 10, 21, 1, 5)
    trigger, _ = await find_trigger(_c3_graph(), "user-a", PEER, naive)
    assert trigger is not None


# ── API ──────────────────────────────────────────────────────────────────────────────────────

HEADERS = {"X-User-Id": "user-a"}
# c3에서 내가 보낸 메시지에 앞뒤로 한 건씩 더한 것 (10/22: 변화 뒤 3건 — 건수만으로는 묻지 않게)
MINE = [
    ("m00", "2026-10-06T19:30:00+09:00", "오늘 좀 늦을 것 같아."),
    ("m02", "2026-10-07T12:11:00+09:00", "서연아 오늘 국밥 먹을래?"),
    ("m04", "2026-10-07T12:15:00+09:00", "혼난 건 아니고 일정 얘기하다가 좀 그랬어."),
    ("m06", "2026-10-17T17:42:00+09:00", "헐 축하해!! 근데 이제 회사에선 존댓말 써야겠네"),
    ("m08", "2026-10-21T10:05:00+09:00", "서연 파트장님, 오늘 스탠드업 회의록 공유드립니다."),
    ("m10", "2026-10-21T10:22:00+09:00", "네, 금요일 오전까지 정리하겠습니다."),
    ("m12", "2026-10-22T09:00:00+09:00", "API 문서 초안 올렸습니다."),
]


def _client(**overrides: object) -> TestClient:
    settings = Settings(_env_file=None, llm_provider="fake", embedding_provider="fake", **overrides)  # type: ignore[arg-type]
    return TestClient(create_app(settings))


@pytest.fixture
def client() -> TestClient:
    return _client()


def _contact_with_history(client: TestClient, mine: list[tuple[str, str, str]]) -> str:
    contact = client.post(
        "/v1/contacts", json={"alias": "이서연", "relationship_type": "colleague"}, headers=HEADERS
    ).json()
    conv = client.post(
        "/v1/conversations",
        json={"contact_id": contact["id"], "title": "방", "source_key": "c3", "me_name": "김민수"},
        headers=HEADERS,
    )
    assert conv.status_code == 201, conv.text
    messages = [{"id": key, "speaker": "me", "sent_at": at, "text": text} for key, at, text in mine]
    r = client.post(
        f"/v1/conversations/{conv.json()['id']}/imports", json={"messages": messages}, headers=HEADERS
    )
    assert r.status_code in (200, 201, 202), r.text
    return f"/v1/contacts/{contact['id']}/persona/suggestion"


def test_api_card_for_a_contact_whose_speech_level_changed(client: TestClient) -> None:
    url = _contact_with_history(client, MINE[1:6])  # c3 그대로: 변화 뒤 2건
    body = client.get(url, headers=HEADERS).json()

    sources = [s["source"] for s in body["signals"]]
    assert sources == sorted(sources, key=["messages", "register", "graph"].index)  # 근거 순서
    register = [(s["field"], s["value"], s["since"]) for s in body["signals"] if s["source"] == "register"]
    assert register == [("formality", 4, "2026-10-21"), ("address_term", "서연 파트장님", "2026-10-21")]

    card = body["card"]
    assert card["contact_alias"] == "이서연"
    assert card["speech_previous"]["level"] == "hae"
    assert card["speech_current"] == {
        "level": "hapsyo",
        "count": 2,
        "message_ids": ["c3:m08", "c3:m10"],
        "since": "2026-10-21",
    }
    assert card["address"]["term"] == "서연 파트장님" and card["address"]["status"] == "candidate"
    assert card["address"]["message_ids"] == ["c3:m08"]
    assert card["proposed"] == {"formality": 4, "address_term": "서연 파트장님"}
    # 그래프가 꺼져 있다 — 계기·단서 없이도 카드는 온다
    assert card["trigger"] is None and card["scope_hints"] == [] and card["two_valued"] is None
    # 변화 뒤 2건뿐이라 묻는다: 말투 프로필의 값은 제안 설정에 넣지 않는다 (근거에는 남는다)
    assert card["ask"] is True and "2건" in card["ask_reason"]
    # 묻는 항목은 지금 설정(동료 기본값 격식 3) 그대로다. 겉모습 추정의 "섞여 있음 → 격식 2"도 설정에
    # 넣지 않는다 — 묻는 카드(제안 격식 4) 옆에 반대쪽 값을 내밀지 않게. 근거 목록에는 남는다.
    assert body["settings"]["formality"] == 3 and body["settings"]["address_term"] is None
    assert ("formality", 2, "messages") in [(x["field"], x["value"], x["source"]) for x in body["signals"]]
    # 저장하지 않는다
    assert client.get(url.removesuffix("/suggestion"), headers=HEADERS).status_code == 404


def test_api_scope_hints_from_the_graph_turn_the_card_into_a_question(client: TestClient) -> None:
    url = _contact_with_history(client, MINE)  # 변화 뒤 3건
    plain = client.get(url, headers=HEADERS).json()
    # 그래프 없이: 묻지 않고, 말투 프로필의 값이 제안 설정에 들어간다
    assert plain["card"]["ask"] is False and plain["card"]["speech_current"]["count"] == 3
    assert ("formality", 2, "messages") in [(s["field"], s["value"], s["source"]) for s in plain["signals"]]
    assert plain["settings"]["formality"] == 4 and plain["settings"]["address_term"] == "서연 파트장님"

    client.app.state.container.graph_store = _c3_graph()  # type: ignore[attr-defined]
    body = client.get(url, headers=HEADERS).json()
    card = body["card"]
    assert card["trigger"] == {"event": "파트장 발령", "at": "2026-10-17", "message_ids": ["c3:m05"]}
    assert card["scope_hints"] == [{"text": AGREEMENT_FACT, "message_ids": ["c3:m06", "c3:m07"]}]
    assert card["ask"] is True and card["ask_reason"] == SCOPE_ASK_REASON
    # 카드가 물으므로 값은 제안 설정에서 빠지고, 근거와 카드의 `proposed`에는 남는다
    assert (
        body["settings"]["formality"] == 3
    )  # 지금 설정 그대로 (겉모습 추정의 2도, 프로필의 4도 넣지 않는다)
    assert body["settings"]["address_term"] is None
    assert card["proposed"] == {"formality": 4, "address_term": "서연 파트장님"}
    assert body["signals"] == plain["signals"]


def test_api_without_register_suggestions_is_the_old_response() -> None:
    client = _client(register_suggestions=False)
    url = _contact_with_history(client, MINE)
    body = client.get(url, headers=HEADERS).json()
    assert set(body) == {"based_on_messages", "settings", "signals"}  # card 없음(null)
    assert body["signals"] and all(s["source"] == "messages" for s in body["signals"])
    # 켠 서버의 메시지 근거와 같다 — 말투 프로필은 뒤에 붙을 뿐 앞의 근거를 바꾸지 않는다
    on = _client()
    with_register = on.get(_contact_with_history(on, MINE), headers=HEADERS).json()
    assert [s for s in with_register["signals"] if s["source"] == "messages"] == body["signals"]


def test_api_card_survives_a_graph_failure_and_a_missing_contact_node(client: TestClient) -> None:
    url = _contact_with_history(client, MINE)
    plain = client.get(url, headers=HEADERS).json()

    class Down(FakeGraphStore):
        async def neighborhood(self, group_id, node_uuid):  # noqa: ANN001, ANN201
            raise RuntimeError("neo4j down")

    container = client.app.state.container  # type: ignore[attr-defined]
    for store in (Down(entities={"이서연": PEER}), _c3_graph(entities={})):  # 조회 실패 / 상대 노드 없음
        container.graph_store = store
        assert client.get(url, headers=HEADERS).json() == plain
    assert plain["card"]["trigger"] is None


def test_api_has_no_message_count_threshold_for_the_register_card(client: TestClient) -> None:
    """설계 3.2: 건수 문턱을 두지 않고 건수를 보여 준다. 내 메시지 4건(반말 1 → 합쇼체 3)도 카드가 나온다.

    겉모습 추정(messages)은 예전처럼 5건 미만이면 말하지 않는다.
    """
    url = _contact_with_history(client, MINE[3:])
    body = client.get(url, headers=HEADERS).json()
    assert body["based_on_messages"] == 4
    assert body["signals"] and {s["source"] for s in body["signals"]} == {"register"}
    card = body["card"]
    assert (card["speech_current"]["level"], card["speech_current"]["count"]) == ("hapsyo", 3)
    assert card["address"]["term"] == "서연 파트장님" and card["address"]["count"] == 1
    # 구간 규칙(연속 2건)은 그대로다: 내 메시지 1건으로는 말투를 제안하지 않는다
    one = _client()
    single = one.get(_contact_with_history(one, MINE[6:]), headers=HEADERS).json()
    assert single == {"based_on_messages": 1, "settings": None, "signals": []}
    # 말투 프로필을 끈 서버는 예전 응답 그대로다 (표본이 작아 추정하지 않는다)
    off = _client(register_suggestions=False)
    assert off.get(_contact_with_history(off, MINE[3:]), headers=HEADERS).json() == {
        "based_on_messages": 4,
        "settings": None,
        "signals": [],
    }


def test_two_messages_before_and_after_a_change_still_give_a_card() -> None:
    """반말 2건 → 합쇼체 2건: 구간 규칙은 충족하지만 총 4건이다. 문턱이 있던 때는 카드가 없었다."""
    client = _client()
    mine = [
        ("m02", "2026-10-07T12:11:00+09:00", "오늘 좀 늦을 것 같아."),
        ("m04", "2026-10-07T12:15:00+09:00", "먼저 먹어"),
        ("m08", "2026-10-21T10:05:00+09:00", "회의록 공유드립니다."),
        ("m10", "2026-10-21T10:22:00+09:00", "금요일 오전까지 정리하겠습니다."),
    ]
    card = client.get(_contact_with_history(client, mine), headers=HEADERS).json()["card"]
    assert (card["speech_previous"]["level"], card["speech_previous"]["count"]) == ("hae", 2)
    assert (card["speech_current"]["level"], card["speech_current"]["count"]) == ("hapsyo", 2)
    assert card["ask"] is True  # 바뀐 뒤 2건뿐이라 값을 정해 주지 않고 묻는다


def test_suggestion_service_passes_k_to_the_profile() -> None:
    """K 민감도 보고(설계 3.2)용 인자: K=1이면 존댓말 한 건으로 구간이 열린다."""
    import asyncio

    from app.services.suggestion_service import suggest_persona_for_contact

    client = _client()
    url = _contact_with_history(client, MINE[:5])  # 반말 4건 뒤 합쇼체 1건(m08)
    state = client.app.state.container  # type: ignore[attr-defined]

    async def card_for(k: int) -> dict:
        contact = (await state.contacts.list("user-a"))[0]
        s = await suggest_persona_for_contact(state, "user-a", contact, sources=frozenset({"register"}), k=k)
        assert s.card is not None
        return s.card.speech_current

    assert url
    assert asyncio.run(card_for(2))["level"] == "hae"
    assert asyncio.run(card_for(1))["level"] == "hapsyo"


# ── 확정된 호칭 뒤의 1회 후보, 옛 구간 ─────────────────────────────────────────────────────────


def test_one_off_address_after_a_confirmed_one_is_proposed_as_a_candidate() -> None:
    profile = _profile(
        [_seg("hapsyo", 5, ["c1:m01", "c1:m03", "c1:m05"])],
        [_seg("팀장님", 5, ["c1:m01", "c1:m05"])],
        senior=True,
        candidate=_seg("지현 팀장님", 6, ["c1:m07"], "candidate"),
    )
    base = PersonaSettings(formality=4)
    [signal] = register_signals(profile, base)
    assert (signal.field, signal.value) == ("address_term", "지현 팀장님")
    assert "1회 관찰" in signal.reason and "그 전에는 '팀장님'" in signal.reason
    card = build_card(profile, base, "박지현")
    assert (card.address["term"], card.address["status"], card.address["count"]) == (
        "지현 팀장님",
        "candidate",
        1,
    )
    # 후보가 이미 설정에 있으면 확정된 예전 호칭을 대신 내밀지 않는다
    saved = PersonaSettings(formality=4, address_term="지현 팀장님")
    assert register_signals(profile, saved) == []


def test_a_speech_segment_missing_from_recent_messages_proposes_nothing() -> None:
    """현재 구간이 '후보'면(최근 메시지에 없음) 격식을 제안하지도, 지금 말투로 보여 주지도 않는다."""
    stale = _profile([_seg("hae", 1, ["c1:m00", "c1:m01"], "candidate")])
    base = PersonaSettings(formality=4)
    assert register_signals(stale, base) == []
    card = build_card(stale, base, "홍길동")
    assert card.speech_current is None and card.proposed["formality"] is None
    assert has_content(card) is False
    # 같은 구간이 확정이면 격식 1을 제안한다 (대조)
    fresh = _profile([_seg("hae", 1, ["c1:m00", "c1:m01"])])
    assert [s.value for s in register_signals(fresh, base)] == [1]
