"""서버 종료는 뒤에서 도는 일을 무한정 기다리지 않는다 (2026-11-14).

예전에는 `wait_graph_tasks`가 시간 제한 없이 기다려, 그래프 수집이 도는 중에 서버를 내리면 수집이 끝날
때까지(실측 75초, 긴 대화는 몇 시간) 요청에 답하지 않았다. 이제
- 수집에는 멈춤 신호를 보내 넣던 에피소드까지만 끝내게 하고,
- 정한 시간(`SHUTDOWN_GRACE_SECONDS`)이 지나면 남은 태스크를 끊는다. 끊긴 일은 다음 시작 때 이어받는다.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import date, timedelta

import pytest

from app.ai.embeddings import FakeEmbedder
from app.graph.ingest import GraphIngestService
from app.repositories.memory import InMemoryContactRepo, InMemoryConversationRepo
from app.repositories.usage import DELETED_USERS, InMemoryUsageRepo
from app.services.import_service import ImportMessage, ImportPayload, ImportService
from tests.test_graph import FakeGraphStore
from tests.test_import_background import SlowEmbedder

pytestmark = pytest.mark.anyio

MESSAGES = [
    ImportMessage(
        id=f"m{i:02d}",
        speaker="me" if i % 2 else "contact",
        sent_at=f"2026-10-05T09:{i:02d}:00+09:00",
        text=f"합성 문장 {i}번입니다.",
    )
    for i in range(1, 9)
]


class SlowGraphStore(FakeGraphStore):
    """에피소드 하나에 시간이 걸린다."""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay
        self.started = 0

    async def add_episode(self, **kw):  # noqa: ANN003, ANN201
        self.started += 1
        await asyncio.sleep(self.delay)
        await super().add_episode(**kw)


async def _setup(delay: float, **caps: int):  # noqa: ANN202
    contacts, conversations = InMemoryContactRepo(), InMemoryConversationRepo()
    store = SlowGraphStore(delay)
    ingest = GraphIngestService(store, conversations, contacts, **caps)
    svc = ImportService(
        contacts, conversations, FakeEmbedder(dim=32), graph_ingest=ingest, graph_mode="background"
    )
    boss = await contacts.create("u", "박지현", "superior")
    conv = await svc.create_conversation("u", boss.id, "방", "json", "c1", me_name="김민수")
    return conversations, store, ingest, svc, conv


async def test_shutdown_lets_the_current_episode_finish_and_leaves_the_rest_for_resume() -> None:
    conversations, store, ingest, svc, conv = await _setup(delay=0.1)
    job = await svc.import_messages("u", conv.id, ImportPayload(messages=MESSAGES))
    await asyncio.sleep(0.15)  # 두 번째 에피소드를 넣는 중

    began = time.monotonic()
    cancelled = await svc.shutdown(grace=5.0)
    took = time.monotonic() - began

    assert cancelled == 0  # 끊지 않았다 — 넣던 것까지 끝내고 스스로 멈췄다
    assert took < 1.0  # 남은 6건(0.6초 이상)을 기다리지 않았다
    done = len(store.added)
    assert 1 <= done < len(MESSAGES) and store.started == done  # 넣다 만 에피소드가 없다
    rows = await conversations.list_messages("u", conv.id)
    assert sum(m.graph_ingested_at is not None for m in rows) == done
    stopped = await conversations.get_job("u", job.id)
    assert (stopped.graph_status, stopped.graph_episodes, stopped.graph_error) == ("pending", done, None)

    # 다음 시작: 새 프로세스의 수집 서비스가 남은 것만 이어서 넣는다
    resumed = GraphIngestService(store, conversations, svc._contacts)
    store.delay = 0
    assert await resumed.resume_all() == {"u": len(MESSAGES) - done}
    assert len(store.added) == len(MESSAGES)
    assert (await conversations.get_job("u", job.id)).graph_status == "completed"


async def test_shutdown_cancels_what_does_not_finish_in_time_and_returns_the_budget() -> None:
    conversations, store, ingest, svc, conv = await _setup(delay=30.0, user_message_cap=100)
    await svc.import_messages("u", conv.id, ImportPayload(messages=MESSAGES))
    await asyncio.sleep(0.05)  # 첫 에피소드를 넣는 중 (30초짜리)
    today = ingest._today()[0]
    assert await conversations.graph_usage(today, "u") == 1  # 넣기 전에 한 건을 잡아 뒀다

    began = time.monotonic()
    cancelled = await svc.shutdown(grace=0.2)
    took = time.monotonic() - began

    assert cancelled == 1 and took < 2.5
    assert store.added == []
    assert await conversations.graph_usage(today, "u") == 0  # 끊긴 에피소드의 예산은 돌려놓았다
    assert all(m.graph_ingested_at is None for m in await conversations.list_messages("u", conv.id))
    assert await conversations.users_with_pending_graph() == ["u"]  # 다음 시작 때 이어받는다


async def test_shutdown_cancels_a_slow_index_job_and_it_stays_resumable() -> None:
    contacts, conversations = InMemoryContactRepo(), InMemoryConversationRepo()
    svc = ImportService(contacts, conversations, SlowEmbedder(delay=30.0), index_wait=0)
    boss = await contacts.create("u", "박지현", "superior")
    conv = await svc.create_conversation("u", boss.id, "방", "json", "c1")
    job = await svc.import_messages("u", conv.id, ImportPayload(messages=MESSAGES))
    assert job.status == "processing"

    assert await svc.shutdown(grace=0.1) == 1
    left = await conversations.get_job("u", job.id)
    assert left.status == "processing"  # 실패로 적지 않는다 — 재시작이 이어서 만든다
    assert [j.id for j in await conversations.processing_jobs()] == [job.id]


async def test_shutdown_with_nothing_running_returns_at_once() -> None:
    _, _, _, svc, _ = await _setup(delay=0)
    began = time.monotonic()
    assert await svc.shutdown(grace=10.0) == 0
    assert time.monotonic() - began < 0.5


async def test_after_a_stop_request_new_ingests_do_nothing() -> None:
    conversations, store, ingest, svc, conv = await _setup(delay=0)
    ingest.request_stop()
    job = await svc.import_messages("u", conv.id, ImportPayload(messages=MESSAGES))
    await svc.wait_graph_tasks()
    assert store.added == []
    assert (await conversations.get_job("u", job.id)).graph_status == "pending"


# ---- 계정 삭제 뒤에도 그날 합계는 줄지 않는다 --------------------------------------------------------


async def test_deleting_a_user_keeps_the_days_totals_in_memory() -> None:
    usage = InMemoryUsageRepo()
    day = date(2026, 11, 14)
    for _ in range(3):
        await usage.reserve_rewrite("u1", day, 10)
    await usage.add_tokens("u1", day, 100, 40)
    await usage.reserve_rewrite("u2", day, 10)

    await usage.delete_user("u1")

    assert (await usage.get("u1", day)).rewrites == 0  # 그 사용자의 행은 없다
    total = await usage.totals(day)
    assert (total.rewrites, total.input_tokens, total.output_tokens) == (4, 100, 40)
    assert (await usage.get(DELETED_USERS, day)).rewrites == 3


async def test_deleting_a_user_keeps_the_days_totals_on_postgres(pg_url: str) -> None:
    from app.db.session import create_engine, create_session_factory
    from app.repositories.postgres import PostgresConversationRepo
    from app.repositories.usage import PostgresUsageRepo

    engine = create_engine(pg_url)
    sessions = create_session_factory(engine)
    try:
        usage, conversations = PostgresUsageRepo(sessions), PostgresConversationRepo(sessions)
        day = date(2032, 1, 1) + timedelta(days=uuid.uuid4().int % 300)  # 다른 테스트와 겹치지 않는 날
        u1, u2 = f"gone-{uuid.uuid4().hex[:8]}", f"gone-{uuid.uuid4().hex[:8]}"
        for user, n in ((u1, 2), (u2, 1)):
            for _ in range(n):
                await usage.reserve_rewrite(user, day, 10)
        await usage.add_tokens(u1, day, 100, 40)
        await conversations.add_graph_usage(u1, day, 36)
        await conversations.add_graph_usage(u2, day, 4)
        await usage.reserve_rewrite(u1, day + timedelta(days=1), 10)

        await usage.delete_user(u1)
        assert (await usage.get(u1, day)).rewrites == 0
        total = await usage.totals(day)
        assert (total.rewrites, total.input_tokens, total.output_tokens) == (3, 100, 40)
        assert await conversations.graph_usage(day) == 40  # 하루 전체 수집 상한이 보는 값
        assert await conversations.graph_usage(day, u1) == 0
        assert (await usage.totals(day + timedelta(days=1))).rewrites == 1

        await usage.delete_user(u2)  # 같은 날의 이름 없는 행에 더해진다
        assert (await usage.totals(day)).rewrites == 3
        assert await conversations.graph_usage(day) == 40
        assert await conversations.graph_usage(day, DELETED_USERS) == 40
    finally:
        await engine.dispose()
