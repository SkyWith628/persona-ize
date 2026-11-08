"""그래프 수집·검색 규칙 (Neo4j·LLM 없이 FakeGraphStore로).

- 수집: 미수집 메시지만, 전체 시각 순, 본문 형식, 재실행 0건, 작업 graph_* 상태.
- 검색: 상대 노드 중심, as_of 이후 출처는 제외, 무효화된 사실 제외, source_message_ids는 에피소드 이름.
- D = C + 그래프: 한쪽이 죽어도 나머지는 돌려준다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from app.ai.embeddings import FakeEmbedder
from app.ai.retrieval.composite import CompositeRetriever
from app.ai.retrieval.vector import VectorStyleRetriever
from app.graph.ingest import GraphIngestService
from app.graph.retriever import GraphContextRetriever
from app.graph.store import EpisodeRef, GraphEdgeHit
from app.repositories.memory import InMemoryContactRepo, InMemoryConversationRepo
from app.services.import_service import ImportMessage, ImportPayload, ImportService

pytestmark = pytest.mark.anyio


@dataclass
class FakeGraphStore:
    added: list[dict] = field(default_factory=list)
    prepared: int = 0
    edges: list[GraphEdgeHit] = field(default_factory=list)
    refs: dict[str, EpisodeRef] = field(default_factory=dict)
    entities: dict[str, str] = field(default_factory=dict)  # name → uuid
    events: dict[str, set[str]] = field(default_factory=dict)  # node uuid → 이웃 Event uuid
    searches: list[tuple[str, str, str | None, int]] = field(default_factory=list)
    fail_on: str | None = None  # 이 이름의 에피소드에서 예외

    async def prepare(self) -> None:
        self.prepared += 1

    async def add_episode(self, *, name, body, reference_time, group_id, source_description) -> None:  # noqa: ANN001
        if name == self.fail_on:
            raise RuntimeError("boom")
        self.added.append(
            {"name": name, "body": body, "reference_time": reference_time, "group_id": group_id}
        )

    async def search(self, query, group_id, center_node_uuid, limit) -> list[GraphEdgeHit]:  # noqa: ANN001
        self.searches.append((query, group_id, center_node_uuid, limit))
        return list(self.edges)

    async def find_entity_uuid(self, group_id, name) -> str | None:  # noqa: ANN001
        return self.entities.get(name)

    async def event_neighbors(self, group_id, node_uuid) -> set[str]:  # noqa: ANN001
        return set(self.events.get(node_uuid, set()))

    async def episode_refs(self, uuids) -> dict[str, EpisodeRef]:  # noqa: ANN001
        return {u: self.refs[u] for u in uuids if u in self.refs}

    async def remove_episodes(self, group_id, names) -> int:  # noqa: ANN001
        before = len(self.added)
        self.added = [a for a in self.added if not (a["group_id"] == group_id and a["name"] in names)]
        return before - len(self.added)

    async def close(self) -> None:
        pass


MESSAGES_C1 = [
    ImportMessage(
        id="m01", speaker="contact", sent_at="2026-10-05T09:25:00+09:00", text="언제까지 가능해요?"
    ),
    ImportMessage(
        id="m02", speaker="me", sent_at="2026-10-05T09:31:00+09:00", text="다음 주 수요일까지는 가능합니다."
    ),
]
MESSAGES_C2 = [
    ImportMessage(id="m01", speaker="me", sent_at="2026-10-05T09:28:00+09:00", text="야 토요일에 국밥 ㄱ?"),
]


async def _setup(graph_mode: str = "off"):  # noqa: ANN202
    contacts = InMemoryContactRepo()
    conversations = InMemoryConversationRepo()
    store = FakeGraphStore()
    ingest = GraphIngestService(store, conversations, contacts)
    svc = ImportService(
        contacts,
        conversations,
        FakeEmbedder(dim=32),
        graph_ingest=ingest,
        graph_mode=graph_mode,  # type: ignore[arg-type]
    )
    boss = await contacts.create("u", "박지현", "superior")
    bro = await contacts.create("u", "준호", "family")
    c1 = await svc.create_conversation("u", boss.id, "팀장 방", "json", "c1", me_name="김민수")
    c2 = await svc.create_conversation("u", bro.id, None, "json", "c2", me_name="김민수")
    return contacts, conversations, store, ingest, svc, boss, bro, c1, c2


async def test_ingest_pending_is_time_ordered_idempotent_and_formats_body() -> None:
    _, conversations, store, ingest, svc, _, _, c1, c2 = await _setup()
    await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))
    await svc.import_messages("u", c2.id, ImportPayload(messages=MESSAGES_C2))

    assert await ingest.ingest_pending("u") == 3
    assert store.prepared == 1
    # 대화방을 가로질러 발화 시각 순: c1:m01(09:25) → c2:m01(09:28) → c1:m02(09:31)
    assert [a["name"] for a in store.added] == ["c1:m01", "c2:m01", "c1:m02"]
    assert all(a["group_id"] == "u" for a in store.added)
    assert store.added[0]["body"] == "[2026-10-05 월 09:25] 박지현 → 김민수: 언제까지 가능해요?"
    assert (
        store.added[2]["body"]
        == "[2026-10-05 월 09:31] 김민수 → 박지현: 다음 주 수요일(=2026-10-14)까지는 가능합니다."
    )
    # 앵커 없는 요일은 두 후보 (스파이크 6차 규칙)
    assert store.added[1]["body"].startswith(
        "[2026-10-05 월 09:28] 김민수 → 준호: 야 토요일(=2026-10-10 또는 2026-10-17)에"
    )

    # 전부 표시됐으니 다시 돌리면 0
    assert all(m.graph_ingested_at is not None for m in await conversations.list_messages("u", c1.id))
    assert await ingest.ingest_pending("u") == 0
    assert len(store.added) == 3


async def test_set_me_name_changes_future_episode_bodies() -> None:
    _, conversations, store, ingest, svc, boss, _, _, _ = await _setup()
    c3 = await svc.create_conversation("u", boss.id, None, "json", "c3")  # me_name 없음 → '사용자'
    await svc.import_messages("u", c3.id, ImportPayload(messages=MESSAGES_C1[:1]))
    await ingest.ingest_conversation("u", c3.id)
    assert store.added[-1]["body"].endswith("박지현 → 사용자: 언제까지 가능해요?")
    assert (await conversations.set_me_name("u", c3.id, "김민수")).me_name == "김민수"
    await svc.import_messages("u", c3.id, ImportPayload(messages=MESSAGES_C1))  # m02 추가
    await ingest.ingest_conversation("u", c3.id)
    assert store.added[-1]["body"].startswith("[2026-10-05 월 09:31] 김민수 → 박지현:")


async def test_ingest_failure_keeps_progress_and_marks_job() -> None:
    _, conversations, store, ingest, svc, _, _, c1, _ = await _setup()
    job = await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))
    store.fail_on = "c1:m02"
    with pytest.raises(RuntimeError):
        await ingest.ingest_conversation("u", c1.id, job.id)
    failed = await conversations.get_job("u", job.id)
    assert (failed.graph_status, failed.graph_episodes) == ("failed", 1)
    assert "boom" in (failed.graph_error or "")
    # 첫 메시지 표시는 남아 재시도는 둘째부터
    store.fail_on = None
    assert await ingest.ingest_conversation("u", c1.id, job.id) == 1
    assert (await conversations.get_job("u", job.id)).graph_status == "completed"


async def test_import_sync_graph_mode_fills_job_graph_fields() -> None:
    _, _, store, _, svc, _, _, c1, _ = await _setup(graph_mode="sync")
    job = await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))
    assert (job.status, job.graph_status, job.graph_episodes) == ("completed", "completed", 2)
    assert len(store.added) == 2
    # 같은 내용 재업로드는 같은 작업, 그래프 추가 없음
    again = await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))
    assert again.id == job.id and len(store.added) == 2


async def test_import_background_graph_mode_marks_pending_then_completes() -> None:
    _, conversations, store, _, svc, _, _, c1, _ = await _setup(graph_mode="background")
    job = await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))
    assert job.graph_status == "pending"
    await svc.wait_graph_tasks()
    done = await conversations.get_job("u", job.id)
    assert (done.graph_status, done.graph_episodes) == ("completed", 2)
    assert len(store.added) == 2


def _edge(  # noqa: ANN001
    uuid: str, fact: str, episodes: list[str], valid_at=None, invalid_at=None, src="node-me", dst="node-boss"
) -> GraphEdgeHit:
    return GraphEdgeHit(
        uuid=uuid,
        name="Involved",
        fact=fact,
        valid_at=valid_at,
        invalid_at=invalid_at,
        episode_uuids=episodes,
        source_node_uuid=src,
        target_node_uuid=dst,
    )


async def test_graph_retriever_filters_by_as_of_and_invalidation() -> None:
    contacts = InMemoryContactRepo()
    boss = await contacts.create("u", "박지현", "superior")
    t = lambda d, h=9: datetime(2026, 10, d, h, tzinfo=UTC)  # noqa: E731
    store = FakeGraphStore(
        entities={"박지현": "node-boss"},
        refs={
            "e1": EpisodeRef("e1", "c1:m03", t(5)),
            "e2": EpisodeRef("e2", "c1:m07", t(6)),
            "e3": EpisodeRef("e3", "c1:m10", t(14)),
        },
        edges=[
            _edge("x1", "김민수가 박지현에게 일정 조율을 요청했다", ["e1"], valid_at=t(5)),
            _edge("x2", "김민수가 박지현에게 사과했다", ["e2"], valid_at=t(6)),
            _edge("x3", "김민수가 11월 5일까지 자료를 맡았다", ["e3"], valid_at=t(14)),
            _edge("x4", "박지현은 김민수의 동기다", ["e1"], valid_at=t(1), invalid_at=t(3)),
        ],
    )
    r = GraphContextRetriever(store, contacts, top_k=4)
    got = await r.retrieve(
        user_id="u", contact_id=boss.id, draft_text="자료 보내드립니다", situation="보고", as_of=t(10)
    )
    assert [e.id for e in got] == ["x1", "x2"]  # x3는 as_of 이후 출처, x4는 무효화됨
    assert got[0].purpose == "context" and got[0].speaker_role == "graph"
    assert got[0].source_message_ids == ["c1:m03"]
    assert got[0].sent_at == t(5)
    # 상대 노드를 중심으로, 질의는 상황+초안, 과대 샘플링 4배
    assert store.searches == [("보고\n자료 보내드립니다", "u", "node-boss", 16)]
    assert await r.retrieve(user_id="u", contact_id=boss.id, draft_text=" ", situation="") == []


async def test_graph_retriever_keeps_only_contact_or_its_events() -> None:
    contacts = InMemoryContactRepo()
    boss = await contacts.create("u", "박지현", "superior")
    t = datetime(2026, 10, 5, tzinfo=UTC)
    store = FakeGraphStore(
        entities={"박지현": "node-boss"},
        events={"node-boss": {"ev-dashboard"}},
        refs={
            "e1": EpisodeRef("e1", "c1:m03", t),
            "e2": EpisodeRef("e2", "c4:m02", t),
            "e3": EpisodeRef("e3", "c1:m05", t),
            "e4": EpisodeRef("e4", "c2:m02", t),
        },
        edges=[
            _edge("keep-contact", "박지현이 고객사 미팅에 간다", ["e1"], src="node-boss", dst="ev-meeting"),
            _edge(
                "drop-other-room", "사용자가 홈커밍 참석 여부를 묻는다", ["e2"], src="node-me", dst="ev-home"
            ),
            _edge("keep-event", "사용자가 대시보드 개편을 맡았다", ["e3"], src="node-me", dst="ev-dashboard"),
            _edge("drop-me-only", "사용자가 케이크를 산다", ["e4"], src="node-me", dst="node-bro"),
        ],
    )
    r = GraphContextRetriever(store, contacts, top_k=4)
    got = await r.retrieve(user_id="u", contact_id=boss.id, draft_text="일정", situation="")
    assert [e.id for e in got] == ["keep-contact", "keep-event"]

    # 상대 노드를 못 찾으면 필터 없이 전부 (거리 재정렬만)
    store.entities = {}
    got = await r.retrieve(user_id="u", contact_id=boss.id, draft_text="일정", situation="")
    assert len(got) == 4


async def test_delete_conversation_removes_rows_episodes_and_graph() -> None:
    _, conversations, store, _, svc, boss, _, c1, c2 = await _setup(graph_mode="sync")
    await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))
    await svc.import_messages("u", c2.id, ImportPayload(messages=MESSAGES_C2))
    assert len(store.added) == 3
    vector = VectorStyleRetriever(conversations, FakeEmbedder(dim=32))
    assert await vector.retrieve(user_id="u", contact_id=boss.id, draft_text="수요일", situation="") != []

    d = await svc.delete_conversation("u", c1.id)
    assert (d.messages, d.episodes, d.graph_episodes, d.graph_error) == (2, 1, 2, None)
    assert [a["name"] for a in store.added] == ["c2:m01"]  # 다른 방은 그대로
    from app.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await conversations.get("u", c1.id)
    # 삭제 뒤 검색 차단
    assert await vector.retrieve(user_id="u", contact_id=boss.id, draft_text="수요일", situation="") == []
    with pytest.raises(NotFoundError):
        await svc.delete_conversation("someone-else", c2.id)


async def test_graph_retriever_dedupes_facts_from_same_source() -> None:
    contacts = InMemoryContactRepo()
    boss = await contacts.create("u", "박지현", "superior")
    t = datetime(2026, 10, 5, tzinfo=UTC)
    store = FakeGraphStore(
        refs={"e1": EpisodeRef("e1", "c1:m01", t), "e2": EpisodeRef("e2", "c1:m04", t)},
        edges=[
            _edge("a", "박지현이 주간보고 초안을 받았다", ["e1"]),
            _edge("b", "김민수가 주간보고 초안을 공유했다", ["e1"]),  # 같은 출처 → 제외
            _edge("c", "고객사 미팅이 금요일에 있다", ["e2"]),
        ],
    )
    got = await GraphContextRetriever(store, contacts, top_k=4).retrieve(
        user_id="u", contact_id=boss.id, draft_text="보고", situation=""
    )
    assert [e.id for e in got] == ["a", "c"]


async def test_composite_merges_and_degrades() -> None:
    contacts = InMemoryContactRepo()
    conversations = InMemoryConversationRepo()
    boss = await contacts.create("u", "박지현", "superior")
    svc = ImportService(contacts, conversations, FakeEmbedder(dim=32))
    c1 = await svc.create_conversation("u", boss.id, None, "json", "c1")
    await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))

    class Broken:
        version = "broken"

        async def retrieve(self, **kwargs):  # noqa: ANN003, ANN202
            raise RuntimeError("neo4j down")

    vector = VectorStyleRetriever(conversations, FakeEmbedder(dim=32))
    d = CompositeRetriever([vector, Broken()])
    assert d.version.startswith("knn-style-v1/") and d.version.endswith("+broken")
    got = await d.retrieve(user_id="u", contact_id=boss.id, draft_text="수요일까지 가능합니다", situation="")
    assert [e.purpose for e in got] == ["style"]  # 그래프가 죽어도 C는 나온다
    with pytest.raises(ValueError):
        CompositeRetriever([])
