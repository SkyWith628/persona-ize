"""그래프 수집 비용 상한 (`GRAPH_INGEST_USER_MESSAGE_CAP`, `GRAPH_INGEST_DAILY_GLOBAL_CAP`).

Neo4j·모델 호출 없이 FakeGraphStore로 본다.
- 상한에 닿으면 **가장 최근** 메시지를 예산만큼 골라 시각 순으로 넣고 `limited`.
- 넣은 양은 지워지지 않는 카운터(사용자·일)에 남는다 — 지우고 다시 올려도, 서버를 다시 띄워도
  오늘 쓴 예산은 되살아나지 않는다 (2026-11-11 검토 D4).
- 긴 메시지는 더 많이 센다. 재시작 이어받기는 상한에 닿은 사용자를 다시 돌리지 않고 작업을 `limited`로
  정리한다. 날이 바뀌면 이어서 넣는다. 운영자 경로(스크립트)는 상한도 카운터도 없다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.ai.embeddings import FakeEmbedder
from app.core.config import Settings
from app.domain.conversation import graph_units
from app.graph.ingest import GraphIngestService, pick_recent
from app.main import create_app
from app.repositories.memory import InMemoryContactRepo, InMemoryConversationRepo
from app.services.import_service import ImportMessage, ImportPayload, ImportService
from tests.test_graph import MESSAGES_C1, MESSAGES_C2, FakeGraphStore

pytestmark = pytest.mark.anyio

# c1: 09:25(상대), 09:31(나) / c2: 09:28(나) → 전체 시각 순은 c1:m01, c2:m01, c1:m02
MESSAGES_C3 = [
    ImportMessage(
        id="m01", speaker="me", sent_at="2026-10-07T10:00:00+09:00", text="내일 회의 자료 보냈습니다."
    ),
    ImportMessage(id="m02", speaker="contact", sent_at="2026-10-07T10:05:00+09:00", text="확인했어요."),
]


@dataclass
class CountingStore(FakeGraphStore):
    """`add_episode` 호출(= 모델 호출이 드는 수집)의 누적 수. 지워도 줄지 않는다."""

    calls: int = 0

    async def add_episode(self, **kw) -> None:  # noqa: ANN003
        self.calls += 1
        await super().add_episode(**kw)


def _msgs(n: int, start: str = "2026-10-05T09:00:00+09:00", prefix: str = "m") -> list[ImportMessage]:
    base = datetime.fromisoformat(start)
    return [
        ImportMessage(
            id=f"{prefix}{i:02d}",
            speaker="me" if i % 2 else "contact",
            sent_at=base + timedelta(minutes=i),
            text=f"메시지 {prefix}{i}",
        )
        for i in range(n)
    ]


async def _setup(graph_mode: str = "sync", store: FakeGraphStore | None = None, **caps: int):  # noqa: ANN202
    contacts = InMemoryContactRepo()
    conversations = InMemoryConversationRepo()
    store = store or FakeGraphStore()
    ingest = GraphIngestService(store, conversations, contacts, **caps)
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
    return contacts, conversations, store, ingest, svc, boss, c1, c2


def _next_day(ingest: GraphIngestService, days: int = 1) -> None:
    """날이 바뀐 것처럼 만든다 (사용량 날짜가 하루 지난다)."""
    today, started = ingest._today()
    shift = timedelta(days=days)
    ingest._today = lambda now=None: (today + shift, started + shift)  # type: ignore[method-assign]


async def test_user_cap_ingests_the_most_recent_messages_then_marks_job_limited() -> None:
    _, conversations, store, ingest, svc, boss, c1, c2 = await _setup(graph_mode="off", user_message_cap=2)
    await svc.import_messages("u", c2.id, ImportPayload(messages=MESSAGES_C2))
    svc.graph_mode = "sync"
    job = await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))

    # 미수집 3건(c1:m01 09:25, c2:m01 09:28, c1:m02 09:31) 중 가장 최근 2건을, 방을 가로질러 시각 순으로
    assert [a["name"] for a in store.added] == ["c2:m01", "c1:m02"]
    assert (job.status, job.graph_status, job.graph_episodes, job.graph_error) == (
        "completed",
        "limited",
        2,
        "user_cap",
    )
    assert job.episodes == 1  # 문체 묶음(C)은 상한과 무관하게 전부 만들어진다
    assert await conversations.users_with_pending_graph() == ["u"]  # 남은 메시지는 미수집 그대로
    assert await ingest.is_limited("u") is True
    assert await ingest.remaining("u") == (0, "user_cap")

    # 상한에 닿은 뒤의 가져오기: 모델 호출 없이 곧바로 limited
    c3 = await svc.create_conversation("u", boss.id, None, "json", "c3", me_name="김민수")
    later = await svc.import_messages("u", c3.id, ImportPayload(messages=MESSAGES_C3))
    assert (later.graph_status, later.graph_episodes, later.graph_error) == ("limited", 0, "user_cap")
    assert len(store.added) == 2 and store.prepared == 1

    # 서버 재시작 이어받기는 상한에 닿은 사용자를 다시 돌리지 않는다
    assert await ingest.resume_all() == {}
    assert await ingest.resume_all() == {}
    assert len(store.added) == 2 and store.prepared == 1


async def test_a_long_history_fills_the_graph_from_the_recent_end() -> None:
    """상한 300에서 1,000일 치 대화를 가져오면 최근 300건이 들어간다 (예전에는 가장 오래된 300건)."""
    _, conversations, store, ingest, svc, _, c1, _ = await _setup(user_message_cap=300)
    base = datetime.fromisoformat("2024-01-01T09:00:00+09:00")
    msgs = [
        ImportMessage(
            id=f"m{i:04d}",
            speaker="me" if i % 2 else "contact",
            sent_at=base + timedelta(days=i),
            text=f"{i}번째 날의 약속",
        )
        for i in range(1000)
    ]
    job = await svc.import_messages("u", c1.id, ImportPayload(messages=msgs))
    assert (job.graph_status, job.graph_episodes, job.graph_error) == ("limited", 300, "user_cap")
    names = [a["name"] for a in store.added]
    assert names == [f"c1:m{i:04d}" for i in range(700, 1000)]  # 최근 300건, 넣는 순서는 시각 순
    stats = await conversations.conversation_stats("u", c1.id)
    assert (stats.graph_ingested, stats.graph_pending) == (300, 700)


@pytest.mark.parametrize("cap_kind", ["user_message_cap", "daily_global_cap"])
@pytest.mark.parametrize(("cap", "n"), [(3, 2), (3, 3), (3, 4), (1, 1), (1, 2)])
async def test_cap_boundaries(cap_kind: str, cap: int, n: int) -> None:
    _, _, store, ingest, svc, _, c1, _ = await _setup(**{cap_kind: cap})
    job = await svc.import_messages("u", c1.id, ImportPayload(messages=_msgs(n)))
    assert job.graph_episodes == min(cap, n) == len(store.added)
    assert job.graph_status == ("limited" if n > cap else "completed")  # 딱 맞으면 completed
    reason = {"user_message_cap": "user_cap", "daily_global_cap": "daily_cap"}[cap_kind]
    assert await ingest.remaining("u") == (max(cap - n, 0), reason)


async def test_deleting_and_reimporting_does_not_buy_more_ingestion() -> None:
    """상한 2건: 지우고 다시 올리기를 5번 해도 모델 호출이 드는 수집은 2건뿐이다 (예전에는 10건)."""
    _, conversations, store, ingest, svc, boss, _, _ = await _setup(store=CountingStore(), user_message_cap=2)
    statuses = []
    for round_no in range(5):
        conv = await svc.create_conversation("u", boss.id, None, "json", f"r{round_no}", me_name="김민수")
        job = await svc.import_messages("u", conv.id, ImportPayload(messages=_msgs(2)))
        statuses.append((job.graph_status, job.graph_episodes))
        await svc.delete_conversation("u", conv.id)
    assert statuses == [("completed", 2)] + [("limited", 0)] * 4
    assert store.calls == 2
    assert await conversations.count_graph_ingested("u") == 0  # 그래프에는 아무것도 없지만
    assert await ingest.used_by("u") == 2  # 오늘 넣은 양은 남는다
    assert await ingest.remaining("u") == (0, "user_cap")

    # 날이 바뀌면 "지금 그래프에 든 양"(0) 기준으로 다시 여유가 생긴다
    _next_day(ingest)
    assert await ingest.remaining("u") == (2, "user_cap")


async def test_freed_budget_is_used_the_next_day_and_closes_limited_jobs() -> None:
    _, conversations, store, ingest, svc, _, c1, c2 = await _setup(user_message_cap=2)
    job1 = await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))  # 2건 → 상한에 딱 맞음
    assert (job1.graph_status, job1.graph_episodes) == ("completed", 2)
    job2 = await svc.import_messages("u", c2.id, ImportPayload(messages=MESSAGES_C2))
    assert (job2.graph_status, job2.graph_episodes, job2.graph_error) == ("limited", 0, "user_cap")

    # 대화방을 지워도 오늘은 여유가 생기지 않는다
    await svc.delete_conversation("u", c1.id)
    assert await ingest.remaining("u") == (0, "user_cap")
    assert await ingest.resume_all() == {} and store.added == []

    # 다음 날: 그래프에 든 것이 없으니 상한만큼 다시 넣을 수 있고, 남아 있던 작업이 정리된다
    _next_day(ingest)
    assert await ingest.remaining("u") == (2, "user_cap")
    assert await ingest.resume_all() == {"u": 1}
    assert [a["name"] for a in store.added] == ["c2:m01"]
    closed = await conversations.get_job("u", job2.id)
    assert (closed.graph_status, closed.graph_error) == ("completed", None)
    assert await conversations.users_with_pending_graph() == []


async def test_daily_global_cap_is_shared_by_all_users_and_resumes_next_day() -> None:
    contacts, conversations, store, ingest, svc, _, c1, _ = await _setup(daily_global_cap=2)
    other = await contacts.create("v", "최유진", "friend")
    cv = await svc.create_conversation("v", other.id, None, "json", "v1", me_name="이서연")

    job_u = await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))  # 2건 → 하루 예산 소진
    assert (job_u.graph_status, job_u.graph_episodes) == ("completed", 2)
    job_v = await svc.import_messages("v", cv.id, ImportPayload(messages=MESSAGES_C3))
    assert (job_v.graph_status, job_v.graph_episodes, job_v.graph_error) == ("limited", 0, "daily_cap")
    assert await ingest.remaining("v") == (0, "daily_cap")
    assert await ingest.resume_all() == {}  # 오늘은 더 돌지 않는다
    assert len(store.added) == 2

    # 수집한 대화를 지워도 오늘 쓴 예산은 되살아나지 않는다 (지웠다 다시 올려 상한을 넘기는 것 방지)
    await svc.delete_conversation("u", c1.id)
    assert await conversations.count_graph_ingested() == 0
    assert await ingest.remaining("v") == (0, "daily_cap")

    # 서버를 다시 띄워도(새 서비스 객체 = 프로세스 메모리 없음) 마찬가지다 — 카운터가 저장소에 있다
    restarted = GraphIngestService(store, conversations, contacts, daily_global_cap=2)
    assert await restarted.remaining("v") == (0, "daily_cap")
    assert await restarted.resume_all() == {} and len(store.added) == 0

    # 날이 바뀌면 재시작 이어받기가 남은 것을 넣는다
    _next_day(ingest)
    assert await ingest.resume_all() == {"v": 2}
    assert [a["name"] for a in store.added] == ["v1:m01", "v1:m02"]
    assert (await conversations.get_job("v", job_v.id)).graph_status == "completed"


async def test_partial_run_takes_the_latest_message_within_the_daily_cap() -> None:
    _, conversations, store, ingest, svc, _, c1, c2 = await _setup(
        graph_mode="off", daily_global_cap=1, user_message_cap=5
    )
    await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))
    await svc.import_messages("u", c2.id, ImportPayload(messages=MESSAGES_C2))
    assert await ingest.remaining("u") == (1, "daily_cap")  # 두 상한 중 먼저 닿는 쪽
    assert await ingest.ingest_user("u") == 1
    assert [a["name"] for a in store.added] == ["c1:m02"]  # 가장 최근 메시지
    assert await conversations.count_graph_ingested("u") == 1


async def test_long_messages_weigh_more_against_the_cap() -> None:
    assert [graph_units("가" * n) for n in (1, 999, 1000, 2500, 4000)] == [1, 1, 2, 3, 5]
    _, conversations, store, ingest, svc, _, c1, _ = await _setup(user_message_cap=6)
    msgs = [
        ImportMessage(id="m01", speaker="me", sent_at="2026-10-05T09:00:00+09:00", text="짧은 말"),
        ImportMessage(id="m02", speaker="me", sent_at="2026-10-05T09:01:00+09:00", text="가" * 4000),  # 5
        ImportMessage(id="m03", speaker="me", sent_at="2026-10-05T09:02:00+09:00", text="나" * 1500),  # 2
        ImportMessage(id="m04", speaker="me", sent_at="2026-10-05T09:03:00+09:00", text="짧은 말 둘"),  # 1
    ]
    job = await svc.import_messages("u", c1.id, ImportPayload(messages=msgs))
    # 최근 것부터: m04(1) + m03(2) = 3, m02(5)를 더하면 8 > 6 → 거기서 멈춘다 (건수로는 4건 ≤ 6이었다)
    assert [a["name"] for a in store.added] == ["c1:m03", "c1:m04"]
    assert (job.graph_status, job.graph_episodes, job.graph_error) == ("limited", 2, "user_cap")
    assert await ingest.used_by("u") == 3
    assert await ingest.remaining("u") == (3, "user_cap")
    assert await conversations.graph_units_in_graph("u") == 3


async def test_pick_recent_keeps_a_contiguous_recent_range_in_time_order() -> None:
    _, conversations, _, ingest, svc, _, c1, _ = await _setup(graph_mode="off")
    await svc.import_messages("u", c1.id, ImportPayload(messages=_msgs(5)))
    messages = await conversations.list_messages("u", c1.id)
    conv = await conversations.get("u", c1.id)
    pending = [(m, conv, m.text) for m in messages]
    assert [m.source_key for m, _, _ in pick_recent(pending, 2)] == ["m03", "m04"]
    assert pick_recent(pending, 0) == []
    assert pick_recent(pending, 99) == pending


async def test_scripts_calling_ingest_pending_directly_are_not_capped_or_counted() -> None:
    """합성 적재·평가 스크립트(`ingest_pending`)는 설정과 무관하게 전부 넣고, 카운터도 건드리지 않는다."""
    _, conversations, store, ingest, svc, _, c1, c2 = await _setup(
        graph_mode="off", user_message_cap=1, daily_global_cap=1
    )
    await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))
    await svc.import_messages("u", c2.id, ImportPayload(messages=MESSAGES_C2))
    assert await ingest.ingest_pending("u") == 3
    assert len(store.added) == 3
    assert await ingest.ingest_user("u", enforce_caps=False) == 0  # 남은 것 없음
    today, _ = ingest._today()
    assert await conversations.graph_usage(today) == 0  # 하루 전체 예산을 쓰지 않았다
    # 사용자 상한은 "지금 그래프에 든 양"도 보므로 이 사용자의 API 수집은 막힌다
    assert await ingest.remaining("u") == (0, "user_cap")


async def test_graph_reset_reingests_everything_regardless_of_caps() -> None:
    """`scripts/graph_reset.py`의 경로: 지운 만큼 전부 다시 들어가야 한다 (예전에는 상한만큼만)."""
    _, conversations, store, ingest, svc, _, c1, _ = await _setup(graph_mode="off")
    await svc.import_messages("u", c1.id, ImportPayload(messages=_msgs(10)))
    assert await ingest.ingest_pending("u") == 10  # 상한을 걸기 전에 들어가 있던 10건
    ingest.user_message_cap = 4  # 운영에서 상한을 건 뒤 스크립트를 돌린다
    removed, reset = await ingest.reset("u")
    assert (removed, reset) == (10, 10)
    assert await ingest.ingest_user("u", enforce_caps=False) == 10
    assert len(store.added) == 10 and await conversations.users_with_pending_graph() == []
    # 같은 호출을 상한을 따르게 하면 최근 4건만 들어간다 — 스크립트가 이 경로를 쓰면 안 되는 이유
    await ingest.reset("u")
    assert await ingest.ingest_user("u") == 4


async def test_failed_episode_releases_the_reserved_budget() -> None:
    _, conversations, store, ingest, svc, _, c1, _ = await _setup(graph_mode="off", daily_global_cap=2)
    job = await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))
    store.fail_on = "c1:m02"
    with pytest.raises(RuntimeError):
        await ingest.ingest_user("u", job.id)
    failed = await conversations.get_job("u", job.id)
    assert (failed.graph_status, failed.graph_episodes) == ("failed", 1)
    assert await ingest.remaining("u") == (1, "daily_cap")  # 실패한 1건의 예약은 돌려놓는다
    store.fail_on = None
    assert await ingest.ingest_user("u", job.id) == 1
    assert (await conversations.get_job("u", job.id)).graph_status == "completed"
    today, _ = ingest._today()
    assert await conversations.graph_usage(today, "u") == 2


async def test_concurrent_users_never_exceed_the_daily_cap_together() -> None:
    """두 사용자의 수집이 겹쳐도(각자 예산이 남아 보이는 순간에 시작) 합계는 하루 상한을 넘지 않는다."""
    contacts, conversations, store, ingest, svc, _, c1, _ = await _setup(graph_mode="off", daily_global_cap=3)
    other = await contacts.create("v", "최유진", "friend")
    cv = await svc.create_conversation("v", other.id, None, "json", "v1", me_name="이서연")
    await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))  # 2건
    await svc.import_messages("v", cv.id, ImportPayload(messages=MESSAGES_C3))  # 2건
    orig = store.add_episode

    async def slow_add(**kw):  # noqa: ANN003, ANN202
        await asyncio.sleep(0.01)  # 모델 호출 흉내 — 그 사이 다른 사용자의 수집이 끼어든다
        await orig(**kw)

    store.add_episode = slow_add  # type: ignore[method-assign]
    done = await asyncio.gather(ingest.ingest_user("u"), ingest.ingest_user("v"))
    assert sum(done) == 3 and len(store.added) == 3
    assert await conversations.count_graph_ingested() == 3
    assert await ingest.remaining("u") == (0, "daily_cap")


async def test_two_background_imports_share_one_user_cap() -> None:
    _, conversations, store, ingest, svc, _, c1, c2 = await _setup(
        graph_mode="background", user_message_cap=4
    )
    orig = store.add_episode

    async def slow_add(**kw):  # noqa: ANN003, ANN202
        await asyncio.sleep(0.005)
        await orig(**kw)

    store.add_episode = slow_add  # type: ignore[method-assign]
    job1 = await svc.import_messages(
        "u", c1.id, ImportPayload(messages=_msgs(3, start="2026-10-06T09:00:00+09:00"))
    )
    job2 = await svc.import_messages(
        "u", c2.id, ImportPayload(messages=_msgs(3, start="2026-10-05T09:00:00+09:00", prefix="x"))
    )
    await svc.wait_graph_tasks()
    assert len(store.added) == 4 and await conversations.count_graph_ingested("u") == 4
    states = [(await conversations.get_job("u", j.id)).graph_status for j in (job1, job2)]
    assert "limited" in states and set(states) <= {"completed", "limited"}  # pending으로 남지 않는다


# ---- 재시작 뒤 작업 상태 ---------------------------------------------------------------------------


async def test_jobs_left_running_by_a_dead_process_become_limited_when_the_cap_stops_them() -> None:
    """프로세스가 죽어 running/pending으로 남은 작업: 상한 때문에 다 못 넣으면 limited로 정리한다."""
    _, conversations, store, ingest, svc, _, c1, c2 = await _setup(graph_mode="off", daily_global_cap=2)
    job1 = await svc.import_messages("u", c1.id, ImportPayload(messages=_msgs(5)))
    job2 = await svc.import_messages("u", c2.id, ImportPayload(messages=_msgs(2, prefix="x")))
    await conversations.update_job_graph("u", job1.id, "running", 0, None)  # 수집 도중에 죽었다
    await conversations.update_job_graph("u", job2.id, "pending", 0, None)  # 시작도 못 하고 죽었다

    # 예산 2건만큼 넣고 멈춘다 → 남은 메시지가 있는 두 작업 모두 limited (예전에는 running/pending 그대로)
    assert await ingest.resume_all() == {"u": 2}
    after = [await conversations.get_job("u", j.id) for j in (job1, job2)]
    assert [(j.graph_status, j.graph_error) for j in after] == [("limited", "daily_cap")] * 2

    # 같은 날 다시 떠도 건너뛴다. 상태는 그대로 limited
    assert await ingest.resume_all() == {} and len(store.added) == 2
    assert (await conversations.get_job("u", job1.id)).graph_status == "limited"


async def test_resume_marks_stale_jobs_limited_even_when_nothing_can_be_ingested() -> None:
    _, conversations, store, ingest, svc, _, c1, c2 = await _setup(graph_mode="off", user_message_cap=2)
    await svc.import_messages("u", c1.id, ImportPayload(messages=_msgs(2)))
    assert await ingest.ingest_user("u") == 2  # 상한을 다 썼다
    job = await svc.import_messages("u", c2.id, ImportPayload(messages=_msgs(3, prefix="x")))
    await conversations.update_job_graph("u", job.id, "pending", 0, None)  # 백그라운드 수집 직전에 죽었다

    assert await ingest.resume_all() == {}  # 예산이 없어 모델을 부르지 않는다
    stale = await conversations.get_job("u", job.id)
    assert (stale.graph_status, stale.graph_error) == ("limited", "user_cap")
    assert len(store.added) == 2


async def test_reuploading_a_limited_import_continues_once_budget_allows() -> None:
    _, conversations, store, ingest, svc, _, c1, _ = await _setup(user_message_cap=3)
    job = await svc.import_messages("u", c1.id, ImportPayload(messages=_msgs(5)))
    assert (job.graph_status, job.graph_episodes) == ("limited", 3)

    # 예산이 그대로면 같은 작업을 돌려줄 뿐, 모델을 부르지 않는다
    again = await svc.import_messages("u", c1.id, ImportPayload(messages=_msgs(5)))
    assert (again.id, again.graph_status, len(store.added)) == (job.id, "limited", 3)

    # 상한이 올라가면(또는 날이 바뀌어 예산이 생기면) 같은 파일을 다시 올려 남은 것을 넣을 수 있다
    ingest.user_message_cap = 10
    again = await svc.import_messages("u", c1.id, ImportPayload(messages=_msgs(5)))
    assert (again.id, again.graph_status, again.graph_episodes) == (job.id, "completed", 2)
    assert len(store.added) == 5 and await conversations.users_with_pending_graph() == []


# ---- API -----------------------------------------------------------------------------------------


def _graph_client(**caps: int) -> tuple[TestClient, FakeGraphStore]:
    """NEO4J_URI 없이 그래프가 켜진 서버를 흉내 낸다 (deps._build_graph가 하는 배선)."""
    names = {"user_message_cap": "graph_ingest_user_message_cap"}
    settings = Settings(
        _env_file=None,
        llm_provider="fake",
        embedding_provider="fake",
        **{names[k]: v for k, v in caps.items()},
    )  # type: ignore[call-arg]
    client = TestClient(create_app(settings))
    state = client.app.state.container
    store = FakeGraphStore()
    ingest = GraphIngestService(store, state.conversations, state.contacts, **caps)
    state.graph_store, state.graph_ingest = store, ingest
    state.import_service._graph_ingest, state.import_service.graph_mode = ingest, "sync"
    state.rewrite_service.graph_enabled = True
    return client, store


def test_stats_and_usage_show_graph_progress_and_limit() -> None:
    """API: 가져오기 뒤 stats의 그래프 필드와 limited, usage의 그래프 수집 수, 조건 auto → D."""
    client, _ = _graph_client(user_message_cap=1)
    h = {"X-User-Id": "user-a"}
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
    assert (job.json()["graph_status"], job.json()["graph_episodes"], job.json()["graph_error"]) == (
        "limited",
        1,
        "user_cap",
    )

    stats = client.get(f"/v1/conversations/{conv_id}/stats", headers=h).json()
    assert (stats["messages"], stats["graph_enabled"], stats["graph_ingested"], stats["graph_pending"]) == (
        2,
        True,
        1,
        1,
    )
    assert stats["graph_limited"] is True and stats["latest_job"]["graph_status"] == "limited"
    usage = client.get("/v1/usage", headers=h).json()
    assert (usage["graph_messages_ingested"], usage["graph_message_cap"]) == (1, 1)
    summary = client.get("/v1/data/summary", headers=h).json()
    assert summary["graph_enabled"] is True and summary["contacts"][0]["graph_ingested"] == 1

    # 조건 auto: 그래프 사용 가능 + 이 상대의 그래프 수집 메시지 ≥1 → D
    r = client.post(
        "/v1/rewrites",
        json={"contact_id": cid, "draft_text": "수요일까지 가능합니다.", "condition": "auto"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    assert r.json()["condition"] == "D"


def test_usage_keeps_counting_what_was_ingested_today_after_the_conversation_is_deleted() -> None:
    client, store = _graph_client(user_message_cap=2)
    h = {"X-User-Id": "user-a"}
    cid = client.post(
        "/v1/contacts", json={"alias": "박지현", "relationship_type": "superior"}, headers=h
    ).json()["id"]
    text = "2026-10-05 09:25 | contact | 언제까지 가능해요?\n2026-10-05 09:31 | me | 수요일까지 가능합니다.\n"

    def import_once(key: str) -> dict:
        conv_id = client.post(
            "/v1/conversations", json={"contact_id": cid, "source_key": key}, headers=h
        ).json()["id"]
        job = client.post(f"/v1/conversations/{conv_id}/imports/raw", json={"text": text}, headers=h)
        assert job.status_code == 202, job.text
        return {"conv": conv_id, **job.json()}

    first = import_once("c1")
    assert (first["graph_status"], first["graph_episodes"]) == ("completed", 2)
    assert client.delete(f"/v1/conversations/{first['conv']}", headers=h).status_code == 200
    assert store.added == []  # 그래프에서는 지워졌다

    usage = client.get("/v1/usage", headers=h).json()
    assert (usage["graph_messages_ingested"], usage["graph_message_cap"]) == (2, 2)  # 오늘 넣은 양은 남는다
    second = import_once("c2")
    assert (second["graph_status"], second["graph_episodes"], second["graph_error"]) == (
        "limited",
        0,
        "user_cap",
    )
    stats = client.get(f"/v1/conversations/{second['conv']}/stats", headers=h).json()
    assert (stats["graph_ingested"], stats["graph_pending"], stats["graph_limited"]) == (0, 2, True)


def test_usage_reports_graph_numbers_even_when_the_graph_is_switched_off(
    client: TestClient, user_a: dict[str, str]
) -> None:
    """그래프를 꺼 둔 서버에서도 예전에 넣은 것이 있으면 usage와 데이터 요약이 같은 수를 보여 준다."""
    state = client.app.state.container
    cid = client.post(
        "/v1/contacts", json={"alias": "박지현", "relationship_type": "superior"}, headers=user_a
    ).json()["id"]
    conv_id = client.post(
        "/v1/conversations", json={"contact_id": cid, "source_key": "c1"}, headers=user_a
    ).json()["id"]
    text = "2026-10-05 09:25 | contact | 언제까지 가능해요?\n2026-10-05 09:31 | me | 수요일까지 가능합니다.\n"
    assert (
        client.post(f"/v1/conversations/{conv_id}/imports/raw", json={"text": text}, headers=user_a)
    ).status_code == 202

    async def mark_ingested() -> None:  # 그래프가 켜져 있던 때 들어간 것처럼
        from uuid import UUID

        messages = await state.conversations.list_messages("user-a", UUID(conv_id))
        await state.conversations.mark_graph_ingested(
            "user-a", [m.id for m in messages], datetime.fromisoformat("2026-10-06T00:00:00+00:00")
        )

    asyncio.run(mark_ingested())
    assert state.graph_enabled is False
    usage = client.get("/v1/usage", headers=user_a).json()
    summary = client.get("/v1/data/summary", headers=user_a).json()
    assert summary["graph_enabled"] is False
    assert usage["graph_messages_ingested"] == summary["contacts"][0]["graph_ingested"] == 2


def test_build_state_passes_caps_and_graph_flag_to_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """배선 확인: NEO4J_URI가 있으면 설정의 상한이 수집 서비스로, 그래프 사용 여부가 첨삭 서비스로 넘어간다.

    Neo4j에도 graphiti에도 닿지 않는다: `app.graph.client` 모듈 자리에 가짜를 끼워 클라이언트 생성만 바꾼다
    (진짜 모듈을 import하면 graphiti가 .env를 환경변수로 올려 뒤 테스트의 설정이 오염된다 — CLAUDE.md 함정).
    """
    import sys
    import types

    from app.api.deps import build_state

    fake_client = types.ModuleType("app.graph.client")
    fake_client.build_graphiti = lambda settings, embedder: object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.graph.client", fake_client)
    settings = Settings(
        _env_file=None,
        llm_provider="fake",
        embedding_provider="fake",
        neo4j_uri="bolt://not-used:7687",
        graph_ingest_user_message_cap=7,
        graph_ingest_daily_global_cap=9,
        usage_day_offset_hours=0,
    )  # type: ignore[call-arg]
    state = build_state(settings)
    assert state.graph_enabled is True and state.rewrite_service.graph_enabled is True
    assert (state.graph_ingest.user_message_cap, state.graph_ingest.daily_global_cap) == (7, 9)
    assert state.graph_ingest._day_tz.utcoffset(None) == timedelta(0)
    assert state.import_service.graph_mode == "background"
    # 그래프가 없는 기본 서버: 수집 서비스도 없고 D를 고르지 않는다
    plain = build_state(Settings(_env_file=None, llm_provider="fake", embedding_provider="fake"))  # type: ignore[call-arg]
    assert plain.graph_ingest is None and plain.rewrite_service.graph_enabled is False
