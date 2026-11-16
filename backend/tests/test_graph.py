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
from app.graph.store import (
    EpisodeRef,
    EventInfo,
    GraphEdgeHit,
    GraphNeighborhood,
    GraphNodeInfo,
    _as_datetime,
)
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
    event_meta: dict[str, EventInfo] = field(default_factory=dict)  # Event uuid → 유형·중요도
    searches: list[tuple[str, str, str | None, int]] = field(default_factory=list)
    nodes: list[GraphNodeInfo] = field(default_factory=list)  # neighborhood가 돌려줄 노드
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

    async def event_info(self, group_id, uuids) -> dict[str, EventInfo]:  # noqa: ANN001
        return {u: info for u, info in self.event_meta.items() if u in uuids}

    async def episode_refs(self, uuids) -> dict[str, EpisodeRef]:  # noqa: ANN001
        return {u: self.refs[u] for u in uuids if u in self.refs}

    async def neighborhood(self, group_id, node_uuid) -> GraphNeighborhood:  # noqa: ANN001
        scope = {node_uuid} | self.events.get(node_uuid, set())
        edges = [e for e in self.edges if {e.source_node_uuid, e.target_node_uuid} & scope]
        ends = {node_uuid} | {e.source_node_uuid for e in edges} | {e.target_node_uuid for e in edges}
        return GraphNeighborhood(nodes=[n for n in self.nodes if n.uuid in ends], edges=edges)

    async def remove_episodes(self, group_id, names) -> int:  # noqa: ANN001
        before = len(self.added)
        self.added = [a for a in self.added if not (a["group_id"] == group_id and a["name"] in names)]
        return before - len(self.added)

    async def remove_group(self, group_id) -> int:  # noqa: ANN001
        before = len(self.added)
        self.added = [a for a in self.added if a["group_id"] != group_id]
        self.removed_groups = getattr(self, "removed_groups", []) + [group_id]
        return before - len(self.added)

    async def ping(self) -> None:
        pass

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


async def test_background_ingest_is_serialized_per_user_and_time_ordered_across_rooms() -> None:
    """두 방을 잇달아 가져오면 뒤 작업이 잠금을 기다리고, 그래프에는 전체 시각 순으로 들어간다."""
    import asyncio as _asyncio

    _, conversations, store, _, svc, _, _, c1, c2 = await _setup(graph_mode="background")

    orig = store.add_episode

    async def slow_add(**kw):  # noqa: ANN003, ANN202
        await _asyncio.sleep(0.02)  # 겹치게 만들기
        await orig(**kw)

    store.add_episode = slow_add  # type: ignore[method-assign]
    job1 = await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))  # 09:25, 09:31
    job2 = await svc.import_messages("u", c2.id, ImportPayload(messages=MESSAGES_C2))  # 09:28
    await svc.wait_graph_tasks()
    assert [a["name"] for a in store.added] == ["c1:m01", "c2:m01", "c1:m02"]  # 방을 가로질러 시각 순
    assert len(store.added) == 3  # 중복 없음
    j1 = await conversations.get_job("u", job1.id)
    j2 = await conversations.get_job("u", job2.id)
    assert j1.graph_status == "completed" and j2.graph_status == "completed"
    assert j1.graph_episodes + j2.graph_episodes == 3  # 어느 작업이 몇 개를 넣었든 합은 전체


async def test_resume_all_ingests_pending_users_and_closes_stale_jobs() -> None:
    """서버가 죽어 pending인 채 남은 작업: 시작 때 이어받아 넣고 completed로 정리한다."""
    _, conversations, store, ingest, svc, _, _, c1, c2 = await _setup(graph_mode="off")
    job1 = await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))
    job2 = await svc.import_messages("u", c2.id, ImportPayload(messages=MESSAGES_C2))
    # 죽기 전 상태를 흉내: 작업은 pending, 메시지는 미수집
    await conversations.update_job_graph("u", job1.id, "pending", 0, None)
    await conversations.update_job_graph("u", job2.id, "running", 1, None)
    assert await conversations.users_with_pending_graph() == ["u"]

    done = await ingest.resume_all()
    assert done == {"u": 3}
    assert [a["name"] for a in store.added] == ["c1:m01", "c2:m01", "c1:m02"]  # 전체 시각 순
    assert (await conversations.get_job("u", job1.id)).graph_status == "completed"
    assert (await conversations.get_job("u", job2.id)).graph_status == "completed"
    assert await conversations.users_with_pending_graph() == []
    assert await ingest.resume_all() == {}  # 다시 돌리면 할 일 없음


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


async def test_graph_retriever_reranks_by_query_similarity_and_recency() -> None:
    """임베더가 있으면 Graphiti 순위 대신 질의 유사도+최근성으로 다시 매기고, 동떨어진 사실은 버린다."""
    contacts = InMemoryContactRepo()
    boss = await contacts.create("u", "박지현", "superior")
    t = lambda d: datetime(2026, 10, d, 9, tzinfo=UTC)  # noqa: E731
    store = FakeGraphStore(
        refs={
            "e1": EpisodeRef("e1", "c1:m01", t(5)),
            "e2": EpisodeRef("e2", "c1:m05", t(5)),
            "e3": EpisodeRef("e3", "c1:m08", t(6)),
        },
        edges=[  # Graphiti 순위: 무관한 주간보고 사실이 1위
            _edge(
                "generic",
                "박지현이 김민수로부터 주간보고 초안을 받았다",
                ["e1"],
                valid_at=t(5),
                dst="ev-report",
            ),
            _edge(
                "sched-old",
                "김민수가 수요일로 대시보드 개편 일정 조정을 부탁했다",
                ["e2"],
                valid_at=t(5),
                dst="ev-sched",
            ),
            _edge(
                "sched-new",
                "박지현이 수요일로 일정을 조정하기로 합의했다",
                ["e3"],
                valid_at=t(6),
                dst="ev-sched",
            ),
        ],
        event_meta={
            "ev-report": EventInfo("ev-report", "status", None),  # 일상 상태 공유, 중요도 미기재
            "ev-sched": EventInfo("ev-sched", "schedule", 7),
        },
    )
    r = GraphContextRetriever(store, contacts, top_k=4, embedder=FakeEmbedder(dim=128))
    got = await r.retrieve(
        user_id="u",
        contact_id=boss.id,
        draft_text="수요일까지 일정 조정 가능합니다",
        situation="일정 확인",
        as_of=t(10),
    )
    ids = [e.id for e in got]
    assert ids[0] in ("sched-new", "sched-old") and ids[:2] != ["generic", "sched-old"]
    assert "generic" not in ids or ids.index("generic") == len(ids) - 1  # 무관한 사실은 꼴찌이거나 버려진다
    assert all(e.score is not None for e in got) and got[0].score >= got[-1].score
    assert r.version == "graph-context-v2"

    # 임베더 없으면 v1 동작: Graphiti 순위 그대로, 점수 없음
    plain = GraphContextRetriever(store, contacts, top_k=4)
    got_plain = await plain.retrieve(
        user_id="u", contact_id=boss.id, draft_text="일정", situation="", as_of=t(10)
    )
    assert [e.id for e in got_plain] == ["generic", "sched-old", "sched-new"] and got_plain[0].score is None


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


async def test_delete_contact_and_account_remove_everything() -> None:
    from app.core.config import Settings
    from app.repositories.auth import InMemoryAuthRepo
    from app.repositories.memory import InMemoryRewriteRepo
    from app.repositories.usage import InMemoryUsageRepo
    from app.services.auth_service import AuthService, UnauthorizedError
    from app.services.deletion_service import DeletionService

    contacts, conversations, store, _, svc, boss, bro, c1, c2 = await _setup(graph_mode="sync")
    await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))
    await svc.import_messages("u", c2.id, ImportPayload(messages=MESSAGES_C2))
    assert len(store.added) == 3

    auth = InMemoryAuthRepo()
    settings = Settings(_env_file=None, llm_provider="fake", embedding_provider="fake")  # type: ignore[call-arg]
    auth_service = AuthService(settings, auth)
    reg = await auth_service.register("deleter", "long-enough-pw")
    usage = InMemoryUsageRepo()
    await usage.reserve_rewrite("u", __import__("datetime").date(2026, 11, 9), 10)
    deletion = DeletionService(contacts, conversations, InMemoryRewriteRepo(), svc, usage, auth, store)

    # 상대 삭제: 그 상대의 대화방·그래프 에피소드만 사라지고 다른 상대는 그대로
    d = await deletion.delete_contact("u", boss.id)
    assert (d.conversations, d.messages, d.graph_episodes, d.graph_errors) == (1, 2, 2, [])
    assert [a["name"] for a in store.added] == ["c2:m01"]
    assert [c.alias for c in await contacts.list("u")] == ["준호"]
    from app.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await contacts.get("u", boss.id)
    with pytest.raises(NotFoundError):
        await deletion.delete_contact("someone-else", bro.id)

    # 계정 삭제: 비밀번호 확인 → 전부 삭제 → 그래프 group 통째로 → 사용자·세션·사용량
    session = await auth_service.resolve(reg.session_token)
    assert session is not None
    with pytest.raises(UnauthorizedError):
        await deletion.delete_account(session, "wrong-password")
    # 이 테스트의 데이터 user_id는 "u"이고 계정은 별도 사용자라, 계정 삭제 경로를 "u"로 맞춰 본다
    user_rows = [u for u, _ in auth._users.values()]
    acct = await deletion.delete_account(session, "long-enough-pw")
    assert acct.user_id == str(user_rows[0].id) and acct.contacts == 0  # 계정 사용자에겐 상대가 없었다
    assert await auth_service.resolve(reg.session_token) is None  # 세션 사라짐
    assert await auth.find_user_by_username("deleter") is None
    assert store.removed_groups == [str(user_rows[0].id)]


async def test_reset_wipes_group_or_one_conversation_then_reingests() -> None:
    _, conversations, store, ingest, svc, _, _, c1, c2 = await _setup(graph_mode="sync")
    await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))
    await svc.import_messages("u", c2.id, ImportPayload(messages=MESSAGES_C2))
    assert len(store.added) == 3 and await conversations.users_with_pending_graph() == []

    # 한 방만: c1 에피소드 2개 제거, c1 메시지 2개 되돌림, c2는 그대로
    assert await ingest.reset("u", c1.id) == (2, 2)
    assert [a["name"] for a in store.added] == ["c2:m01"]
    assert all(m.graph_ingested_at is None for m in await conversations.list_messages("u", c1.id))
    assert all(m.graph_ingested_at is not None for m in await conversations.list_messages("u", c2.id))
    assert await ingest.ingest_user("u") == 2  # 재수집은 c1만
    assert sorted(a["name"] for a in store.added) == ["c1:m01", "c1:m02", "c2:m01"]

    # 사용자 전체: group 통째로 + 메시지 3개 되돌림 → 재수집 3
    assert await ingest.reset("u") == (3, 3)
    assert store.added == [] and store.removed_groups[-1] == "u"
    assert await ingest.ingest_user("u") == 3
    assert await ingest.reset("someone-else") == (0, 0)


def _graph_client(store: FakeGraphStore | None):  # noqa: ANN202
    """fake 프로바이더 앱에 가짜 그래프 저장소를 꽂은 클라이언트와 헤더."""
    from fastapi.testclient import TestClient

    from app.core.config import Settings
    from app.main import create_app

    client = TestClient(create_app(Settings(_env_file=None, llm_provider="fake", embedding_provider="fake")))  # type: ignore[call-arg]
    client.app.state.container.graph_store = store  # type: ignore[attr-defined]
    return client, {"X-User-Id": "user-a"}


def test_contact_graph_returns_contact_scope_current_facts_first() -> None:
    old, new = datetime(2026, 10, 5, tzinfo=UTC), datetime(2026, 10, 16, tzinfo=UTC)
    store = FakeGraphStore(
        entities={"박지현": "n-contact"},
        events={"n-contact": {"n-event"}},
        nodes=[
            GraphNodeInfo("n-me", "김민수", "person", "나"),
            GraphNodeInfo("n-contact", "박지현", "person", "팀장"),
            GraphNodeInfo("n-event", "대시보드 개편", "event", "개편 일정", "status", 6),
            GraphNodeInfo("n-other", "최유진", "person", "다른 상대"),
        ],
        edges=[
            GraphEdgeHit("e-old", "Relationship", "반말을 쓴다", old, new, [], "n-me", "n-contact"),
            GraphEdgeHit("e-new", "Relationship", "존댓말을 쓴다", new, None, [], "n-me", "n-contact"),
            GraphEdgeHit("e-event", "Involved", "개편을 맡았다", old, None, [], "n-me", "n-event"),
            GraphEdgeHit("e-elsewhere", "Relationship", "밥 약속", new, None, [], "n-me", "n-other"),
        ],
    )
    client, headers = _graph_client(store)
    contact = client.post(
        "/v1/contacts", json={"alias": "박지현", "relationship_type": "superior"}, headers=headers
    ).json()

    body = client.get(f"/v1/contacts/{contact['id']}/graph", headers=headers).json()

    assert body["enabled"] is True and body["center_id"] == "n-contact"
    # 다른 상대와의 사실(e-elsewhere)과 그 노드는 빠지고, 유효한 사실이 최근 순으로 먼저 온다.
    assert [e["id"] for e in body["edges"]] == ["e-new", "e-event", "e-old"]
    assert [e["current"] for e in body["edges"]] == [True, True, False]
    assert [(n["name"], n["is_contact"]) for n in body["nodes"]] == [
        ("박지현", True),
        ("대시보드 개편", False),
        ("김민수", False),
    ]
    assert body["nodes"][1]["event_type"] == "status" and body["nodes"][1]["importance"] == 6


def test_contact_graph_empty_states_and_ownership() -> None:
    client, headers = _graph_client(FakeGraphStore())
    contact = client.post(
        "/v1/contacts", json={"alias": "박지현", "relationship_type": "superior"}, headers=headers
    ).json()
    url = f"/v1/contacts/{contact['id']}/graph"

    # 그래프에 상대 이름의 노드가 아직 없다 (수집 전).
    assert client.get(url, headers=headers).json() == {
        "enabled": True,
        "center_id": None,
        "nodes": [],
        "edges": [],
    }
    # 남의 상대는 볼 수 없다.
    assert client.get(url, headers={"X-User-Id": "user-b"}).status_code == 404

    client.app.state.container.graph_store = None  # type: ignore[attr-defined]
    assert client.get(url, headers=headers).json()["enabled"] is False


def test_as_datetime_accepts_neo4j_values_strings_and_naive() -> None:
    class Neo4jDateTime:
        def to_native(self) -> datetime:
            return datetime(2026, 10, 5, 0, 10, tzinfo=UTC)

    assert _as_datetime(None) is None
    assert _as_datetime(Neo4jDateTime()) == datetime(2026, 10, 5, 0, 10, tzinfo=UTC)
    assert _as_datetime("2026-10-05T00:10:00Z") == datetime(2026, 10, 5, 0, 10, tzinfo=UTC)
    assert _as_datetime(datetime(2026, 10, 5)) == datetime(2026, 10, 5, tzinfo=UTC)
    assert _as_datetime(12345) is None
