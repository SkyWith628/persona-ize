"""질의 검색의 무효화 처리 방식 세 가지 (`GraphContextRetriever(invalidation_mode=...)`, 재설계 4.4).

손으로 만든 사실 묶음 하나를 세 방식으로 읽어 본다. Neo4j·LLM 없이 `FakeGraphStore`로 돈다.
- trust(기본): 무효화된 사실은 모두 버린다 — 지금까지의 동작.
- artifacts: 1시간 안에 무효화됐거나, 같은 대화·같은 날(KST)의 메시지가 무효화한 사실만 되살린다.
- ignore: invalid_at을 보지 않는다.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.graph.retriever import (
    ARTIFACT_WINDOW,
    GraphContextRetriever,
    _same_day_same_conversation,
    _within_artifact_window,
)
from app.graph.store import EpisodeRef, GraphEdgeHit, GraphNeighborhood
from app.repositories.memory import InMemoryContactRepo
from tests.test_graph import FakeGraphStore

pytestmark = pytest.mark.anyio


def _t(day: int, hour: int = 0, minute: int = 0) -> datetime:
    """2026년 10월의 UTC 시각. KST로는 9시간 뒤다 (00:25 UTC = 09:25 KST)."""
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def _edge(
    uuid: str, episodes: list[str], valid_at: datetime | None, invalid_at: datetime | None = None
) -> GraphEdgeHit:
    # 이름을 Involved로 둬서 관계 상태 슬롯(Relationship만 읽는다)에는 들어가지 않게 한다
    return GraphEdgeHit(
        uuid, "Involved", f"사실 {uuid}", valid_at, invalid_at, episodes, "node-me", "node-boss"
    )


async def _setup() -> tuple[InMemoryContactRepo, object, FakeGraphStore]:
    contacts = InMemoryContactRepo()
    boss = await contacts.create("u", "박지현", "superior")
    store = FakeGraphStore(
        entities={"박지현": "node-boss"},
        refs={
            "e1": EpisodeRef("e1", "c1:m02", _t(5, 0, 25)),
            "e2": EpisodeRef("e2", "c1:m03", _t(5, 0, 31)),
            "e3": EpisodeRef("e3", "c1:m04", _t(5, 0, 40)),
            "e4": EpisodeRef("e4", "c1:m05", _t(5, 3, 0)),  # 같은 대화, 같은 날 낮 12시(KST)
            "e5": EpisodeRef("e5", "c2:m01", _t(5, 4, 0)),  # 다른 대화, 같은 날
            "e6": EpisodeRef("e6", "c1:m06", _t(5, 5, 0)),
            "e7": EpisodeRef("e7", "c1:m07", _t(5, 6, 0)),
            "e8": EpisodeRef("e8", "c1:m09", _t(14)),  # 아흐레 뒤
        },
        edges=[
            # 6분 뒤 다음 메시지가 덮어썼다 — 1시간 안의 추출 부산물
            _edge("hour", ["e1"], _t(5, 0, 25), invalid_at=_t(5, 0, 31)),
            # 2시간 반 뒤, 같은 대화·같은 날의 메시지(c1:m05)가 무효화했다 — 같은 날 부산물
            _edge("sameday", ["e2"], _t(5, 0, 31), invalid_at=_t(5, 3, 0)),
            # 같은 날이지만 다른 대화(c2:m01)가 무효화했다 — 부산물로 보지 않는다
            _edge("otherconv", ["e3"], _t(5, 0, 40), invalid_at=_t(5, 4, 0)),
            # 아흐레 뒤 같은 대화의 메시지(c1:m09)가 대체했다 — 실제로 바뀐 사실
            _edge("superseded", ["e6"], _t(5, 5, 0), invalid_at=_t(14)),
            # 같은 날 무효화됐지만 그 시각에 말한 메시지가 없다(모델이 지어낸 시각) — 알 수 없으면 믿는다
            _edge("unknown", ["e7"], _t(5, 6, 0), invalid_at=_t(5, 9, 10)),
            _edge("live-c1", ["e4"], _t(5, 3, 0)),
            _edge("live-c2", ["e5"], _t(5, 4, 0)),
            _edge("live-new", ["e8"], _t(14)),
        ],
    )
    return contacts, boss, store


async def _ids(
    contacts: InMemoryContactRepo,
    boss: object,
    store: FakeGraphStore,
    as_of: datetime | None = None,
    **kwargs: object,
) -> list[str]:
    r = GraphContextRetriever(store, contacts, top_k=10, **kwargs)  # type: ignore[arg-type]
    got = await r.retrieve(
        user_id="u",
        contact_id=boss.id,  # type: ignore[attr-defined]
        draft_text="자료 보내드립니다",
        situation="보고",
        as_of=as_of or _t(20),
    )
    return [e.id for e in got]


async def test_trust_is_the_default_and_drops_every_invalidated_fact() -> None:
    contacts, boss, store = await _setup()
    live = ["live-c1", "live-c2", "live-new"]
    assert await _ids(contacts, boss, store) == live
    assert await _ids(contacts, boss, store, invalidation_mode="trust") == live


async def test_artifacts_restores_only_extraction_artefacts() -> None:
    contacts, boss, store = await _setup()
    got = await _ids(contacts, boss, store, invalidation_mode="artifacts")
    # 1시간 안(hour)과 같은 대화·같은 날(sameday)만 돌아온다.
    # 다른 대화가 무효화한 것, 며칠 뒤 대체된 것, 무효화한 메시지를 모르는 것은 계속 걸러진다
    assert got == ["hour", "sameday", "live-c1", "live-c2", "live-new"]


async def test_ignore_keeps_everything() -> None:
    contacts, boss, store = await _setup()
    got = await _ids(contacts, boss, store, invalidation_mode="ignore")
    assert got == ["hour", "sameday", "otherconv", "superseded", "unknown", "live-c1", "live-c2", "live-new"]


async def test_modes_respect_as_of() -> None:
    contacts, boss, store = await _setup()
    # 10/5 02:00(UTC) 시점: sameday·otherconv는 아직 무효화 전이라 어느 방식에서도 남는다.
    # hour는 이미 무효화됐다(00:31). 02:00 이후 메시지에서 나온 사실은 어느 방식에서도 쓰지 않는다
    as_of = _t(5, 2, 0)
    assert await _ids(contacts, boss, store, as_of=as_of) == ["sameday", "otherconv"]
    restored = ["hour", "sameday", "otherconv"]
    assert await _ids(contacts, boss, store, as_of=as_of, invalidation_mode="artifacts") == restored
    assert await _ids(contacts, boss, store, as_of=as_of, invalidation_mode="ignore") == restored
    # 10/5 03:30 시점: sameday가 무효화됐다(03:00). 무효화한 c1:m05는 그 전에 말한 메시지라 찾을 수 있다
    as_of = _t(5, 3, 30)
    assert await _ids(contacts, boss, store, as_of=as_of) == ["otherconv", "live-c1"]
    assert await _ids(contacts, boss, store, as_of=as_of, invalidation_mode="artifacts") == [
        "hour",
        "sameday",
        "otherconv",
        "live-c1",
    ]


async def test_artifacts_falls_back_to_the_window_when_neighborhood_fails() -> None:
    contacts, boss, store = await _setup()
    # 검색 결과에는 무효화된 사실 둘만 온다 — 무효화한 메시지(c1:m05)는 이웃 조회로만 찾을 수 있다
    hits = [e for e in store.edges if e.uuid in ("hour", "sameday")]

    async def search(query, group_id, center_node_uuid, limit) -> list[GraphEdgeHit]:  # noqa: ANN001
        return list(hits)

    store.search = search  # type: ignore[method-assign]
    assert await _ids(contacts, boss, store, invalidation_mode="artifacts", state_slots=False) == [
        "hour",
        "sameday",
    ]

    async def broken(group_id, node_uuid) -> GraphNeighborhood:  # noqa: ANN001
        raise RuntimeError("neo4j down")

    store.neighborhood = broken  # type: ignore[method-assign]
    # 이웃 조회가 실패하면 같은 날 규칙은 판단할 수 없다 — 1시간 규칙만 남고, 검색은 계속된다
    assert await _ids(contacts, boss, store, invalidation_mode="artifacts", state_slots=False) == ["hour"]


async def test_only_artifacts_mode_reads_the_neighborhood_for_invalidation() -> None:
    contacts, boss, store = await _setup()
    calls = 0
    original = store.neighborhood

    async def counting(group_id, node_uuid) -> GraphNeighborhood:  # noqa: ANN001
        nonlocal calls
        calls += 1
        return await original(group_id, node_uuid)

    store.neighborhood = counting  # type: ignore[method-assign]
    # 기본(trust)과 ignore는 그래프를 더 읽지 않는다 (관계 상태 슬롯을 끄면 이웃 조회가 아예 없다)
    await _ids(contacts, boss, store, state_slots=False)
    await _ids(contacts, boss, store, invalidation_mode="ignore", state_slots=False)
    assert calls == 0
    # artifacts는 1시간 규칙으로 안 풀리는 사실이 있을 때 한 번만 읽는다
    await _ids(contacts, boss, store, invalidation_mode="artifacts", state_slots=False)
    assert calls == 1


async def test_relationship_state_slots_are_unaffected_by_the_mode() -> None:
    contacts, boss, store = await _setup()
    # 며칠 뒤 무효화된 관계 사실: 상태 슬롯은 원래 무효화를 보지 않으므로 어느 방식에서도 맨 앞에 온다
    state = GraphEdgeHit(
        "rel",
        "Relationship",
        "김민수가 박지현을 팀장님이라 부른다",
        _t(6),
        _t(12),
        [],
        "node-me",
        "node-boss",
    )
    store.edges.append(state)
    for mode in ("trust", "artifacts", "ignore"):
        got = await _ids(contacts, boss, store, invalidation_mode=mode)
        assert got[0] == "rel" and got.count("rel") == 1


async def test_version_string_distinguishes_modes() -> None:
    contacts, _, store = await _setup()
    assert GraphContextRetriever.version == "graph-context-v3"
    assert GraphContextRetriever(store, contacts).version == "graph-context-v3"
    assert GraphContextRetriever(store, contacts, invalidation_mode="trust").version == "graph-context-v3"
    artifacts = GraphContextRetriever(store, contacts, invalidation_mode="artifacts")
    ignore = GraphContextRetriever(store, contacts, invalidation_mode="ignore")
    assert artifacts.version == "graph-context-v3+inv-artifacts"
    assert ignore.version == "graph-context-v3+inv-ignore"
    # 문체 검색기 버전이 앞에 붙어도 DB 열(200자)에 넉넉히 들어가야 한다 (CLAUDE.md 함정)
    assert max(len(artifacts.version), len(ignore.version)) <= 64


async def test_unknown_mode_is_rejected() -> None:
    contacts, _, store = await _setup()
    with pytest.raises(ValueError, match="invalidation_mode"):
        GraphContextRetriever(store, contacts, invalidation_mode="artefacts")  # type: ignore[arg-type]


def test_artifact_window_boundaries() -> None:
    ep = [EpisodeRef("e", "c1:m01", _t(5, 0, 0))]
    assert ARTIFACT_WINDOW == timedelta(hours=1)
    assert _within_artifact_window(_edge("a", ["e"], _t(5), _t(5, 1, 0)), ep)  # 정확히 1시간은 포함
    assert not _within_artifact_window(_edge("b", ["e"], _t(5), _t(5, 1, 1)), ep)
    # 유효 시각이 없으면 가장 이른 출처 메시지의 시각으로 잰다
    assert _within_artifact_window(_edge("c", ["e"], None, _t(5, 0, 30)), ep)
    assert not _within_artifact_window(_edge("d", [], None, _t(5, 0, 30)), [])
    # invalid_at이 유효 시각보다 이르면(시간이 거꾸로) 부산물로 단정하지 않는다
    assert not _within_artifact_window(_edge("e", ["e"], _t(5, 2), _t(5, 1, 30)), ep)


def test_same_day_is_judged_in_kst() -> None:
    ep = [EpisodeRef("e", "c1:m01", _t(5, 14, 0))]
    # 10/5 14:00 UTC = 10/5 23:00 KST, 10/5 16:00 UTC = 10/6 01:00 KST — UTC로는 같은 날, KST로는 다른 날
    assert not _same_day_same_conversation(_edge("a", ["e"], _t(5, 14), _t(5, 16)), ep, {"c1:m02"})
    # 10/5 16:00 UTC와 10/6 02:00 UTC는 둘 다 10/6 KST
    late = [EpisodeRef("e", "c1:m01", _t(5, 16, 0))]
    assert _same_day_same_conversation(_edge("b", ["e"], _t(5, 16), _t(6, 2)), late, {"c1:m02"})
    assert not _same_day_same_conversation(_edge("c", ["e"], _t(5, 16), _t(6, 2)), late, {"c2:m02"})
    assert not _same_day_same_conversation(_edge("d", ["e"], _t(5, 16), _t(6, 2)), late, set())
