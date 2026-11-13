"""거듭 실패하는 메시지의 그래프 수집 (2026-11-13).

에피소드 하나가 늘 실패하면 예전에는 그 사용자의 수집이 거기서 멈추고 이어받을 때마다 다시 시도했다.
이제 메시지마다 실패를 세어 `max_attempts`번까지만 시도하고, 넘으면 보류하고 다음 메시지로 넘어간다.
일시 장애(연결·요청 한도 등)는 세지 않는다.
"""

from __future__ import annotations

import uuid
from collections import Counter

import pytest

from app.ai.embeddings import FakeEmbedder
from app.graph.ingest import GIVE_UP_STREAK, GraphIngestService, is_transient
from app.services.import_service import ImportMessage, ImportPayload, ImportService
from tests.test_graph import MESSAGES_C1, MESSAGES_C2, FakeGraphStore, _setup

pytestmark = pytest.mark.anyio


class BadOutputError(Exception):
    """모델 출력이 어긋났다 — 메시지에 달린 실패."""


class RateLimitError(Exception):
    """이름으로 알아보는 일시 장애 (anthropic·graphiti의 같은 이름 예외를 흉내)."""


def _failing(
    store: FakeGraphStore, failures: dict[str, int], exc: type[Exception] = BadOutputError
) -> Counter:
    """이름별로 정한 횟수만큼 실패한 뒤 성공하는 add_episode로 바꾼다. 호출 수를 돌려준다."""
    calls: Counter = Counter()
    orig = store.add_episode

    async def add_episode(**kw):  # noqa: ANN003, ANN202
        name = kw["name"]
        calls[name] += 1
        if failures.get(name, 0) > 0:
            failures[name] -= 1
            raise exc("이 문장은 기록에 남으면 안 된다")
        await orig(**kw)

    store.add_episode = add_episode  # type: ignore[method-assign]
    return calls


async def _imported(max_attempts: int = 3):  # noqa: ANN202
    _, conversations, store, ingest, svc, _, _, c1, c2 = await _setup()
    ingest.max_attempts = max_attempts
    job = await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))
    await svc.import_messages("u", c2.id, ImportPayload(messages=MESSAGES_C2))
    return conversations, store, ingest, job, c1


def test_is_transient_tells_outages_from_message_failures() -> None:
    class OverloadedApiError(Exception):
        status_code = 529

    class BadRequestApiError(Exception):
        status_code = 400

    assert is_transient(RateLimitError())
    assert is_transient(ConnectionError()) and is_transient(TimeoutError())
    assert is_transient(OverloadedApiError())
    assert not is_transient(BadRequestApiError())
    assert not is_transient(BadOutputError()) and not is_transient(ValueError())


async def test_a_flaky_episode_is_retried_in_the_same_run() -> None:
    conversations, store, ingest, _, c1 = await _imported()
    calls = _failing(store, {"c1:m02": 2})
    assert await ingest.ingest_user("u") == 3
    assert calls["c1:m02"] == 3  # 두 번 실패, 세 번째에 성공
    assert [a["name"] for a in store.added] == ["c1:m01", "c2:m01", "c1:m02"]
    m02 = (await conversations.list_messages("u", c1.id))[1]
    assert (m02.graph_attempts, m02.graph_held_at, m02.graph_ingested_at is not None) == (2, None, True)


async def test_an_episode_that_always_fails_is_held_and_the_rest_goes_on() -> None:
    conversations, store, ingest, job, c1 = await _imported()
    calls = _failing(store, {"c1:m01": 99})
    assert await ingest.ingest_user("u", job.id) == 2  # 실패한 것만 빼고 들어간다
    assert calls["c1:m01"] == 3
    assert [a["name"] for a in store.added] == ["c2:m01", "c1:m02"]
    m01 = (await conversations.list_messages("u", c1.id))[0]
    assert (m01.graph_attempts, m01.graph_ingested_at) == (3, None)
    assert m01.graph_held_at is not None
    done = await conversations.get_job("u", job.id)
    assert (done.graph_status, done.graph_episodes) == ("completed", 2)
    # 통계는 보류를 따로 센다 — 앞으로 넣을 것(pending)에는 들어가지 않는다
    stats = await conversations.conversation_stats("u", c1.id)
    assert (stats.messages, stats.graph_ingested, stats.graph_held, stats.graph_pending) == (2, 1, 1, 0)

    # 보류된 메시지는 미수집으로 세지 않는다 — 이어받기도, 다음 수집도 모델을 다시 부르지 않는다
    assert await conversations.users_with_pending_graph() == []
    assert await ingest.resume_all() == {}
    assert await ingest.ingest_user("u") == 0
    assert calls["c1:m01"] == 3


async def test_attempts_are_remembered_across_runs() -> None:
    conversations, store, ingest, _, c1 = await _imported(max_attempts=3)
    # 한 번 실패한 채로 일시 장애가 나 실행이 끝났다고 치자: 다음 실행은 남은 두 번만 쓴다
    first = (await conversations.list_messages("u", c1.id))[0]
    await conversations.record_graph_failure("u", first.id, False, first.sent_at)
    calls = _failing(store, {"c1:m01": 99})
    assert await ingest.ingest_user("u") == 2
    assert calls["c1:m01"] == 2


async def test_an_outage_stops_the_run_without_counting() -> None:
    conversations, store, ingest, job, c1 = await _imported()
    calls = _failing(store, {"c1:m01": 99}, RateLimitError)
    with pytest.raises(RateLimitError):
        await ingest.ingest_user("u", job.id)
    assert calls["c1:m01"] == 1 and store.added == []  # 다시 시도하지도, 뒤 메시지로 넘어가지도 않는다
    m01 = (await conversations.list_messages("u", c1.id))[0]
    assert (m01.graph_attempts, m01.graph_held_at) == (0, None)
    failed = await conversations.get_job("u", job.id)
    assert (failed.graph_status, failed.graph_error) == ("failed", "RateLimitError")
    assert await conversations.users_with_pending_graph() == ["u"]  # 장애가 풀리면 이어받는다


async def test_failures_on_every_message_stop_the_run_instead_of_holding_everything() -> None:
    conversations, store, ingest, job, _ = await _imported()
    calls = _failing(store, {"c1:m01": 99, "c2:m01": 99, "c1:m02": 99})
    with pytest.raises(BadOutputError):
        await ingest.ingest_user("u", job.id)
    assert sum(calls.values()) == 3 * GIVE_UP_STREAK  # 세 번째 메시지는 건드리지 않았다
    assert calls["c1:m02"] == 0
    failed = await conversations.get_job("u", job.id)
    assert (failed.graph_status, failed.graph_error) == ("failed", "BadOutputError")
    assert await conversations.users_with_pending_graph() == ["u"]


async def test_without_a_limit_a_failure_stops_the_run_as_before() -> None:
    conversations, store, ingest, _, c1 = await _imported(max_attempts=0)
    calls = _failing(store, {"c1:m01": 1})
    with pytest.raises(BadOutputError):
        await ingest.ingest_user("u")
    assert calls["c1:m01"] == 1
    assert (await conversations.list_messages("u", c1.id))[0].graph_attempts == 0


async def test_reset_releases_held_messages() -> None:
    conversations, store, ingest, _, c1 = await _imported()
    _failing(store, {"c1:m01": 3})  # 세 번 실패해 보류된 뒤에는 성공한다
    assert await ingest.ingest_user("u") == 2
    assert await conversations.users_with_pending_graph() == []

    _, reset = await ingest.reset("u")
    assert reset == 3  # 들어간 2건 + 보류된 1건
    m01 = (await conversations.list_messages("u", c1.id))[0]
    assert (m01.graph_attempts, m01.graph_held_at) == (0, None)
    assert await ingest.ingest_user("u", enforce_caps=False) == 3


def test_stats_api_reports_held_messages() -> None:
    from tests.test_graph_caps import _graph_client

    client, store = _graph_client()
    client.app.state.container.graph_ingest.max_attempts = 2
    # 붙여넣기 가져오기는 메시지 키를 서버가 만든다 — 처음 본 에피소드 하나를 늘 실패시킨다
    calls: Counter = Counter()
    orig = store.add_episode

    async def add_episode(**kw):  # noqa: ANN003, ANN202
        calls[kw["name"]] += 1
        if next(iter(calls)) == kw["name"]:
            raise BadOutputError("기록에 남으면 안 된다")
        await orig(**kw)

    store.add_episode = add_episode  # type: ignore[method-assign]
    h = {"X-User-Id": "held-api"}
    cid = client.post(
        "/v1/contacts",
        json={"alias": "박지현", "relationship_type": "superior", "use_default_persona": True},
        headers=h,
    ).json()["id"]
    conv_id = client.post(
        "/v1/conversations", json={"contact_id": cid, "source_key": "c1"}, headers=h
    ).json()["id"]
    text = "2026-10-05 09:25 | contact | 언제까지 가능해요?\n2026-10-05 09:31 | me | 수요일까지 가능합니다.\n"
    job = client.post(f"/v1/conversations/{conv_id}/imports/raw", json={"text": text}, headers=h)
    assert job.status_code == 202, job.text

    stats = client.get(f"/v1/conversations/{conv_id}/stats", headers=h).json()
    assert sorted(calls.values()) == [1, 2]  # 하나는 두 번 시도하고 보류, 하나는 들어갔다
    assert (stats["graph_ingested"], stats["graph_held"], stats["graph_pending"]) == (1, 1, 0)
    assert stats["graph_limited"] is False and stats["latest_job"]["graph_status"] == "completed"


async def _admin_held(sessions) -> int:  # noqa: ANN001
    from datetime import UTC, datetime, timedelta

    from app.repositories.admin import AdminRepo

    now = datetime.now(UTC)
    raw = await AdminRepo(sessions).overview(
        start=now - timedelta(days=1),
        end=now + timedelta(days=1),
        first_day=now.date(),
        last_day=now.date(),
        today=now.date(),
        offset_hours=0,
        rewrite_limit=0,
        token_limit=0,
    )
    return raw.graph_held


async def test_held_messages_on_postgres(pg_url: str) -> None:
    from app.db.session import create_engine, create_session_factory
    from app.repositories.postgres import PostgresContactRepo, PostgresConversationRepo

    engine = create_engine(pg_url)
    sessions = create_session_factory(engine)
    try:
        contacts, conversations = PostgresContactRepo(sessions), PostgresConversationRepo(sessions)
        store = FakeGraphStore()
        ingest = GraphIngestService(store, conversations, contacts, max_attempts=2)
        svc = ImportService(contacts, conversations, FakeEmbedder(dim=384))
        user = f"held-{uuid.uuid4().hex[:8]}"
        boss = await contacts.create(user, "박지현", "superior")
        conv = await svc.create_conversation(user, boss.id, "방", "json", "c1", me_name="김민수")
        messages = [
            *MESSAGES_C1,
            ImportMessage(id="m03", speaker="contact", sent_at="2026-10-05T09:40:00+09:00", text="네"),
        ]
        job = await svc.import_messages(user, conv.id, ImportPayload(messages=messages))
        await conversations.update_job_graph(user, job.id, "pending", 0, None)

        calls = _failing(store, {"c1:m02": 99})
        assert await ingest.ingest_user(user, job.id) == 2
        assert calls["c1:m02"] == 2
        rows = await conversations.list_messages(user, conv.id)
        assert [(m.graph_attempts, m.graph_held_at is not None) for m in rows] == [
            (0, False),
            (2, True),
            (0, False),
        ]
        assert user not in await conversations.users_with_pending_graph()
        assert (await conversations.get_job(user, job.id)).graph_status == "completed"
        stats = await conversations.conversation_stats(user, conv.id)
        assert (stats.messages, stats.graph_ingested, stats.graph_held, stats.graph_pending) == (3, 2, 1, 0)
        held_before_reset = await _admin_held(sessions)
        assert held_before_reset >= 1

        assert await conversations.reset_graph_ingested(user) == 3
        assert user in await conversations.users_with_pending_graph()
        assert await _admin_held(sessions) == held_before_reset - 1  # 관리자 집계에서도 빠진다
        assert (await conversations.conversation_stats(user, conv.id)).graph_held == 0
        assert all(
            m.graph_attempts == 0 and m.graph_held_at is None
            for m in await conversations.list_messages(user, conv.id)
        )
    finally:
        await engine.dispose()


# ---------- 보류만 풀어 다시 시도 (관리자 화면의 "다시 시도") ----------


async def test_release_held_touches_only_held_messages() -> None:
    conversations, store, ingest, _, c1 = await _imported()
    calls = _failing(store, {"c1:m01": 3})  # 세 번 실패해 보류된 뒤에는 성공한다
    assert await ingest.ingest_user("u") == 2
    before = await conversations.list_messages("u", c1.id)
    assert before[0].graph_held_at is not None and before[1].graph_ingested_at is not None

    assert await ingest.release_held() == {"u": 1}
    after = await conversations.list_messages("u", c1.id)
    assert (after[0].graph_attempts, after[0].graph_held_at, after[0].graph_ingested_at) == (0, None, None)
    assert after[1] == before[1]  # 이미 들어간 메시지는 그대로 — 그래프도 지우지 않는다
    assert [a["name"] for a in store.added] == ["c2:m01", "c1:m02"]
    assert await conversations.users_with_pending_graph() == ["u"]
    assert await ingest.release_held() == {}  # 풀 것이 없으면 아무것도 바꾸지 않는다

    # 풀린 메시지만 다시 넣는다 (모델 호출 1번)
    assert ingest.start_resume(["u"]) is True
    await ingest.wait_tasks()
    assert calls["c1:m01"] == 4 and calls["c1:m02"] == 1
    assert [a["name"] for a in store.added] == ["c2:m01", "c1:m02", "c1:m01"]
    assert await conversations.users_with_pending_graph() == []
    assert ingest.start_resume([]) is False


async def test_release_held_leaves_counted_but_unheld_messages_and_other_users() -> None:
    conversations, _, ingest, _, c1 = await _imported()
    rows = await conversations.list_messages("u", c1.id)
    await conversations.record_graph_failure("u", rows[0].id, False, rows[0].sent_at)  # 세기만 했다
    await conversations.record_graph_failure("u", rows[1].id, True, rows[1].sent_at)  # 보류

    assert await conversations.release_held_graph("someone-else") == {}
    assert await conversations.release_held_graph("u") == {"u": 1}
    rows = await conversations.list_messages("u", c1.id)
    assert [(m.graph_attempts, m.graph_held_at) for m in rows] == [(1, None), (0, None)]


async def test_retry_after_release_follows_the_caps() -> None:
    conversations, store, ingest, _, _ = await _imported()
    calls = _failing(store, {"c1:m01": 3})
    assert await ingest.ingest_user("u") == 2
    ingest.user_message_cap = 2  # 이미 2건이 들어 있다 — 예산 0

    assert await ingest.release_held() == {"u": 1}
    assert await ingest.resume_users(["u"]) == {}  # 모델을 부르지 않는다
    assert calls["c1:m01"] == 3
    assert await conversations.users_with_pending_graph() == ["u"]  # 예산이 생기면 이어받는다

    ingest.user_message_cap = 3
    assert await ingest.resume_users(["u"]) == {"u": 1}
    assert calls["c1:m01"] == 4


async def test_a_released_message_that_still_fails_is_held_again() -> None:
    conversations, store, ingest, _, c1 = await _imported()
    calls = _failing(store, {"c1:m01": 99})
    assert await ingest.ingest_user("u") == 2
    await ingest.release_held()
    assert await ingest.resume_users(["u"]) == {"u": 0}
    assert calls["c1:m01"] == 6  # 다시 세 번까지만
    m01 = (await conversations.list_messages("u", c1.id))[0]
    assert m01.graph_attempts == 3 and m01.graph_held_at is not None


async def test_release_held_on_postgres(pg_url: str) -> None:
    from app.db.session import create_engine, create_session_factory
    from app.repositories.postgres import PostgresContactRepo, PostgresConversationRepo

    engine = create_engine(pg_url)
    sessions = create_session_factory(engine)
    try:
        contacts, conversations = PostgresContactRepo(sessions), PostgresConversationRepo(sessions)
        store = FakeGraphStore()
        ingest = GraphIngestService(store, conversations, contacts, max_attempts=2)
        svc = ImportService(contacts, conversations, FakeEmbedder(dim=384))
        user, other = f"rel-{uuid.uuid4().hex[:8]}", f"rel-{uuid.uuid4().hex[:8]}"
        convs = {}
        for owner in (user, other):
            boss = await contacts.create(owner, "박지현", "superior")
            convs[owner] = await svc.create_conversation(owner, boss.id, "방", "json", "c1", me_name="김민수")
            await svc.import_messages(owner, convs[owner].id, ImportPayload(messages=MESSAGES_C1))

        calls = _failing(store, {"c1:m02": 4})  # 두 사용자 모두 m02가 두 번씩 실패해 보류된다
        assert await ingest.ingest_user(user) == 1
        assert await ingest.ingest_user(other) == 1
        first = (await conversations.list_messages(other, convs[other].id))[0]
        await conversations.record_graph_failure(other, first.id, False, first.sent_at)  # 세기만 한 것
        held_before = await _admin_held(sessions)
        assert held_before >= 2

        # 사용자를 주면 그 사용자 것만 푼다
        assert await conversations.release_held_graph(user) == {user: 1}
        rows = await conversations.list_messages(user, convs[user].id)
        assert [(m.graph_attempts, m.graph_held_at, m.graph_ingested_at is not None) for m in rows] == [
            (0, None, True),
            (0, None, False),
        ]
        assert await _admin_held(sessions) == held_before - 1
        assert (await conversations.conversation_stats(user, convs[user].id)).graph_held == 0
        assert user in await conversations.users_with_pending_graph()
        assert other not in await conversations.users_with_pending_graph()

        # 사용자를 안 주면 전부 (테스트 DB는 다른 테스트와 함께 쓴다 — 우리 것만 확인한다)
        released = await ingest.release_held()
        assert released[other] == 1 and user not in released
        rows = await conversations.list_messages(other, convs[other].id)
        # 이미 들어간 메시지의 횟수는 그대로다 (보류된 것만 지운다)
        assert [(m.graph_attempts, m.graph_held_at) for m in rows] == [(1, None), (0, None)]

        assert await ingest.resume_users([user, other]) == {user: 1, other: 1}
        assert calls["c1:m02"] == 6
        assert [a["name"] for a in store.added].count("c1:m02") == 2
        assert (await conversations.conversation_stats(user, convs[user].id)).graph_pending == 0
    finally:
        await engine.dispose()
