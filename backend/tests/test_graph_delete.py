"""그래프 삭제는 모델을 부르지 않는다 (2026-11-14).

`graphiti.remove_episode`는 에피소드를 하나 지울 때마다 남는 개체의 요약을 LLM으로 다시 만든다 —
실측으로 메시지 36건짜리 계정 삭제에 haiku 159회, 248초가 걸렸다. 이제
- 계정 삭제는 대화방마다 에피소드를 지우지 않고 그 사용자의 그래프를 통째로 지운다.
- 대화방·상대 삭제와 화자 역할 고치기는 `GraphitiStore`가 Cypher로 직접 지운다(요약은 비운다).
"""

from __future__ import annotations

import pytest

from app.ai.embeddings import FakeEmbedder
from app.core.config import Settings
from app.graph.ingest import GraphIngestService
from app.graph.store import GraphitiStore
from app.repositories.auth import InMemoryAuthRepo
from app.repositories.memory import InMemoryContactRepo, InMemoryConversationRepo, InMemoryRewriteRepo
from app.repositories.usage import InMemoryUsageRepo
from app.services.auth_service import AuthService
from app.services.deletion_service import DeletionService
from app.services.import_service import ImportPayload, ImportService
from tests.test_graph import MESSAGES_C1, MESSAGES_C2, FakeGraphStore

pytestmark = pytest.mark.anyio


class CountingStore(FakeGraphStore):
    """에피소드 단위 삭제가 몇 번 불렸는지 센다."""

    def __init__(self) -> None:
        super().__init__()
        self.episode_removals: list[list[str]] = []

    async def remove_episodes(self, group_id, names) -> int:  # noqa: ANN001
        self.episode_removals.append(list(names))
        return await super().remove_episodes(group_id, names)


async def _account(store: FakeGraphStore | None):  # noqa: ANN202
    contacts, conversations = InMemoryContactRepo(), InMemoryConversationRepo()
    auth = InMemoryAuthRepo()
    settings = Settings(_env_file=None, llm_provider="fake", embedding_provider="fake")  # type: ignore[call-arg]
    auth_service = AuthService(settings, auth)
    reg = await auth_service.register("deleter", "long-enough-pw")
    session = await auth_service.resolve(reg.session_token)
    assert session is not None
    user = str(session.user_id)
    ingest = GraphIngestService(store, conversations, contacts) if store is not None else None
    svc = ImportService(
        contacts,
        conversations,
        FakeEmbedder(dim=32),
        graph_ingest=ingest,
        graph_mode="sync" if store is not None else "off",
    )
    boss = await contacts.create(user, "박지현", "superior")
    bro = await contacts.create(user, "준호", "family")
    c1 = await svc.create_conversation(user, boss.id, "팀장 방", "json", "c1", me_name="김민수")
    c2 = await svc.create_conversation(user, bro.id, None, "json", "c2", me_name="김민수")
    await svc.import_messages(user, c1.id, ImportPayload(messages=MESSAGES_C1))
    await svc.import_messages(user, c2.id, ImportPayload(messages=MESSAGES_C2))
    deletion = DeletionService(
        contacts, conversations, InMemoryRewriteRepo(), svc, InMemoryUsageRepo(), auth, store
    )
    return user, session, contacts, conversations, svc, deletion, boss, c1


async def test_account_deletion_removes_the_group_once_not_episode_by_episode() -> None:
    store = CountingStore()
    user, session, contacts, conversations, _, deletion, _, _ = await _account(store)
    assert len(store.added) == 3

    acct = await deletion.delete_account(session, "long-enough-pw")

    assert store.episode_removals == []  # 에피소드를 하나씩 지우지 않았다
    assert store.removed_groups == [user] and store.added == []
    assert (acct.contacts, acct.conversations, acct.messages) == (2, 2, 3)
    assert (acct.graph_nodes_removed, acct.graph_error) == (3, None)
    assert await contacts.list(user) == [] and await conversations.list(user) == []


async def test_contact_and_conversation_deletion_still_remove_their_episodes() -> None:
    store = CountingStore()
    user, _, _, _, svc, deletion, boss, c1 = await _account(store)

    d = await deletion.delete_contact(user, boss.id)  # 계정 삭제가 아니면 그 방의 에피소드만 지운다
    assert (d.conversations, d.graph_episodes, d.graph_errors) == (1, 2, [])
    assert store.episode_removals == [["c1:m01", "c1:m02"]]
    assert [a["name"] for a in store.added] == ["c2:m01"]
    assert getattr(store, "removed_groups", []) == []


async def test_account_deletion_without_a_graph_still_wipes_the_data() -> None:
    user, session, contacts, conversations, _, deletion, _, _ = await _account(None)
    acct = await deletion.delete_account(session, "long-enough-pw")
    assert (acct.contacts, acct.conversations, acct.messages, acct.graph_error) == (2, 2, 3, None)
    assert await contacts.list(user) == [] and await conversations.list(user) == []


async def test_account_deletion_reports_a_graph_failure_but_deletes_the_rest() -> None:
    store = CountingStore()

    async def boom(group_id) -> int:  # noqa: ANN001
        raise ConnectionError("neo4j down")

    store.remove_group = boom  # type: ignore[method-assign]
    user, session, contacts, _, _, deletion, _, _ = await _account(store)
    acct = await deletion.delete_account(session, "long-enough-pw")
    assert acct.graph_error == "ConnectionError" and acct.messages == 3
    assert await contacts.list(user) == []


# ---- GraphitiStore: 모델을 부르지 않는 에피소드 삭제 ------------------------------------------------


class _Result:
    def __init__(self, records: list[dict]) -> None:
        self.records = records


class _Driver:
    """질의를 받아 적고, 정해 둔 답을 돌려준다."""

    def __init__(self, found: list[str], mentioned: list[dict]) -> None:
        self.found, self.mentioned = found, mentioned
        self.calls: list[tuple[str, dict]] = []

    async def execute_query(self, query: str, **params):  # noqa: ANN003, ANN201
        self.calls.append((query, params))
        if "RETURN e.uuid AS uuid" in query:
            return _Result([{"uuid": u} for u in self.found])
        if "RETURN n.uuid AS uuid, others" in query:
            return _Result(self.mentioned)
        return _Result([])


class _Graphiti:
    def __init__(self, driver: _Driver) -> None:
        self.driver = driver
        self.llm_calls = 0

    async def remove_episode(self, uuid: str) -> None:  # 불리면 안 된다 — LLM을 부르는 경로
        self.llm_calls += 1


async def test_graphiti_store_removes_episodes_with_cypher_only() -> None:
    driver = _Driver(
        found=["ep-2", "ep-1"],
        mentioned=[{"uuid": "only-here", "others": 0}, {"uuid": "shared", "others": 3}],
    )
    graphiti = _Graphiti(driver)
    store = GraphitiStore(graphiti)  # type: ignore[arg-type]

    assert await store.remove_episodes("user-1", ["c1:m01", "c1:m02"]) == 2
    assert graphiti.llm_calls == 0

    steps = [q for q, _ in driver.calls]
    assert len(steps) == 6
    assert all(
        p.get("group_id") == "user-1" for _, p in driver.calls
    )  # 다른 사용자의 그래프는 건드리지 않는다
    gone = ["ep-1", "ep-2"]
    # 사실: 근거에서 빼고 비면 지운다
    assert "SET r.episodes = [x IN r.episodes WHERE NOT x IN $gone]" in steps[2] and "DELETE r" in steps[2]
    assert driver.calls[2][1]["gone"] == gone
    # 개체: 이 에피소드만 언급한 것은 지우고, 남는 것은 요약을 비운다
    assert "DETACH DELETE n" in steps[3] and driver.calls[3][1]["uuids"] == ["only-here"]
    assert "SET n.summary = ''" in steps[4] and driver.calls[4][1]["uuids"] == ["shared"]
    # 에피소드는 맨 나중에 (도중에 실패하면 다시 불러 이어서 지울 수 있게)
    assert "(e:Episodic {group_id: $group_id})" in steps[5] and "DETACH DELETE e" in steps[5]
    assert driver.calls[5][1]["gone"] == gone


async def test_graphiti_store_does_nothing_when_no_episode_matches() -> None:
    driver = _Driver(found=[], mentioned=[])
    store = GraphitiStore(_Graphiti(driver))  # type: ignore[arg-type]
    assert await store.remove_episodes("user-1", ["c9:m01"]) == 0
    assert len(driver.calls) == 1  # 찾는 질의 하나뿐
    assert await store.remove_episodes("user-1", []) == 0
    assert len(driver.calls) == 1
