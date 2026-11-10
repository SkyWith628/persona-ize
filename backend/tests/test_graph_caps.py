"""그래프 수집 비용 상한 (`GRAPH_INGEST_USER_MESSAGE_CAP`, `GRAPH_INGEST_DAILY_GLOBAL_CAP`).

Neo4j·모델 호출 없이 FakeGraphStore로 본다: 상한에 닿으면 시각 순으로 예산만큼만 넣고 `limited`,
재시작 이어받기가 다시 돌지 않음, 날이 바뀌면 이어서 넣음, 스크립트 경로는 상한 없음.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.ai.embeddings import FakeEmbedder
from app.core.config import Settings
from app.graph.ingest import GraphIngestService
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


async def _setup(graph_mode: str = "sync", **caps: int):  # noqa: ANN202
    contacts = InMemoryContactRepo()
    conversations = InMemoryConversationRepo()
    store = FakeGraphStore()
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


async def test_user_cap_ingests_earliest_messages_then_marks_job_limited() -> None:
    _, conversations, store, ingest, svc, boss, c1, c2 = await _setup(graph_mode="off", user_message_cap=2)
    await svc.import_messages("u", c2.id, ImportPayload(messages=MESSAGES_C2))
    svc.graph_mode = "sync"
    job = await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))

    # 미수집 3건 중 예산 2건만, 방을 가로질러 시각 순으로
    assert [a["name"] for a in store.added] == ["c1:m01", "c2:m01"]
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


async def test_freed_budget_is_used_by_the_next_run_and_closes_limited_jobs() -> None:
    _, conversations, store, ingest, svc, _, c1, c2 = await _setup(user_message_cap=2)
    job1 = await svc.import_messages(
        "u", c1.id, ImportPayload(messages=MESSAGES_C1)
    )  # 2건 → 상한 2에 딱 맞음
    assert (job1.graph_status, job1.graph_episodes) == ("completed", 2)
    job2 = await svc.import_messages("u", c2.id, ImportPayload(messages=MESSAGES_C2))
    assert (job2.graph_status, job2.graph_episodes, job2.graph_error) == ("limited", 0, "user_cap")

    # 대화방을 지우면 그만큼 다시 여유가 생긴다 (상한은 그래프에 '들어가 있는' 메시지 수 기준)
    await svc.delete_conversation("u", c1.id)
    assert await ingest.remaining("u") == (2, "user_cap")
    assert await ingest.resume_all() == {"u": 1}
    assert [a["name"] for a in store.added] == ["c2:m01"]
    closed = await conversations.get_job("u", job2.id)
    assert (closed.graph_status, closed.graph_error) == ("completed", None)  # 남은 것이 다 들어가면 정리된다
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

    # 날이 바뀌면 재시작 이어받기가 남은 것을 넣는다
    today, started = ingest._today()
    ingest._today = lambda now=None: (today + timedelta(days=1), started + timedelta(days=1))  # type: ignore[method-assign]
    assert await ingest.resume_all() == {"v": 2}
    assert [a["name"] for a in store.added] == ["v1:m01", "v1:m02"]
    assert (await conversations.get_job("v", job_v.id)).graph_status == "completed"


async def test_partial_run_stops_at_daily_cap_and_keeps_time_order() -> None:
    _, conversations, store, ingest, svc, _, c1, c2 = await _setup(
        graph_mode="off", daily_global_cap=1, user_message_cap=5
    )
    await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))
    await svc.import_messages("u", c2.id, ImportPayload(messages=MESSAGES_C2))
    assert await ingest.remaining("u") == (1, "daily_cap")  # 두 상한 중 먼저 닿는 쪽
    assert await ingest.ingest_user("u") == 1
    assert [a["name"] for a in store.added] == ["c1:m01"]  # 가장 이른 메시지부터
    assert await conversations.count_graph_ingested("u") == 1


async def test_scripts_calling_ingest_pending_directly_are_not_capped() -> None:
    """합성 적재·평가 스크립트(`ingest_pending`)는 설정과 무관하게 전부 넣는다."""
    _, _, store, ingest, svc, _, c1, c2 = await _setup(
        graph_mode="off", user_message_cap=1, daily_global_cap=1
    )
    await svc.import_messages("u", c1.id, ImportPayload(messages=MESSAGES_C1))
    await svc.import_messages("u", c2.id, ImportPayload(messages=MESSAGES_C2))
    assert await ingest.ingest_pending("u") == 3
    assert len(store.added) == 3
    assert await ingest.ingest_user("u", enforce_caps=False) == 0  # 남은 것 없음


async def test_failed_episode_releases_the_reserved_daily_budget() -> None:
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


async def test_concurrent_users_never_exceed_the_daily_cap_together() -> None:
    """두 사용자의 수집이 겹쳐도(각자 예산이 남아 보이는 순간에 시작) 합계는 하루 상한을 넘지 않는다."""
    import asyncio

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


def test_stats_and_usage_show_graph_progress_and_limit() -> None:
    """API: 가져오기 뒤 stats의 그래프 필드와 limited, usage의 그래프 수집 수, 조건 auto → D."""
    settings = Settings(
        _env_file=None, llm_provider="fake", embedding_provider="fake", graph_ingest_user_message_cap=1
    )  # type: ignore[call-arg]
    client = TestClient(create_app(settings))
    state = client.app.state.container
    store = FakeGraphStore()
    ingest = GraphIngestService(store, state.conversations, state.contacts, user_message_cap=1)
    # NEO4J_URI 없이 그래프가 켜진 서버를 흉내 낸다 (deps._build_graph가 하는 배선)
    state.graph_store, state.graph_ingest = store, ingest
    state.import_service._graph_ingest, state.import_service.graph_mode = ingest, "sync"
    state.rewrite_service.graph_enabled = True

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
