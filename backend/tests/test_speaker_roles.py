"""화자 역할 고치기: 화자(나/상대)를 잘못 지정한 가져오기를 대화방을 지우지 않고 고친다 (인메모리 + API).

메시지에는 원래 화자 이름이 없고 역할만 남아 있어 방 전체의 나↔상대 맞바꾸기다.
- "이 메시지는 X가 한 말"로 요청한다 — 이미 그러면 아무 일도 하지 않는다(멱등).
- 바뀌면 묶음을 다시 만들고(가져오기와 같은 작업), 그래프에 들어간 것은 지우고 다시 넣는다.
- 그래프 수집이 돌고 있으면 넣던 에피소드까지만 끝내고 비켜 준다(제때 못 비키면 409).
- 그래프에 닿지 못하면 아무것도 바꾸지 않고 503.
- 돌고 있는 묶음 생성과 겹치면 그 뒤에 줄을 서고, 마지막 묶음은 바뀐 역할로 만들어진다.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.ai.embeddings import FakeEmbedder
from app.core.config import Settings
from app.core.errors import GraphUnavailableError, NotFoundError, VersionConflictError
from app.graph.ingest import GraphIngestService
from app.main import create_app
from app.repositories.memory import InMemoryContactRepo, InMemoryConversationRepo
from app.services.import_service import ImportMessage, ImportPayload, ImportService
from tests.test_graph import FakeGraphStore
from tests.test_import_background import SlowEmbedder

pytestmark = pytest.mark.anyio

# 화자를 거꾸로 지정한 가져오기: 실제로는 '부탁'이 상대(팀장), '답'이 나다.
WRONG = ImportPayload(
    messages=[
        ImportMessage(id="m1", speaker="me", sent_at="2026-10-05T09:25:00+09:00", text="보고서 언제 돼요?"),
        ImportMessage(
            id="m2", speaker="contact", sent_at="2026-10-05T09:31:00+09:00", text="수요일까지 드릴게요."
        ),
        ImportMessage(id="m3", speaker="me", sent_at="2026-10-05T09:40:00+09:00", text="화요일은 어려워요?"),
        ImportMessage(
            id="m4", speaker="contact", sent_at="2026-10-05T09:52:00+09:00", text="네, 수요일로 부탁드려요."
        ),
    ]
)
ASKS = ["보고서 언제 돼요?", "화요일은 어려워요?"]
ANSWERS = ["수요일까지 드릴게요.", "네, 수요일로 부탁드려요."]


async def _setup(  # noqa: ANN202
    graph_mode: str = "off",
    *,
    embedder: FakeEmbedder | None = None,
    index_wait: float | None = None,
    store: FakeGraphStore | None = None,
    with_graph: bool = True,
):
    contacts, conversations = InMemoryContactRepo(), InMemoryConversationRepo()
    store = store or FakeGraphStore()
    ingest = GraphIngestService(store, conversations, contacts) if with_graph else None
    svc = ImportService(
        contacts,
        conversations,
        embedder or FakeEmbedder(dim=32),
        graph_ingest=ingest,
        graph_mode=graph_mode,  # type: ignore[arg-type]
        index_wait=index_wait,
    )
    boss = await contacts.create("u", "박지현", "superior")
    conv = await svc.create_conversation("u", boss.id, "팀장 방", "json", "c1", me_name="박지현")
    return conversations, svc, store, ingest, boss, conv


async def _roles(conversations, conv) -> dict[str, str]:  # noqa: ANN001
    return {m.source_key: m.speaker_role for m in await conversations.list_messages("u", conv.id)}


async def _style_lines(conversations, boss) -> set[str]:  # noqa: ANN001
    """이 상대의 묶음에 문체 예시로 들어간 줄들."""
    query = await FakeEmbedder(dim=32).embed_query("수요일")
    hits = await conversations.search_episodes("u", boss.id, query, 50)
    return {line for h in hits for line in h.episode.style_text.split("\n")}


async def _anchor(conversations, conv, key: str = "m2"):  # noqa: ANN001, ANN202
    return next(m for m in await conversations.list_messages("u", conv.id) if m.source_key == key)


async def test_swaps_roles_rebuilds_episodes_and_is_idempotent() -> None:
    conversations, svc, _, _, boss, conv = await _setup(with_graph=False)
    first = await svc.import_messages("u", conv.id, WRONG)
    assert await _style_lines(conversations, boss) == set(ASKS)  # 상대의 말이 내 말투로 들어가 있다

    m2 = await _anchor(conversations, conv)
    r = await svc.set_speaker_role("u", conv.id, m2.id, "me", me_name=" 김민수 ")
    assert (r.changed, r.messages, r.my_messages, r.me_name) == (True, 4, 2, "김민수")
    assert (r.graph_removed, r.graph_requeued) == (0, 0)
    assert r.job is not None and r.job.id != first.id
    assert (r.job.status, r.job.imported, r.job.episodes, r.job.graph_status) == (
        "completed",
        0,
        2,
        "skipped",
    )
    assert await _roles(conversations, conv) == {"m1": "contact", "m2": "me", "m3": "contact", "m4": "me"}
    assert await _style_lines(conversations, boss) == set(ANSWERS)
    assert (await conversations.get("u", conv.id)).me_name == "김민수"
    stats = await conversations.conversation_stats("u", conv.id)
    assert (stats.messages, stats.my_messages, stats.episodes) == (4, 2, 2)  # 메시지는 그대로 남아 있다

    # 같은 요청을 다시 보내도 도로 뒤집히지 않는다 — 작업도 새로 생기지 않는다
    again = await svc.set_speaker_role("u", conv.id, m2.id, "me", me_name="다른이름")
    assert (again.changed, again.messages, again.job, again.me_name) == (False, 0, None, "김민수")
    assert await _roles(conversations, conv) == {"m1": "contact", "m2": "me", "m3": "contact", "m4": "me"}
    assert (await conversations.latest_job("u", conv.id)).id == r.job.id
    # 상대의 메시지를 기준으로 말해도 같다
    m1 = await _anchor(conversations, conv, "m1")
    assert (await svc.set_speaker_role("u", conv.id, m1.id, "contact")).changed is False
    assert len(svc._conv_locks) == 0


async def test_me_name_is_cleared_unless_given() -> None:
    conversations, svc, _, _, _, conv = await _setup(with_graph=False)
    await svc.import_messages("u", conv.id, WRONG)
    m2 = await _anchor(conversations, conv)
    # 예전 이름('박지현')은 '나'로 잘못 지정한 상대의 이름이었다. '회원님' 같은 표기는 이름으로 쓰지 않는다
    r = await svc.set_speaker_role("u", conv.id, m2.id, "me", me_name="회원님")
    assert r.me_name is None and (await conversations.get("u", conv.id)).me_name is None


async def test_unknown_message_other_room_and_other_user_are_not_found() -> None:
    conversations, svc, _, _, boss, conv = await _setup(with_graph=False)
    await svc.import_messages("u", conv.id, WRONG)
    other = await svc.create_conversation("u", boss.id, "다른 방", "json", "c2")
    await svc.import_messages("u", other.id, WRONG)
    m2 = await _anchor(conversations, conv)
    before = await _roles(conversations, conv)
    with pytest.raises(NotFoundError):
        await svc.set_speaker_role("u", conv.id, uuid4(), "me")
    with pytest.raises(NotFoundError):  # 다른 방의 메시지로는 이 방을 바꾸지 못한다
        await svc.set_speaker_role("u", other.id, m2.id, "me")
    with pytest.raises(NotFoundError):
        await svc.set_speaker_role("someone-else", conv.id, m2.id, "me")
    assert await _roles(conversations, conv) == before
    assert await _roles(conversations, other) == before


async def test_graph_episodes_are_removed_and_ingested_again_with_the_new_roles() -> None:
    conversations, svc, store, _, boss, conv = await _setup("sync")
    other = await svc.create_conversation("u", boss.id, "다른 방", "json", "c2", me_name="김민수")
    await svc.import_messages("u", conv.id, WRONG)
    await svc.import_messages("u", other.id, WRONG)
    assert [a["name"] for a in store.added if a["name"].startswith("c1:")] == [
        "c1:m1",
        "c1:m2",
        "c1:m3",
        "c1:m4",
    ]
    wrong = next(a for a in store.added if a["name"] == "c1:m2")
    assert "박지현 → 박지현" in wrong["body"]  # 잘못된 역할: 상대의 이름이 내 이름으로도 들어갔다
    other_bodies = [a["body"] for a in store.added if a["name"].startswith("c2:")]

    m2 = await _anchor(conversations, conv)
    r = await svc.set_speaker_role("u", conv.id, m2.id, "me", me_name="김민수")
    assert (r.changed, r.graph_removed, r.graph_requeued) == (True, 4, 4)
    assert r.job is not None
    assert (r.job.status, r.job.graph_status, r.job.graph_episodes) == ("completed", "completed", 4)
    bodies = {a["name"]: a["body"] for a in store.added}
    assert len(store.added) == 8  # 이 방 4건은 지웠다가 다시 넣었고, 다른 방 4건은 그대로다
    assert "김민수 → 박지현: 수요일(=" in bodies["c1:m2"]
    assert "박지현 → 김민수: 보고서 언제 돼요?" in bodies["c1:m1"]
    assert [a["body"] for a in store.added if a["name"].startswith("c2:")] == other_bodies
    stats = await conversations.conversation_stats("u", conv.id)
    assert (stats.graph_ingested, stats.graph_pending) == (4, 0)

    # 다시 보내면 그래프도 건드리지 않는다
    again = await svc.set_speaker_role("u", conv.id, m2.id, "me")
    assert (again.changed, again.graph_removed) == (False, 0) and len(store.added) == 8


async def test_held_messages_are_tried_again_after_the_fix() -> None:
    """보류(거듭 실패)는 옛 본문으로 실패한 것이다 — 역할을 고치면 다시 수집 대상이 된다."""
    conversations, svc, store, ingest, _, conv = await _setup("sync")
    assert ingest is not None
    ingest.max_attempts = 1
    store.fail_on = "c1:m2"
    await svc.import_messages("u", conv.id, WRONG)
    assert (await conversations.conversation_stats("u", conv.id)).graph_held == 1
    store.fail_on = None
    m2 = await _anchor(conversations, conv)
    r = await svc.set_speaker_role("u", conv.id, m2.id, "me")
    assert (r.graph_removed, r.graph_requeued) == (3, 4)
    stats = await conversations.conversation_stats("u", conv.id)
    assert (stats.graph_ingested, stats.graph_held) == (4, 0)


async def test_graph_wired_but_not_ingesting_from_imports_only_requeues() -> None:
    """수집을 가져오기가 시작하지 않는 구성(graph_mode=off): 지우고 미수집으로 돌려 두기만 한다."""
    conversations, svc, store, ingest, _, conv = await _setup("off")
    assert ingest is not None
    await svc.import_messages("u", conv.id, WRONG)
    assert await ingest.ingest_user("u") == 4
    m2 = await _anchor(conversations, conv)
    r = await svc.set_speaker_role("u", conv.id, m2.id, "me")
    assert (r.graph_removed, r.graph_requeued) == (4, 4)
    assert r.job is not None and r.job.graph_status == "skipped"
    assert store.added == []
    assert (await conversations.conversation_stats("u", conv.id)).graph_pending == 4


class GatedStore(FakeGraphStore):
    """`gate`가 열릴 때까지 에피소드 추가가 멈춘다 (오래 걸리는 그래프 수집)."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.started = asyncio.Event()

    async def add_episode(self, **kwargs) -> None:  # noqa: ANN003
        self.started.set()
        await self.gate.wait()
        await super().add_episode(**kwargs)


async def test_a_running_ingest_steps_aside_for_the_swap() -> None:
    """수집이 도는 중에도 고친다: 도는 수집이 넣던 에피소드까지만 끝내고 비켜 준다 (2026-11-14)."""
    store = GatedStore()
    conversations, svc, _, ingest, _, conv = await _setup("background", store=store)
    first = await svc.import_messages("u", conv.id, WRONG)
    await asyncio.wait_for(store.started.wait(), 2)  # 수집이 첫 에피소드에서 멈춰 있다
    m2 = await _anchor(conversations, conv)

    swap = asyncio.create_task(svc.set_speaker_role("u", conv.id, m2.id, "me", me_name="김민수"))
    await asyncio.sleep(0.05)
    assert not swap.done()  # 넣던 에피소드가 끝나길 기다린다
    assert await _roles(conversations, conv) == {"m1": "me", "m2": "contact", "m3": "me", "m4": "contact"}

    store.gate.set()  # 그 에피소드가 끝난다 → 수집이 멈추고 잠금을 넘긴다
    r = await asyncio.wait_for(swap, 5)
    # 옛 역할로 들어간 한 건은 지우고 다시 넣는다. 나머지 세 건은 아직 들어가지 않았었다
    assert (r.changed, r.messages, r.graph_removed, r.graph_requeued) == (True, 4, 1, 1)
    assert await _roles(conversations, conv) == {"m1": "contact", "m2": "me", "m3": "contact", "m4": "me"}

    await svc.wait_graph_tasks()
    assert ingest is not None and ingest._yield == {}  # 비켜 달라는 표시가 남지 않았다
    assert sorted(a["name"] for a in store.added) == ["c1:m1", "c1:m2", "c1:m3", "c1:m4"]
    assert all("박지현 → 박지현" not in a["body"] for a in store.added)  # 전부 바뀐 역할로 들어갔다
    assert all(m.graph_ingested_at is not None for m in await conversations.list_messages("u", conv.id))
    assert (await conversations.get_job("u", r.job.id)).graph_status == "completed"
    assert (await conversations.get_job("u", first.id)).graph_status == "completed"  # 멈췄던 작업도 정리됐다


async def test_refused_with_409_when_the_ingest_does_not_step_aside_in_time() -> None:
    store = GatedStore()
    conversations, svc, _, ingest, boss, conv = await _setup("background", store=store)
    assert ingest is not None
    ingest.yield_wait = 0.1  # 에피소드 하나가 이보다 오래 걸린다
    await svc.import_messages("u", conv.id, WRONG)
    await asyncio.wait_for(store.started.wait(), 2)
    m2 = await _anchor(conversations, conv)
    jobs_before = (await conversations.latest_job("u", conv.id)).id

    with pytest.raises(VersionConflictError) as err:
        await svc.set_speaker_role("u", conv.id, m2.id, "me", me_name="김민수")
    assert "바로 멈추지 않아" in err.value.message
    # 아무것도 바뀌지 않았다: 역할·이름·묶음·작업
    assert await _roles(conversations, conv) == {"m1": "me", "m2": "contact", "m3": "me", "m4": "contact"}
    assert (await conversations.get("u", conv.id)).me_name == "박지현"
    assert await _style_lines(conversations, boss) == set(ASKS)
    assert (await conversations.latest_job("u", conv.id)).id == jobs_before
    assert len(svc._conv_locks) == 0  # 대화방 잠금을 쥔 채 끝나지 않았다
    assert ingest._yield == {}

    # 포기한 뒤에도 원래 수집은 끝까지 간다 (멈춰 세운 채 버려두지 않는다)
    store.gate.set()
    await svc.wait_graph_tasks()
    await ingest.wait_tasks()
    assert sorted(a["name"] for a in store.added) == ["c1:m1", "c1:m2", "c1:m3", "c1:m4"]
    # 그 뒤에는 된다
    r = await svc.set_speaker_role("u", conv.id, m2.id, "me", me_name="김민수")
    assert (r.changed, r.graph_removed, r.graph_requeued) == (True, 4, 4)
    await svc.wait_graph_tasks()
    assert sorted(a["name"] for a in store.added) == ["c1:m1", "c1:m2", "c1:m3", "c1:m4"]
    assert all("박지현 → 박지현" not in a["body"] for a in store.added)
    assert (await conversations.get_job("u", r.job.id)).graph_status == "completed"


async def test_a_failed_swap_does_not_leave_the_interrupted_ingest_stopped() -> None:
    """수집을 멈춰 세운 뒤 그래프 정리가 실패하면(503) 멈춘 수집을 뒤에서 다시 띄운다."""
    store = GatedStore()
    conversations, svc, _, ingest, _, conv = await _setup("background", store=store)
    assert ingest is not None
    await svc.import_messages("u", conv.id, WRONG)
    await asyncio.wait_for(store.started.wait(), 2)
    m2 = await _anchor(conversations, conv)

    async def broken(group_id, names) -> int:  # noqa: ANN001
        raise ConnectionError("neo4j down")

    store.remove_episodes = broken  # type: ignore[method-assign]
    swap = asyncio.create_task(svc.set_speaker_role("u", conv.id, m2.id, "me"))
    await asyncio.sleep(0.05)
    store.gate.set()
    with pytest.raises(GraphUnavailableError):
        await asyncio.wait_for(swap, 5)
    assert await _roles(conversations, conv) == {"m1": "me", "m2": "contact", "m3": "me", "m4": "contact"}

    await svc.wait_graph_tasks()
    await ingest.wait_tasks()
    assert sorted(a["name"] for a in store.added) == [
        "c1:m1",
        "c1:m2",
        "c1:m3",
        "c1:m4",
    ]  # 남은 세 건도 들어갔다
    assert all(m.graph_ingested_at is not None for m in await conversations.list_messages("u", conv.id))


class BrokenRemoval(FakeGraphStore):
    async def remove_episodes(self, group_id, names) -> int:  # noqa: ANN001
        raise ConnectionError("그래프에 닿지 못했다 — 이 문장은 응답에 나가면 안 된다")


async def test_nothing_changes_when_the_graph_cannot_be_cleaned() -> None:
    conversations, svc, store, _, boss, conv = await _setup("sync", store=BrokenRemoval())
    await svc.import_messages("u", conv.id, WRONG)
    m2 = await _anchor(conversations, conv)
    with pytest.raises(GraphUnavailableError) as err:
        await svc.set_speaker_role("u", conv.id, m2.id, "me")
    assert err.value.status_code == 503 and "닿지 못했다" not in err.value.message
    assert await _roles(conversations, conv) == {"m1": "me", "m2": "contact", "m3": "me", "m4": "contact"}
    assert await _style_lines(conversations, boss) == set(ASKS)
    assert (await conversations.conversation_stats("u", conv.id)).graph_ingested == 4
    assert len(store.added) == 4 and len(svc._conv_locks) == 0


async def test_overlapping_index_job_is_followed_by_the_new_one() -> None:
    """가져오기의 묶음 생성이 아직 도는 중에 고쳐도 된다 — 뒤에 줄을 서고, 마지막 묶음이 바뀐 역할이다."""
    embedder = SlowEmbedder(delay=0.05)
    conversations, svc, _, _, boss, conv = await _setup(with_graph=False, embedder=embedder, index_wait=0.0)
    imported = await svc.import_messages("u", conv.id, WRONG)
    assert imported.status == "processing"
    m2 = await _anchor(conversations, conv)

    r = await svc.set_speaker_role("u", conv.id, m2.id, "me")
    assert r.changed and r.job is not None and r.job.status == "processing"  # 요청은 기다리지 않는다
    assert await _roles(conversations, conv) == {"m1": "contact", "m2": "me", "m3": "contact", "m4": "me"}

    await svc.wait_graph_tasks()
    assert (await conversations.get_job("u", imported.id)).status == "completed"
    done = await conversations.get_job("u", r.job.id)
    assert (done.status, done.episodes) == ("completed", 2)
    assert await _style_lines(conversations, boss) == set(ANSWERS)
    assert not svc._index_tasks and len(svc._conv_locks) == 0


async def test_a_processing_role_job_is_resumed_after_a_restart() -> None:
    """서버가 묶음 생성 도중 죽어도 역할은 이미 바뀌어 있고, 남은 작업은 시작 때 이어받는다."""
    embedder = SlowEmbedder(delay=0.05)
    conversations, svc, _, _, boss, conv = await _setup(with_graph=False, embedder=embedder, index_wait=None)
    await svc.import_messages("u", conv.id, WRONG)
    svc._index_wait = 0.0
    m2 = await _anchor(conversations, conv)
    r = await svc.set_speaker_role("u", conv.id, m2.id, "me")
    assert r.job is not None and r.job.status == "processing"
    running = list(svc._index_tasks.values())
    for task in running:  # 서버가 죽었다
        task.cancel()
    await asyncio.gather(*running, return_exceptions=True)
    await asyncio.sleep(0)

    restarted = ImportService(InMemoryContactRepo(), conversations, FakeEmbedder(dim=32))
    assert await restarted.resume_indexing() == 1
    await restarted.wait_graph_tasks()
    assert (await conversations.get_job("u", r.job.id)).status == "completed"
    assert await _style_lines(conversations, boss) == set(ANSWERS)


# ---- API -----------------------------------------------------------------------------------------

RAW = (
    "2026-10-05 09:25 | me | 보고서 언제 돼요?\n"
    "2026-10-05 09:31 | contact | 수요일까지 드릴게요.\n"
    "2026-10-05 09:40 | me | 화요일은 어려워요?\n"
    "2026-10-05 09:52 | contact | 네, 수요일로 부탁드려요.\n"
)


def _room(client: TestClient, h: dict[str, str], key: str = "c1") -> tuple[str, list[dict]]:
    cid = client.post(
        "/v1/contacts",
        json={"alias": f"박지현-{key}", "relationship_type": "superior", "use_default_persona": True},
        headers=h,
    ).json()["id"]
    conv = client.post(
        "/v1/conversations", json={"contact_id": cid, "source_key": key, "me_name": "박지현"}, headers=h
    )
    assert conv.status_code == 201, conv.text
    conv_id = conv.json()["id"]
    job = client.post(f"/v1/conversations/{conv_id}/imports/raw", json={"text": RAW}, headers=h)
    assert job.status_code == 202, job.text
    return conv_id, client.get(f"/v1/conversations/{conv_id}/messages", headers=h).json()


def test_api_fixes_roles_without_deleting_the_room(
    client: TestClient, user_a: dict[str, str], user_b: dict[str, str]
) -> None:
    conv_id, messages = _room(client, user_a)
    url = f"/v1/conversations/{conv_id}/speaker-roles"
    answer = next(m for m in messages if m["text"] == "수요일까지 드릴게요.")
    assert answer["speaker_role"] == "contact"
    body = {"message_id": answer["id"], "speaker_role": "me", "me_name": "김민수"}

    # 남의 대화방·없는 메시지는 404, 모르는 역할·깨진 이름은 422 — 아무것도 바뀌지 않는다
    assert client.put(url, json=body, headers=user_b).status_code == 404
    missing = client.put(url, json={**body, "message_id": str(uuid4())}, headers=user_a)
    assert (missing.status_code, missing.json()["code"]) == (404, "not_found")
    assert client.put(url, json={**body, "speaker_role": "skip"}, headers=user_a).status_code == 422
    assert client.put(url, json={**body, "me_name": "김\x00민수"}, headers=user_a).status_code == 422
    assert client.put(url, json={**body, "me_name": "가" * 51}, headers=user_a).status_code == 422
    assert client.put(url, json=body).status_code == 401
    stats = client.get(f"/v1/conversations/{conv_id}/stats", headers=user_a).json()
    assert (stats["messages"], stats["my_messages"]) == (4, 2)

    r = client.put(url, json=body, headers=user_a)
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == {
        "conversation_id",
        "changed",
        "messages",
        "my_messages",
        "me_name",
        "graph_episodes_removed",
        "graph_requeued",
        "job",
    }
    assert (out["changed"], out["messages"], out["my_messages"], out["me_name"]) == (True, 4, 2, "김민수")
    assert (out["graph_episodes_removed"], out["graph_requeued"]) == (0, 0)
    assert (out["job"]["status"], out["job"]["episodes"], out["job"]["graph_status"]) == (
        "completed",
        2,
        "skipped",
    )
    assert client.get(f"/v1/imports/{out['job']['id']}", headers=user_a).json()["status"] == "completed"

    after = client.get(f"/v1/conversations/{conv_id}/messages", headers=user_a).json()
    assert [m["id"] for m in after] == [m["id"] for m in messages]  # 메시지는 그대로, 역할만 바뀐다
    assert [m["speaker_role"] for m in after] == ["contact", "me", "contact", "me"]
    assert client.get(f"/v1/conversations/{conv_id}", headers=user_a).json()["me_name"] == "김민수"
    stats = client.get(f"/v1/conversations/{conv_id}/stats", headers=user_a).json()
    assert (stats["messages"], stats["my_messages"], stats["episodes"]) == (4, 2, 2)
    assert stats["latest_job"]["id"] == out["job"]["id"]

    again = client.put(url, json=body, headers=user_a)
    assert again.status_code == 200
    assert (again.json()["changed"], again.json()["job"], again.json()["messages"]) == (False, None, 0)
    after_again = client.get(f"/v1/conversations/{conv_id}/messages", headers=user_a).json()
    assert [m["speaker_role"] for m in after_again] == ["contact", "me", "contact", "me"]


def test_api_reingests_the_graph_on_a_graph_server() -> None:
    settings = Settings(_env_file=None, llm_provider="fake", embedding_provider="fake")  # type: ignore[call-arg]
    client = TestClient(create_app(settings))
    state = client.app.state.container
    store = FakeGraphStore()
    ingest = GraphIngestService(store, state.conversations, state.contacts)
    state.graph_store, state.graph_ingest = store, ingest
    state.import_service._graph_ingest, state.import_service.graph_mode = ingest, "sync"
    h = {"X-User-Id": "user-a"}
    conv_id, messages = _room(client, h)
    assert len(store.added) == 4

    body = {"message_id": messages[1]["id"], "speaker_role": "me", "me_name": "김민수"}
    out = client.put(f"/v1/conversations/{conv_id}/speaker-roles", json=body, headers=h).json()
    assert (out["changed"], out["graph_episodes_removed"], out["graph_requeued"]) == (True, 4, 4)
    assert (out["job"]["graph_status"], out["job"]["graph_episodes"]) == ("completed", 4)
    assert len(store.added) == 4
    assert any("김민수 → 박지현-c1: 수요일(=" in a["body"] for a in store.added)
    stats = client.get(f"/v1/conversations/{conv_id}/stats", headers=h).json()
    assert (stats["graph_ingested"], stats["graph_pending"]) == (4, 0)


def test_api_requires_csrf_with_a_cookie_session() -> None:
    settings = Settings(_env_file=None, llm_provider="fake", embedding_provider="fake")  # type: ignore[call-arg]
    client = TestClient(create_app(settings))
    reg = client.post(
        "/v1/auth/register", json={"username": "minsu_kim", "password": "correct horse battery"}
    )
    assert reg.status_code == 201, reg.text
    csrf = {"X-CSRF-Token": reg.json()["csrf_token"]}
    conv_id, messages = _room(client, csrf)
    url = f"/v1/conversations/{conv_id}/speaker-roles"
    body = {"message_id": messages[1]["id"], "speaker_role": "me"}
    assert client.put(url, json=body).status_code == 403
    assert client.put(url, json=body, headers={"X-CSRF-Token": "nope"}).status_code == 403
    assert [m["speaker_role"] for m in client.get(f"/v1/conversations/{conv_id}/messages").json()][
        1
    ] == "contact"
    ok = client.put(url, json=body, headers=csrf)
    assert ok.status_code == 200 and ok.json()["changed"] is True
