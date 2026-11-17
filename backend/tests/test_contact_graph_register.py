"""상대 주변 그래프 조회의 "말투 간선" (`GET /v1/contacts/{id}/graph`의 `register`, 설계 3.2).

말투 프로필은 그래프에 저장된 값이 아니라 메시지에서 읽을 때 계산한다 — 그래서 그래프가 꺼져 있어도 오고,
계산이 실패하면 그 항목만 빠진 예전 응답이 나가야 한다. 메모리 저장소 + FakeGraphStore로 본다
(c3: 동기가 파트장이 된 대화, `test_register_suggest.py`와 같은 메시지·그래프).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.ai import register as register_module
from app.core.config import Settings
from app.main import create_app
from app.services import register_view as register_view_module
from app.services import suggestion_service as suggestion_module
from tests.test_graph import FakeGraphStore
from tests.test_register_suggest import MINE, _c3_graph, _profile, _seg

HEADERS = {"X-User-Id": "user-a"}


@pytest.fixture
def client() -> TestClient:
    settings = Settings(_env_file=None, llm_provider="fake", embedding_provider="fake")  # type: ignore[call-arg]
    return TestClient(create_app(settings))


def _contact(client: TestClient, messages: list[dict]) -> str:
    """상대 '이서연'과 대화방 c3를 만들고 메시지를 넣는다. 그래프 조회 주소를 돌려준다."""
    contact = client.post(
        "/v1/contacts", json={"alias": "이서연", "relationship_type": "colleague"}, headers=HEADERS
    ).json()
    conv = client.post(
        "/v1/conversations",
        json={"contact_id": contact["id"], "title": "방", "source_key": "c3", "me_name": "김민수"},
        headers=HEADERS,
    )
    assert conv.status_code == 201, conv.text
    if messages:
        r = client.post(
            f"/v1/conversations/{conv.json()['id']}/imports", json={"messages": messages}, headers=HEADERS
        )
        assert r.status_code in (200, 201, 202), r.text
    return f"/v1/contacts/{contact['id']}/graph"


def _mine(rows: list[tuple[str, str, str]]) -> list[dict]:
    return [{"id": key, "speaker": "me", "sent_at": at, "text": text} for key, at, text in rows]


def test_register_has_two_segments_when_speech_changed_and_graph_is_off(client: TestClient) -> None:
    url = _contact(client, _mine(MINE[1:6]))  # c3 그대로: 반말 3건 → 10/21부터 합쇼체 2건
    body = client.get(url, headers=HEADERS).json()

    # 그래프가 꺼져 있어도 말투는 온다. 기존 필드는 그대로다.
    assert body["enabled"] is False and body["center_id"] is None
    assert body["nodes"] == [] and body["edges"] == []
    assert list(body) == ["enabled", "center_id", "nodes", "edges", "register"]

    register = body["register"]
    assert register["based_on_messages"] == 5
    assert register["segments"] == [
        {
            "level": "hae",
            "start": "2026-10-07",
            "end": "2026-10-21",
            "count": 3,
            "status": "previous",
            "message_ids": ["c3:m02", "c3:m04", "c3:m06"],
            "trigger": None,
        },
        {
            "level": "hapsyo",
            "start": "2026-10-21",
            "end": None,
            "count": 2,
            "status": "confirmed",
            "message_ids": ["c3:m08", "c3:m10"],
            "trigger": None,
        },
    ]
    assert register["address"] == {"term": "서연 파트장님", "count": 1, "status": "candidate"}


def test_register_is_absent_without_my_messages(client: TestClient) -> None:
    theirs = [
        {"id": "m01", "speaker": "contact", "sent_at": "2026-10-07T12:10:00+09:00", "text": "점심 먹자"}
    ]
    url = _contact(client, theirs)

    off = client.get(url, headers=HEADERS).json()
    assert off == {"enabled": False, "center_id": None, "nodes": [], "edges": []}  # 예전 응답과 글자까지 같다
    assert off.get("register") is None

    client.app.state.container.graph_store = FakeGraphStore()  # type: ignore[attr-defined]
    assert client.get(url, headers=HEADERS).json() == {
        "enabled": True,
        "center_id": None,
        "nodes": [],
        "edges": [],
    }


def test_trigger_event_is_attached_to_the_confirmed_current_segment(client: TestClient) -> None:
    url = _contact(client, _mine(MINE))
    client.app.state.container.graph_store = _c3_graph()  # type: ignore[attr-defined]
    body = client.get(url, headers=HEADERS).json()

    assert body["enabled"] is True and body["center_id"] == "n-peer"
    assert {e["id"] for e in body["edges"]} == {"e-lunch", "e-peer", "e-promo", "e-agree", "e-notes"}
    previous, current = body["register"]["segments"]
    assert previous["level"] == "hae" and previous["trigger"] is None  # 옛 구간에는 붙이지 않는다
    assert (current["level"], current["start"], current["count"]) == ("hapsyo", "2026-10-21", 3)
    # 변화 시점(10/21) 이전에서 가장 가까운 사건. 그날 내가 말한 '회의록 공유'는 결과라 계기가 아니다.
    assert current["trigger"] == {"event": "파트장 발령", "at": "2026-10-17"}


def test_no_trigger_when_the_contact_node_is_not_in_the_graph_yet(client: TestClient) -> None:
    url = _contact(client, _mine(MINE))
    client.app.state.container.graph_store = _c3_graph(entities={})  # type: ignore[attr-defined]
    body = client.get(url, headers=HEADERS).json()

    assert body["enabled"] is True and body["center_id"] is None and body["edges"] == []
    assert [s["level"] for s in body["register"]["segments"]] == ["hae", "hapsyo"]
    assert all(s["trigger"] is None for s in body["register"]["segments"])


def test_segment_evidence_is_capped_at_five_recent_messages(client: TestClient) -> None:
    text = "오늘 회의 자료 공유드립니다."
    rows = [(f"m{i:02d}", f"2026-10-{i:02d}T09:00:00+09:00", text) for i in range(1, 9)]
    body = client.get(_contact(client, _mine(rows)), headers=HEADERS).json()

    (only,) = body["register"]["segments"]
    assert only["level"] == "hapsyo" and only["count"] == 8 and only["end"] is None
    assert only["message_ids"] == [f"c3:m{i:02d}" for i in range(4, 9)]


def test_profile_failure_returns_the_old_response(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = _contact(client, _mine(MINE))
    client.app.state.container.graph_store = _c3_graph()  # type: ignore[attr-defined]
    with_register = client.get(url, headers=HEADERS).json()
    assert with_register["register"] is not None

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("대화 원문이 섞인 오류 문장")

    monkeypatch.setattr(register_view_module, "build_profile", boom)
    r = client.get(url, headers=HEADERS)

    assert r.status_code == 200
    body = r.json()
    assert "register" not in body
    assert body == {k: v for k, v in with_register.items() if k != "register"}  # 기존 필드는 값·순서 그대로


def test_trigger_lookup_failure_keeps_the_segments(client: TestClient) -> None:
    class BrokenEpisodeRefs(FakeGraphStore):
        """그래프 화면의 이웃 조회는 되고, 계기 조회만 쓰는 에피소드 조회가 실패한다."""

        async def episode_refs(self, uuids):  # noqa: ANN001, ANN201
            raise RuntimeError("neo4j down")

    url = _contact(client, _mine(MINE))
    base = _c3_graph()
    client.app.state.container.graph_store = BrokenEpisodeRefs(  # type: ignore[attr-defined]
        entities=base.entities, nodes=base.nodes, events=base.events, edges=base.edges, refs=base.refs
    )
    body = client.get(url, headers=HEADERS).json()

    assert len(body["edges"]) == 5
    assert [s["level"] for s in body["register"]["segments"]] == ["hae", "hapsyo"]
    assert all(s["trigger"] is None for s in body["register"]["segments"])


def test_graph_view_reads_the_neighborhood_once(client: TestClient) -> None:
    """계기 조회는 그래프 화면이 이미 읽은 이웃을 받아 쓴다 — 한 요청에 같은 질의를 두 번 돌리지 않는다."""

    class Counting(FakeGraphStore):
        calls = 0

        async def neighborhood(self, group_id, node_uuid):  # noqa: ANN001, ANN201
            self.calls += 1
            return await super().neighborhood(group_id, node_uuid)

    url = _contact(client, _mine(MINE))
    base = _c3_graph()
    store = Counting(
        entities=base.entities, nodes=base.nodes, events=base.events, edges=base.edges, refs=base.refs
    )
    client.app.state.container.graph_store = store  # type: ignore[attr-defined]
    body = client.get(url, headers=HEADERS).json()

    assert store.calls == 1
    # 넘겨받은 이웃으로도 계기는 예전과 같이 찾는다
    assert body["register"]["segments"][-1]["trigger"] == {"event": "파트장 발령", "at": "2026-10-17"}


def test_register_is_off_when_register_suggestions_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """말투 프로필을 끈 서버(REGISTER_SUGGESTIONS=false)는 그래프 조회도 예전 응답 그대로다(계산도 안 함)."""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, llm_provider="fake", embedding_provider="fake", register_suggestions=False
    )
    off = TestClient(create_app(settings))
    url = _contact(off, _mine(MINE))

    def must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("말투 프로필을 끈 서버가 프로필을 계산했다")

    monkeypatch.setattr(register_view_module, "build_profile", must_not_run)
    monkeypatch.setattr(register_view_module, "own_messages", must_not_run)

    assert off.get(url, headers=HEADERS).json() == {  # 그래프도 꺼진 서버: 예전 응답과 글자까지 같다
        "enabled": False,
        "center_id": None,
        "nodes": [],
        "edges": [],
    }
    off.app.state.container.graph_store = _c3_graph()  # type: ignore[attr-defined]
    body = off.get(url, headers=HEADERS).json()
    assert "register" not in body and len(body["edges"]) == 5


def test_changed_segment_is_the_confirmed_current_segment_after_another_one() -> None:
    before, now = _seg("hae", 10, ["c3:m02", "c3:m04"], status="previous"), _seg("hapsyo", 21, ["c3:m08"])
    assert register_module.changed_segment(_profile([before, now])) is now
    assert register_module.changed_segment(_profile([now])) is None  # 바뀐 적이 없다
    assert register_module.changed_segment(_profile([])) is None
    stale = _seg("hapsyo", 21, ["c3:m08"], status="candidate")  # 최근 메시지에 없는 옛 구간
    assert register_module.changed_segment(_profile([before, stale])) is None


def test_suggestion_card_and_graph_view_share_the_changed_segment_rule(client: TestClient) -> None:
    """제안 카드와 그래프 화면이 계기를 찾을 구간을 같은 함수로 정한다 — 복사본이 생기면 여기서 걸린다."""
    assert suggestion_module.changed_segment is register_module.changed_segment
    assert register_view_module.changed_segment is register_module.changed_segment

    url = _contact(client, _mine(MINE))
    client.app.state.container.graph_store = _c3_graph()  # type: ignore[attr-defined]
    current = client.get(url, headers=HEADERS).json()["register"]["segments"][-1]
    card = client.get(url.replace("/graph", "/persona/suggestion"), headers=HEADERS).json()["card"]
    # 같은 상대에 대해 두 화면이 같은 구간(시작일)과 같은 계기를 말한다
    assert card["speech_current"]["since"] == current["start"]
    assert (card["trigger"]["event"], card["trigger"]["at"]) == (
        current["trigger"]["event"],
        current["trigger"]["at"],
    )
